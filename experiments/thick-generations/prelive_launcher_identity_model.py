#!/usr/bin/python3
"""Pure identity model for a future forked-Python pre-exec launcher.

This module performs no open, fork, procfs read, grant or exec operation.  It
keeps interpreter, already-loaded launcher code, inherited process identity and
the pinned payload in separate domains so none can be used as evidence for
another.
"""

import copy
import hashlib
import json
import posixpath
import re
import stat
import threading


class Refusal(RuntimeError):
    pass


HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BOOT = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
_HANDLE_KEY = object()
_PREEXEC_KEY = object()


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _closed_dict(value, fields, message):
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and frozenset(value) == frozenset(fields), message)
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def _owner(value):
    _closed_dict(value, {"pid", "starttime", "boot_id"},
                 "supervisor identity")
    require(integer(value["pid"], 2)
            and integer(value["starttime"], 1)
            and type(value["boot_id"]) is str
            and BOOT.fullmatch(value["boot_id"]), "supervisor identity")
    return copy.deepcopy(value)


def _object(value, role):
    fields = {"path_hint", "dev", "inode", "size", "mode", "uid",
              "content_sha256", "pin_kind", "pin_id"}
    _closed_dict(value, fields, f"{role} object schema")
    require(type(value["path_hint"]) is str
            and value["path_hint"].startswith("/")
            and posixpath.normpath(value["path_hint"]) == value["path_hint"]
            and "\x00" not in value["path_hint"]
            and integer(value["dev"], 1)
            and integer(value["inode"], 1)
            and integer(value["size"], 1)
            and integer(value["mode"], 1)
            and stat.S_ISREG(value["mode"])
            and integer(value["uid"])
            and type(value["content_sha256"]) is str
            and HEX64.fullmatch(value["content_sha256"])
            and type(value["pin_kind"]) is str
            and value["pin_kind"] == "MODEL_OPAQUE_FD_BINDING"
            and type(value["pin_id"]) is str
            and HEX32.fullmatch(value["pin_id"]), f"{role} object identity")
    return copy.deepcopy(value)


def _environment(value):
    require(type(value) is dict and value
            and all(type(key) is str for key in value)
            and len(value) <= 32, "payload environment schema")
    for key, item in value.items():
        require(type(key) is str and type(item) is str and key
                and "=" not in key and "\x00" not in key
                and "\x00" not in item and len(key) <= 128
                and len(item) <= 4096, "payload environment entry")
    return copy.deepcopy(value)


def _descriptor(value):
    fields = {"schema", "kind", "run_binding", "interpreter", "launcher",
              "inherited", "payload"}
    _closed_dict(value, fields, "launcher descriptor schema")
    require(type(value["kind"]) is str
            and type(value["schema"]) is int and value["schema"] == 1
            and value["kind"] == "FORKED_PYTHON_LAUNCHER_V1",
            "launcher descriptor schema")
    run = value["run_binding"]
    _closed_dict(run, {
                "request_id", "boot_id", "supervisor", "unarmed_deadline_ns",
                "fd_graph_digest"}, "run binding")
    require(type(run["request_id"]) is str
            and HEX32.fullmatch(run["request_id"])
            and type(run["boot_id"]) is str and BOOT.fullmatch(run["boot_id"])
            and integer(run["unarmed_deadline_ns"], 1)
            and type(run["fd_graph_digest"]) is str
            and HEX64.fullmatch(run["fd_graph_digest"]), "run binding")
    supervisor = _owner(run["supervisor"])
    require(supervisor["boot_id"] == run["boot_id"], "run boot mismatch")
    interpreter = _object(value["interpreter"], "interpreter")
    launcher = value["launcher"]
    _closed_dict(launcher, {
                "entrypoint", "source_manifest_sha256", "loader_policy",
                "loaded_code_provenance"}, "launcher source identity")
    require(type(launcher["entrypoint"]) is str
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*",
                             launcher["entrypoint"])
            and type(launcher["source_manifest_sha256"]) is str
            and HEX64.fullmatch(launcher["source_manifest_sha256"])
            and type(launcher["loader_policy"]) is str
            and launcher["loader_policy"]
            == "VERIFIED_BYTES_COMPILED_BEFORE_FORK",
            "launcher source identity")
    loaded = launcher["loaded_code_provenance"]
    _closed_dict(loaded, {"kind", "manifest_sha256", "frozen_before_fork"},
                 "loaded launcher code provenance")
    require(type(loaded["kind"]) is str
            and loaded["kind"] == "MODEL_VERIFIED_BYTES_COMPILED"
            and type(loaded["manifest_sha256"]) is str
            and loaded["manifest_sha256"]
            == launcher["source_manifest_sha256"]
            and loaded["frozen_before_fork"] is True,
            "loaded launcher code provenance")
    inherited = value["inherited"]
    _closed_dict(inherited, {
                "proc_cmdline_sha256", "launcher_environment_policy_sha256"}
                , "inherited process identity")
    require(type(inherited["proc_cmdline_sha256"]) is str
            and HEX64.fullmatch(inherited["proc_cmdline_sha256"])
            and type(inherited["launcher_environment_policy_sha256"]) is str
            and HEX64.fullmatch(inherited["launcher_environment_policy_sha256"]),
            "inherited process identity")
    payload = value["payload"]
    _closed_dict(payload, {"object", "argv", "environment"}, "payload schema")
    payload_object = _object(payload["object"], "payload")
    require(payload_object["pin_id"] != interpreter["pin_id"],
            "interpreter and payload pin alias")
    semantic_hashes = {
        interpreter["content_sha256"],
        launcher["source_manifest_sha256"],
        inherited["proc_cmdline_sha256"],
        inherited["launcher_environment_policy_sha256"],
        payload_object["content_sha256"],
    }
    require(len(semantic_hashes) == 5,
            "identity evidence reused across semantic domains")
    require(type(payload["argv"]) is list and payload["argv"]
            and len(payload["argv"]) <= 64
            and all(type(item) is str and item and "\x00" not in item
                    and len(item) <= 4096 for item in payload["argv"]),
            "payload argv")
    require(payload["argv"][0] == payload_object["path_hint"],
            "payload argv/path binding")
    environment = _environment(payload["environment"])
    frozen = copy.deepcopy(value)
    frozen["run_binding"]["supervisor"] = supervisor
    frozen["interpreter"] = interpreter
    frozen["payload"]["object"] = payload_object
    frozen["payload"]["environment"] = environment
    return frozen


def _child(value, descriptor):
    _closed_dict(value, {"pid", "starttime", "boot_id", "fork_token", "pidfd"},
                 "owned child identity")
    require(integer(value["pid"], 2)
            and value["pid"] != descriptor["run_binding"]["supervisor"]["pid"]
            and integer(value["starttime"], 1)
            and type(value["boot_id"]) is str
            and value["boot_id"] == descriptor["run_binding"]["boot_id"]
            and type(value["fork_token"]) is str
            and HEX32.fullmatch(value["fork_token"])
            and integer(value["pidfd"]), "owned child identity")
    return copy.deepcopy(value)


def _unarmed_observation(value, descriptor):
    _closed_dict(value, {
                "child", "interpreter", "payload", "proc_cmdline_sha256",
                "launcher_environment_policy_sha256", "payload_argv_sha256",
                "payload_environment_sha256",
                "fd_graph_digest", "loaded_code_manifest_sha256",
                "status_state"}, "unarmed observation schema")
    child = _child(value["child"], descriptor)
    interpreter = _object(value["interpreter"], "observed interpreter")
    require(interpreter == descriptor["interpreter"],
            "child executable is not the pinned interpreter")
    payload = _object(value["payload"], "observed payload")
    require(payload == descriptor["payload"]["object"],
            "pinned payload object changed")
    require(type(value["proc_cmdline_sha256"]) is str
            and value["proc_cmdline_sha256"]
            == descriptor["inherited"]["proc_cmdline_sha256"],
            "inherited raw cmdline changed")
    require(type(value["launcher_environment_policy_sha256"]) is str
            and value["launcher_environment_policy_sha256"]
            == descriptor["inherited"]["launcher_environment_policy_sha256"],
            "launcher environment policy changed")
    require(type(value["payload_argv_sha256"]) is str
            and value["payload_argv_sha256"]
            == digest(descriptor["payload"]["argv"]),
            "payload argv binding changed")
    require(type(value["payload_environment_sha256"]) is str
            and value["payload_environment_sha256"]
            == digest(descriptor["payload"]["environment"]),
            "payload environment binding changed")
    require(type(value["fd_graph_digest"]) is str
            and value["fd_graph_digest"]
            == descriptor["run_binding"]["fd_graph_digest"],
            "child FD graph changed")
    require(type(value["loaded_code_manifest_sha256"]) is str
            and value["loaded_code_manifest_sha256"]
            == descriptor["launcher"]["source_manifest_sha256"],
            "loaded launcher code changed")
    require(type(value["status_state"]) is str
            and value["status_state"] == "NO_EXEC_ERROR_REPORTED",
            "launcher status is not clean")
    frozen = copy.deepcopy(value)
    frozen["child"] = child
    frozen["interpreter"] = interpreter
    frozen["payload"] = payload
    return frozen


class LauncherIdentityHandle:
    def __init__(self, key, descriptor):
        require(key is _HANDLE_KEY, "launcher handle construction is private")
        self.descriptor = descriptor
        self.descriptor_digest = digest(descriptor)
        self.owner_thread = threading.current_thread()
        self.state = "ENROLLED"
        self.child = None
        self.unarmed_digest = None
        self.preexec_used = False
        self.preexec_capability = None

    def __copy__(self):
        raise Refusal("launcher identity handle is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("launcher identity handle is noncopyable")


class PreExecModelCapability:
    def __init__(self, key, handle, observation, now_ns):
        require(key is _PREEXEC_KEY, "pre-exec capability construction is private")
        self.handle = handle
        self.descriptor_digest = handle.descriptor_digest
        self.observation = copy.deepcopy(observation)
        self.observation_digest = digest(observation)
        self.now_ns = now_ns
        self.owner_thread = threading.current_thread()
        self.used = False

    def __copy__(self):
        raise Refusal("pre-exec model capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("pre-exec model capability is noncopyable")


def enroll_model(descriptor):
    return LauncherIdentityHandle(_HANDLE_KEY, _descriptor(descriptor))


def bind_unarmed_model(handle, first, second):
    require(type(handle) is LauncherIdentityHandle
            and threading.current_thread() is handle.owner_thread
            and handle.state == "ENROLLED", "fresh launcher enrollment required")
    handle.state = "BINDING"
    try:
        current_descriptor = _descriptor(handle.descriptor)
        require(digest(current_descriptor) == handle.descriptor_digest,
                "launcher descriptor changed")
        first = _unarmed_observation(first, current_descriptor)
        second = _unarmed_observation(second, current_descriptor)
        require(first == second, "unarmed child changed between observations")
        final_descriptor = _descriptor(handle.descriptor)
        require(handle.state == "BINDING"
                and digest(final_descriptor) == handle.descriptor_digest,
                "launcher binding changed during validation")
        handle.child = copy.deepcopy(first["child"])
        handle.unarmed_digest = digest(first)
        handle.state = "UNARMED_BOUND"
        return {"schema": 1,
                "classification": "MODEL_FORKED_LAUNCHER_BINDING_CONSISTENT",
                "descriptor_digest": handle.descriptor_digest,
                "unarmed_observation_digest": handle.unarmed_digest,
                "runtime_authorized": False, "storage_authorized": False,
                "exec_proven": False}
    except BaseException:
        handle.state = "UNKNOWN"
        raise


def validate_preexec_model(handle, observation, now_ns):
    require(type(handle) is LauncherIdentityHandle
            and threading.current_thread() is handle.owner_thread
            and handle.state == "UNARMED_BOUND"
            and handle.preexec_used is False, "bound launcher required")
    handle.preexec_used = True
    handle.state = "VALIDATING_PREEXEC"
    try:
        current_descriptor = _descriptor(handle.descriptor)
        require(digest(current_descriptor) == handle.descriptor_digest,
                "launcher descriptor changed")
        require(integer(now_ns)
                and now_ns < current_descriptor["run_binding"]
                ["unarmed_deadline_ns"], "pre-exec deadline")
        observed = _unarmed_observation(observation, current_descriptor)
        stored_child = _child(handle.child, current_descriptor)
        require(observed["child"] == stored_child
                and digest(observed) == handle.unarmed_digest,
                "pre-exec child identity changed")
        final_descriptor = _descriptor(handle.descriptor)
        require(handle.state == "VALIDATING_PREEXEC"
                and digest(final_descriptor) == handle.descriptor_digest
                and _child(handle.child, final_descriptor) == stored_child,
                "pre-exec authority changed during validation")
        handle.state = "MODEL_PREEXEC_VALIDATED"
        capability = PreExecModelCapability(
            _PREEXEC_KEY, handle, observed, now_ns)
        require(handle.preexec_capability is None,
                "pre-exec capability already retained")
        handle.preexec_capability = capability
        return capability, {
            "schema": 1,
            "classification": "MODEL_PREEXEC_BINDING_CONSISTENT",
            "descriptor_digest": handle.descriptor_digest,
            "preexec_observation_digest": capability.observation_digest,
            "payload_pin_id": handle.descriptor["payload"]["object"]["pin_id"],
            "runtime_authorized": False, "storage_authorized": False,
            "exec_proven": False}
    except BaseException:
        handle.state = "UNKNOWN"
        raise
