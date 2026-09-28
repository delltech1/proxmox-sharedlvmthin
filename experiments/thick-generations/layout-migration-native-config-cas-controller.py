#!/usr/bin/env python3
"""Mockable controller, NOT a live PVE helper or an authorization service.

This module imports no PVE/OS code and performs no I/O. Backend callables are an
internal test/composition boundary, never request data. Their acknowledgements
are assumed, not physical proofs. A future adapter must qualify every boundary.
An inline readback is not settlement: the CAS executor has not terminated yet.
"""

import hashlib
import json
import re
from types import FunctionType, MethodType


class Refusal(Exception):
    pass


class BoundaryRaised(Exception):
    """A backend raised; deliberately do not stringify arbitrary exceptions."""


def require(value, message):
    if not value:
        raise Refusal(message)


MAX_BYTES = 1024 * 1024
# Real API15 candidate imports 247 modules; keep a finite cross-language bound.
MAX_MODULES = 1024
MAX_INT = (1 << 63) - 1
SHA = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
DECIMAL = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")


def tree(value, depth=0, ancestors=None):
    kind = type(value)
    require(depth <= 16 and any(kind is t for t in (dict, list, str, int, bool, type(None))),
            "non-builtin value or excessive nesting")
    if kind is str:
        require(len(value) <= 2 * MAX_BYTES, "string exceeds bound")
    elif kind is int:
        require(0 <= value <= MAX_INT, "integer exceeds bound")
    elif kind is dict or kind is list:
        require(len(value) <= (MAX_MODULES if kind is list else 256), "container exceeds bound")
        ancestors = set() if ancestors is None else ancestors
        require(id(value) not in ancestors, "cyclic input")
        ancestors.add(id(value))
        if kind is dict:
            require(all(type(key) is str and len(key) <= 256 for key in value),
                    "custom namespace key")
            values = value.values()
        else:
            values = value
        for item in values:
            tree(item, depth + 1, ancestors)
        ancestors.remove(id(value))


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def text(value, pattern, label):
    require(type(value) is str and pattern.fullmatch(value) is not None, label + " invalid")


def decimal(value, label):
    text(value, DECIMAL, label)
    result = int(value)
    require(result <= MAX_INT, label + " exceeds bound")
    return result


def canonical(value):
    tree(value)
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    require(len(raw) <= 4 * MAX_BYTES, "JSON exceeds 4 MiB bound")
    return raw


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def raw_hex(value, label, allow_empty=False):
    require(type(value) is str and len(value) % 2 == 0
            and (allow_empty or len(value) > 0) and len(value) <= 2 * MAX_BYTES
            and re.fullmatch(r"[0-9a-f]*", value) is not None, label + " hex invalid")
    return bytes.fromhex(value)


def request_body_sha256(request):
    """Hash authorization's body; caller still must validate the whole request."""
    tree(request)
    require(type(request) is dict, "request is not an object")
    return digest({key: value for key, value in request.items() if key != "authorization"})


def validate_request(request):
    tree(request)
    exact(request, {"schema", "tx", "generation", "attempt_id", "executor", "context_sha256",
                    "candidate", "config", "changes", "participants", "evidence", "serializer",
                    "authorization"}, "request")
    require(request["schema"] == "slt-native-config-cas-controller/v1", "request schema invalid")
    for key in ("tx", "attempt_id"):
        text(request[key], HEX32, key)
    require(decimal(request["generation"], "generation") > 0, "generation must be positive")
    text(request["context_sha256"], SHA, "context SHA")
    candidate = request["candidate"]
    exact(candidate, {"package", "version", "flavor", "deb_sha256", "artifact_sha256"}, "candidate")
    require(candidate["package"] == "pve-sharedlvmthin" and candidate["flavor"] == "dual", "candidate profile invalid")
    text(candidate["version"], re.compile(r"[A-Za-z0-9.+:~_-]{1,128}\Z"), "candidate version")
    for key in ("deb_sha256", "artifact_sha256"):
        text(candidate[key], SHA, key)
    config = request["config"]
    exact(config, {"baseline_hex", "target_hex", "baseline_native_digest"}, "config")
    baseline = raw_hex(config["baseline_hex"], "baseline")
    target = raw_hex(config["target_hex"], "target")
    require(baseline != target, "baseline and target are identical")
    text(config["baseline_native_digest"], re.compile(r"[0-9a-f]{40}\Z"), "native digest")
    changes = request["changes"]
    require(type(changes) is list and 0 < len(changes) <= 64, "changes invalid")
    changed_ids = []
    for change in changes:
        exact(change, {"storage_id", "property", "old_value", "new_value"}, "change")
        text(change["storage_id"], NAME, "storage ID")
        require(change["property"] == "slt-vg-layout" and change["old_value"] is None
                and change["new_value"] == "mixed", "change is outside allowed delta")
        changed_ids.append(change["storage_id"])
    require(changed_ids == sorted(set(changed_ids)), "changes duplicated or unordered")
    nodes = request["participants"]
    require(type(nodes) is list and len(nodes) == 4, "four participants required")
    names, san, boots = [], [], {}
    for node in nodes:
        exact(node, {"node", "boot_id", "role"}, "participant")
        text(node["node"], NAME, "node")
        text(node["boot_id"], UUID, "boot")
        require(type(node["role"]) is str and node["role"] in ("SAN_PARTICIPANT", "CONTROL_ONLY"), "role invalid")
        names.append(node["node"])
        boots[node["node"]] = node["boot_id"]
        if node["role"] == "SAN_PARTICIPANT":
            san.append(node["node"])
    require(names == sorted(set(names)) and len(san) == 3, "topology invalid")
    executor = request["executor"]
    exact(executor, {"node", "boot_id"}, "executor")
    require(type(executor["node"]) is str and executor["node"] in san
            and type(executor["boot_id"]) is str and executor["boot_id"] == boots[executor["node"]], "executor invalid")
    evidence = request["evidence"]
    exact(evidence, {"barrier_sha256", "preinst_sha256", "payload_sha256", "prepare_manifest_sha256"}, "evidence")
    for key in ("barrier_sha256", "prepare_manifest_sha256"):
        text(evidence[key], SHA, key)
    for key, coverage in (("preinst_sha256", san), ("payload_sha256", names)):
        exact(evidence[key], coverage, key)
        for value in evidence[key].values():
            text(value, SHA, key)
    serializer = request["serializer"]
    exact(serializer, {"helper_sha256", "perl_sha256", "perl_hash_seed", "perl_perturb_keys",
                       "modules", "registered_plugins"}, "serializer")
    for key in ("helper_sha256", "perl_sha256"):
        text(serializer[key], SHA, key)
    require(serializer["perl_hash_seed"] == "0" and serializer["perl_perturb_keys"] == "0",
            "serializer hash-order controls invalid")
    require(type(serializer["modules"]) is list and 0 < len(serializer["modules"]) <= MAX_MODULES, "modules invalid")
    module_names = []
    for module in serializer["modules"]:
        exact(module, {"name", "path", "sha256"}, "module")
        text(module["name"], re.compile(r"[A-Za-z_][A-Za-z0-9_/]*\.pm\Z"), "module name")
        text(module["path"], re.compile(r"/[A-Za-z0-9_./+-]+\Z"), "module path")
        require(all(part not in ("", ".", "..") for part in module["path"].split("/")[1:]), "module path not canonical")
        text(module["sha256"], SHA, "module SHA")
        module_names.append(module["name"])
    require(module_names == sorted(set(module_names)), "module names duplicated or unordered")
    require({"PVE/Storage.pm", "PVE/Storage/Plugin.pm", "PVE/SectionConfig.pm",
             "PVE/Cluster.pm", "PVE/Tools.pm", "PVE/JSONSchema.pm",
             "PVE/Storage/Custom/SharedLvmThinPlugin.pm"} <= set(module_names),
            "required serializer modules missing")
    plugins = serializer["registered_plugins"]
    require(type(plugins) is list and plugins, "plugin registry invalid")
    for plugin in plugins:
        text(plugin, re.compile(r"[A-Za-z_][A-Za-z0-9_:]*\Z"), "plugin")
    require(plugins == sorted(set(plugins)), "plugin registry duplicated or unordered")
    require("PVE::Storage::Custom::SharedLvmThinPlugin" in plugins, "candidate plugin missing")
    auth = request["authorization"]
    exact(auth, {"action", "request_body_sha256", "issued_wall_ns", "expires_wall_ns"}, "authorization")
    require(auth["action"] == "CONTROLLER_MODEL_ONE_CAS_ONLY", "model authorization action invalid")
    text(auth["request_body_sha256"], SHA, "authorization body SHA")
    require(auth["request_body_sha256"] == request_body_sha256(request), "authorization body differs")
    issued = decimal(auth["issued_wall_ns"], "issued time")
    expires = decimal(auth["expires_wall_ns"], "expiry")
    require(0 < expires - issued <= 1800 * 10**9, "authorization duration invalid")
    return canonical(request)


BOUNDARIES = ("observe_local_identity", "sample_clock", "verify_admission", "verify_serializer",
              "with_storage_config_lock", "read_native_config", "read_raw_config",
              "render_native_config", "write_native_config_once", "persist_record_once")


class Backend:
    """Immutable, internally supplied callable table. No request-selected code."""
    __slots__ = ("_callbacks",)

    def __init__(self, **callbacks):
        require(set(callbacks) == set(BOUNDARIES), "backend boundaries invalid")
        require(all(type(fn) is FunctionType or type(fn) is MethodType for fn in callbacks.values()),
                "backend requires exact functions or bound methods")
        object.__setattr__(self, "_callbacks", tuple(callbacks[name] for name in BOUNDARIES))

    def __setattr__(self, name, value):
        raise Refusal("backend is immutable")


class Controller:
    __slots__ = ("_request_bytes", "_callbacks", "_state", "_poisoned", "_result_bytes")

    def __init__(self, request, backend):
        frozen = validate_request(request)
        require(type(backend) is Backend, "exact internal backend required")
        object.__setattr__(self, "_request_bytes", frozen)
        object.__setattr__(self, "_callbacks", backend._callbacks)
        object.__setattr__(self, "_state", "NEW")
        object.__setattr__(self, "_poisoned", False)
        object.__setattr__(self, "_result_bytes", None)

    def __setattr__(self, name, value):
        raise Refusal("controller is immutable")

    def snapshot(self):
        return None if self._result_bytes is None else json.loads(self._result_bytes)

    def run(self):
        if self._state == "RUNNING":
            object.__setattr__(self, "_poisoned", True)
            raise Refusal("reentry poisoned the active attempt")
        require(self._state == "NEW", "attempt is consumed; no retry")
        object.__setattr__(self, "_state", "RUNNING")
        request = json.loads(self._request_bytes)
        request_sha = hashlib.sha256(self._request_bytes).hexdigest()
        callbacks = dict(zip(BOUNDARIES, self._callbacks))
        baseline_hex = request["config"]["baseline_hex"]
        target_hex = request["config"]["target_hex"]
        issued = decimal(request["authorization"]["issued_wall_ns"], "issued")
        expires = decimal(request["authorization"]["expires_wall_ns"], "expiry")
        mono_deadline = wall_last = mono_last = None
        lock_entries, lock_open, lock_closed, body_completed = 0, False, False, False
        protected_config = config_seal = None
        reserved = intent_durable = write_entered = outcome_durable = False
        write_outcome = inline_hex = observation = None
        fault = "UNFINISHED"
        outcome_record = None

        def guard():
            require(self._state == "RUNNING" and self._poisoned is False,
                    "attempt state poisoned")
            if config_seal is not None:
                require(canonical(protected_config) == config_seal, "prepared config drifted")

        def call(name, *args):
            guard()
            # Request/config/evidence objects passed to a callback are copies;
            # still reject mutation rather than silently using changed inputs.
            before = [canonical(arg) for arg in args]
            try:
                result = callbacks[name](*args)
            except Exception:
                guard()
                for arg, frozen in zip(args, before):
                    require(canonical(arg) == frozen, "backend mutated argument")
                raise BoundaryRaised() from None
            guard()
            for arg, frozen in zip(args, before):
                require(canonical(arg) == frozen, "backend mutated argument")
            tree(result)
            return json.loads(canonical(result))

        def clock():
            nonlocal mono_deadline, wall_last, mono_last
            sample = call("sample_clock")
            exact(sample, {"wall_ns", "monotonic_ns"}, "clock")
            require(all(type(sample[k]) is int and 0 <= sample[k] <= MAX_INT for k in sample), "clock scalar invalid")
            wall, mono = sample["wall_ns"], sample["monotonic_ns"]
            require(issued <= wall <= expires, "authorization expired or not yet issued")
            if mono_deadline is None:
                mono_deadline = mono + expires - wall
                require(mono_deadline <= MAX_INT, "monotonic deadline overflow")
            else:
                require(wall >= wall_last and mono >= mono_last and mono <= mono_deadline,
                        "clock regressed or authorization expired")
            wall_last, mono_last = wall, mono

        def identity():
            value = call("observe_local_identity")
            exact(value, {"node", "boot_id", "quorate"}, "identity")
            require(value["node"] == request["executor"]["node"]
                    and value["boot_id"] == request["executor"]["boot_id"]
                    and value["quorate"] is True, "identity/quorum differs")

        def admission():
            value = call("verify_admission", json.loads(self._request_bytes))
            exact(value, {"request_sha256", "admitted"}, "admission")
            require(value["request_sha256"] == request_sha and value["admitted"] is True,
                    "admission not proven")

        def serializer():
            manifest = request["serializer"]
            value = call("verify_serializer", json.loads(canonical(manifest)))
            exact(value, {"manifest_sha256", "verified"}, "serializer acknowledgement")
            require(value["manifest_sha256"] == digest(manifest) and value["verified"] is True,
                    "serializer differs")

        def raw():
            value = call("read_raw_config")
            exact(value, {"raw_hex"}, "raw config")
            raw_hex(value["raw_hex"], "raw config", allow_empty=True)
            return value["raw_hex"]

        def persist(kind, record):
            expected = digest(record)
            value = call("persist_record_once", kind, json.loads(canonical(record)))
            exact(value, {"record_sha256", "created", "file_synced", "directory_synced"}, "journal acknowledgement")
            require(value["record_sha256"] == expected and all(value[k] is True for k in
                    ("created", "file_synced", "directory_synced")), "journal durability unproven")

        def locked_body():
            nonlocal lock_entries, lock_open, reserved, intent_durable, write_entered
            nonlocal write_outcome, inline_hex, observation
            nonlocal protected_config, config_seal, body_completed
            if lock_closed or lock_entries != 0:
                object.__setattr__(self, "_poisoned", True)
                raise Refusal("lock callback repeated or invoked after return")
            guard()
            lock_entries += 1
            lock_open = True
            try:
                clock(); identity(); admission(); serializer()
                require(raw() == baseline_hex, "raw baseline differs")
                native = call("read_native_config")
                exact(native, {"digest", "config"}, "native config")
                require(type(native["digest"]) is str and native["digest"] == request["config"]["baseline_native_digest"],
                        "native digest differs")
                cfg = native["config"]
                require(type(cfg) is dict and type(cfg.get("ids")) is dict, "native section map invalid")
                require(raw() == baseline_hex, "baseline changed during native read")
                for change in request["changes"]:
                    section = cfg["ids"].get(change["storage_id"])
                    require(type(section) is dict and section.get("type") == "sharedlvmthin"
                            and change["property"] not in section, "native delta precondition differs")
                    section[change["property"]] = change["new_value"]
                protected_config, config_seal = cfg, canonical(cfg)
                rendered = call("render_native_config", cfg)
                exact(rendered, {"raw_hex", "warnings"}, "render")
                require(type(rendered["raw_hex"]) is str and rendered["raw_hex"] == target_hex
                        and type(rendered["warnings"]) is list and rendered["warnings"] == [],
                        "native render differs or emitted warnings")
                clock()
                intent = {"schema": "slt-controller-cas-intent/v1", "request_sha256": request_sha,
                          "tx": request["tx"], "generation": request["generation"],
                          "attempt_id": request["attempt_id"], "executor": request["executor"],
                          "reservation": request["tx"] + ":" + request["generation"] + ":storage-config-cas"}
                reserved = True  # BEFORE any potentially after-effect persistence error.
                persist("INTENT", intent)
                intent_durable = True
                clock(); identity(); admission(); serializer()
                require(raw() == baseline_hex, "baseline changed before write")
                clock(); guard()
                require(lock_open and not write_entered, "write latch invalid")
                write_entered = True  # NEVER clear, including after exception/malformed ACK.
                write_outcome = "ENTERED_OUTCOME_UNKNOWN"
                try:
                    written = call("write_native_config_once", cfg)
                except BoundaryRaised:
                    write_outcome = "RAISED"
                    guard()
                else:
                    exact(written, {"status"}, "write acknowledgement")
                    require(written["status"] == "RETURNED", "write acknowledgement invalid")
                    write_outcome = "RETURNED"
                # Readback is only attempted after a completed call and valid
                # controller state. It is NOT executor-terminated settlement.
                inline_hex = raw()
                observation = ("TARGET" if inline_hex == target_hex else
                               "BASELINE" if inline_hex == baseline_hex else "FOREIGN")
                clock(); identity(); admission(); serializer()
                require(write_outcome != "RETURNED" or observation == "TARGET",
                        "successful write contradicts readback")
                body_completed = True
            except Exception:
                object.__setattr__(self, "_poisoned", True)
                raise
            finally:
                lock_open = False

        try:
            clock(); identity(); admission(); serializer()
            # The lock callback is a code boundary, not serializable request data.
            guard()
            try:
                lock_result = callbacks["with_storage_config_lock"](locked_body)
            finally:
                lock_closed = True
            guard(); tree(lock_result)
            exact(lock_result, {"released"}, "lock acknowledgement")
            require(lock_entries == 1 and body_completed and lock_result["released"] is True and not lock_open,
                    "lock callback missing or release unproven")
            clock()
            outcome_record = {"schema": "slt-controller-cas-outcome/v1", "request_sha256": request_sha,
                              "attempt_id": request["attempt_id"], "write_entered": write_entered,
                              "write_outcome": write_outcome, "inline_observation": observation,
                              "inline_raw_hex": inline_hex, "settlement": False, "authorization": "NONE"}
            persist("OUTCOME", outcome_record)
            outcome_durable = True
            clock(); guard()
            fault = None
        except Exception:
            object.__setattr__(self, "_poisoned", True)
            fault = "BOUNDARY_FAILED_OR_AMBIGUOUS"
        completed = fault is None and outcome_durable
        result = {"schema": "slt-native-config-cas-controller-result/v1", "request_sha256": request_sha,
                  "classification": "INLINE_OBSERVATION_ONLY" if completed else "UNKNOWN_RETAIN",
                  "fault": fault, "reservation_entered": reserved, "intent_durable": intent_durable,
                  "write_entered": write_entered, "write_outcome": write_outcome,
                  "inline_observation": observation, "inline_raw_hex": inline_hex,
                  "outcome_record": outcome_record, "outcome_durable": outcome_durable,
                  "authorization": "NONE", "settlement_proven": False,
                  "retry_authorized": False, "hold_transition_authorized": False,
                  "release_authorized": False, "runtime_qualified": False,
                  "model_only": True}
        object.__setattr__(self, "_result_bytes", canonical(result))
        object.__setattr__(self, "_state", "COMPLETE" if completed else "UNKNOWN")
        return self.snapshot()
