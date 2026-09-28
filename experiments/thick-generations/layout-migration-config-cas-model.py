#!/usr/bin/env python3
"""Pure, inspection-only model of one storage.cfg CAS and its settlement.

No OS access, clock, backend, dispatch callback, or live capability exists here.
Events describe assumed effects, not verified physical facts. Re-evaluating a
trace is historical inspection, never permission to resume or repeat a write.
The executor's quiescence/serialization claims require a separately qualified
collector. This model does not qualify that collector or publish package holds.
"""

import hashlib
import json
import re


class Refusal(Exception):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


HEX32 = re.compile(r"[0-9a-f]{32}\Z")
SHA = re.compile(r"[0-9a-f]{64}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
MAX_BYTES = 1024 * 1024
MAX_EVENTS = 16
MAX_INT = (1 << 63) - 1


def _builtin(value, depth=0, ancestors=None):
    """Reject custom dispatch surfaces before keys, equality or conversion."""
    require(depth <= 16, "input nesting exceeds bound")
    kind = type(value)
    require(any(kind is allowed for allowed in (dict, list, str, bytes, int, bool, type(None))),
            "input contains a non-builtin value")
    if kind in (str, bytes):
        require(len(value) <= MAX_BYTES, "input scalar exceeds bound")
    elif kind is int:
        require(0 <= value <= MAX_INT, "integer outside model range")
    elif kind in (dict, list):
        require(len(value) <= 64, "input container exceeds bound")
        ancestors = set() if ancestors is None else ancestors
        require(id(value) not in ancestors, "cyclic input")
        ancestors.add(id(value))
        if kind is dict:
            require(all(type(key) is str for key in value), "non-builtin namespace key")
            for key, item in value.items():
                require(len(key) <= 128, "namespace key exceeds bound")
                _builtin(item, depth + 1, ancestors)
        else:
            for item in value:
                _builtin(item, depth + 1, ancestors)
        ancestors.remove(id(value))


def _exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def _text(value, pattern, label):
    require(type(value) is str and pattern.fullmatch(value) is not None, label + " invalid")


def _number(value, label):
    require(type(value) is int and 0 <= value <= MAX_INT, label + " invalid")


def _canonical(value):
    # Bytes have an explicit type tag, so they cannot alias a string in a seal.
    if type(value) is bytes:
        return {"$bytes": value.hex()}
    if type(value) is dict:
        return {key: _canonical(item) for key, item in value.items()}
    if type(value) is list:
        return [_canonical(item) for item in value]
    return value


def _digest(value):
    raw = json.dumps(_canonical(value), sort_keys=True, separators=(",", ":"),
                     ensure_ascii=True).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


def validate_request(request):
    _builtin(request)
    _exact(request, {"schema", "tx", "generation", "attempt_id", "context_sha256",
                     "candidate_sha256", "serializer_sha256", "executor", "nodes",
                     "preinst_sha256", "payload_sha256", "baseline", "target",
                     "baseline_native_digest", "authorization"}, "request")
    require(request["schema"] == "slt-config-cas-model/v1", "request schema invalid")
    for key in ("tx", "attempt_id"):
        _text(request[key], HEX32, key)
    _number(request["generation"], "generation")
    require(request["generation"] > 0, "generation must be positive")
    for key in ("context_sha256", "candidate_sha256", "serializer_sha256"):
        _text(request[key], SHA, key)
    for key in ("baseline", "target"):
        raw = request[key]
        require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES,
                key + " must be nonempty exact bytes")
    require(request["baseline"] != request["target"], "baseline and target are identical")
    _text(request["baseline_native_digest"], re.compile(r"[0-9a-f]{40}\Z"), "native digest")
    require(type(request["nodes"]) is list and len(request["nodes"]) == 4,
            "exactly four nodes required")
    names, san, boots = [], [], {}
    for node in request["nodes"]:
        _exact(node, {"node", "boot_id", "role"}, "node")
        _text(node["node"], NAME, "node name")
        _text(node["boot_id"], UUID, "boot ID")
        require(type(node["role"]) is str and node["role"] in
                ("SAN_PARTICIPANT", "CONTROL_ONLY"), "node role invalid")
        names.append(node["node"])
        boots[node["node"]] = node["boot_id"]
        if node["role"] == "SAN_PARTICIPANT":
            san.append(node["node"])
    require(names == sorted(set(names)) and len(san) == 3, "node topology invalid")
    _exact(request["executor"], {"node", "boot_id"}, "executor")
    executor = request["executor"]
    require(type(executor["node"]) is str and executor["node"] in san
            and type(executor["boot_id"]) is str
            and executor["boot_id"] == boots[executor["node"]], "executor identity invalid")
    for key, expected in (("preinst_sha256", san), ("payload_sha256", names)):
        _exact(request[key], expected, key)
        for value in request[key].values():
            _text(value, SHA, key)
    auth = request["authorization"]
    _exact(auth, {"action", "issued_wall_ns", "expires_wall_ns", "start_monotonic_ns"},
           "authorization")
    require(auth["action"] == "MODEL_ONE_CAS_ONLY", "authorization action invalid")
    for key in ("issued_wall_ns", "expires_wall_ns", "start_monotonic_ns"):
        _number(auth[key], key)
    budget = auth["expires_wall_ns"] - auth["issued_wall_ns"]
    require(0 < budget <= 1800 * 10**9
            and auth["start_monotonic_ns"] + budget <= MAX_INT,
            "authorization budget invalid")
    return _digest(request)


EVENT_FIELDS = {
    "RESERVE": set(),
    "INTENT_ACK": {"durability"},
    "CAS_DISPATCH": set(),
    "CAS_RETURN": {"outcome", "detail"},
    "CRASH": set(),
    "QUIESCENCE": {"executor_terminated", "config_lock_held", "barrier_current"},
    "OBSERVE": {"result", "raw"},
    "RECEIPT_ACK": {"durability"},
}
ENVELOPE = {"sequence", "kind", "attempt_id", "request_sha256", "wall_ns", "monotonic_ns"}


def evaluate(request, events):
    """Inspect a closed trace. Malformed/impossible histories raise Refusal.

    An incomplete or ambiguous valid prefix returns UNKNOWN_RETAIN, preserving
    the observed write/result/bytes evidence. Only a durable settlement receipt
    can complete a classification, and even that has no continuation authority.
    The model does not permit a second observation, publication, or CAS attempt.
    Wall rollback/expiry poison the attempt, but diagnostic settlement remains
    possible. Event monotonic timestamps share one observer clock domain.
    """
    request_sha = validate_request(request)
    _builtin(events)
    require(type(events) is list and len(events) <= MAX_EVENTS, "event list invalid")
    auth = request["authorization"]
    deadline = auth["start_monotonic_ns"] + auth["expires_wall_ns"] - auth["issued_wall_ns"]
    wall, mono = auth["issued_wall_ns"], auth["start_monotonic_ns"]
    phase = "NEW"
    dispatches = 0
    outcome = detail = observed = observed_raw = None
    intent_durable = receipt_durable = False
    crashed = False
    poison = []
    observed_phase = None
    for index, event in enumerate(events, 1):
        require(type(event) is dict and type(event.get("kind")) is str
                and event["kind"] in EVENT_FIELDS, "event kind invalid")
        kind = event["kind"]
        _exact(event, ENVELOPE | EVENT_FIELDS[kind], "event")
        _number(event["sequence"], "sequence")
        require(event["sequence"] == index and type(event["attempt_id"]) is str
                and event["attempt_id"] == request["attempt_id"]
                and type(event["request_sha256"]) is str
                and event["request_sha256"] == request_sha, "event binding invalid")
        for key in ("wall_ns", "monotonic_ns"):
            _number(event[key], key)
        require(event["monotonic_ns"] >= mono, "monotonic clock regressed")
        require(phase != "TERMINAL", "terminal attempt cannot continue")
        if event["wall_ns"] < wall and "WALL_CLOCK_ROLLBACK" not in poison:
            poison.append("WALL_CLOCK_ROLLBACK")
        if (event["wall_ns"] > auth["expires_wall_ns"] or event["monotonic_ns"] > deadline):
            if "EXPIRED" not in poison:
                poison.append("EXPIRED")
        wall, mono = event["wall_ns"], event["monotonic_ns"]

        if kind == "RESERVE":
            require(phase == "NEW" and not poison, "reservation not admissible")
            phase = "INTENT_PENDING"
        elif kind == "INTENT_ACK":
            require(phase == "INTENT_PENDING", "intent acknowledgement out of order")
            require(type(event["durability"]) is str and event["durability"] in
                    ("DURABLE", "FAILED", "UNKNOWN"), "intent durability invalid")
            intent_durable = event["durability"] == "DURABLE"
            phase = "INTENT_DURABLE" if intent_durable else "TERMINAL"
            if not intent_durable:
                poison.append("INTENT_DURABILITY_UNPROVEN")
        elif kind == "CAS_DISPATCH":
            require(phase == "INTENT_DURABLE" and intent_durable and not crashed
                    and not poison and dispatches == 0, "CAS dispatch not admissible")
            dispatches = 1
            phase = "CAS_DISPATCHED"
        elif kind == "CAS_RETURN":
            require(phase == "CAS_DISPATCHED", "CAS return out of order")
            require(type(event["outcome"]) is str and event["outcome"] in
                    ("SUCCESS", "ERROR", "UNKNOWN"), "CAS outcome invalid")
            require(type(event["detail"]) is str and len(event["detail"]) <= 4096,
                    "CAS detail invalid")
            outcome, detail = event["outcome"], event["detail"]
            phase = "SETTLEMENT_PENDING"
        elif kind == "CRASH":
            require(phase != "NEW" and not crashed, "crash out of order")
            crashed = True
            # Never re-publish/read a partially published settlement. Preserve it.
            phase = "TERMINAL" if observed_phase is not None else "SETTLEMENT_PENDING"
            if observed_phase is not None:
                poison.append("SETTLEMENT_PUBLICATION_INTERRUPTED")
        elif kind == "QUIESCENCE":
            require(phase == "SETTLEMENT_PENDING", "quiescence out of order")
            require(all(event[key] is True for key in
                        ("executor_terminated", "config_lock_held", "barrier_current")),
                    "settlement requires quiescence, serialization and barrier")
            phase = "SETTLEMENT_ADMITTED"
        elif kind == "OBSERVE":
            require(phase == "SETTLEMENT_ADMITTED", "observation out of order")
            require(type(event["result"]) is str and event["result"] in ("OK", "ERROR"),
                    "observation result invalid")
            if event["result"] == "ERROR":
                require(event["raw"] is None, "failed observation must not claim bytes")
                observed = "UNKNOWN"
            else:
                require(type(event["raw"]) is bytes and len(event["raw"]) <= MAX_BYTES,
                        "observation bytes invalid")
                observed_raw = event["raw"]
                observed = ("TARGET" if observed_raw == request["target"] else
                            "BASELINE" if observed_raw == request["baseline"] else "FOREIGN")
            if outcome == "SUCCESS" and observed != "TARGET":
                poison.append("SUCCESS_OBSERVATION_CONTRADICTION")
            observed_phase = phase = "OBSERVED"
        elif kind == "RECEIPT_ACK":
            require(phase == "OBSERVED", "receipt acknowledgement out of order")
            require(type(event["durability"]) is str and event["durability"] in
                    ("DURABLE", "FAILED", "UNKNOWN"), "receipt durability invalid")
            receipt_durable = event["durability"] == "DURABLE"
            if not receipt_durable:
                poison.append("SETTLEMENT_DURABILITY_UNPROVEN")
            phase = "TERMINAL"

    classification = "NEW_NO_EFFECT" if phase == "NEW" else "UNKNOWN_RETAIN"
    if receipt_durable and not poison:
        classification = {"TARGET": "TARGET_OBSERVED_NONQUALIFIED",
                          "BASELINE": "BASELINE_OBSERVED_NONQUALIFIED",
                          "FOREIGN": "FOREIGN_OBSERVED_RETAIN",
                          "UNKNOWN": "UNKNOWN_RETAIN"}[observed]
    return {"schema": "slt-config-cas-model-result/v1", "request_sha256": request_sha,
            "trace_sha256": _digest(events), "attempt_id": request["attempt_id"],
            "classification": classification, "phase": phase, "reasons": poison,
            "modeled_write_dispatches": dispatches, "intent_durable": intent_durable,
            "cas_outcome": outcome, "cas_detail": detail, "crashed": crashed,
            "observed": observed, "observed_raw": observed_raw,
            "settlement_receipt_durable": receipt_durable,
            "authorization": "NONE", "inspection_only": True,
            "retry_authorized": False, "hold_transition_authorized": False,
            "configure_authorized": False, "release_authorized": False,
            "mutation_performed": False}
