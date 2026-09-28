#!/usr/bin/python3
"""Injected STRICT_NO_GRANT_ABORT ordering model; performs no OS operation."""

import copy
import fcntl
import hashlib
import os
import re
import stat
import threading
import types

import prelive_launcher_identity_consumer as CONSUMER
import prelive_supervisor_model as SUP
import prelive_v2_intent_file_bridge as INTENT


HEX32 = re.compile(r"[0-9a-f]{32}")
EXPECTED_EXIT = 73
STDOUT_CANARY = "SLT_NO_GRANT_STDOUT_V1"
STDERR_CANARY = "SLT_NO_GRANT_STDERR_V1"
FD_ROLES = ("grant_child", "grant_parent", "status_parent", "status_child",
            "stdout_parent", "stdout_child", "stderr_parent", "stderr_child")
READ_ROLES = frozenset(("grant_child", "status_parent", "stdout_parent",
                        "stderr_parent"))
NONBLOCK_ROLES = frozenset(("grant_parent", "status_parent", "stdout_parent",
                            "stderr_parent"))


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _strict(value, label):
    try:
        CONSUMER._builtin_tree(value, label)
    except CONSUMER.Refusal as exc:
        raise Refusal(str(exc)) from exc
    return value


class NoGrantAbortController:
    """One model attempt. No method can write a grant or request payload exec."""

    def __init__(self, backend, request, owner, intent_bytes,
                 started_ns, deadline_ns):
        backend_type = type(backend)
        require(backend_type.__dict__.get(
                    "model_only_no_grant_abort_backend") is True,
                "model-only no-grant backend required")
        method_names = ("monotonic_ns_model", "preflight_model",
                       "allocate_fd_graph_model",
                       "persist_intent_model", "fork_abort_once_model",
                       "bind_pidfd_model", "observe_ready_model",
                       "close_grant_writer_no_write_model",
                       "observe_abort_exit_model", "reap_exact_model")
        for method in method_names:
            require(callable(backend_type.__dict__.get(method)),
                    "closed no-grant backend surface required")
        self._methods = types.MappingProxyType({
            name: backend_type.__dict__[name] for name in method_names})
        request = CONSUMER.validate_v2_request(request)
        _strict(owner, "no-grant owner")
        try:
            owner = SUP.validate_owner(copy.deepcopy(owner), request["boot_id"])
        except SUP.Refusal as exc:
            raise Refusal(str(exc)) from exc
        intent_event = INTENT._strict_event(intent_bytes)
        require(intent_event["payload"]["request"] == request
                and intent_event["payload"]["owner"] == owner,
                "exact request-bound INTENT bytes required")
        require(integer(started_ns)
                and integer(deadline_ns, 1) and deadline_ns > started_ns
                and deadline_ns == request["launcher_identity"]
                ["run_binding"]["unarmed_deadline_ns"],
                "original absolute deadline required")
        self.backend = backend
        self._backend = backend
        self.request = request
        self._request_bytes = SUP.canonical(request)
        self._request_digest = SUP.digest(request)
        self.owner = owner
        self._owner_bytes = SUP.canonical(owner)
        self.intent_bytes = intent_bytes
        self._intent_bytes = intent_bytes
        self._intent_digest = hashlib.sha256(intent_bytes).hexdigest()
        self.started_ns = started_ns
        self.deadline_ns = deadline_ns
        self._started_ns = started_ns
        self._deadline_ns = deadline_ns
        self.last_clock_ns = started_ns
        self.owner_thread = threading.current_thread()
        self.state = "NEW"
        self.child_may_exist = False
        self.attempted_fork = False
        self.grant_write_attempts = 0
        self.exec_attempts = 0
        self.graph = None
        self._graph_bytes = None
        self.child = None
        self._child_bytes = None
        self.fork_token = None
        self.pidfd_token = None
        self.failure_phase = None
        self._fork_attempt_epoch = 0
        self._reap_proven = False
        self._pidfd_authority = None
        self._graph_enrolled = False
        self._child_enrolled = False
        self._pidfd_enrolled = False
        self._construction_sealed = True

    def __setattr__(self, name, value):
        if (getattr(self, "_construction_sealed", False)
                and name in {"_fork_attempt_epoch", "_reap_proven",
                             "_pidfd_authority", "_backend",
                             "_graph_enrolled", "_child_enrolled",
                             "_pidfd_enrolled",
                             "_request_bytes", "_intent_bytes",
                             "_started_ns", "_deadline_ns", "_graph_bytes",
                             "_child_bytes", "fork_token", "last_clock_ns",
                             "_methods"}):
            raise Refusal("no-grant construction authority is immutable")
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("no-grant controller is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("no-grant controller is noncopyable")

    def _poison(self, phase):
        survivor = self._fork_attempt_epoch > 0 and not self._reap_proven
        self.state = "UNKNOWN_SURVIVOR" if survivor else "UNKNOWN"
        self.failure_phase = phase

    def _validate(self, state):
        require(threading.current_thread() is self.owner_thread
                and self.backend is self._backend
                and type(self.state) is str and self.state == state
                and type(self.child_may_exist) is bool
                and type(self.attempted_fork) is bool
                and ((self._fork_attempt_epoch == 0
                      and self.attempted_fork is False)
                     or (self._fork_attempt_epoch == 1
                         and self.attempted_fork is True))
                and ((self._fork_attempt_epoch == 0
                      and self.child_may_exist is False)
                     or (self._fork_attempt_epoch == 1
                         and self._reap_proven is False
                         and self.child_may_exist is True)
                     or (self._fork_attempt_epoch == 1
                         and self._reap_proven is True
                         and self.child_may_exist is False))
                and type(self.grant_write_attempts) is int
                and self.grant_write_attempts == 0
                and type(self.exec_attempts) is int and self.exec_attempts == 0,
                "no-grant controller authority changed")
        current = CONSUMER.validate_v2_request(self.request)
        _strict(self.owner, "stored no-grant owner")
        try:
            owner = SUP.validate_owner(copy.deepcopy(self.owner),
                                       current["boot_id"])
        except SUP.Refusal as exc:
            raise Refusal(str(exc)) from exc
        require(SUP.canonical(current) == self._request_bytes
                and SUP.digest(current) == self._request_digest
                and SUP.canonical(owner) == self._owner_bytes
                and self.intent_bytes is self._intent_bytes
                and hashlib.sha256(self.intent_bytes).hexdigest()
                == self._intent_digest
                and integer(self.started_ns)
                and self.started_ns == self._started_ns
                and integer(self.deadline_ns, 1)
                and self.deadline_ns == self._deadline_ns
                and self.deadline_ns > self.started_ns,
                "frozen no-grant request authority changed")
        if self._graph_enrolled:
            require(self.graph is not None,
                    "enrolled no-grant graph disappeared")
            _strict(self.graph, "stored no-grant graph")
            require(type(self._graph_bytes) is bytes
                    and SUP.canonical(self.graph) == self._graph_bytes,
                    "frozen no-grant graph changed")
        if self._child_enrolled:
            require(self.child is not None,
                    "enrolled no-grant child disappeared")
            _strict(self.child, "stored no-grant child")
            require(type(self._child_bytes) is bytes
                    and SUP.canonical(self.child) == self._child_bytes
                    and type(self.fork_token) is str
                    and HEX32.fullmatch(self.fork_token)
                    and self.child["fork_token"] == self.fork_token,
                    "frozen no-grant child changed")
        if self._pidfd_enrolled:
            require(self.pidfd_token is not None,
                    "enrolled no-grant pidfd disappeared")
            require(integer(self.pidfd_token, 1)
                    and type(self._pidfd_authority) is int
                    and self.pidfd_token == self._pidfd_authority,
                    "frozen no-grant pidfd changed")

    def _invoke(self, name, *args):
        require(type(name) is str and name in self._methods
                and type(self.backend).__dict__.get(name)
                is self._methods[name]
                and name not in self.backend.__dict__,
                "no-grant backend method authority changed")
        return self._methods[name](self.backend, *args)

    def _clock(self):
        previous = self.last_clock_ns
        require(integer(previous), "strict no-grant clock watermark required")
        value = self._invoke("monotonic_ns_model")
        require(type(self.last_clock_ns) is int
                and self.last_clock_ns == previous
                and integer(value)
                and value >= previous
                and value < self._deadline_ns,
                "no-grant monotonic deadline reached or regressed")
        object.__setattr__(self, "last_clock_ns", value)
        return value

    def _call(self, ready, busy, next_state, method_name, *args):
        if self.state != ready:
            self._poison(busy)
            raise Refusal("no-grant phase or reentrancy")
        try:
            self._validate(ready)
            self.state = busy
            self._clock()
            self._validate(busy)
            result = self._invoke(method_name, *args)
            _strict(result, busy + " result")
            result = copy.deepcopy(result)
            require(self.state == busy,
                    "no-grant callback changed controller phase")
            self._validate(busy)
            self._clock()
            self._validate(busy)
            self.state = next_state
            self._validate(next_state)
            return result
        except BaseException:
            self._poison(busy)
            raise

    def run(self):
        try:
            preflight = self._call(
                "NEW", "PREFLIGHTING", "PREFLIGHTED",
                "preflight_model")
            require(type(preflight) is dict and set(preflight) == {
                        "dedicated_wait_domain", "unexpected_children",
                        "sigchld_autoreap", "subreaper_supported",
                        "subreaper_readback", "pidfd", "waitid_pidfd"}
                    and preflight["dedicated_wait_domain"] is True
                    and type(preflight["unexpected_children"]) is int
                    and preflight["unexpected_children"] == 0
                    and preflight["sigchld_autoreap"] is False
                    and preflight["subreaper_supported"] is True
                    and preflight["subreaper_readback"] is True
                    and preflight["pidfd"] is True
                    and preflight["waitid_pidfd"] is True,
                    "strict no-grant preflight")

            graph = self._call(
                "PREFLIGHTED", "ALLOCATING_GRAPH", "GRAPH_READY",
                "allocate_fd_graph_model", copy.deepcopy(self.owner))
            require(type(graph) is dict and set(graph) == {
                        "descriptor", "fd_graph_digest"}
                    and type(graph["descriptor"]) is dict,
                    "closed no-grant FD graph")
            descriptor = graph["descriptor"]
            try:
                graph_supervisor = SUP.validate_owner(
                    copy.deepcopy(descriptor.get("supervisor")),
                    self.request["boot_id"])
            except (AttributeError, SUP.Refusal) as exc:
                raise Refusal("strict FD graph supervisor required") from exc
            require(set(descriptor) == {
                        "schema", "kind", "request_id", "graph_id",
                        "supervisor", "pre_fork_ticket_id", "phase",
                        "grant_policy", "endpoints", "endpoint_count",
                        "all_cloexec", "parent_nonblocking"}
                    and type(descriptor["schema"]) is int
                    and descriptor["schema"] == 1
                    and descriptor["kind"]
                    == "NO_GRANT_ALLOCATED_FD_GRAPH_V1"
                    and descriptor["request_id"] == self.request["request_id"]
                    and type(descriptor["graph_id"]) is str
                    and HEX32.fullmatch(descriptor["graph_id"])
                    and SUP.canonical(graph_supervisor) == self._owner_bytes
                    and type(descriptor["pre_fork_ticket_id"]) is str
                    and HEX32.fullmatch(descriptor["pre_fork_ticket_id"])
                    and descriptor["phase"]
                    == "PARENT_PRE_FORK_ALL_ENDPOINTS_OWNED"
                    and descriptor["grant_policy"]
                    == "CLOSE_ONLY_NO_WRITE"
                    and type(descriptor["endpoints"]) is list
                    and len(descriptor["endpoints"]) == 8
                    and type(descriptor["endpoint_count"]) is int
                    and descriptor["endpoint_count"] == 8
                    and descriptor["all_cloexec"] is True
                    and descriptor["parent_nonblocking"] is True,
                    "strict no-grant FD graph descriptor")
            endpoints = descriptor["endpoints"]
            require(all(type(item) is dict and set(item) == {
                        "role", "fd", "dev", "inode", "mode", "flags",
                        "fd_flags"} for item in endpoints)
                    and tuple(item["role"] for item in endpoints) == FD_ROLES,
                    "closed no-grant endpoint descriptor")
            by_role = {}
            for item in endpoints:
                role = item["role"]
                require(type(role) is str
                        and all(type(item[key]) is int and item[key] >= 0
                                for key in ("fd", "dev", "inode", "mode",
                                            "flags", "fd_flags"))
                        and item["fd"] > 2 and item["inode"] > 0
                        and stat.S_ISFIFO(item["mode"])
                        and (item["flags"] & os.O_ACCMODE)
                        == (os.O_RDONLY if role in READ_ROLES else os.O_WRONLY)
                        and item["flags"]
                        == ((os.O_RDONLY if role in READ_ROLES
                             else os.O_WRONLY)
                            | (os.O_NONBLOCK
                               if role in NONBLOCK_ROLES else 0))
                        and item["fd_flags"] == fcntl.FD_CLOEXEC,
                        "strict no-grant endpoint identity")
                by_role[role] = item
            require(len({item["fd"] for item in endpoints}) == 8,
                    "no-grant endpoint FD alias")
            pipe_pairs = (("grant_child", "grant_parent"),
                          ("status_parent", "status_child"),
                          ("stdout_parent", "stdout_child"),
                          ("stderr_parent", "stderr_child"))
            pipes = set()
            for left, right in pipe_pairs:
                require((by_role[left]["dev"], by_role[left]["inode"])
                        == (by_role[right]["dev"], by_role[right]["inode"]),
                        "no-grant pipe peer identity mismatch")
                pipes.add((by_role[left]["dev"], by_role[left]["inode"]))
            require(len(pipes) == 4, "no-grant pipe identities alias")
            require(type(graph["fd_graph_digest"]) is str
                    and CONSUMER.IDENTITY.HEX64.fullmatch(
                        graph["fd_graph_digest"])
                    and graph["fd_graph_digest"] == SUP.digest(descriptor)
                    and graph["fd_graph_digest"] == self.request
                    ["launcher_identity"]["run_binding"]["fd_graph_digest"],
                    "no-grant FD graph/request binding")
            self.graph = copy.deepcopy(graph)
            object.__setattr__(self, "_graph_bytes",
                               SUP.canonical(self.graph))
            object.__setattr__(self, "_graph_enrolled", True)

            intent = self._call(
                "GRAPH_READY", "PERSISTING_INTENT", "INTENT_VERIFIED",
                "persist_intent_model", self._intent_bytes,
                self._intent_digest, self.deadline_ns)
            require(type(intent) is dict and set(intent) == {
                        "classification", "sha256", "exact_bytes_verified",
                        "child_started", "grant_attempted", "exec_proven"}
                    and intent["classification"]
                    == "V2_INTENT_EXACT_BYTES_VERIFIED"
                    and intent["sha256"] == self._intent_digest
                    and intent["exact_bytes_verified"] is True
                    and intent["child_started"] is False
                    and intent["grant_attempted"] is False
                    and intent["exec_proven"] is False,
                    "exact INTENT required before fork")

            self.attempted_fork = True
            object.__setattr__(self, "_fork_attempt_epoch", 1)
            self.child_may_exist = True
            child = self._call(
                "INTENT_VERIFIED", "FORKING_ABORT_CHILD", "CHILD_RECORDED",
                "fork_abort_once_model", copy.deepcopy(self.graph),
                self.deadline_ns)
            require(type(child) is dict and set(child) == {
                        "pid", "starttime", "boot_id", "fork_token",
                        "fork_count", "has_exec_branch"}
                    and integer(child["pid"], 1)
                    and integer(child["starttime"], 1)
                    and child["boot_id"] == self.request["boot_id"]
                    and type(child["fork_token"]) is str
                    and HEX32.fullmatch(child["fork_token"])
                    and type(child["fork_count"]) is int
                    and child["fork_count"] == 1
                    and child["has_exec_branch"] is False,
                    "exact abort-only child required")
            self.child = copy.deepcopy(child)
            object.__setattr__(self, "_child_bytes",
                               SUP.canonical(self.child))
            object.__setattr__(self, "fork_token", child["fork_token"])
            object.__setattr__(self, "_child_enrolled", True)

            bound = self._call(
                "CHILD_RECORDED", "BINDING_PIDFD", "PIDFD_BOUND",
                "bind_pidfd_model", copy.deepcopy(self.child))
            require(type(bound) is dict and set(bound) == {
                        "child", "pidfd_token", "observations_match",
                        "open_count"}
                    and type(bound["child"]) is dict
                    and SUP.canonical(bound["child"]) == self._child_bytes
                    and integer(bound["pidfd_token"], 1)
                    and bound["observations_match"] is True
                    and type(bound["open_count"]) is int
                    and bound["open_count"] == 1,
                    "exact abort child pidfd binding")
            self.pidfd_token = bound["pidfd_token"]
            object.__setattr__(self, "_pidfd_authority", bound["pidfd_token"])
            object.__setattr__(self, "_pidfd_enrolled", True)

            ready = self._call(
                "PIDFD_BOUND", "OBSERVING_READY", "READY_CAPTURED",
                "observe_ready_model", copy.deepcopy(self.child),
                self.pidfd_token, self.deadline_ns)
            require(type(ready) is dict and set(ready) == {
                        "ready_count", "stdout_canary", "stderr_canary",
                        "status_error", "payload_exec_calls", "grant_bytes"}
                    and type(ready["ready_count"]) is int
                    and ready["ready_count"] == 1
                    and ready["stdout_canary"] == STDOUT_CANARY
                    and ready["stderr_canary"] == STDERR_CANARY
                    and ready["status_error"] is False
                    and type(ready["payload_exec_calls"]) is int
                    and ready["payload_exec_calls"] == 0
                    and type(ready["grant_bytes"]) is int
                    and ready["grant_bytes"] == 0,
                    "strict abort child readiness")

            closed = self._call(
                "READY_CAPTURED", "CLOSING_GRANT_WRITER",
                "GRANT_WRITER_CLOSED", "close_grant_writer_no_write_model",
                copy.deepcopy(self.child), self.pidfd_token)
            require(type(closed) is dict and set(closed) == {
                        "bytes_written", "write_calls", "close_attempts",
                        "close_confirmed", "writer_copies_remaining"}
                    and type(closed["bytes_written"]) is int
                    and closed["bytes_written"] == 0
                    and type(closed["write_calls"]) is int
                    and closed["write_calls"] == 0
                    and type(closed["close_attempts"]) is int
                    and closed["close_attempts"] == 1
                    and closed["close_confirmed"] is True
                    and type(closed["writer_copies_remaining"]) is int
                    and closed["writer_copies_remaining"] == 0,
                    "grant writer close-only proof")

            terminal = self._call(
                "GRANT_WRITER_CLOSED", "OBSERVING_ABORT_EXIT",
                "ABORT_EXIT_OBSERVED", "observe_abort_exit_model",
                copy.deepcopy(self.child), self.pidfd_token, self.deadline_ns)
            require(type(terminal) is dict and set(terminal) == {
                        "grant_bytes_received", "grant_eof", "exit_code",
                        "stdout_eof", "stderr_eof", "status_eof",
                        "payload_exec_calls", "unreaped"}
                    and type(terminal["grant_bytes_received"]) is int
                    and terminal["grant_bytes_received"] == 0
                    and terminal["grant_eof"] is True
                    and type(terminal["exit_code"]) is int
                    and terminal["exit_code"] == EXPECTED_EXIT
                    and terminal["stdout_eof"] is True
                    and terminal["stderr_eof"] is True
                    and terminal["status_eof"] is True
                    and type(terminal["payload_exec_calls"]) is int
                    and terminal["payload_exec_calls"] == 0
                    and terminal["unreaped"] is True,
                    "exact no-grant abort terminal evidence")

            reaped = self._call(
                "ABORT_EXIT_OBSERVED", "REAPING_EXACT", "REAPED_EXACT",
                "reap_exact_model", copy.deepcopy(self.child),
                self.pidfd_token, EXPECTED_EXIT)
            require(type(reaped) is dict and set(reaped) == {
                        "child", "pidfd_token", "wnowait_observed",
                        "reaped", "exit_code", "echild_after_reap",
                        "unexpected_children", "survivor"}
                    and type(reaped["child"]) is dict
                    and SUP.canonical(reaped["child"]) == self._child_bytes
                    and type(reaped["pidfd_token"]) is int
                    and reaped["pidfd_token"] == self._pidfd_authority
                    and reaped["wnowait_observed"] is True
                    and reaped["reaped"] is True
                    and type(reaped["exit_code"]) is int
                    and reaped["exit_code"] == EXPECTED_EXIT
                    and reaped["echild_after_reap"] is True
                    and type(reaped["unexpected_children"]) is int
                    and reaped["unexpected_children"] == 0
                    and reaped["survivor"] is False,
                    "exact abort child reap evidence")
            self.child_may_exist = False
            object.__setattr__(self, "_reap_proven", True)
            self.state = "COMPLETE"
            self._validate("COMPLETE")
            return {"schema": 1,
                    "classification":
                    "MODEL_NO_GRANT_LAUNCHER_REAPED_CAPTURE_COMPLETE",
                    "request_id": self.request["request_id"],
                    "intent_sha256": self._intent_digest,
                    "child": copy.deepcopy(self.child),
                    "pidfd_token": self.pidfd_token,
                    "grant_write_attempts": 0, "grant_bytes_received": 0,
                    "payload_exec_calls": 0, "expected_exit": EXPECTED_EXIT,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            if not self.state.startswith("UNKNOWN"):
                self._poison("RUN")
            raise


def create_no_grant_abort_model(backend, request, owner, intent_bytes,
                                started_ns, deadline_ns):
    return NoGrantAbortController(
        backend, request, owner, intent_bytes, started_ns, deadline_ns)
