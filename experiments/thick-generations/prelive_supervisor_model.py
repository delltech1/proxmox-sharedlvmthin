#!/usr/bin/python3
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Pure model of a one-shot process supervisor; contains no process backend."""

import copy
import hashlib
import json
import posixpath
import re


class Refusal(RuntimeError):
    pass


HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
BOOT = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
PURPOSES = frozenset({"RECOVERY_READONLY_PROBE", "DISPOSABLE_KERNEL_LAB"})
REQUEST_FIELDS = frozenset({
    "schema", "request_id", "purpose", "boot_id", "argv", "environment",
    "executable", "launcher", "timeout_ms", "cleanup_ms", "capture_limit",
    "signal_policy", "storage_authorized", "postcondition_verified",
})
EXECUTABLE_FIELDS = frozenset({"path", "sha256", "dev", "inode"})
CHILD_FIELDS = frozenset({
    "request_id", "boot_id", "pid", "starttime", "owner_pid",
    "owner_starttime", "launcher_sha256", "armed",
})
CAPTURE_FIELDS = frozenset({"eof", "truncated", "bytes", "sha256"})
COMMAND_FIELDS = frozenset({
    "pid", "starttime", "boot_id", "request_id", "argv",
    "environment_sha256", "executable", "armed",
})
ENVIRONMENT = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def validate_environment(environment):
    if type(environment) is not dict or environment != ENVIRONMENT:
        raise Refusal("environment must equal the closed clean mapping")
    for key, value in environment.items():
        if (type(key) is not str or type(value) is not str or not key
                or "=" in key or "\x00" in key or "\x00" in value
                or len(key) > 128 or len(value) > 4096):
            raise Refusal("environment entry is invalid")
    return copy.deepcopy(environment)


def validate_executable(executable):
    if type(executable) is not dict or set(executable) != EXECUTABLE_FIELDS:
        raise Refusal("executable identity schema is invalid")
    if (type(executable["path"]) is not str
            or not executable["path"].startswith("/")
            or posixpath.normpath(executable["path"]) != executable["path"]
            or "\x00" in executable["path"]):
        raise Refusal("executable path is invalid")
    if type(executable["sha256"]) is not str or not HEX64.fullmatch(executable["sha256"]):
        raise Refusal("executable digest is invalid")
    if not integer(executable["dev"], 1) or not integer(executable["inode"], 1):
        raise Refusal("executable inode identity is invalid")
    return copy.deepcopy(executable)


def validate_request(request):
    if type(request) is not dict or set(request) != REQUEST_FIELDS:
        raise Refusal("request schema is incomplete or open")
    if type(request["schema"]) is not int or request["schema"] != 1:
        raise Refusal("request schema version is unsupported")
    if type(request["request_id"]) is not str or not HEX32.fullmatch(request["request_id"]):
        raise Refusal("request identity is invalid")
    if request["purpose"] not in PURPOSES:
        raise Refusal("request purpose is not allowed by the model")
    if type(request["boot_id"]) is not str or not BOOT.fullmatch(request["boot_id"]):
        raise Refusal("request boot identity is invalid")
    if (type(request["argv"]) is not list or not request["argv"]
            or len(request["argv"]) > 64
            or any(type(item) is not str or not item or "\x00" in item
                   or len(item) > 4096 for item in request["argv"])):
        raise Refusal("request argv is invalid")
    executable = validate_executable(request["executable"])
    launcher = validate_executable(request["launcher"])
    if request["argv"][0] != executable["path"]:
        raise Refusal("argv executable differs from pinned identity")
    environment = validate_environment(request["environment"])
    if not integer(request["timeout_ms"], 1) or request["timeout_ms"] > 60000:
        raise Refusal("execution deadline is invalid")
    if not integer(request["cleanup_ms"], 1) or request["cleanup_ms"] > 10000:
        raise Refusal("cleanup budget is invalid")
    if (not integer(request["capture_limit"], 1)
            or request["capture_limit"] > 1048576):
        raise Refusal("capture limit is invalid")
    if request["signal_policy"] not in ("NONE", "PIDFD_TERM_THEN_KILL"):
        raise Refusal("signal policy is invalid")
    if request["storage_authorized"] is not False or request["postcondition_verified"] is not False:
        raise Refusal("process request cannot authorize storage")
    frozen = copy.deepcopy(request)
    frozen["environment"] = environment
    frozen["executable"] = executable
    frozen["launcher"] = launcher
    return frozen


def validate_owner(owner, boot_id):
    if (type(owner) is not dict or set(owner) != {"pid", "starttime", "boot_id"}
            or not integer(owner["pid"], 2) or not integer(owner["starttime"], 1)
            or owner["boot_id"] != boot_id):
        raise Refusal("supervisor owner identity is invalid")
    return copy.deepcopy(owner)


def validate_child(child, request, owner, armed):
    if type(child) is not dict or set(child) != CHILD_FIELDS:
        raise Refusal("launcher identity schema is invalid")
    if (child["request_id"] != request["request_id"]
            or child["boot_id"] != request["boot_id"]
            or type(child["owner_pid"]) is not int
            or child["owner_pid"] != owner["pid"]
            or type(child["owner_starttime"]) is not int
            or child["owner_starttime"] != owner["starttime"]
            or not integer(child["pid"], 2)
            or child["pid"] == owner["pid"]
            or not integer(child["starttime"], 1)
            or type(child["launcher_sha256"]) is not str
            or not HEX64.fullmatch(child["launcher_sha256"])
            or child["launcher_sha256"] != request["launcher"]["sha256"]
            or child["armed"] is not armed):
        raise Refusal("launcher identity binding is invalid")
    return copy.deepcopy(child)


def validate_capture(capture, limit):
    if type(capture) is not dict or set(capture) != CAPTURE_FIELDS:
        raise Refusal("capture schema is invalid")
    if (capture["eof"] is not True or capture["truncated"] is not False
            or not integer(capture["bytes"])
            or capture["bytes"] > limit
            or type(capture["sha256"]) is not str
            or not HEX64.fullmatch(capture["sha256"])):
        raise Refusal("capture is incomplete or exceeds bounds")
    if capture["bytes"] == 0 and capture["sha256"] != hashlib.sha256(b"").hexdigest():
        raise Refusal("empty capture digest is inconsistent")
    return copy.deepcopy(capture)


def validate_command(command, request, child):
    if type(command) is not dict or set(command) != COMMAND_FIELDS:
        raise Refusal("command identity schema is invalid")
    executable = validate_executable(command["executable"])
    if (type(command["pid"]) is not int or command["pid"] != child["pid"]
            or type(command["starttime"]) is not int
            or command["starttime"] != child["starttime"]
            or command["boot_id"] != child["boot_id"]
            or command["request_id"] != request["request_id"]
            or type(command["argv"]) is not list
            or command["argv"] != request["argv"]
            or type(command["environment_sha256"]) is not str
            or command["environment_sha256"] != digest(request["environment"])
            or executable != request["executable"]
            or command["armed"] is not True):
        raise Refusal("command identity binding is invalid")
    return copy.deepcopy(command)


class SupervisorModel:
    """Single-use orchestration model with injectable, model-only callbacks."""

    def __init__(self, journal, backend, owner):
        if getattr(backend, "model_only", None) is not True:
            raise Refusal("model-only backend is required")
        self.journal = journal
        self.backend = backend
        self.owner = copy.deepcopy(owner)
        self.state = "NEW"

    def _persist(self, event):
        self.journal.append(copy.deepcopy(event))

    def run(self, supplied_request):
        if self.state != "NEW":
            raise Refusal("supervisor model is single-use")
        self.state = "STARTING"
        child = None
        pidfd = None
        request = None
        owner = None
        process_settled = False
        try:
            request = validate_request(copy.deepcopy(supplied_request))
            owner = validate_owner(copy.deepcopy(self.owner), request["boot_id"])
            capabilities = copy.deepcopy(self.backend.capabilities_model())
            expected_capabilities = {
                    "pidfd": True, "pidfd_waitid": True,
                    "pidfd_signal": True, "subreaper": True,
                    "nonblocking_capture": True}
            if (type(capabilities) is not dict
                    or set(capabilities) != set(expected_capabilities)
                    or any(type(capabilities[key]) is not bool
                           or capabilities[key] is not True
                           for key in expected_capabilities)):
                raise Refusal("mandatory supervisor capabilities are unavailable")
            subreaper = copy.deepcopy(
                self.backend.enable_subreaper_model(copy.deepcopy(owner))
            )
            if (type(subreaper) is not dict
                    or set(subreaper) != {"before", "after", "process_local"}
                    or subreaper["before"] is not False
                    or subreaper["after"] is not True
                    or subreaper["process_local"] is not True):
                raise Refusal("subreaper enable/readback is ambiguous")
            self._persist({"kind": "INTENT", "request": request, "owner": owner,
                           "request_sha256": digest(request)})
            self.state = "INTENT_PERSISTED"
            child = copy.deepcopy(self.backend.prepare_unarmed_model(
                copy.deepcopy(request), copy.deepcopy(owner)
            ))
            child = validate_child(child, request, owner, False)
            observed = validate_child(
                copy.deepcopy(self.backend.observe_launcher_model(copy.deepcopy(child))),
                request, owner, False,
            )
            if observed != child:
                raise Refusal("launcher changed before pidfd binding")
            binding = copy.deepcopy(self.backend.bind_pidfd_model(copy.deepcopy(child)))
            if (type(binding) is not dict or set(binding) != {"child", "token"}
                    or validate_child(binding["child"], request, owner, False) != child
                    or not integer(binding["token"], 1)):
                raise Refusal("pidfd binding is invalid")
            pidfd = binding["token"]
            observed = validate_child(
                copy.deepcopy(self.backend.observe_launcher_model(copy.deepcopy(child))),
                request, owner, False,
            )
            if observed != child:
                raise Refusal("launcher changed while binding pidfd")
            self._persist({"kind": "CHILD_BOUND", "child": child,
                           "pidfd_token": pidfd})
            self.state = "CHILD_BOUND"
            self._persist({"kind": "EXEC_ISSUED", "request_id": request["request_id"],
                           "grant_bytes": 1})
            self.state = "EXEC_ISSUED"
            grant = copy.deepcopy(self.backend.grant_exec_model(
                copy.deepcopy(child), pidfd, copy.deepcopy(request)
            ))
            if (type(grant) is not dict
                    or set(grant) != {"request_id", "bytes_written", "grant_eof"}
                    or grant["request_id"] != request["request_id"]
                    or type(grant["bytes_written"]) is not int
                    or grant["bytes_written"] != 1
                    or grant["grant_eof"] is not True):
                raise Refusal("one-shot exec grant is ambiguous")
            command = validate_command(copy.deepcopy(self.backend.observe_exec_model(
                copy.deepcopy(child), pidfd, copy.deepcopy(request)
            )), request, child)
            self.state = "MONITORING"
            # No journal call is permitted between grant and returned monitor result.
            observed_result = copy.deepcopy(
                self.backend.monitor_model(
                    copy.deepcopy(command), pidfd, copy.deepcopy(request)
                )
            )
            expected_fields = {
                "command", "pidfd_token", "terminal", "unreaped", "returncode",
                "deadline_exceeded", "survivor", "descendants", "descendants_complete",
                "stdout", "stderr", "elapsed_ms",
            }
            if type(observed_result) is not dict or set(observed_result) != expected_fields:
                raise Refusal("monitor receipt schema is invalid")
            observed_command = validate_command(
                copy.deepcopy(observed_result["command"]), request, child
            )
            if (observed_command != command
                    or type(observed_result["pidfd_token"]) is not int
                    or observed_result["pidfd_token"] != pidfd
                    or observed_result["terminal"] != "EXITED"
                    or observed_result["unreaped"] is not True
                    or type(observed_result["returncode"]) is not int
                    or observed_result["returncode"] != 0
                    or observed_result["deadline_exceeded"] is not False
                    or observed_result["survivor"] is not False
                    or observed_result["descendants_complete"] is not True
                    or observed_result["descendants"] != []
                    or not integer(observed_result["elapsed_ms"])
                    or observed_result["elapsed_ms"] >= request["timeout_ms"]):
                raise Refusal("process or descendant terminal proof is incomplete")
            stdout = validate_capture(observed_result["stdout"], request["capture_limit"])
            stderr = validate_capture(observed_result["stderr"], request["capture_limit"])
            remaining_ms = request["timeout_ms"] - observed_result["elapsed_ms"]
            reaped = copy.deepcopy(self.backend.reap_exact_model(
                copy.deepcopy(command), pidfd, remaining_ms
            ))
            if (type(reaped) is not dict
                    or set(reaped) != {"command", "pidfd_token", "reaped",
                                       "returncode", "echild_after_reap", "elapsed_ms"}
                    or validate_command(copy.deepcopy(reaped["command"]), request, child) != command
                    or type(reaped["pidfd_token"]) is not int
                    or reaped["pidfd_token"] != pidfd
                    or reaped["reaped"] is not True
                    or type(reaped["returncode"]) is not int
                    or reaped["returncode"] != 0
                    or reaped["echild_after_reap"] is not True
                    or not integer(reaped["elapsed_ms"])
                    or reaped["elapsed_ms"] > remaining_ms):
                raise Refusal("exact reap proof is incomplete")
            process_settled = True
            result = {
                "classification": "PROCESS_TERMINAL_EXIT0",
                "request_id": request["request_id"], "command": command,
                "stdout": stdout, "stderr": stderr, "reap": reaped,
                "storage_authorized": False, "postcondition_verified": False,
                "runtime_authorized": False,
            }
            self._persist({"kind": "PROCESS_TERMINAL", "result": result})
            self.state = "TERMINAL"
            return result
        except BaseException as exc:
            self.state = "UNKNOWN"
            settlement = None
            if child is not None and not process_settled:
                try:
                    settlement = copy.deepcopy(self.backend.settle_unknown_model(
                        copy.deepcopy(child), pidfd,
                        copy.deepcopy(request), request["cleanup_ms"]
                    ))
                    required = {"child", "pidfd_token", "cleanup_elapsed_ms",
                                "signal_policy", "signal_sent", "terminal",
                                "reaped", "echild_after_reap", "survivor"}
                    settlement_child = validate_child(
                        copy.deepcopy(settlement.get("child")), request, owner, False
                    ) if type(settlement) is dict else None
                    pidfd_matches = (
                        (pidfd is None and settlement.get("pidfd_token") is None)
                        or (type(pidfd) is int
                            and type(settlement.get("pidfd_token")) is int
                            and settlement.get("pidfd_token") == pidfd)
                    ) if type(settlement) is dict else False
                    if (type(settlement) is not dict or set(settlement) != required
                            or settlement_child != child
                            or not pidfd_matches
                            or not integer(settlement["cleanup_elapsed_ms"])
                            or settlement["cleanup_elapsed_ms"] > request["cleanup_ms"]
                            or settlement["signal_policy"] != request["signal_policy"]
                            or settlement["signal_sent"] not in (None, "SIGTERM", "SIGKILL")
                            or any(type(settlement[key]) is not bool for key in
                                   ("terminal", "reaped", "echild_after_reap", "survivor"))
                            or (settlement["survivor"] is False and not (
                                settlement["terminal"] is True
                                and settlement["reaped"] is True
                                and settlement["echild_after_reap"] is True))
                            or (settlement["survivor"] is True and any(
                                settlement[key] is True for key in
                                ("terminal", "reaped", "echild_after_reap")))):
                        raise Refusal("bounded settlement receipt is invalid")
                    if (request["signal_policy"] == "NONE"
                            and settlement["signal_sent"] is not None):
                        raise Refusal("signal contradicts persisted policy")
                    if settlement["signal_sent"] is not None and not integer(pidfd, 1):
                        raise Refusal("signal lacks an exact pidfd binding")
                except BaseException as settlement_exc:
                    settlement = {"classification": "SETTLEMENT_UNKNOWN",
                                  "error": str(settlement_exc), "survivor": True}
            try:
                self._persist({"kind": "UNKNOWN_PRESERVE",
                               "request_id": request.get("request_id") if request else None,
                               "child": child, "pidfd_token": pidfd,
                               "settlement": settlement,
                               "error": str(exc), "retry_dispatched": False,
                               "storage_cleanup_dispatched": False})
            except BaseException as journal_exc:
                exc.add_note("failure evidence unavailable: " + str(journal_exc))
            raise
