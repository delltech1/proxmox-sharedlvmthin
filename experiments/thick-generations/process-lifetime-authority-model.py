#!/usr/bin/python3
"""Pure fail-closed process-lifetime authority state-machine model.

The model has no I/O and authorizes no production action.  ACTIVE fixtures
represent assumptions for interleaving review, not a real enrollment proof.
"""

import copy
import re


HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
BOOT_ID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


def refusal(state, reason):
    return {"runtime_authorized": 0, "action": "REFUSE", "reason": reason,
            "state": copy.deepcopy(state)}


def cold_state(enrollment_sha256):
    if not HEX64.fullmatch(enrollment_sha256 or ""):
        raise ValueError("enrollment digest is invalid")
    return {"schema": 1, "state": "COLD", "enrollment_sha256": enrollment_sha256,
            "session_nonce": None, "boot_id": None, "invocation_id": None,
            "pid": None, "start_ticks": None, "ram_revision": None,
            "ram_ledger_sha256": None, "in_flight": None}


def validate_state(state):
    fields = {"schema", "state", "enrollment_sha256", "session_nonce", "boot_id",
              "invocation_id", "pid", "start_ticks", "ram_revision",
              "ram_ledger_sha256", "in_flight"}
    if not isinstance(state, dict) or set(state) != fields \
            or type(state.get("schema")) is not int or state["schema"] != 1 \
            or state.get("state") not in {"COLD", "ACTIVE", "POISONED"} \
            or not HEX64.fullmatch(state.get("enrollment_sha256", "")):
        raise ValueError("process-lifetime authority state is malformed")
    if state["state"] == "COLD":
        if any(state[name] is not None for name in fields - {
                "schema", "state", "enrollment_sha256"}):
            raise ValueError("COLD authority carries live-session state")
        return state
    if not HEX32.fullmatch(state.get("session_nonce", "")) \
            or not BOOT_ID.fullmatch(state.get("boot_id", "")) \
            or not HEX32.fullmatch(state.get("invocation_id", "")) \
            or type(state.get("pid")) is not int or state["pid"] <= 0 \
            or type(state.get("start_ticks")) is not int or state["start_ticks"] <= 0 \
            or type(state.get("ram_revision")) is not int or state["ram_revision"] < 1 \
            or not HEX64.fullmatch(state.get("ram_ledger_sha256", "")):
        raise ValueError("live authority session identity is malformed")
    if state["in_flight"] is not None:
        flight = state["in_flight"]
        if not isinstance(flight, dict) or set(flight) != {
                "request_id", "transaction", "attempt", "pre_revision",
                "pre_ledger_sha256"} \
                or not all(HEX32.fullmatch(flight.get(name, "")) for name in (
                    "request_id", "transaction", "attempt")) \
                or type(flight.get("pre_revision")) is not int \
                or flight["pre_revision"] < 1 \
                or not HEX64.fullmatch(flight.get("pre_ledger_sha256", "")) \
                or flight["pre_revision"] != state["ram_revision"] \
                or flight["pre_ledger_sha256"] != state["ram_ledger_sha256"]:
            raise ValueError("in-flight authority mutation is malformed")
    return state


def active_fixture(cold, session, head):
    """Create model-only ACTIVE assumptions; never use as runtime enrollment."""
    validate_state(cold)
    if cold["state"] != "COLD" or not isinstance(session, dict) \
            or set(session) != {"session_nonce", "boot_id", "invocation_id",
                                "pid", "start_ticks"} \
            or not isinstance(head, dict) \
            or set(head) != {"revision", "ledger_sha256"}:
        raise ValueError("ACTIVE fixture assumptions are malformed")
    state = {**cold, **session, "state": "ACTIVE",
             "ram_revision": head["revision"],
             "ram_ledger_sha256": head["ledger_sha256"], "in_flight": None}
    return validate_state(state)


def client_binding(state):
    validate_state(state)
    if state["state"] != "ACTIVE":
        raise ValueError("only ACTIVE model fixture has a client binding")
    return {name: state[name] for name in (
        "enrollment_sha256", "session_nonce", "boot_id", "invocation_id"
    )}


def _binding_matches(state, binding):
    fields = {"enrollment_sha256", "session_nonce", "boot_id", "invocation_id"}
    return isinstance(binding, dict) and set(binding) == fields \
        and all(binding[name] == state[name] for name in fields)


def observe_head(state, disk_head):
    validate_state(state)
    if state["state"] != "ACTIVE":
        return refusal(state, "authority session is not ACTIVE")
    if state["in_flight"] is not None:
        return refusal(state, "standalone head observation is forbidden during in-flight mutation")
    if not isinstance(disk_head, dict) or set(disk_head) != {
            "revision", "ledger_sha256"} \
            or type(disk_head.get("revision")) is not int \
            or not HEX64.fullmatch(disk_head.get("ledger_sha256", "")):
        poisoned = {**state, "state": "POISONED"}
        return refusal(poisoned, "disk head observation is malformed or ambiguous")
    if (disk_head["revision"], disk_head["ledger_sha256"]) != (
            state["ram_revision"], state["ram_ledger_sha256"]):
        poisoned = {**state, "state": "POISONED"}
        return refusal(poisoned, "disk head differs from process-lifetime RAM head")
    return {"runtime_authorized": 0, "action": "MODEL_HEAD_MATCH_ONLY",
            "assumptions_matched": 1, "state": copy.deepcopy(state)}


def begin_mutation(state, binding, disk_head, request):
    validate_state(state)
    if state["state"] != "ACTIVE":
        return refusal(state, "authority session is not ACTIVE")
    if not _binding_matches(state, binding):
        return refusal(state, "client is not bound to the exact live session")
    if state["in_flight"] is not None:
        return refusal(state, "another mutation is already in flight")
    observed = observe_head(state, disk_head)
    state = observed["state"]
    if observed["action"] == "REFUSE":
        return observed
    if not isinstance(request, dict) or set(request) != {
            "request_id", "transaction", "attempt"} \
            or not all(HEX32.fullmatch(request.get(name, "")) for name in request):
        return refusal(state, "request identity is malformed")
    flight = {**request, "pre_revision": state["ram_revision"],
              "pre_ledger_sha256": state["ram_ledger_sha256"]}
    return {"runtime_authorized": 0, "action": "MODEL_BEGIN_ASSUMED",
            "assumptions_matched": 1, "state": {**state, "in_flight": flight}}


def complete_persist(state, binding, request, post_head, persist_outcome):
    validate_state(state)
    if state["state"] != "ACTIVE" or not _binding_matches(state, binding) \
            or state["in_flight"] is None:
        return refusal(state, "exact ACTIVE in-flight session is absent")
    flight = state["in_flight"]
    if any(request.get(name) != flight[name] for name in (
            "request_id", "transaction", "attempt")):
        return refusal(state, "completion request differs from in-flight mutation")
    if persist_outcome != "EXACT_COMMITTED" \
            or not isinstance(post_head, dict) \
            or set(post_head) != {"revision", "ledger_sha256"} \
            or type(post_head.get("revision")) is not int \
            or post_head["revision"] != flight["pre_revision"] + 1 \
            or not HEX64.fullmatch(post_head.get("ledger_sha256", "")) \
            or post_head["ledger_sha256"] == flight["pre_ledger_sha256"]:
        return refusal({**state, "state": "POISONED"},
                       "persist result is ambiguous or violates exact head transition")
    advanced = {**state, "ram_revision": post_head["revision"],
                "ram_ledger_sha256": post_head["ledger_sha256"],
                "in_flight": None}
    return {"runtime_authorized": 0, "action": "MODEL_RAM_HEAD_ADVANCED_ASSUMED",
            "assumptions_matched": 1, "state": advanced}


def process_lost(state):
    validate_state(state)
    return cold_state(state["enrollment_sha256"])


def correlate_response(state, binding, expected_request, response):
    validate_state(state)
    if state["state"] != "ACTIVE" or not _binding_matches(state, binding):
        return refusal(state, "response belongs to an absent or different session")
    if state["in_flight"] is not None:
        return refusal(state, "response correlation is forbidden before in-flight completion")
    fields = {"session_nonce", "request_id", "transaction", "attempt",
              "ledger_revision", "ledger_sha256"}
    request_fields = {"request_id", "transaction", "attempt"}
    if not isinstance(expected_request, dict) \
            or set(expected_request) != request_fields \
            or not all(HEX32.fullmatch(expected_request.get(name, ""))
                       for name in request_fields) \
            or not isinstance(response, dict) or set(response) != fields \
            or response.get("session_nonce") != state["session_nonce"] \
            or any(response.get(name) != expected_request[name]
                   for name in request_fields) \
            or type(response.get("ledger_revision")) is not int \
            or response["ledger_revision"] != state["ram_revision"] \
            or response.get("ledger_sha256") != state["ram_ledger_sha256"]:
        return refusal(state, "response is session/request mismatched or not current-head-bound")
    return {"runtime_authorized": 0, "action": "MODEL_RESPONSE_CORRELATION_ONLY",
            "assumptions_matched": 1, "replay_uniqueness_proven": 0,
            "state": copy.deepcopy(state)}
