#!/usr/bin/python3
"""Injected abort-only fork split; no live fork backend or exec surface."""

import copy
import errno
import hashlib
import json
import os
import re
import select
import threading
import time
import types

import prelive_allocated_graph_enrollment as GRAPH
import prelive_owned_child as OWNED
import prelive_real_fd_adapter as FDS
import prelive_supervisor_model as SUP


HEX32 = re.compile(r"[0-9a-f]{32}")
EXIT_ABORT_EOF = 73
EXIT_GRANT_DATA = 74
EXIT_CHILD_ERROR = 75
STATUS_READY = b"R"
STDOUT_CANARY = b"SLT_NO_GRANT_STDOUT_V1"
STDERR_CANARY = b"SLT_NO_GRANT_STDERR_V1"
CHILD_CLOSE_ROLES = ("grant_parent", "status_parent", "stdout_parent",
                     "stderr_parent")
PARENT_CLOSE_ROLES = ("grant_child", "status_child", "stdout_child",
                      "stderr_child")
PARENT_READ_ROLES = ("status_parent", "stdout_parent", "stderr_parent")
_ATTEMPT_KEY = object()
_TERMINAL_RELEASE_KEY = object()
_MODEL_PIDFD_ANCHOR_KEY = object()
_CHILD_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


class AbortChildRoutine:
    """Child-only state machine over a restricted injected syscall object."""

    def __init__(self, key, context):
        require(key is _CHILD_KEY, "private abort child routine")
        GRAPH.CONSUMER._builtin_tree(context, "abort child context")
        require(type(context) is dict and set(context) == {
                    "schema", "parent", "started_ns", "deadline_ns", "descriptor",
                    "descriptor_digest"}
                and type(context["schema"]) is int and context["schema"] == 1
                and _integer(context["started_ns"])
                and _integer(context["deadline_ns"], 1)
                and context["deadline_ns"] > context["started_ns"]
                and type(context["descriptor_digest"]) is str
                and GRAPH.CONSUMER.IDENTITY.HEX64.fullmatch(
                    context["descriptor_digest"])
                and SUP.digest(context["descriptor"])
                == context["descriptor_digest"],
                "strict abort child context")
        self.context = copy.deepcopy(context)
        self._context_bytes = SUP.canonical(context)
        self.state = "INIT"
        self.received = b""
        self.owner_thread = threading.current_thread()
        self.poisoned = False
        self.exit_attempted = False
        self.last_clock_ns = context["started_ns"]
        self._backend = None
        self._methods = None
        self._sealed = True

    _PROTECTED = frozenset(("state", "received", "poisoned",
                            "exit_attempted", "last_clock_ns", "context",
                            "_context_bytes", "_sealed", "_backend",
                            "_methods", "owner_thread"))

    def __setattr__(self, name, value):
        if "_sealed" in self.__dict__ and name in type(self)._PROTECTED:
            raise Refusal("abort child authority is immutable externally")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if "_sealed" in self.__dict__:
            raise Refusal("abort child authority cannot be deleted")
        object.__delattr__(self, name)

    def _set(self, name, value):
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("abort child routine is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("abort child routine is noncopyable")

    def _validate(self, expected):
        GRAPH.CONSUMER._builtin_tree(self.context,
                                     "stored abort child context")
        require(threading.current_thread() is self.owner_thread
                and self.state == expected
                and self.poisoned is False
                and SUP.canonical(self.context) == self._context_bytes
                and type(self.received) is bytes and len(self.received) <= 1,
                "abort child routine changed")

    def _install_backend(self, syscalls):
        backend_type = type(syscalls)
        names = ("monotonic_ns", "current_child_identity",
                 "validate_child_graph", "close_role_once", "wait_writable",
                 "write_role", "read_grant", "exit_child")
        markers = (backend_type.__dict__.get("abort_child_split_mock") is True,
                   backend_type.__dict__.get("abort_child_split_linux") is True)
        require(sum(markers) == 1
                and all(callable(backend_type.__dict__.get(name))
                        for name in names)
                and all(name not in syscalls.__dict__ for name in names),
                "closed abort child syscall mock required")
        self._set("_backend", syscalls)
        self._set("_methods", {name: backend_type.__dict__[name]
                               for name in names})

    def _invoke(self, name, *args):
        require(self._backend is not None and self._methods is not None
                and type(self._backend).__dict__.get(name)
                is self._methods[name] and name not in self._backend.__dict__,
                "abort child syscall surface changed")
        return self._methods[name](self._backend, *args)

    def _guard(self, expected):
        self._validate(expected)
        require(self.exit_attempted is False,
                "abort child exit already attempted")

    def _clock(self, expected):
        self._guard(expected)
        previous = self.last_clock_ns
        value = self._invoke("monotonic_ns")
        self._guard(expected)
        require(_integer(value) and value >= previous
                and value < self.context["deadline_ns"],
                "abort child deadline reached or clock regressed")
        self._set("last_clock_ns", value)
        return value

    def _write_exact(self, role, payload, expected_state):
        offset = 0
        while offset < len(payload):
            self._clock(expected_state)
            try:
                count = self._invoke("write_role", role, payload[offset:])
            except InterruptedError:
                self._clock(expected_state)
                continue
            except BlockingIOError as exc:
                require(exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK),
                        "abort child write error")
                self._invoke("wait_writable", role,
                             self.context["deadline_ns"])
                self._clock(expected_state)
                continue
            self._clock(expected_state)
            require(type(count) is int and 0 < count <= len(payload) - offset,
                    "abort child short write made no valid progress")
            offset += count

    def _poison(self):
        self._set("poisoned", True)
        self._set("state", "UNKNOWN")

    def _exit_once(self, code):
        if self.exit_attempted:
            self._poison()
            raise Refusal("abort child exit replay")
        self._set("exit_attempted", True)
        self._set("state", "EXITING")
        self._invoke("exit_child", code)
        self._poison()
        raise Refusal("child exit returned")

    def step(self, syscalls):
        try:
            if self.state == "INIT":
                self._validate("INIT")
                self._install_backend(syscalls)
                self._set("state", "INITIALIZING")
                self._clock("INITIALIZING")
                child = self._invoke("current_child_identity")
                self._guard("INITIALIZING")
                GRAPH.CONSUMER._builtin_tree(child, "abort child identity")
                parent = self.context["parent"]
                require(type(child) is dict and set(child) == {
                            "pid", "ppid", "starttime", "boot_id"}
                        and _integer(child["pid"], 1)
                        and _integer(child["ppid"], 1)
                        and _integer(child["starttime"], 1)
                        and child["pid"] != parent["pid"]
                        and child["ppid"] == parent["pid"]
                        and child["boot_id"] == parent["boot_id"],
                        "abort child identity mismatch")
                graph_result = self._invoke(
                    "validate_child_graph",
                    copy.deepcopy(self.context["descriptor"]))
                self._guard("INITIALIZING")
                require(graph_result is True,
                        "abort child graph validation not confirmed")
                by_role = {item["role"]: item
                           for item in self.context["descriptor"]["endpoints"]}
                for role in CHILD_CLOSE_ROLES:
                    close_result = self._invoke(
                        "close_role_once", role, copy.deepcopy(by_role[role]))
                    self._guard("INITIALIZING")
                    require(close_result is True,
                            "abort child close not confirmed")
                self._write_exact("status_child", STATUS_READY,
                                  "INITIALIZING")
                self._write_exact("stdout_child", STDOUT_CANARY,
                                  "INITIALIZING")
                self._write_exact("stderr_child", STDERR_CANARY,
                                  "INITIALIZING")
                self._clock("INITIALIZING")
                self._set("state", "WAIT_GRANT_EOF")
                return {"classification": "CHILD_WAITING_GRANT_EOF"}
            self._validate("WAIT_GRANT_EOF")
            self._set("state", "READING_GRANT")
            self._clock("READING_GRANT")
            try:
                result = self._invoke("read_grant", 2 - len(self.received))
            except InterruptedError:
                self._clock("READING_GRANT")
                self._set("state", "WAIT_GRANT_EOF")
                return {"classification": "CHILD_WAITING_GRANT_EOF"}
            except BlockingIOError as exc:
                require(exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK),
                        "abort child grant read error")
                self._clock("READING_GRANT")
                self._set("state", "WAIT_GRANT_EOF")
                return {"classification": "CHILD_WAITING_GRANT_EOF"}
            self._clock("READING_GRANT")
            require(type(result) is bytes, "strict abort child grant read")
            if result:
                self._set("received", self.received + result)
                self._exit_once(EXIT_GRANT_DATA)
            require(self.received == b"", "grant data preceded EOF")
            self._exit_once(EXIT_ABORT_EOF)
        except Exception:
            if not self.exit_attempted:
                self._poison()
                self._set("exit_attempted", False)
                self._exit_once(EXIT_CHILD_ERROR)
            self._poison()
            raise


class _ModelPidfdAnchorCapability:
    """Opaque assumed anchor model; never proves or owns a real descriptor."""

    __slots__ = ("_claim", "_owner_thread", "_primary_fd", "_anchor_fd",
                 "_receipt_bytes", "_consumed", "_primary_model_owned",
                 "_sealed")

    def __init__(self, key, claim, primary_fd, anchor_fd, receipt):
        require(key is _MODEL_PIDFD_ANCHOR_KEY
                and type(claim) is _AbortTerminalReleaseClaim
                and type(primary_fd) is int and primary_fd >= 3
                and type(anchor_fd) is int and anchor_fd >= 3
                and anchor_fd != primary_fd,
                "private model pidfd anchor capability")
        GRAPH.CONSUMER._builtin_tree(receipt, "model pidfd anchor receipt")
        object.__setattr__(self, "_claim", claim)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_primary_fd", primary_fd)
        object.__setattr__(self, "_anchor_fd", anchor_fd)
        object.__setattr__(self, "_receipt_bytes", SUP.canonical(receipt))
        object.__setattr__(self, "_consumed", False)
        object.__setattr__(self, "_primary_model_owned", True)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("model pidfd anchor capability is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("model pidfd anchor capability cannot be deleted")

    def __copy__(self):
        raise Refusal("model pidfd anchor capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("model pidfd anchor capability is noncopyable")

    def receipt(self):
        require(type(self) is _ModelPidfdAnchorCapability
                and threading.current_thread() is self._owner_thread
                and type(self._consumed) is bool,
                "foreign model pidfd anchor capability")
        return json.loads(self._receipt_bytes.decode("ascii"))


class _AbortTerminalReleaseClaim:
    """Opaque source-only claim; performs no close or other syscall."""

    __slots__ = ("_attempt", "_owner_thread", "_handle", "_capture",
                 "_owned_pidfd", "_poller", "_resource_scalar",
                 "_receipt_bytes", "_epoll_attempted", "_epoll_active",
                 "_epoll_outcome_bytes", "_release_active",
                 "_status_attempted", "_status_active",
                 "_status_outcome_bytes", "_stdout_attempted",
                 "_stdout_active", "_stdout_outcome_bytes",
                 "_stderr_attempted", "_stderr_active",
                 "_stderr_outcome_bytes", "_anchor_attempted",
                 "_anchor_active", "_anchor_capability",
                 "_anchor_outcome_bytes", "_pidfd_attempted",
                 "_pidfd_active", "_pidfd_outcome_bytes", "_sealed")

    def __init__(self, key, attempt, handle, capture, owned_pidfd, poller,
                 resource_scalar, receipt):
        require(key is _TERMINAL_RELEASE_KEY,
                "terminal release claim construction is private")
        object.__setattr__(self, "_attempt", attempt)
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_handle", handle)
        object.__setattr__(self, "_capture", capture)
        object.__setattr__(self, "_owned_pidfd", owned_pidfd)
        object.__setattr__(self, "_poller", poller)
        object.__setattr__(self, "_resource_scalar", resource_scalar)
        GRAPH.CONSUMER._builtin_tree(receipt, "terminal release claim receipt")
        object.__setattr__(self, "_receipt_bytes", SUP.canonical(receipt))
        object.__setattr__(self, "_epoll_attempted", False)
        object.__setattr__(self, "_epoll_active", False)
        object.__setattr__(self, "_epoll_outcome_bytes", None)
        object.__setattr__(self, "_release_active", None)
        object.__setattr__(self, "_status_attempted", False)
        object.__setattr__(self, "_status_active", False)
        object.__setattr__(self, "_status_outcome_bytes", None)
        object.__setattr__(self, "_stdout_attempted", False)
        object.__setattr__(self, "_stdout_active", False)
        object.__setattr__(self, "_stdout_outcome_bytes", None)
        object.__setattr__(self, "_stderr_attempted", False)
        object.__setattr__(self, "_stderr_active", False)
        object.__setattr__(self, "_stderr_outcome_bytes", None)
        object.__setattr__(self, "_anchor_attempted", False)
        object.__setattr__(self, "_anchor_active", False)
        object.__setattr__(self, "_anchor_capability", None)
        object.__setattr__(self, "_anchor_outcome_bytes", None)
        object.__setattr__(self, "_pidfd_attempted", False)
        object.__setattr__(self, "_pidfd_active", False)
        object.__setattr__(self, "_pidfd_outcome_bytes", None)
        object.__setattr__(self, "_sealed", True)

    def __copy__(self):
        raise Refusal("terminal release claim is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("terminal release claim is noncopyable")

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("terminal release claim is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("terminal release claim cannot be deleted")

    def receipt(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread
                and self._attempt.terminal_release_claim is self
                and self._attempt.state
                    == "LINUX_ABORT_TERMINAL_RELEASE_CLAIMED"
                and self._attempt.poisoned is False
                and self._attempt.handle is self._handle
                and self._attempt.capture_handle is self._capture
                and self._attempt.owned_pidfd is self._owned_pidfd
                and self._capture.poller is self._poller,
                "terminal release claim owner changed")
        self._attempt._validate_minted_terminal_claim_current(self)
        return json.loads(self._receipt_bytes.decode("ascii"))

    def epoll_outcome(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread,
                "terminal release claim owner changed")
        return (None if self._epoll_outcome_bytes is None else
                json.loads(self._epoll_outcome_bytes.decode("ascii")))

    def release_epoll_model_once(self, backend):
        """Model one epoll close; closed backend forbids real OS operations."""
        if self._epoll_attempted is not False:
            if self._epoll_active is True:
                object.__setattr__(self, "_epoll_active", False)
            if self._release_active is not None:
                object.__setattr__(self, "_release_active", "POISONED")
            raise Refusal("epoll release is one-shot")
        backend_type = type(backend)
        methods = ("observe_epoll", "close_epoll")
        require(type(backend_type) is type
                and backend_type.__dict__.get(
                    "source_epoll_release_mock") is True
                and all(type(backend_type.__dict__.get(name))
                        is types.FunctionType
                        for name in methods),
                "closed source epoll-release mock required")
        observe_method = backend_type.__dict__["observe_epoll"]
        close_method = backend_type.__dict__["close_epoll"]
        require(self._epoll_active is False
                and self._release_active is None
                and self._epoll_outcome_bytes is None,
                "fresh epoll release required")
        object.__setattr__(self, "_epoll_attempted", True)
        object.__setattr__(self, "_epoll_active", True)
        object.__setattr__(self, "_release_active", "EPOLL")
        attempted = False
        confirmed = False
        raw_result = None
        raw_result_safe = None
        error_type = None
        classification = "MODEL_EPOLL_RELEASE_UNKNOWN"
        try:
            self._attempt._validate_minted_terminal_claim_current(self)
            receipt = json.loads(self._receipt_bytes.decode("ascii"))
            poller_fd = self._resource_scalar[0]
            observed = observe_method(
                backend, self._poller, poller_fd)
            GRAPH.CONSUMER._builtin_tree(
                observed, "source epoll observation")
            require(type(observed) is dict
                    and all(type(key) is str for key in observed)
                    and set(observed) == {"same_object", "closed", "fd",
                                          "observed_ns"}
                    and type(observed["same_object"]) is bool
                    and observed["same_object"] is True
                    and type(observed["closed"]) is bool
                    and observed["closed"] is False
                    and type(observed["fd"]) is int
                    and observed["fd"] == poller_fd
                    and type(observed["observed_ns"]) is int
                    and receipt["claimed_ns"] <= observed["observed_ns"]
                    < receipt["deadline_ns"],
                    "exact source epoll observation required")
            observed_snapshot = copy.deepcopy(observed)
            observed_ns = observed_snapshot["observed_ns"]
            self._attempt._validate_minted_terminal_claim_current(self)
            require(self._epoll_active is True
                    and self._release_active == "EPOLL"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_epoll")
                        is observe_method
                    and backend_type.__dict__.get("close_epoll")
                        is close_method,
                    "reentrant epoll release changed authority")
            attempted = True
            raw_result = close_method(
                backend, self._poller, poller_fd)
            GRAPH.CONSUMER._builtin_tree(
                raw_result, "source epoll close result")
            require(self._epoll_active is True
                    and self._release_active == "EPOLL"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_epoll")
                        is observe_method
                    and backend_type.__dict__.get("close_epoll")
                        is close_method,
                    "epoll close callback changed authority")
            self._attempt._validate_minted_terminal_claim_current(self)
            require(type(raw_result) is dict
                    and set(raw_result) == {"closed", "finished_ns"}
                    and raw_result["closed"] is True
                    and type(raw_result["finished_ns"]) is int
                    and raw_result["finished_ns"] >= observed_ns,
                    "strict source epoll close result")
            candidate_result = copy.deepcopy(raw_result)
            SUP.canonical(candidate_result)
            raw_result_safe = candidate_result
            confirmed = True
            classification = ("MODEL_EPOLL_CLOSE_CONFIRMED"
                if raw_result["finished_ns"] < receipt["deadline_ns"]
                else "MODEL_EPOLL_CLOSE_CONFIRMED_LATE")
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            outcome = {"schema": 1, "classification": classification,
                "epoll_close_attempted": attempted,
                "epoll_close_confirmed": confirmed,
                "raw_result": raw_result_safe,
                "error_type": error_type,
                "pipe_close_attempts": 0, "pidfd_close_attempts": 0,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            try:
                GRAPH.CONSUMER._builtin_tree(
                    outcome, "source epoll release outcome")
                object.__setattr__(self, "_epoll_outcome_bytes",
                                   SUP.canonical(outcome))
            finally:
                object.__setattr__(self, "_epoll_active", False)
                object.__setattr__(self, "_release_active", None)
        return self.epoll_outcome()

    def status_outcome(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread,
                "terminal release claim owner changed")
        return (None if self._status_outcome_bytes is None else
                json.loads(self._status_outcome_bytes.decode("ascii")))

    def _timely_epoll_model_outcome(self):
        require(self._epoll_attempted is True
                and self._epoll_active is False
                and self._epoll_outcome_bytes is not None,
                "confirmed epoll model release required")
        outcome = json.loads(self._epoll_outcome_bytes.decode("ascii"))
        GRAPH.CONSUMER._builtin_tree(outcome, "epoll release prerequisite")
        require(SUP.canonical(outcome) == self._epoll_outcome_bytes
                and type(outcome) is dict
                and set(outcome) == {"schema", "classification",
                    "epoll_close_attempted", "epoll_close_confirmed",
                    "raw_result", "error_type", "pipe_close_attempts",
                    "pidfd_close_attempts", "resources_closed",
                    "runtime_authorized", "storage_authorized",
                    "exec_proven"}
                and outcome["schema"] == 1
                and outcome["classification"]
                    == "MODEL_EPOLL_CLOSE_CONFIRMED"
                and outcome["epoll_close_attempted"] is True
                and outcome["epoll_close_confirmed"] is True
                and outcome["error_type"] is None
                and outcome["pipe_close_attempts"] == 0
                and outcome["pidfd_close_attempts"] == 0
                and outcome["resources_closed"] is False
                and outcome["runtime_authorized"] is False
                and outcome["storage_authorized"] is False
                and outcome["exec_proven"] is False
                and type(outcome["raw_result"]) is dict
                and set(outcome["raw_result"]) == {"closed", "finished_ns"}
                and outcome["raw_result"]["closed"] is True
                and type(outcome["raw_result"]["finished_ns"]) is int,
                "exact timely epoll model outcome required")
        return outcome

    def release_status_model_once(self, backend):
        """Model one status read-end close; performs no real FD operation."""
        if self._release_active is not None:
            object.__setattr__(self, "_release_active", "POISONED")
            if self._status_active is True:
                object.__setattr__(self, "_status_active", False)
            raise Refusal("resource release is already active")
        if self._status_attempted is not False:
            if self._status_active is True:
                object.__setattr__(self, "_status_active", False)
            if self._release_active is not None:
                object.__setattr__(self, "_release_active", "POISONED")
            raise Refusal("status release is one-shot")
        self._attempt._validate_minted_terminal_claim_current(self)
        epoll = self._timely_epoll_model_outcome()
        backend_type = type(backend)
        methods = ("observe_status", "close_status")
        require(type(backend_type) is type
                and backend_type.__dict__.get(
                    "source_status_release_mock") is True
                and all(type(backend_type.__dict__.get(name))
                        is types.FunctionType for name in methods),
                "closed source status-release mock required")
        observe_method = backend_type.__dict__["observe_status"]
        close_method = backend_type.__dict__["close_status"]
        require(self._release_active is None
                and self._status_active is False
                and self._status_outcome_bytes is None,
                "fresh status release required")
        object.__setattr__(self, "_status_attempted", True)
        object.__setattr__(self, "_status_active", True)
        object.__setattr__(self, "_release_active", "STATUS")
        attempted = False
        confirmed = False
        raw_result_safe = None
        error_type = None
        classification = "MODEL_STATUS_RELEASE_UNKNOWN"
        try:
            receipt = json.loads(self._receipt_bytes.decode("ascii"))
            status_identity = self._handle._authority_record("status_parent")
            GRAPH.CONSUMER._builtin_tree(
                status_identity, "source status allocation identity")
            status_identity_bytes = SUP.canonical(status_identity)
            status_fd = dict(self._resource_scalar[1])["status_parent"]
            require(status_identity["fd"] == status_fd,
                    "status release allocation identity changed")
            observed = observe_method(
                backend, status_fd, copy.deepcopy(status_identity))
            GRAPH.CONSUMER._builtin_tree(
                observed, "source status observation")
            require(type(observed) is dict
                    and all(type(key) is str for key in observed)
                    and set(observed) == {"role", "fd", "identity",
                        "identity_matches", "closed", "observed_ns"}
                    and type(observed["role"]) is str
                    and observed["role"] == "status_parent"
                    and type(observed["fd"]) is int
                    and observed["fd"] == status_fd
                    and type(observed["identity"]) is dict
                    and SUP.canonical(observed["identity"])
                        == status_identity_bytes
                    and type(observed["identity_matches"]) is bool
                    and observed["identity_matches"] is True
                    and type(observed["closed"]) is bool
                    and observed["closed"] is False
                    and type(observed["observed_ns"]) is int
                    and epoll["raw_result"]["finished_ns"]
                        <= observed["observed_ns"]
                    < receipt["deadline_ns"],
                    "exact source status observation required")
            observed_snapshot = copy.deepcopy(observed)
            observed_ns = observed_snapshot["observed_ns"]
            self._attempt._validate_minted_terminal_claim_current(self)
            require(self._status_active is True
                    and self._release_active == "STATUS"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_status")
                        is observe_method
                    and backend_type.__dict__.get("close_status")
                        is close_method
                    and SUP.canonical(self._handle._authority_record(
                        "status_parent")) == status_identity_bytes,
                    "reentrant status release changed authority")
            attempted = True
            raw_result = close_method(
                backend, status_fd, copy.deepcopy(status_identity))
            GRAPH.CONSUMER._builtin_tree(
                raw_result, "source status close result")
            require(self._status_active is True
                    and self._release_active == "STATUS"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_status")
                        is observe_method
                    and backend_type.__dict__.get("close_status")
                        is close_method,
                    "status close callback changed authority")
            self._attempt._validate_minted_terminal_claim_current(self)
            require(SUP.canonical(self._handle._authority_record(
                        "status_parent")) == status_identity_bytes
                    and type(raw_result) is dict
                    and set(raw_result) == {"closed", "finished_ns"}
                    and raw_result["closed"] is True
                    and type(raw_result["finished_ns"]) is int
                    and raw_result["finished_ns"] >= observed_ns,
                    "strict source status close result")
            candidate_result = copy.deepcopy(raw_result)
            SUP.canonical(candidate_result)
            raw_result_safe = candidate_result
            confirmed = True
            classification = ("MODEL_STATUS_CLOSE_CONFIRMED"
                if raw_result["finished_ns"] < receipt["deadline_ns"]
                else "MODEL_STATUS_CLOSE_CONFIRMED_LATE")
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            outcome = {"schema": 1, "classification": classification,
                "status_close_attempted": attempted,
                "status_close_confirmed": confirmed,
                "raw_result": raw_result_safe,
                "error_type": error_type,
                "epoll_model_confirmed": True,
                "stdout_close_attempts": 0,
                "stderr_close_attempts": 0,
                "pidfd_close_attempts": 0,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            try:
                GRAPH.CONSUMER._builtin_tree(
                    outcome, "source status release outcome")
                object.__setattr__(self, "_status_outcome_bytes",
                                   SUP.canonical(outcome))
            finally:
                object.__setattr__(self, "_status_active", False)
                object.__setattr__(self, "_release_active", None)
        return self.status_outcome()

    def stdout_outcome(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread,
                "terminal release claim owner changed")
        return (None if self._stdout_outcome_bytes is None else
                json.loads(self._stdout_outcome_bytes.decode("ascii")))

    def _timely_status_model_outcome(self):
        require(self._status_attempted is True
                and self._status_active is False
                and self._status_outcome_bytes is not None,
                "confirmed status model release required")
        outcome = json.loads(self._status_outcome_bytes.decode("ascii"))
        GRAPH.CONSUMER._builtin_tree(outcome, "status release prerequisite")
        require(SUP.canonical(outcome) == self._status_outcome_bytes
                and type(outcome) is dict
                and set(outcome) == {"schema", "classification",
                    "status_close_attempted", "status_close_confirmed",
                    "raw_result", "error_type", "epoll_model_confirmed",
                    "stdout_close_attempts", "stderr_close_attempts",
                    "pidfd_close_attempts", "resources_closed",
                    "runtime_authorized", "storage_authorized",
                    "exec_proven"}
                and outcome["schema"] == 1
                and outcome["classification"]
                    == "MODEL_STATUS_CLOSE_CONFIRMED"
                and outcome["status_close_attempted"] is True
                and outcome["status_close_confirmed"] is True
                and outcome["error_type"] is None
                and outcome["epoll_model_confirmed"] is True
                and outcome["stdout_close_attempts"] == 0
                and outcome["stderr_close_attempts"] == 0
                and outcome["pidfd_close_attempts"] == 0
                and outcome["resources_closed"] is False
                and outcome["runtime_authorized"] is False
                and outcome["storage_authorized"] is False
                and outcome["exec_proven"] is False
                and type(outcome["raw_result"]) is dict
                and set(outcome["raw_result"]) == {"closed", "finished_ns"}
                and outcome["raw_result"]["closed"] is True
                and type(outcome["raw_result"]["finished_ns"]) is int,
                "exact timely status model outcome required")
        self._timely_epoll_model_outcome()
        return outcome

    def _stdout_release_evidence(self):
        """Purely bind stdout canary/capture receipt to allocation identity."""
        import prelive_capture_settlement as CAPTURE
        self._attempt._validate_minted_terminal_claim_current(self)
        allocation = self._handle._authority_record("stdout_parent")
        GRAPH.CONSUMER._builtin_tree(
            allocation, "stdout release allocation identity")
        identities = self._capture.origin.identities
        require(type(identities) is dict
                and all(type(key) is str for key in identities)
                and set(identities) == {"stdout", "stderr"},
                "strict stdout release origin identities")
        origin = CAPTURE._identity(identities["stdout"])
        projection = {key: allocation[key]
                      for key in ("fd", "dev", "inode", "mode", "flags")}
        stream = self._capture.streams["stdout"]
        snapshot = CAPTURE._strict_stream_snapshot(stream, "stdout")
        frozen = self._attempt.monitor_capability.completion["streams"][
            "stdout"]
        policy = self._attempt._protocol_policy
        require(type(policy) is dict
                and all(type(key) is str for key in policy)
                and set(policy) == {"schema", "exit_code", "stdout_hex",
                                    "stderr_hex"},
                "strict stdout release protocol policy")
        GRAPH.CONSUMER._builtin_tree(
            policy, "stdout release protocol policy")
        require(SUP.canonical(policy)
                == self._attempt._protocol_policy_bytes,
                "stdout release protocol policy changed")
        expected = bytes.fromhex(policy["stdout_hex"])
        require(origin == projection
                and snapshot == frozen
                and snapshot["pipe_identity"] == origin
                and snapshot["eof"] is True
                and snapshot["truncated"] is False
                and snapshot["stored_bytes"] == len(expected)
                and snapshot["observed_bytes"] == len(expected)
                and snapshot["prefix_sha256"]
                    == hashlib.sha256(expected).hexdigest()
                and snapshot["digest_scope"] == "FULL_CAPTURE"
                and bytes(stream.stored) == expected,
                "exact stdout release evidence required")
        evidence = {"role": "stdout_parent",
            "allocation_identity": allocation,
            "capture_receipt": copy.deepcopy(snapshot),
            "canary_hex": expected.hex()}
        GRAPH.CONSUMER._builtin_tree(evidence, "stdout release evidence")
        return SUP.canonical(evidence)

    def release_stdout_model_once(self, backend):
        """Model one stdout read-end close; performs no real FD operation."""
        if self._release_active is not None:
            object.__setattr__(self, "_release_active", "POISONED")
            if self._stdout_active is True:
                object.__setattr__(self, "_stdout_active", False)
            raise Refusal("resource release is already active")
        if self._stdout_attempted is not False:
            raise Refusal("stdout release is one-shot")
        status = self._timely_status_model_outcome()
        evidence_bytes = self._stdout_release_evidence()
        backend_type = type(backend)
        methods = ("observe_stdout", "close_stdout")
        require(type(backend_type) is type
                and backend_type.__dict__.get(
                    "source_stdout_release_mock") is True
                and all(type(backend_type.__dict__.get(name))
                        is types.FunctionType for name in methods),
                "closed source stdout-release mock required")
        observe_method = backend_type.__dict__["observe_stdout"]
        close_method = backend_type.__dict__["close_stdout"]
        require(self._stdout_active is False
                and self._stdout_outcome_bytes is None,
                "fresh stdout release required")
        object.__setattr__(self, "_stdout_attempted", True)
        object.__setattr__(self, "_stdout_active", True)
        object.__setattr__(self, "_release_active", "STDOUT")
        attempted = False
        confirmed = False
        raw_result_safe = None
        error_type = None
        classification = "MODEL_STDOUT_RELEASE_UNKNOWN"
        try:
            receipt = json.loads(self._receipt_bytes.decode("ascii"))
            identity = self._handle._authority_record("stdout_parent")
            identity_bytes = SUP.canonical(identity)
            stdout_fd = dict(self._resource_scalar[1])["stdout_parent"]
            require(identity["fd"] == stdout_fd,
                    "stdout release allocation identity changed")
            observed = observe_method(
                backend, stdout_fd, copy.deepcopy(identity))
            GRAPH.CONSUMER._builtin_tree(
                observed, "source stdout observation")
            require(type(observed) is dict
                    and all(type(key) is str for key in observed)
                    and set(observed) == {"role", "fd", "identity",
                        "identity_matches", "closed", "observed_ns"}
                    and type(observed["role"]) is str
                    and observed["role"] == "stdout_parent"
                    and type(observed["fd"]) is int
                    and observed["fd"] == stdout_fd
                    and type(observed["identity"]) is dict
                    and SUP.canonical(observed["identity"])
                        == identity_bytes
                    and type(observed["identity_matches"]) is bool
                    and observed["identity_matches"] is True
                    and type(observed["closed"]) is bool
                    and observed["closed"] is False
                    and type(observed["observed_ns"]) is int
                    and status["raw_result"]["finished_ns"]
                        <= observed["observed_ns"]
                    < receipt["deadline_ns"],
                    "exact source stdout observation required")
            observed_snapshot = copy.deepcopy(observed)
            observed_ns = observed_snapshot["observed_ns"]
            current_evidence = self._stdout_release_evidence()
            require(self._stdout_active is True
                    and self._release_active == "STDOUT"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_stdout")
                        is observe_method
                    and backend_type.__dict__.get("close_stdout")
                        is close_method
                    and SUP.canonical(self._handle._authority_record(
                        "stdout_parent")) == identity_bytes
                    and current_evidence == evidence_bytes,
                    "reentrant stdout release changed authority")
            attempted = True
            raw_result = close_method(
                backend, stdout_fd, copy.deepcopy(identity))
            GRAPH.CONSUMER._builtin_tree(
                raw_result, "source stdout close result")
            require(self._stdout_active is True
                    and self._release_active == "STDOUT"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_stdout")
                        is observe_method
                    and backend_type.__dict__.get("close_stdout")
                        is close_method,
                    "stdout close callback changed authority")
            current_evidence = self._stdout_release_evidence()
            require(SUP.canonical(self._handle._authority_record(
                        "stdout_parent")) == identity_bytes
                    and self._stdout_active is True
                    and self._release_active == "STDOUT"
                    and current_evidence == evidence_bytes
                    and type(raw_result) is dict
                    and set(raw_result) == {"closed", "finished_ns"}
                    and raw_result["closed"] is True
                    and type(raw_result["finished_ns"]) is int
                    and raw_result["finished_ns"] >= observed_ns,
                    "strict source stdout close result")
            candidate_result = copy.deepcopy(raw_result)
            SUP.canonical(candidate_result)
            raw_result_safe = candidate_result
            confirmed = True
            classification = ("MODEL_STDOUT_CLOSE_CONFIRMED"
                if raw_result["finished_ns"] < receipt["deadline_ns"]
                else "MODEL_STDOUT_CLOSE_CONFIRMED_LATE")
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            outcome = {"schema": 1, "classification": classification,
                "stdout_close_attempted": attempted,
                "stdout_close_confirmed": confirmed,
                "raw_result": raw_result_safe,
                "error_type": error_type,
                "status_model_confirmed": True,
                "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                "stderr_close_attempts": 0,
                "pidfd_close_attempts": 0,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            try:
                GRAPH.CONSUMER._builtin_tree(
                    outcome, "source stdout release outcome")
                object.__setattr__(self, "_stdout_outcome_bytes",
                                   SUP.canonical(outcome))
            finally:
                object.__setattr__(self, "_stdout_active", False)
                object.__setattr__(self, "_release_active", None)
        return self.stdout_outcome()

    def stderr_outcome(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread,
                "terminal release claim owner changed")
        return (None if self._stderr_outcome_bytes is None else
                json.loads(self._stderr_outcome_bytes.decode("ascii")))

    def _timely_stdout_model_outcome(self):
        require(self._stdout_attempted is True
                and self._stdout_active is False
                and self._stdout_outcome_bytes is not None,
                "confirmed stdout model release required")
        outcome = json.loads(self._stdout_outcome_bytes.decode("ascii"))
        GRAPH.CONSUMER._builtin_tree(outcome, "stdout release prerequisite")
        require(SUP.canonical(outcome) == self._stdout_outcome_bytes
                and type(outcome) is dict
                and set(outcome) == {"schema", "classification",
                    "stdout_close_attempted", "stdout_close_confirmed",
                    "raw_result", "error_type", "status_model_confirmed",
                    "evidence_sha256", "stderr_close_attempts",
                    "pidfd_close_attempts", "resources_closed",
                    "runtime_authorized", "storage_authorized",
                    "exec_proven"}
                and outcome["schema"] == 1
                and outcome["classification"]
                    == "MODEL_STDOUT_CLOSE_CONFIRMED"
                and outcome["stdout_close_attempted"] is True
                and outcome["stdout_close_confirmed"] is True
                and outcome["error_type"] is None
                and outcome["status_model_confirmed"] is True
                and type(outcome["evidence_sha256"]) is str
                and outcome["evidence_sha256"]
                    == hashlib.sha256(
                        self._stdout_release_evidence()).hexdigest()
                and outcome["stderr_close_attempts"] == 0
                and outcome["pidfd_close_attempts"] == 0
                and outcome["resources_closed"] is False
                and outcome["runtime_authorized"] is False
                and outcome["storage_authorized"] is False
                and outcome["exec_proven"] is False
                and type(outcome["raw_result"]) is dict
                and set(outcome["raw_result"]) == {"closed", "finished_ns"}
                and outcome["raw_result"]["closed"] is True
                and type(outcome["raw_result"]["finished_ns"]) is int,
                "exact timely stdout model outcome required")
        self._timely_status_model_outcome()
        return outcome

    def _stderr_release_evidence(self):
        """Purely bind stderr canary/capture receipt to allocation identity."""
        import prelive_capture_settlement as CAPTURE
        self._attempt._validate_minted_terminal_claim_current(self)
        allocation = self._handle._authority_record("stderr_parent")
        GRAPH.CONSUMER._builtin_tree(
            allocation, "stderr release allocation identity")
        identities = self._capture.origin.identities
        require(type(identities) is dict
                and all(type(key) is str for key in identities)
                and set(identities) == {"stdout", "stderr"},
                "strict stderr release origin identities")
        origin = CAPTURE._identity(identities["stderr"])
        projection = {key: allocation[key]
                      for key in ("fd", "dev", "inode", "mode", "flags")}
        streams = self._capture.streams
        require(type(streams) is dict
                and all(type(key) is str for key in streams)
                and set(streams) == {"stdout", "stderr"},
                "strict stderr release streams")
        stream = streams["stderr"]
        snapshot = CAPTURE._strict_stream_snapshot(stream, "stderr")
        completion = self._attempt.monitor_capability.completion
        require(type(completion) is dict
                and type(completion.get("streams")) is dict
                and all(type(key) is str for key in completion["streams"])
                and set(completion["streams"]) == {"stdout", "stderr"},
                "strict stderr release completion")
        frozen = completion["streams"]["stderr"]
        policy = self._attempt._protocol_policy
        require(type(policy) is dict
                and all(type(key) is str for key in policy)
                and set(policy) == {"schema", "exit_code", "stdout_hex",
                                    "stderr_hex"},
                "strict stderr release protocol policy")
        GRAPH.CONSUMER._builtin_tree(
            policy, "stderr release protocol policy")
        require(SUP.canonical(policy)
                == self._attempt._protocol_policy_bytes,
                "stderr release protocol policy changed")
        expected = bytes.fromhex(policy["stderr_hex"])
        require(origin == projection
                and snapshot == frozen
                and snapshot["pipe_identity"] == origin
                and snapshot["eof"] is True
                and snapshot["truncated"] is False
                and snapshot["stored_bytes"] == len(expected)
                and snapshot["observed_bytes"] == len(expected)
                and snapshot["prefix_sha256"]
                    == hashlib.sha256(expected).hexdigest()
                and snapshot["digest_scope"] == "FULL_CAPTURE"
                and bytes(stream.stored) == expected,
                "exact stderr release evidence required")
        evidence = {"role": "stderr_parent",
            "allocation_identity": allocation,
            "capture_receipt": copy.deepcopy(snapshot),
            "canary_hex": expected.hex()}
        GRAPH.CONSUMER._builtin_tree(evidence, "stderr release evidence")
        return SUP.canonical(evidence)

    def release_stderr_model_once(self, backend):
        """Model one stderr read-end close; performs no real FD operation."""
        if self._release_active is not None:
            object.__setattr__(self, "_release_active", "POISONED")
            if self._stderr_active is True:
                object.__setattr__(self, "_stderr_active", False)
            raise Refusal("resource release is already active")
        if self._stderr_attempted is not False:
            raise Refusal("stderr release is one-shot")
        stdout = self._timely_stdout_model_outcome()
        stdout_evidence = self._stdout_release_evidence()
        evidence_bytes = self._stderr_release_evidence()
        backend_type = type(backend)
        methods = ("observe_stderr", "close_stderr")
        require(type(backend_type) is type
                and backend_type.__dict__.get(
                    "source_stderr_release_mock") is True
                and all(type(backend_type.__dict__.get(name))
                        is types.FunctionType for name in methods),
                "closed source stderr-release mock required")
        observe_method = backend_type.__dict__["observe_stderr"]
        close_method = backend_type.__dict__["close_stderr"]
        require(self._stderr_active is False
                and self._stderr_outcome_bytes is None,
                "fresh stderr release required")
        object.__setattr__(self, "_stderr_attempted", True)
        object.__setattr__(self, "_stderr_active", True)
        object.__setattr__(self, "_release_active", "STDERR")
        attempted = False
        confirmed = False
        raw_result_safe = None
        error_type = None
        classification = "MODEL_STDERR_RELEASE_UNKNOWN"
        try:
            receipt = json.loads(self._receipt_bytes.decode("ascii"))
            identity = self._handle._authority_record("stderr_parent")
            identity_bytes = SUP.canonical(identity)
            stderr_fd = dict(self._resource_scalar[1])["stderr_parent"]
            require(identity["fd"] == stderr_fd,
                    "stderr release allocation identity changed")
            observed = observe_method(
                backend, stderr_fd, copy.deepcopy(identity))
            GRAPH.CONSUMER._builtin_tree(
                observed, "source stderr observation")
            require(type(observed) is dict
                    and all(type(key) is str for key in observed)
                    and set(observed) == {"role", "fd", "identity",
                        "identity_matches", "closed", "observed_ns"}
                    and type(observed["role"]) is str
                    and observed["role"] == "stderr_parent"
                    and type(observed["fd"]) is int
                    and observed["fd"] == stderr_fd
                    and type(observed["identity"]) is dict
                    and SUP.canonical(observed["identity"])
                        == identity_bytes
                    and type(observed["identity_matches"]) is bool
                    and observed["identity_matches"] is True
                    and type(observed["closed"]) is bool
                    and observed["closed"] is False
                    and type(observed["observed_ns"]) is int
                    and stdout["raw_result"]["finished_ns"]
                        <= observed["observed_ns"]
                    < receipt["deadline_ns"],
                    "exact source stderr observation required")
            observed_snapshot = copy.deepcopy(observed)
            observed_ns = observed_snapshot["observed_ns"]
            current_stdout = self._stdout_release_evidence()
            current_evidence = self._stderr_release_evidence()
            require(self._stderr_active is True
                    and self._release_active == "STDERR"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_stderr")
                        is observe_method
                    and backend_type.__dict__.get("close_stderr")
                        is close_method
                    and SUP.canonical(self._handle._authority_record(
                        "stderr_parent")) == identity_bytes
                    and current_stdout == stdout_evidence
                    and current_evidence == evidence_bytes,
                    "reentrant stderr release changed authority")
            attempted = True
            raw_result = close_method(
                backend, stderr_fd, copy.deepcopy(identity))
            GRAPH.CONSUMER._builtin_tree(
                raw_result, "source stderr close result")
            require(self._stderr_active is True
                    and self._release_active == "STDERR"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("observe_stderr")
                        is observe_method
                    and backend_type.__dict__.get("close_stderr")
                        is close_method,
                    "stderr close callback changed authority")
            current_stdout = self._stdout_release_evidence()
            current_evidence = self._stderr_release_evidence()
            require(SUP.canonical(self._handle._authority_record(
                        "stderr_parent")) == identity_bytes
                    and self._stderr_active is True
                    and self._release_active == "STDERR"
                    and current_stdout == stdout_evidence
                    and current_evidence == evidence_bytes
                    and type(raw_result) is dict
                    and set(raw_result) == {"closed", "finished_ns"}
                    and raw_result["closed"] is True
                    and type(raw_result["finished_ns"]) is int
                    and raw_result["finished_ns"] >= observed_ns,
                    "strict source stderr close result")
            candidate_result = copy.deepcopy(raw_result)
            SUP.canonical(candidate_result)
            raw_result_safe = candidate_result
            confirmed = True
            classification = ("MODEL_STDERR_CLOSE_CONFIRMED"
                if raw_result["finished_ns"] < receipt["deadline_ns"]
                else "MODEL_STDERR_CLOSE_CONFIRMED_LATE")
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            outcome = {"schema": 1, "classification": classification,
                "stderr_close_attempted": attempted,
                "stderr_close_confirmed": confirmed,
                "raw_result": raw_result_safe,
                "error_type": error_type,
                "stdout_model_confirmed": True,
                "stdout_evidence_sha256": hashlib.sha256(
                    stdout_evidence).hexdigest(),
                "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
                "pidfd_close_attempts": 0,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            try:
                GRAPH.CONSUMER._builtin_tree(
                    outcome, "source stderr release outcome")
                object.__setattr__(self, "_stderr_outcome_bytes",
                                   SUP.canonical(outcome))
            finally:
                object.__setattr__(self, "_stderr_active", False)
                object.__setattr__(self, "_release_active", None)
        return self.stderr_outcome()

    def anchor_outcome(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread,
                "terminal release claim owner changed")
        return (None if self._anchor_outcome_bytes is None else
                json.loads(self._anchor_outcome_bytes.decode("ascii")))

    def _timely_stderr_model_outcome(self):
        require(self._stderr_attempted is True
                and self._stderr_active is False
                and self._stderr_outcome_bytes is not None,
                "confirmed stderr model release required")
        outcome = json.loads(self._stderr_outcome_bytes.decode("ascii"))
        GRAPH.CONSUMER._builtin_tree(outcome, "stderr release prerequisite")
        stdout_evidence = self._stdout_release_evidence()
        stderr_evidence = self._stderr_release_evidence()
        require(SUP.canonical(outcome) == self._stderr_outcome_bytes
                and type(outcome) is dict
                and set(outcome) == {"schema", "classification",
                    "stderr_close_attempted", "stderr_close_confirmed",
                    "raw_result", "error_type", "stdout_model_confirmed",
                    "stdout_evidence_sha256", "evidence_sha256",
                    "pidfd_close_attempts", "resources_closed",
                    "runtime_authorized", "storage_authorized",
                    "exec_proven"}
                and outcome["schema"] == 1
                and outcome["classification"]
                    == "MODEL_STDERR_CLOSE_CONFIRMED"
                and outcome["stderr_close_attempted"] is True
                and outcome["stderr_close_confirmed"] is True
                and outcome["error_type"] is None
                and outcome["stdout_model_confirmed"] is True
                and type(outcome["stdout_evidence_sha256"]) is str
                and outcome["stdout_evidence_sha256"]
                    == hashlib.sha256(stdout_evidence).hexdigest()
                and type(outcome["evidence_sha256"]) is str
                and outcome["evidence_sha256"]
                    == hashlib.sha256(stderr_evidence).hexdigest()
                and outcome["pidfd_close_attempts"] == 0
                and outcome["resources_closed"] is False
                and outcome["runtime_authorized"] is False
                and outcome["storage_authorized"] is False
                and outcome["exec_proven"] is False
                and type(outcome["raw_result"]) is dict
                and set(outcome["raw_result"]) == {"closed", "finished_ns"}
                and outcome["raw_result"]["closed"] is True
                and type(outcome["raw_result"]["finished_ns"]) is int,
                "exact timely stderr model outcome required")
        self._timely_stdout_model_outcome()
        return outcome

    def _pidfd_anchor_evidence(self):
        """Freeze original abort pidfd provenance without claiming OFD proof."""
        self._attempt._validate_minted_terminal_claim_current(self)
        stderr = self._timely_stderr_model_outcome()
        owned = self._owned_pidfd
        lifecycle = owned.lifecycle
        receipt = self._attempt.settlement_receipt
        require(type(owned) is OWNED._OwnedPidfdHandle
                and type(owned.pidfd) is int
                and owned.pidfd == self._resource_scalar[2]
                and type(owned.child) is dict
                and type(owned.launcher) is dict
                and type(lifecycle) is dict
                and lifecycle is self._attempt.lifecycle
                and type(receipt) is dict,
                "strict model pidfd anchor provenance")
        for value, label in ((owned.child, "model anchor child"),
                (owned.launcher, "model anchor launcher"),
                (receipt, "model anchor settlement")):
            GRAPH.CONSUMER._builtin_tree(value, label)
        evidence = {"schema": 1,
            "classification": "PIDFD_ANCHOR_PROVENANCE_MODEL_ONLY",
            "attempt_id": self._attempt.attempt_id,
            "primary_fd": owned.pidfd,
            "child": copy.deepcopy(owned.child),
            "launcher": copy.deepcopy(owned.launcher),
            "lifecycle_token": lifecycle["token"],
            "lifecycle_state": lifecycle["state"],
            "settlement_sha256": hashlib.sha256(
                SUP.canonical(receipt)).hexdigest(),
            "protocol_sha256": hashlib.sha256(
                self._attempt._protocol_evidence_bytes).hexdigest(),
            "stderr_finished_ns": stderr["raw_result"]["finished_ns"],
            "deadline_ns": self.receipt()["deadline_ns"],
            "model_assumed": True,
            "kernel_ofd_proven": False,
            "runtime_authorized": False,
            "storage_authorized": False, "exec_proven": False}
        GRAPH.CONSUMER._builtin_tree(evidence, "model pidfd anchor evidence")
        return SUP.canonical(evidence)

    def acquire_pidfd_anchor_model_once(self, backend):
        """Model anchor acquisition; creates no descriptor and proves no OFD."""
        if self._release_active is not None:
            object.__setattr__(self, "_release_active", "POISONED")
            if self._anchor_active is True:
                object.__setattr__(self, "_anchor_active", False)
            raise Refusal("resource release is already active")
        if self._anchor_attempted is not False:
            raise Refusal("pidfd anchor acquisition is one-shot")
        evidence_bytes = self._pidfd_anchor_evidence()
        stderr = self._timely_stderr_model_outcome()
        backend_type = type(backend)
        method_name = "acquire_anchor"
        require(type(backend_type) is type
                and backend_type.__dict__.get(
                    "source_pidfd_anchor_mock") is True
                and type(backend_type.__dict__.get(method_name))
                    is types.FunctionType,
                "closed source pidfd-anchor mock required")
        acquire_method = backend_type.__dict__[method_name]
        require(self._anchor_active is False
                and self._anchor_capability is None
                and self._anchor_outcome_bytes is None,
                "fresh pidfd anchor model required")
        object.__setattr__(self, "_anchor_attempted", True)
        object.__setattr__(self, "_anchor_active", True)
        object.__setattr__(self, "_release_active", "PIDFD_ANCHOR")
        confirmed = False
        raw_result_safe = None
        error_type = None
        classification = "MODEL_PIDFD_ANCHOR_UNKNOWN"
        capability = None
        try:
            primary_fd = self._resource_scalar[2]
            raw_result = acquire_method(
                backend, primary_fd,
                hashlib.sha256(evidence_bytes).hexdigest())
            GRAPH.CONSUMER._builtin_tree(
                raw_result, "source pidfd anchor result")
            current_evidence = self._pidfd_anchor_evidence()
            require(self._anchor_active is True
                    and self._release_active == "PIDFD_ANCHOR"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get(method_name)
                        is acquire_method
                    and current_evidence == evidence_bytes
                    and type(raw_result) is dict
                    and set(raw_result) == {"primary_fd", "anchor_fd",
                        "acquired_ns", "model_assumed"}
                    and type(raw_result["primary_fd"]) is int
                    and raw_result["primary_fd"] == primary_fd
                    and type(raw_result["anchor_fd"]) is int
                    and raw_result["anchor_fd"] >= 3
                    and raw_result["anchor_fd"] != primary_fd
                    and raw_result["anchor_fd"] not in {
                        self._resource_scalar[0],
                        *(fd for _, fd in self._resource_scalar[1])}
                    and type(raw_result["acquired_ns"]) is int
                    and stderr["raw_result"]["finished_ns"]
                        <= raw_result["acquired_ns"]
                    < self.receipt()["deadline_ns"]
                    and type(raw_result["model_assumed"]) is bool
                    and raw_result["model_assumed"] is True,
                    "strict model pidfd anchor result")
            candidate = copy.deepcopy(raw_result)
            SUP.canonical(candidate)
            raw_result_safe = candidate
            cap_receipt = {"schema": 1,
                "classification": "MODEL_PIDFD_ANCHOR_ACQUIRED_ASSUMED",
                "primary_fd": primary_fd,
                "anchor_fd": raw_result["anchor_fd"],
                "acquired_ns": raw_result["acquired_ns"],
                "deadline_ns": self.receipt()["deadline_ns"],
                "provenance_sha256": hashlib.sha256(
                    evidence_bytes).hexdigest(),
                "model_assumed": True, "kernel_ofd_proven": False,
                "real_descriptor_created": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            capability = _ModelPidfdAnchorCapability(
                _MODEL_PIDFD_ANCHOR_KEY, self, primary_fd,
                raw_result["anchor_fd"], cap_receipt)
            confirmed = True
            classification = "MODEL_PIDFD_ANCHOR_ACQUIRED_ASSUMED"
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            outcome = {"schema": 1, "classification": classification,
                "anchor_acquisition_attempted": True,
                "anchor_acquisition_confirmed": confirmed,
                "raw_result": raw_result_safe, "error_type": error_type,
                "model_assumed": True, "kernel_ofd_proven": False,
                "real_descriptor_created": False,
                "pidfd_close_attempts": 0,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            try:
                GRAPH.CONSUMER._builtin_tree(
                    outcome, "source pidfd anchor outcome")
                object.__setattr__(self, "_anchor_outcome_bytes",
                                   SUP.canonical(outcome))
                if confirmed:
                    object.__setattr__(self, "_anchor_capability", capability)
            finally:
                object.__setattr__(self, "_anchor_active", False)
                object.__setattr__(self, "_release_active", None)
        return capability

    def pidfd_outcome(self):
        require(type(self) is _AbortTerminalReleaseClaim
                and threading.current_thread() is self._owner_thread,
                "terminal release claim owner changed")
        return (None if self._pidfd_outcome_bytes is None else
                json.loads(self._pidfd_outcome_bytes.decode("ascii")))

    def _validate_model_anchor_capability(self, capability, consumed,
                                          primary_model_owned):
        require(type(consumed) is bool
                and type(primary_model_owned) is bool
                and type(capability) is _ModelPidfdAnchorCapability
                and capability is self._anchor_capability
                and capability._claim is self
                and capability._owner_thread is self._owner_thread
                and threading.current_thread() is self._owner_thread
                and type(capability._primary_fd) is int
                and type(capability._anchor_fd) is int
                and type(capability._receipt_bytes) is bytes
                and type(capability._consumed) is bool
                and capability._consumed is consumed
                and type(capability._primary_model_owned) is bool
                and capability._primary_model_owned is primary_model_owned,
                "exact model pidfd anchor capability required")
        anchor_outcome = self.anchor_outcome()
        cap_receipt = json.loads(
            capability._receipt_bytes.decode("ascii"))
        for value, label in ((anchor_outcome, "model anchor outcome"),
                (cap_receipt, "model anchor capability receipt")):
            GRAPH.CONSUMER._builtin_tree(value, label)
        evidence_bytes = self._pidfd_anchor_evidence()
        provenance = hashlib.sha256(evidence_bytes).hexdigest()
        require(SUP.canonical(anchor_outcome) == self._anchor_outcome_bytes
                and set(anchor_outcome) == {"schema", "classification",
                    "anchor_acquisition_attempted",
                    "anchor_acquisition_confirmed", "raw_result",
                    "error_type", "model_assumed", "kernel_ofd_proven",
                    "real_descriptor_created", "pidfd_close_attempts",
                    "resources_closed", "runtime_authorized",
                    "storage_authorized", "exec_proven"}
                and anchor_outcome["schema"] == 1
                and anchor_outcome["classification"]
                    == "MODEL_PIDFD_ANCHOR_ACQUIRED_ASSUMED"
                and anchor_outcome["anchor_acquisition_attempted"] is True
                and anchor_outcome["anchor_acquisition_confirmed"] is True
                and anchor_outcome["error_type"] is None
                and anchor_outcome["model_assumed"] is True
                and anchor_outcome["kernel_ofd_proven"] is False
                and anchor_outcome["real_descriptor_created"] is False
                and anchor_outcome["pidfd_close_attempts"] == 0
                and anchor_outcome["resources_closed"] is False
                and anchor_outcome["runtime_authorized"] is False
                and anchor_outcome["storage_authorized"] is False
                and anchor_outcome["exec_proven"] is False
                and type(anchor_outcome["raw_result"]) is dict
                and set(anchor_outcome["raw_result"]) == {"primary_fd",
                    "anchor_fd", "acquired_ns", "model_assumed"}
                and SUP.canonical(cap_receipt) == capability._receipt_bytes
                and set(cap_receipt) == {"schema", "classification",
                    "primary_fd", "anchor_fd", "acquired_ns",
                    "deadline_ns", "provenance_sha256", "model_assumed",
                    "kernel_ofd_proven", "real_descriptor_created",
                    "runtime_authorized", "storage_authorized",
                    "exec_proven"}
                and cap_receipt["schema"] == 1
                and cap_receipt["classification"]
                    == "MODEL_PIDFD_ANCHOR_ACQUIRED_ASSUMED"
                and type(cap_receipt["primary_fd"]) is int
                and cap_receipt["primary_fd"] == capability._primary_fd
                == self._resource_scalar[2]
                and type(cap_receipt["anchor_fd"]) is int
                and cap_receipt["anchor_fd"] == capability._anchor_fd
                and cap_receipt["anchor_fd"] >= 3
                and cap_receipt["anchor_fd"] != cap_receipt["primary_fd"]
                and type(cap_receipt["acquired_ns"]) is int
                and type(cap_receipt["deadline_ns"]) is int
                and cap_receipt["deadline_ns"] == self.receipt()["deadline_ns"]
                and type(cap_receipt["provenance_sha256"]) is str
                and cap_receipt["provenance_sha256"] == provenance
                and cap_receipt["model_assumed"] is True
                and cap_receipt["kernel_ofd_proven"] is False
                and cap_receipt["real_descriptor_created"] is False
                and cap_receipt["runtime_authorized"] is False
                and cap_receipt["storage_authorized"] is False
                and cap_receipt["exec_proven"] is False
                and anchor_outcome["raw_result"] == {
                    "primary_fd": cap_receipt["primary_fd"],
                    "anchor_fd": cap_receipt["anchor_fd"],
                    "acquired_ns": cap_receipt["acquired_ns"],
                    "model_assumed": True},
                "model pidfd anchor acquisition changed")
        return cap_receipt, provenance

    def release_pidfd_model_once(self, capability, backend):
        """Consume assumed anchor and model pidfd compare/close; no syscall."""
        if self._release_active is not None:
            object.__setattr__(self, "_release_active", "POISONED")
            if self._pidfd_active is True:
                object.__setattr__(self, "_pidfd_active", False)
            raise Refusal("resource release is already active")
        if self._pidfd_attempted is not False:
            raise Refusal("pidfd release is one-shot")
        cap_receipt, provenance = self._validate_model_anchor_capability(
            capability, False, True)
        backend_type = type(backend)
        methods = ("compare_pidfd", "close_pidfd")
        require(type(backend_type) is type
                and backend_type.__dict__.get(
                    "source_pidfd_release_mock") is True
                and all(type(backend_type.__dict__.get(name))
                        is types.FunctionType for name in methods),
                "closed source pidfd-release mock required")
        compare_method = backend_type.__dict__["compare_pidfd"]
        close_method = backend_type.__dict__["close_pidfd"]
        require(self._pidfd_active is False
                and self._pidfd_outcome_bytes is None,
                "fresh pidfd release required")
        object.__setattr__(self, "_pidfd_attempted", True)
        object.__setattr__(self, "_pidfd_active", True)
        object.__setattr__(self, "_release_active", "PIDFD")
        object.__setattr__(capability, "_consumed", True)
        compare_attempted = False
        comparison_safe = None
        close_attempted = False
        close_confirmed = False
        close_result_safe = None
        error_type = None
        classification = "MODEL_PIDFD_RELEASE_UNKNOWN"
        try:
            primary_fd = capability._primary_fd
            anchor_fd = capability._anchor_fd
            compare_attempted = True
            comparison = compare_method(
                backend, primary_fd, anchor_fd, provenance)
            GRAPH.CONSUMER._builtin_tree(
                comparison, "source pidfd comparison")
            require(type(comparison) is dict
                    and all(type(key) is str for key in comparison)
                    and set(comparison) == {"primary_fd", "anchor_fd",
                        "provenance_sha256", "same", "checked_ns",
                        "model_assumed"}
                    and type(comparison["primary_fd"]) is int
                    and comparison["primary_fd"] == primary_fd
                    and type(comparison["anchor_fd"]) is int
                    and comparison["anchor_fd"] == anchor_fd
                    and type(comparison["provenance_sha256"]) is str
                    and comparison["provenance_sha256"] == provenance
                    and type(comparison["same"]) is bool
                    and type(comparison["checked_ns"]) is int
                    and cap_receipt["acquired_ns"]
                        <= comparison["checked_ns"]
                    < cap_receipt["deadline_ns"]
                    and type(comparison["model_assumed"]) is bool
                    and comparison["model_assumed"] is True,
                    "strict source pidfd comparison")
            candidate = copy.deepcopy(comparison)
            comparison_bytes = SUP.canonical(candidate)
            comparison_safe = candidate
            same = candidate["same"]
            checked_ns = candidate["checked_ns"]
            self._validate_model_anchor_capability(
                capability, True, True)
            require(self._pidfd_active is True
                    and self._release_active == "PIDFD"
                    and type(backend) is backend_type
                    and backend_type.__dict__.get("compare_pidfd")
                        is compare_method
                    and backend_type.__dict__.get("close_pidfd")
                        is close_method,
                    "pidfd comparison changed authority")
            if same is False:
                classification = "MODEL_PIDFD_IDENTITY_QUARANTINED"
            else:
                close_attempted = True
                object.__setattr__(
                    capability, "_primary_model_owned", False)
                close_result = close_method(
                    backend, primary_fd, anchor_fd, provenance)
                GRAPH.CONSUMER._builtin_tree(
                    close_result, "source pidfd close result")
                GRAPH.CONSUMER._builtin_tree(
                    comparison, "post-close source pidfd comparison")
                self._validate_model_anchor_capability(
                    capability, True, False)
                require(self._pidfd_active is True
                        and self._release_active == "PIDFD"
                        and type(backend) is backend_type
                        and backend_type.__dict__.get("compare_pidfd")
                            is compare_method
                        and backend_type.__dict__.get("close_pidfd")
                            is close_method
                        and SUP.canonical(comparison) == comparison_bytes
                        and type(close_result) is dict
                        and all(type(key) is str for key in close_result)
                        and set(close_result) == {"primary_fd", "anchor_fd",
                            "provenance_sha256", "closed", "finished_ns"}
                        and type(close_result["primary_fd"]) is int
                        and close_result["primary_fd"] == primary_fd
                        and type(close_result["anchor_fd"]) is int
                        and close_result["anchor_fd"] == anchor_fd
                        and type(close_result["provenance_sha256"]) is str
                        and close_result["provenance_sha256"] == provenance
                        and type(close_result["closed"]) is bool
                        and close_result["closed"] is True
                        and type(close_result["finished_ns"]) is int
                        and close_result["finished_ns"]
                            >= checked_ns,
                        "strict source pidfd close result")
                candidate = copy.deepcopy(close_result)
                SUP.canonical(candidate)
                close_result_safe = candidate
                close_confirmed = True
                classification = ("MODEL_PIDFD_CLOSE_CONFIRMED"
                    if close_result["finished_ns"]
                        < cap_receipt["deadline_ns"]
                    else "MODEL_PIDFD_CLOSE_CONFIRMED_LATE")
        except BaseException as error:
            error_type = type(error).__name__
            raise
        finally:
            outcome = {"schema": 1, "classification": classification,
                "compare_attempted": compare_attempted,
                "comparison": comparison_safe,
                "pidfd_close_attempted": close_attempted,
                "pidfd_close_confirmed": close_confirmed,
                "close_result": close_result_safe,
                "error_type": error_type,
                "capability_consumed": capability._consumed is True,
                "primary_model_owned":
                    capability._primary_model_owned is True,
                "anchor_model_owned": True,
                "model_assumed": True, "kernel_ofd_proven": False,
                "real_pidfd_closed": False,
                "resources_closed": False,
                "descendants_qualified": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            try:
                GRAPH.CONSUMER._builtin_tree(
                    outcome, "source pidfd release outcome")
                object.__setattr__(self, "_pidfd_outcome_bytes",
                                   SUP.canonical(outcome))
            finally:
                object.__setattr__(self, "_pidfd_active", False)
                object.__setattr__(self, "_release_active", None)
        return self.pidfd_outcome()


class AbortForkAttempt:
    def __init__(self, key, persisted, attempt_id, now_ns):
        require(key is _ATTEMPT_KEY
                and type(persisted)
                is GRAPH.PersistedNoGrantIntentCapability
                and type(attempt_id) is str and HEX32.fullmatch(attempt_id),
                "strict abort fork attempt input")
        self.owner_thread = threading.current_thread()
        self.persisted = persisted
        self.enrollment = persisted.enrollment
        self.handle = persisted.handle
        self.ticket = persisted.ticket
        self.attempt_id = attempt_id
        self._protocol_policy = {
            "schema": 1, "exit_code": 73,
            "stdout_hex": "534c545f4e4f5f4752414e545f5354444f55545f5631",
            "stderr_hex": "534c545f4e4f5f4752414e545f5354444552525f5631"}
        self._protocol_policy_bytes = SUP.canonical(self._protocol_policy)
        self.state = "CLAIMING"
        self.fork_attempted = False
        self.child_may_exist = False
        self.lifecycle = None
        self._lifecycle_bytes = None
        self._lifecycle_origin = None
        self._lifecycle_token = None
        self._lifecycle_supervisor_bytes = None
        self.owned_pidfd = None
        self._owned_pidfd_fd = None
        self._owned_child_bytes = None
        self._owned_launcher_bytes = None
        self.expected_launcher = None
        self.capture_handle = None
        self.capture_adapter = None
        self.descendant_attachment = None
        self.descendant_deadline_binding_started = False
        self.descendant_deadline_binding = None
        self.descendant_drain_transfer_started = False
        self.descendant_drain_bridge = None
        self.descendant_step_started = False
        self.descendant_step_result = None
        self.descendant_terminal_evidence = None
        self.allocated_status_started = False
        self.allocated_status_result = None
        self.allocated_status_eof_evidence = None
        self.allocated_protocol_started = False
        self.allocated_protocol_evaluation = None
        self.allocated_protocol_capability = None
        self.allocated_release_inputs_started = False
        self.allocated_release_inputs_operation = None
        self.allocated_release_inputs_capability = None
        self.allocated_release_deadline_started = False
        self.allocated_release_deadline_operation = None
        self.allocated_release_deadline_capability = None
        self._capture_origin = None
        self._capture_origin_bytes = None
        self._capture_poller = None
        self._capture_stream_refs = None
        self.monitor_attempted = False
        self.monitor_result = None
        self._monitor_result_bytes = None
        self.monitor_capability = None
        self.reap_attempted = False
        self.cleanup_started_ns = None
        self.cleanup_deadline_ns = None
        self.settlement_handle = None
        self.settlement_receipt = None
        self._settlement_receipt_bytes = None
        self.leader_reap_confirmed = False
        self.status_tail_attempted = False
        self.status_eof_observed = False
        self.status_read_observation = None
        self._status_read_observation_bytes = None
        self.status_eof_receipt = None
        self._status_eof_receipt_bytes = None
        self.protocol_verify_attempted = False
        self.protocol_evidence = None
        self._protocol_evidence_bytes = None
        self.terminal_release_claim_attempted = False
        self.terminal_release_claim = None
        self.status_ready = b""
        self.returned_pid = None
        self.failure_phase = None
        self.poisoned = False
        self.fork_epoch = 0
        self.last_clock_ns = now_ns
        self._backend = None
        self._methods = None
        receipt = persisted.claim_for_abort_once(self, now_ns)
        GRAPH.CONSUMER._builtin_tree(receipt, "abort claim receipt")
        require(self.state == "CLAIMING" and self.poisoned is False
                and receipt["classification"]
                == "NO_GRANT_GRAPH_INTENT_CLAIMED"
                and receipt["grant_attempted"] is False
                and persisted.state == "CLAIMED"
                and self.ticket.used is False,
                "persisted abort authority claim failed")
        self.owner = copy.deepcopy(self.enrollment.owner)
        self.deadline_ns = self.enrollment.deadline_ns
        self.descriptor = copy.deepcopy(self.enrollment.descriptor)
        self._owner_bytes = SUP.canonical(self.owner)
        self._descriptor_bytes = SUP.canonical(self.descriptor)
        self.state = "READY_TO_FORK"
        self._sealed = True

    _PROTECTED = frozenset(("state", "fork_attempted", "child_may_exist",
                            "lifecycle", "returned_pid", "failure_phase",
                            "poisoned", "fork_epoch", "last_clock_ns",
                            "deadline_ns", "_lifecycle_bytes", "owner",
                            "_lifecycle_origin", "_lifecycle_token",
                            "_lifecycle_supervisor_bytes",
                            "owned_pidfd", "expected_launcher",
                            "_owned_pidfd_fd", "_owned_child_bytes",
                            "_owned_launcher_bytes",
                            "capture_handle", "capture_adapter",
                            "descendant_attachment",
                            "descendant_deadline_binding_started",
                            "descendant_deadline_binding",
                            "descendant_drain_transfer_started",
                            "descendant_drain_bridge",
                            "descendant_step_started",
                            "descendant_step_result",
                            "descendant_terminal_evidence",
                            "allocated_status_started",
                            "allocated_status_result",
                            "allocated_status_eof_evidence",
                            "allocated_protocol_started",
                            "allocated_protocol_evaluation",
                            "allocated_protocol_capability",
                            "allocated_release_inputs_started",
                            "allocated_release_inputs_operation",
                            "allocated_release_inputs_capability",
                            "allocated_release_deadline_started",
                            "allocated_release_deadline_operation",
                            "allocated_release_deadline_capability",
                            "_capture_origin", "_capture_origin_bytes",
                            "_capture_poller", "_capture_stream_refs",
                            "monitor_attempted", "monitor_result",
                            "_monitor_result_bytes", "monitor_capability",
                            "reap_attempted", "cleanup_started_ns",
                            "cleanup_deadline_ns", "settlement_handle",
                            "settlement_receipt", "_settlement_receipt_bytes",
                            "leader_reap_confirmed",
                            "status_tail_attempted", "status_eof_observed",
                            "status_read_observation",
                            "_status_read_observation_bytes",
                            "status_eof_receipt", "_status_eof_receipt_bytes",
                            "protocol_verify_attempted", "protocol_evidence",
                            "_protocol_evidence_bytes",
                            "terminal_release_claim_attempted",
                            "terminal_release_claim",
                            "status_ready",
                            "descriptor", "_owner_bytes", "_descriptor_bytes",
                            "persisted", "enrollment", "handle", "ticket",
                            "attempt_id",
                            "_protocol_policy", "_protocol_policy_bytes",
                            "_sealed", "_backend", "_methods", "owner_thread"))

    def __setattr__(self, name, value):
        if "_sealed" in self.__dict__ and name in type(self)._PROTECTED:
            raise Refusal("abort fork authority is immutable externally")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if "_sealed" in self.__dict__:
            raise Refusal("abort fork authority cannot be deleted")
        object.__delattr__(self, name)

    def _set(self, name, value):
        object.__setattr__(self, name, value)

    def __copy__(self):
        raise Refusal("abort fork attempt is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("abort fork attempt is noncopyable")

    def _poison(self, phase):
        self._set("poisoned", True)
        self._set("failure_phase", phase)
        self._set("state", ("UNKNOWN_SURVIVOR" if self.fork_attempted
                            else "UNKNOWN"))

    def _validate(self, expected, pre_fork):
        GRAPH.CONSUMER._builtin_tree(self.owner, "abort attempt owner")
        GRAPH.CONSUMER._builtin_tree(self.descriptor,
                                     "abort attempt descriptor")
        require(threading.current_thread() is self.owner_thread
                and self.state == expected
                and self.poisoned is False
                and type(self.persisted)
                is GRAPH.PersistedNoGrantIntentCapability
                and self.persisted.enrollment is self.enrollment
                and self.persisted.handle is self.handle
                and self.persisted.ticket is self.ticket
                and self.persisted.state == "CLAIMED"
                and SUP.canonical(self.owner) == self._owner_bytes
                and SUP.canonical(self.descriptor) == self._descriptor_bytes
                and _integer(self.deadline_ns, 1),
                "abort fork attempt authority changed")
        if self.lifecycle is not None:
            require(self.returned_pid is not None
                    and self.ticket.used is True
                    and type(self.lifecycle) is dict
                    and set(self.lifecycle) == {"schema", "state", "token",
                        "supervisor", "owned_child", "origin"}
                    and type(self.lifecycle["schema"]) is int
                    and self.lifecycle["schema"] == 1
                    and self.lifecycle["state"] == "SPAWNED_UNBOUND"
                    and self.lifecycle["token"] == self._lifecycle_token
                    and SUP.canonical(self.lifecycle["supervisor"])
                    == self._lifecycle_supervisor_bytes
                    and self.lifecycle["origin"] is self._lifecycle_origin
                    and type(self._lifecycle_origin) is OWNED._ForkOrigin
                    and type(self._lifecycle_origin.pid) is int
                    and self._lifecycle_origin.pid == self.returned_pid
                    and self._lifecycle_origin.claimed is False
                    and self._lifecycle_origin.pre_fork_enrollment
                    is self.ticket
                    and type(self.lifecycle["owned_child"]["pid"]) is int
                    and self.lifecycle["owned_child"] == {
                        "pid": self.returned_pid, "starttime": None}
                    and self.lifecycle["owned_child"]["pid"]
                    == self.returned_pid,
                    "recorded abort child lifecycle changed")
        if pre_fork:
            require(self.fork_attempted is False
                    and self.child_may_exist is False
                    and self.lifecycle is None
                    and self.returned_pid is None
                    and self.ticket.used is False
                    and self.handle.owned == FDS._ALL_ROLES,
                    "fresh pre-fork authority required")

    def _invoke(self, name, *args):
        require(self._backend is not None and self._methods is not None
                and type(self._backend).__dict__.get(name)
                is self._methods[name] and name not in self._backend.__dict__,
                "abort parent syscall surface changed")
        return self._methods[name](self._backend, *args)

    def _guard(self, expected):
        self._validate(expected, False)

    def _clock(self, expected):
        self._guard(expected)
        previous = self.last_clock_ns
        value = self._invoke("monotonic_ns")
        self._guard(expected)
        require(_integer(value) and value >= previous
                and value < self.deadline_ns,
                "abort parent deadline reached or clock regressed")
        self._set("last_clock_ns", value)
        return value

    def _phase(self, state):
        require(self.poisoned is False, "abort attempt is poisoned")
        self._set("state", state)

    def _final_prefork_admission_for(self, phase):
        self._guard(phase)
        self.enrollment._validate_frozen("BOUND")
        self.persisted._validate_frozen("CLAIMED")
        require(self.handle.state == "FD_GRAPH_READY"
                and self.handle.poisoned is False
                and self.handle.owned == FDS._ALL_ROLES
                and not self.handle.lost
                and not self.handle.attempted_closes
                and not self.handle.attempted_writes
                and self.handle._consumer_claim is self.enrollment
                and self.handle._consumer_claim_started is True
                and self.persisted.bound.state == "CLAIMED"
                and self.persisted.bound.consumer is self
                and self.persisted.state == "CLAIMED"
                and self.persisted.bridge.state == "SEALED_INTENT"
                and self.ticket.used is False,
                "final callback-free pre-fork admission failed")

    def _final_prefork_admission(self):
        self._final_prefork_admission_for("PREFLIGHTING")

    def _strict_linux_owned_graph(self, owned, capture_adapter=None):
        require(type(owned) is OWNED._OwnedPidfdHandle
                and owned.lifecycle is self.lifecycle
                and threading.current_thread() is owned.owner_thread
                and type(owned.state) is str and owned.state == "BOUND"
                and owned.observation is None
                and owned.capture_adapter is capture_adapter
                and type(owned.pidfd) is int and owned.pidfd >= 0
                and type(owned.child) is dict
                and type(owned.launcher) is dict
                and type(self.lifecycle) is dict
                and all(type(key) is str for key in self.lifecycle)
                and set(self.lifecycle) == {"schema", "state", "token",
                    "supervisor", "owned_child", "origin"},
                "strict Linux owned handle graph")
        GRAPH.CONSUMER._builtin_tree(owned.child,
                                     "Linux owned child identity")
        GRAPH.CONSUMER._builtin_tree(owned.launcher,
                                     "Linux owned launcher identity")
        GRAPH.CONSUMER._builtin_tree(self.lifecycle["supervisor"],
                                     "Linux owned supervisor")
        GRAPH.CONSUMER._builtin_tree(self.lifecycle["owned_child"],
                                     "Linux owned lifecycle child")
        origin = self.lifecycle["origin"]
        require(type(self.lifecycle["schema"]) is int
                and self.lifecycle["schema"] == 1
                and type(self.lifecycle["state"]) is str
                and self.lifecycle["state"] == "PIDFD_BOUND"
                and type(self.lifecycle["token"]) is str
                and self.lifecycle["token"] == self._lifecycle_token
                and type(self.lifecycle["owned_child"]) is dict
                and set(self.lifecycle["owned_child"])
                == {"pid", "starttime"}
                and type(self.lifecycle["owned_child"]["pid"]) is int
                and type(self.lifecycle["owned_child"]["starttime"]) is int
                and self.lifecycle["owned_child"]["pid"]
                == self.returned_pid
                and self.lifecycle["owned_child"]["starttime"] > 0
                and type(origin) is OWNED._ForkOrigin
                and origin is self._lifecycle_origin
                and type(origin.pid) is int and origin.pid == self.returned_pid
                and type(origin.claimed) is bool and origin.claimed is True
                and origin.pre_fork_enrollment is self.ticket,
                "strict Linux owned lifecycle graph")

    def _record_shared_descendant_fork(self, lifecycle, expected_state):
        shared = self.enrollment.domain_enrollment
        if shared is not None:
            GRAPH.record_allocated_descendant_fork_once(
                shared, self, lifecycle, expected_state)

    def _validate_shared_descendant_fork(self, expected_state, reaped=False):
        shared = self.enrollment.domain_enrollment
        if shared is not None:
            require(type(expected_state) is str and type(reaped) is bool
                    and type(shared.state) is str,
                    "strict shared descendant continuation state")
            if shared.state == "FORKED":
                GRAPH.validate_allocated_descendant_fork(
                    shared, self, expected_state, reaped)
            elif shared.state == "ATTACHED_HARD_LIMIT_ONLY":
                import prelive_descendant_bridge as DESC_BRIDGE
                DESC_BRIDGE.validate_allocated_attachment_phase(
                    self.descendant_attachment, self, expected_state, reaped)
            else:
                shared._poison()
                shared.graph._poison()
                self._poison("DESCENDANT_ATTACHMENT_PHASE")
                raise Refusal("invalid shared descendant continuation phase")

    def _validate_linux_pidfd_bound(self, owned,
                                    phase="LINUX_BINDING_PIDFD"):
        self._strict_linux_owned_graph(owned)
        require(self.state == phase
                and self.poisoned is False
                and self.owned_pidfd is owned
                and type(owned) is OWNED._OwnedPidfdHandle
                and owned.lifecycle is self.lifecycle
                and threading.current_thread() is owned.owner_thread
                and owned.state == "BOUND"
                and owned.observation is None
                and owned.capture_adapter is None
                and type(owned.pidfd) is int
                and owned.pidfd == self._owned_pidfd_fd
                and SUP.canonical(owned.child) == self._owned_child_bytes
                and owned.child == {"pid": self.returned_pid,
                    "starttime": self.lifecycle["owned_child"]["starttime"],
                    "boot_id": self.owner["boot_id"]}
                and SUP.canonical(owned.launcher)
                == self._owned_launcher_bytes
                and owned.launcher == self.expected_launcher,
                "Linux abort owned pidfd authority changed")
        enrollment = self.enrollment
        GRAPH.CONSUMER._builtin_tree(self.ticket.supervisor,
                                     "consumed Linux ticket supervisor")
        GRAPH.CONSUMER._builtin_tree(enrollment.owner,
                                     "consumed Linux enrollment owner")
        GRAPH.CONSUMER._builtin_tree(enrollment.descriptor,
                                     "consumed Linux descriptor")
        require(type(self.handle.state) is str
                and type(self.handle.poisoned) is bool
                and type(self.handle.owned) is set
                and type(self.handle.lost) is set
                and type(self.handle.attempted_closes) is set
                and type(self.handle.attempted_writes) is set
                and all(type(role) is str for roles in (
                    self.handle.owned, self.handle.lost,
                    self.handle.attempted_closes,
                    self.handle.attempted_writes) for role in roles),
                "strict Linux abort FD authority containers")
        require(threading.current_thread() is enrollment.owner_thread
                and enrollment.state == "BOUND"
                and enrollment._poisoned is False
                and enrollment.handle is self.handle
                and enrollment.handle._consumer_claim is enrollment
                and enrollment.handle._consumer_claim_started is True
                and enrollment.handle._grant_policy == FDS.CLOSE_ONLY_NO_WRITE
                and enrollment.ticket is self.ticket
                and type(self.ticket) is OWNED._PreForkDescendantEnrollment
                and self.ticket.binding is enrollment.binding
                and self.ticket.used is True
                and SUP.canonical(self.ticket.supervisor)
                == enrollment._owner_bytes
                and SUP.canonical(enrollment.owner) == enrollment._owner_bytes
                and SUP.canonical(enrollment.descriptor)
                == enrollment._descriptor_bytes
                and SUP.digest(enrollment.descriptor)
                == enrollment.graph_digest
                and enrollment.bound_capability is self.persisted.bound
                and enrollment.bound_capability.enrollment is enrollment,
                "consumed Linux abort enrollment changed")
        self._validate_shared_descendant_fork(phase)
        self.persisted._validate_frozen("CLAIMED")
        lifecycle = self.lifecycle
        require(type(lifecycle) is dict
                and lifecycle["state"] == "PIDFD_BOUND"
                and lifecycle["token"] == self._lifecycle_token
                and SUP.canonical(lifecycle["supervisor"])
                == self._lifecycle_supervisor_bytes
                and lifecycle["origin"] is self._lifecycle_origin
                and lifecycle["origin"].claimed is True
                and lifecycle["origin"].pre_fork_enrollment is self.ticket
                and lifecycle["owned_child"] == {
                    "pid": owned.child["pid"],
                    "starttime": owned.child["starttime"]}
                and self.ticket.used is True
                and self.handle.state == "FD_GRAPH_READY"
                and self.handle.poisoned is False
                and self.handle.owned == FDS._ALL_ROLES
                and not self.handle.lost
                and not self.handle.attempted_closes
                and not self.handle.attempted_writes,
                "Linux abort post-bind chain changed")

    def _validate_linux_capture_chain(self, phase, attached=False,
                                      grant_closed=False,
                                      validate_shared=True):
        import prelive_capture_settlement as CAPTURE
        owned = self.owned_pidfd
        self._strict_linux_owned_graph(
            owned, self.capture_adapter if attached else None)
        require(self.state == phase and self.poisoned is False
                and owned.pidfd == self._owned_pidfd_fd
                and SUP.canonical(owned.child) == self._owned_child_bytes
                and SUP.canonical(owned.launcher)
                == self._owned_launcher_bytes,
                "Linux capture owned child changed")
        self.persisted._validate_frozen("CLAIMED")
        enrollment = self.enrollment
        GRAPH.CONSUMER._builtin_tree(enrollment.owner,
                                     "Linux capture enrollment owner")
        GRAPH.CONSUMER._builtin_tree(enrollment.descriptor,
                                     "Linux capture descriptor")
        require(type(grant_closed) is bool and type(validate_shared) is bool,
                "strict Linux grant-close validation mode")
        expected_owned = ({"status_parent", "stdout_parent", "stderr_parent"}
                          if grant_closed else
                          {"grant_parent", "status_parent",
                           "stdout_parent", "stderr_parent"})
        expected_closes = (set(PARENT_CLOSE_ROLES) | {"grant_parent"}
                           if grant_closed else set(PARENT_CLOSE_ROLES))
        require(all(type(roles) is set for roles in (
                    self.handle.owned, self.handle.lost,
                    self.handle.attempted_closes,
                    self.handle.attempted_writes))
                and all(type(role) is str for roles in (
                    self.handle.owned, self.handle.lost,
                    self.handle.attempted_closes,
                    self.handle.attempted_writes) for role in roles),
                "strict Linux capture FD role sets")
        self.handle._validate_stored_graph()
        require(enrollment.state == "BOUND"
                and enrollment._poisoned is False
                and enrollment.handle is self.handle
                and enrollment.ticket is self.ticket
                and self.ticket.used is True
                and self.handle._consumer_claim is enrollment
                and type(self.handle.state) is str
                and self.handle.state == "FD_GRAPH_READY"
                and type(self.handle.poisoned) is bool
                and self.handle.poisoned is False
                and type(self.handle.owned) is set
                and self.handle.owned == expected_owned
                and type(self.handle.lost) is set
                and self.handle.lost == set()
                and type(self.handle.attempted_closes) is set
                and self.handle.attempted_closes == expected_closes
                and type(self.handle.attempted_writes) is set
                and self.handle.attempted_writes == set()
                and all(type(role) is str for roles in (
                    self.handle.owned, self.handle.lost,
                    self.handle.attempted_closes,
                    self.handle.attempted_writes) for role in roles),
                "Linux capture partial-close chain changed")
        if validate_shared:
            self._validate_shared_descendant_fork(phase)
        if attached:
            capture = self.capture_handle
            adapter = self.capture_adapter
            require(type(capture) is CAPTURE.CaptureHandle
                    and type(capture.origin) is CAPTURE._PipeOrigin
                    and capture.origin is self._capture_origin
                    and type(capture.origin.owner) is dict
                    and type(capture.origin.child) is dict
                    and type(capture.origin.identities) is dict
                    and type(capture.streams) is dict
                    and all(type(role) is str for role in capture.streams)
                    and type(capture.fd_roles) is dict
                    and all(type(fd) is int and type(role) is str
                            for fd, role in capture.fd_roles.items())
                    and type(self._capture_stream_refs) is dict
                    and all(type(role) is str
                            for role in self._capture_stream_refs),
                    "strict Linux capture containers")
            GRAPH.CONSUMER._builtin_tree(capture.origin.owner,
                                         "capture origin owner")
            GRAPH.CONSUMER._builtin_tree(capture.origin.child,
                                         "capture origin child")
            GRAPH.CONSUMER._builtin_tree(capture.origin.identities,
                                         "capture origin identities")
            for role in ("stdout", "stderr"):
                require(type(capture.streams.get(role)) is CAPTURE._Stream
                        and type(capture.streams[role].role) is str
                        and type(capture.streams[role].identity) is dict
                        and type(capture.streams[role].limit) is int
                        and type(capture.streams[role].stored) is bytearray
                        and capture.streams[role].stored == bytearray()
                        and type(capture.streams[role].observed_bytes) is int
                        and capture.streams[role].observed_bytes == 0
                        and type(capture.streams[role].eof) is bool
                        and capture.streams[role].eof is False
                        and type(capture.streams[role].truncated) is bool
                        and capture.streams[role].truncated is False,
                        "strict fresh Linux capture stream")
                GRAPH.CONSUMER._builtin_tree(
                    capture.streams[role].identity,
                    "capture stream identity")
            request = GRAPH._strict_bound_request(self.persisted.bound)
            require(type(capture.state) is str and capture.state == "BOUND"
                    and capture.owner_thread is self.owner_thread
                    and capture.origin is self._capture_origin
                    and SUP.canonical({"owner": capture.origin.owner,
                        "child": capture.origin.child,
                        "identities": capture.origin.identities})
                    == self._capture_origin_bytes
                    and capture.origin.claimed is True
                    and capture.poller is self._capture_poller
                    and getattr(capture.poller, "closed", False) is False
                    and type(capture.streams) is dict
                    and set(capture.streams) == {"stdout", "stderr"}
                    and all(capture.streams[role]
                            is self._capture_stream_refs[role]
                            for role in ("stdout", "stderr"))
                    and all(capture.streams[role].role == role
                            and capture.streams[role].limit
                            == request["capture_limit"]
                            and capture.streams[role].identity
                            == capture.origin.identities[role]
                            for role in ("stdout", "stderr"))
                    and capture.fd_roles == {
                        capture.streams[role].identity["fd"]: role
                        for role in ("stdout", "stderr")}
                    and capture.monitor_active is False
                    and capture.primary_failure is None
                    and capture.settlement_used is False
                    and capture.execution_deadline_ns is None
                    and capture.normal_completion is None
                    and capture.normal_completion_capability is None
                    and capture.child_adapter is adapter
                    and capture.child_adapter_type
                    is OWNED._CaptureChildAdapter
                    and type(adapter) is OWNED._CaptureChildAdapter
                    and adapter.handle is owned
                    and adapter.capture_consumer is capture
                    and type(adapter.state) is str
                    and adapter.state == "PRIMARY"
                    and adapter.cached is None
                    and adapter.owner_thread is self.owner_thread
                    and owned.capture_adapter is adapter,
                    "Linux attached capture authority changed")
            adapter.assert_current_no_callback(
                CAPTURE._CHILD_ADAPTER_KEY, capture)

    def close_linux_grant_zero_write_once(self):
        """Close the original parent grant writer; do not claim child EOF."""
        try:
            require(self.state == "LINUX_CAPTURE_BOUND_UNARMED"
                    and self.poisoned is False,
                    "bound unarmed Linux capture required for grant EOF")
            self._validate_linux_capture_chain(
                "LINUX_CAPTURE_BOUND_UNARMED", attached=True)
            before = time.monotonic_ns()
            require(type(before) is int and before >= self.last_clock_ns
                    and before < self.deadline_ns,
                    "Linux grant close admission deadline reached")
            self._set("last_clock_ns", before)
            self._validate_linux_capture_chain(
                "LINUX_CAPTURE_BOUND_UNARMED", attached=True)
            self._phase("LINUX_CLOSING_GRANT_ZERO_WRITE")
            receipt = self.handle.close_role_once("grant_parent")
            GRAPH.CONSUMER._builtin_tree(receipt,
                                         "Linux grant close receipt")
            require(type(receipt) is dict
                    and set(receipt) == {"classification", "role",
                        "remaining", "runtime_authorized",
                        "storage_authorized", "exec_proven"}
                    and receipt["classification"] == "FD_CLOSE_CONFIRMED"
                    and receipt["role"] == "grant_parent"
                    and receipt["remaining"] == [
                        "status_parent", "stderr_parent", "stdout_parent"]
                    and receipt["runtime_authorized"] is False
                    and receipt["storage_authorized"] is False
                    and receipt["exec_proven"] is False,
                    "exact Linux zero-write grant close required")
            now = time.monotonic_ns()
            require(type(now) is int and now >= self.last_clock_ns
                    and now < self.deadline_ns,
                    "Linux grant EOF completion exceeded deadline")
            self._set("last_clock_ns", now)
            self._validate_linux_capture_chain(
                "LINUX_CLOSING_GRANT_ZERO_WRITE", attached=True,
                grant_closed=True)
            self._phase("LINUX_GRANT_WRITER_CLOSED_UNSETTLED")
            return {"schema": 1,
                    "classification":
                    "LINUX_ABORT_GRANT_WRITER_CLOSED_UNSETTLED",
                    "pid": self.returned_pid, "pidfd": self._owned_pidfd_fd,
                    "grant_write_calls": 0, "grant_bytes": 0,
                    "grant_writer_open": False,
                    "child_eof_observed": False, "leader_reaped": False,
                    "descendants_qualified": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            self._poison("LINUX_GRANT_CLOSE")
            raise

    def monitor_linux_abort_unreaped_once(self):
        """Run the attached monitor once; retain all terminal evidence unreaped."""
        import prelive_capture_settlement as CAPTURE
        try:
            require(self.state == "LINUX_GRANT_WRITER_CLOSED_UNSETTLED"
                    and self.poisoned is False
                    and self.monitor_attempted is False
                    and self.monitor_result is None
                    and self.monitor_capability is None,
                    "fresh writer-closed Linux monitor required")
            self._validate_linux_capture_chain(
                "LINUX_GRANT_WRITER_CLOSED_UNSETTLED", attached=True,
                grant_closed=True)
            now = time.monotonic_ns()
            require(type(now) is int and now >= self.last_clock_ns
                    and now < self.deadline_ns,
                    "Linux monitor admission deadline reached")
            self._set("last_clock_ns", now)
            self._validate_linux_capture_chain(
                "LINUX_GRANT_WRITER_CLOSED_UNSETTLED", attached=True,
                grant_closed=True)
            self._set("monitor_attempted", True)
            self._phase("LINUX_MONITORING_UNREAPED")
            result = CAPTURE.capture_attached_until_deadline(
                self.capture_handle, self.deadline_ns,
                CAPTURE.LinuxCaptureSyscalls(), self.last_clock_ns)
            GRAPH.CONSUMER._builtin_tree(result,
                                         "Linux attached monitor result")
            require(type(result) is dict
                    and type(result.get("classification")) is str,
                    "strict Linux attached monitor result")
            frozen = copy.deepcopy(result)
            frozen_bytes = SUP.canonical(frozen)
            self._set("monitor_result", frozen)
            self._set("_monitor_result_bytes", frozen_bytes)
            owned = self.owned_pidfd
            GRAPH.CONSUMER._builtin_tree(owned.child,
                                         "post-monitor owned child")
            GRAPH.CONSUMER._builtin_tree(owned.launcher,
                                         "post-monitor owned launcher")
            lifecycle = owned.lifecycle
            require(type(lifecycle) is dict
                    and all(type(key) is str for key in lifecycle)
                    and set(lifecycle) == {"schema", "state", "token",
                        "supervisor", "owned_child", "origin"}
                    and lifecycle is self.lifecycle
                    and type(lifecycle["origin"]) is OWNED._ForkOrigin
                    and lifecycle["origin"] is self._lifecycle_origin
                    and type(lifecycle["origin"].pid) is int
                    and lifecycle["origin"].pid == self.returned_pid
                    and type(lifecycle["origin"].claimed) is bool
                    and lifecycle["origin"].claimed is True
                    and lifecycle["origin"].pre_fork_enrollment is self.ticket,
                    "post-monitor lifecycle origin changed")
            require(self.state == "LINUX_MONITORING_UNREAPED"
                    and self.poisoned is False
                    and self.monitor_attempted is True
                    and self.capture_handle is not None
                    and self.capture_adapter is not None
                    and self.capture_handle is self.capture_adapter.capture_consumer
                    and owned.capture_adapter is self.capture_adapter
                    and type(owned.pidfd) is int
                    and owned.pidfd == self._owned_pidfd_fd
                    and type(owned.state) is str
                    and SUP.canonical(owned.child) == self._owned_child_bytes
                    and SUP.canonical(owned.launcher)
                    == self._owned_launcher_bytes
                    and type(self.capture_handle.origin) is CAPTURE._PipeOrigin
                    and self.capture_handle.origin is self._capture_origin
                    and self.capture_handle.poller is self._capture_poller
                    and type(self.capture_handle.streams) is dict
                    and all(type(role) is str
                            for role in self.capture_handle.streams)
                    and set(self.capture_handle.streams)
                    == {"stdout", "stderr"}
                    and all(self.capture_handle.streams.get(role)
                            is self._capture_stream_refs[role]
                            for role in ("stdout", "stderr")),
                    "Linux post-monitor authority changed")
            GRAPH.CONSUMER._builtin_tree({
                "owner": self.capture_handle.origin.owner,
                "child": self.capture_handle.origin.child,
                "identities": self.capture_handle.origin.identities},
                "Linux post-monitor capture origin")
            require(SUP.canonical({
                        "owner": self.capture_handle.origin.owner,
                        "child": self.capture_handle.origin.child,
                        "identities": self.capture_handle.origin.identities})
                    == self._capture_origin_bytes,
                    "Linux post-monitor origin changed")
            self.persisted._validate_frozen("CLAIMED")
            enrollment = self.enrollment
            require(type(enrollment.state) is str
                    and enrollment.state == "BOUND"
                    and type(enrollment._poisoned) is bool
                    and enrollment._poisoned is False
                    and enrollment.handle is self.handle
                    and enrollment.ticket is self.ticket
                    and self.ticket.used is True
                    and self.handle._consumer_claim is enrollment
                    and self.handle._consumer_claim_started is True
                    and enrollment.bound_capability is self.persisted.bound
                    and enrollment.bound_capability.enrollment is enrollment,
                    "post-monitor enrollment chain changed")
            require(all(type(roles) is set for roles in (
                        self.handle.owned, self.handle.lost,
                        self.handle.attempted_closes,
                        self.handle.attempted_writes))
                    and all(type(role) is str for roles in (
                        self.handle.owned, self.handle.lost,
                        self.handle.attempted_closes,
                        self.handle.attempted_writes) for role in roles),
                    "strict post-monitor FD role sets")
            self.handle._validate_stored_graph()
            require(type(self.handle.state) is str
                    and self.handle.state == "FD_GRAPH_READY"
                    and type(self.handle.poisoned) is bool
                    and self.handle.poisoned is False
                    and self.handle.active is None
                    and self.handle.owned
                    == {"status_parent", "stdout_parent", "stderr_parent"}
                    and self.handle.lost == set()
                    and self.handle.attempted_closes
                    == set(PARENT_CLOSE_ROLES) | {"grant_parent"}
                    and self.handle.attempted_writes == set(),
                    "Linux post-monitor FD graph changed")
            self._validate_shared_descendant_fork(
                "LINUX_MONITORING_UNREAPED")
            classification = result["classification"]
            if classification == \
                    "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED":
                cap = CAPTURE.validate_attached_completion_unreaped(
                    self.capture_handle)
                require(cap is self.capture_handle.normal_completion_capability
                        and cap.execution_deadline_ns == self.deadline_ns
                        and cap.completed_at_ns >= self.last_clock_ns
                        and SUP.canonical(cap.completion) == frozen_bytes,
                        "exact Linux unreaped monitor capability required")
                self._set("monitor_capability", cap)
                self._phase("LINUX_MONITOR_COMPLETE_UNREAPED")
                terminal = frozen["leader_observation"]
                return {"schema": 1,
                        "classification":
                        "LINUX_ABORT_MONITOR_COMPLETE_UNREAPED",
                        "terminal_classification":
                        terminal["classification"],
                        "terminal_status": terminal["status"],
                        "abort_protocol_verified": False,
                        "status_eof_verified": False,
                        "leader_reaped": False,
                        "descendants_qualified": False,
                        "runtime_authorized": False,
                        "storage_authorized": False,
                        "exec_proven": False}
            failure = self.capture_handle.primary_failure
            GRAPH.CONSUMER._builtin_tree(
                failure, "Linux attached monitor primary failure")
            expected_state = {
                "EXECUTION_TIMEOUT_UNSETTLED":
                    "EXECUTION_TIMEOUT_UNSETTLED",
                "CAPTURE_TRUNCATED_UNSETTLED":
                    "CAPTURE_TRUNCATED_UNSETTLED"}.get(classification)
            require(expected_state is not None
                    and type(self.capture_handle.state) is str
                    and type(self.capture_handle.execution_deadline_ns) is int
                    and type(failure) is dict
                    and set(failure) == {"classification",
                        "leader_observation", "streams",
                        "runtime_authorized", "storage_authorized",
                        "postcondition_verified", "leader_reaped",
                        "descendants_qualified"}
                    and failure["classification"] == classification
                    and type(failure["streams"]) is dict
                    and set(failure["streams"]) == {"stdout", "stderr"}
                    and all(type(role) is str for role in failure["streams"])
                    and self.capture_handle.state == expected_state
                    and self.capture_handle.monitor_active is False
                    and self.capture_handle.execution_deadline_ns
                    == self.deadline_ns
                    and classification in (
                        "EXECUTION_TIMEOUT_UNSETTLED",
                        "CAPTURE_TRUNCATED_UNSETTLED")
                    and SUP.canonical(failure) == frozen_bytes
                    and self.capture_handle.normal_completion_capability is None,
                    "strict Linux monitor failure evidence")
            self._phase("LINUX_MONITOR_FAILURE_UNSETTLED")
            return {"schema": 1,
                    "classification": "LINUX_ABORT_MONITOR_FAILURE_UNSETTLED",
                    "monitor_classification": classification,
                    "abort_protocol_verified": False,
                    "status_eof_verified": False,
                    "leader_reaped": False,
                    "descendants_qualified": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            self._poison("LINUX_MONITOR")
            raise

    def reap_linux_completion_once(self):
        """Consume the exact monitor capability once; retain settlement only."""
        import prelive_capture_settlement as CAPTURE
        try:
            require(self.state == "LINUX_MONITOR_COMPLETE_UNREAPED"
                    and self.poisoned is False
                    and self.reap_attempted is False
                    and self.settlement_handle is None
                    and self.monitor_capability is not None,
                    "fresh exact Linux completion reap required")
            cap = CAPTURE.validate_attached_completion_unreaped(
                self.capture_handle)
            require(cap is self.monitor_capability
                    and SUP.canonical(cap.completion)
                    == self._monitor_result_bytes,
                    "original Linux monitor capability changed")
            self.persisted._validate_frozen("CLAIMED")
            def original_graph(phase, reaped=False):
                self.persisted._validate_frozen("CLAIMED")
                enrollment = self.enrollment
                lifecycle = self.owned_pidfd.lifecycle
                require(type(self.state) is str
                        and type(enrollment.state) is str
                        and type(enrollment._poisoned) is bool
                        and type(lifecycle) is dict
                        and all(type(key) is str for key in lifecycle)
                        and set(lifecycle) == {"schema", "state", "token",
                            "supervisor", "owned_child", "origin"}
                        and type(lifecycle["state"]) is str
                        and type(lifecycle["origin"]) is OWNED._ForkOrigin
                        and type(self.returned_pid) is int
                        and type(self._lifecycle_origin.pid) is int
                        and type(self._lifecycle_origin.claimed) is bool
                        and type(self.capture_handle.streams) is dict
                        and all(type(role) is str
                                for role in self.capture_handle.streams)
                        and set(self.capture_handle.streams)
                        == {"stdout", "stderr"},
                        "strict Linux reap graph containers")
                GRAPH.CONSUMER._builtin_tree(
                    cap.completion, "Linux reap monitor completion")
                require(self.state == phase and self.poisoned is False
                        and enrollment.state == "BOUND"
                        and enrollment._poisoned is False
                        and enrollment.handle is self.handle
                        and enrollment.ticket is self.ticket
                        and self.ticket.used is True
                        and self.handle._consumer_claim is enrollment
                        and self.handle._consumer_claim_started is True
                        and enrollment.bound_capability is self.persisted.bound
                        and lifecycle is self.lifecycle
                        and lifecycle["origin"] is self._lifecycle_origin
                        and self._lifecycle_origin.pid == self.returned_pid
                        and self._lifecycle_origin.claimed is True
                        and self._lifecycle_origin.pre_fork_enrollment
                        is self.ticket
                        and lifecycle["state"] == (
                            "OWNED_LEADER_REAPED_ONLY" if reaped
                            else "PIDFD_BOUND")
                        and self.capture_handle.origin is self._capture_origin
                        and self.capture_handle.poller is self._capture_poller
                        and all(self.capture_handle.streams.get(role)
                                is self._capture_stream_refs[role]
                                for role in ("stdout", "stderr"))
                        and SUP.canonical(cap.completion)
                        == self._monitor_result_bytes,
                        "Linux reap original graph changed")
                require(all(type(roles) is set for roles in (
                            self.handle.owned, self.handle.lost,
                            self.handle.attempted_closes,
                            self.handle.attempted_writes))
                        and all(type(role) is str for roles in (
                            self.handle.owned, self.handle.lost,
                            self.handle.attempted_closes,
                            self.handle.attempted_writes) for role in roles),
                        "strict Linux reap FD role sets")
                self.handle._validate_stored_graph()
                require(type(self.handle.state) is str
                        and self.handle.state == "FD_GRAPH_READY"
                        and type(self.handle.poisoned) is bool
                        and self.handle.poisoned is False
                        and self.handle.active is None
                        and self.handle.owned == {
                            "status_parent", "stdout_parent", "stderr_parent"}
                        and self.handle.lost == set()
                        and self.handle.attempted_closes
                        == set(PARENT_CLOSE_ROLES) | {"grant_parent"}
                        and self.handle.attempted_writes == set(),
                        "Linux reap FD authority changed")
                self._validate_shared_descendant_fork(phase, reaped)
            original_graph("LINUX_MONITOR_COMPLETE_UNREAPED")
            require(type(self.handle.state) is str
                    and self.handle.state == "FD_GRAPH_READY"
                    and type(self.handle.poisoned) is bool
                    and self.handle.poisoned is False
                    and self.handle.active is None
                    and self.handle.attempted_writes == set(),
                    "Linux pre-reap FD authority changed")
            request = GRAPH._strict_bound_request(self.persisted.bound)
            cleanup_ms = request["cleanup_ms"]
            require(type(cleanup_ms) is int and 1 <= cleanup_ms <= 10000,
                    "strict cleanup budget")
            self._set("reap_attempted", True)
            self._phase("LINUX_REAP_ADMISSION")
            started = time.monotonic_ns()
            require(type(started) is int
                    and started >= self.last_clock_ns
                    and started >= cap.completed_at_ns,
                    "Linux cleanup admission clock regressed")
            budget_ns = cleanup_ms * 1000000
            hard_limit = self.deadline_ns + budget_ns
            cleanup_deadline = min(started + budget_ns, hard_limit)
            require(started < cleanup_deadline,
                    "Linux cleanup budget expired")
            self._set("last_clock_ns", started)
            self._set("cleanup_started_ns", started)
            self._set("cleanup_deadline_ns", cleanup_deadline)
            cap_again = CAPTURE.validate_attached_completion_unreaped(
                self.capture_handle)
            require(cap_again is cap and self.state == "LINUX_REAP_ADMISSION"
                    and self.poisoned is False,
                    "Linux cleanup authority changed after clock")
            original_graph("LINUX_REAP_ADMISSION")
            self._phase("LINUX_REAPING_COMPLETION")
            settlement = CAPTURE.reap_attached_completion_once(
                self.capture_handle, cleanup_deadline,
                CAPTURE.LinuxCaptureSyscalls(), started)
            self._set("settlement_handle", settlement)
            require(type(settlement) is CAPTURE.SettlementHandle,
                    "exact Linux settlement handle required")
            receipt = settlement.receipt
            GRAPH.CONSUMER._builtin_tree(receipt,
                                         "Linux settlement receipt")
            require(type(receipt) is dict,
                    "strict Linux settlement receipt")
            frozen = copy.deepcopy(receipt)
            frozen_bytes = SUP.canonical(frozen)
            self._set("settlement_receipt", frozen)
            self._set("_settlement_receipt_bytes", frozen_bytes)
            require(type(settlement.__dict__) is dict
                    and all(type(key) is str for key in settlement.__dict__)
                    and set(settlement.__dict__) == {"receipt", "child_adapter",
                        "deadline_ns", "leader_reaped", "owner", "probe_used",
                        "owner_thread", "capture_handle",
                        "normal_completion_capability",
                        "descendant_capability"}
                    and settlement.capture_handle is self.capture_handle
                    and settlement.child_adapter is self.capture_adapter
                    and settlement.normal_completion_capability is cap
                    and settlement.owner_thread is self.owner_thread
                    and type(settlement.deadline_ns) is int
                    and settlement.deadline_ns == cleanup_deadline
                    and type(settlement.leader_reaped) is bool
                    and type(settlement.probe_used) is bool
                    and settlement.probe_used is False
                    and type(settlement.owner) is dict,
                    "Linux settlement authority changed")
            settlement_owner = CAPTURE._owner(settlement.owner)
            receipt_keys = {"schema", "classification", "owner", "child",
                "primary_failure", "primary_completion", "settlement_kind",
                "cleanup_deadline_ns", "started_ns", "finished_ns",
                "cleanup_outcome", "leader_observation", "leader_reap",
                "streams", "remaining_resources", "error",
                "runtime_authorized", "storage_authorized",
                "postcondition_verified", "descendants_qualified"}
            require(set(receipt) == receipt_keys
                    and type(receipt["schema"]) is int
                    and receipt["schema"] == 1
                    and type(receipt["classification"]) is str
                    and receipt["classification"]
                    == "PASSIVE_SETTLEMENT_OBSERVATION_ONLY"
                    and type(receipt["settlement_kind"]) is str
                    and type(receipt["cleanup_deadline_ns"]) is int
                    and type(receipt["started_ns"]) is int
                    and type(receipt["finished_ns"]) is int
                    and type(receipt["cleanup_outcome"]) is str
                    and type(receipt["streams"]) is dict
                    and type(receipt["remaining_resources"]) is dict
                    and all(type(receipt[key]) is bool for key in (
                        "runtime_authorized", "storage_authorized",
                        "postcondition_verified", "descendants_qualified"))
                    and receipt["runtime_authorized"] is False
                    and receipt["storage_authorized"] is False
                    and receipt["postcondition_verified"] is False
                    and receipt["descendants_qualified"] is False
                    and settlement_owner == self.owner
                    and receipt["owner"] == self.owner
                    and receipt["child"] == self.capture_handle.origin.child
                    and receipt["settlement_kind"] == "NORMAL_COMPLETION"
                    and receipt["primary_failure"] is None
                    and receipt["primary_completion"] == cap.completion
                    and SUP.canonical(receipt["primary_completion"])
                    == self._monitor_result_bytes
                    and receipt["leader_observation"]
                    == cap.completion["leader_observation"]
                    and receipt["cleanup_deadline_ns"] == cleanup_deadline
                    and receipt["started_ns"] == started
                    and receipt["started_ns"] <= receipt["finished_ns"]
                    and receipt["streams"] == cap.completion["streams"]
                    and set(receipt["remaining_resources"]) == {
                        "leader_may_survive", "pidfd_owned", "epoll_owned",
                        "stdout_read_fd_owned", "stderr_read_fd_owned"}
                    and type(receipt["remaining_resources"]
                             ["leader_may_survive"]) is bool
                    and (type(receipt["remaining_resources"]["pidfd_owned"])
                         is bool
                         or receipt["remaining_resources"]["pidfd_owned"]
                         == "UNKNOWN")
                    and all(type(receipt["remaining_resources"][key]) is bool
                            for key in ("epoll_owned",
                                "stdout_read_fd_owned",
                                "stderr_read_fd_owned")),
                    "strict Linux settlement receipt changed")
            confirmed = False
            if settlement.leader_reaped is True:
                require(type(receipt["leader_reap"]) is dict,
                        "missing confirmed Linux reap receipt")
                CAPTURE._validate_reap(
                    receipt["leader_reap"], self.capture_handle.origin.child,
                    cap.completion["leader_observation"])
                confirmed = True
                require(receipt["remaining_resources"] == {
                            "leader_may_survive": False,
                            "pidfd_owned": True,
                            "epoll_owned": True,
                            "stdout_read_fd_owned": True,
                            "stderr_read_fd_owned": True},
                        "confirmed Linux reap resources changed")
            self._set("leader_reap_confirmed", confirmed)
            if (confirmed
                    and receipt.get("cleanup_outcome")
                    == "LEADER_REAPED_STREAMS_EOF"
                    and receipt.get("error") is None):
                try:
                    validated = CAPTURE.validate_transferred_normal_completion(
                        settlement, self.capture_handle, self.capture_adapter)
                    original_graph("LINUX_REAPING_COMPLETION", reaped=True)
                    require(validated is cap
                            and settlement.deadline_ns == cleanup_deadline
                            and receipt["cleanup_deadline_ns"] == cleanup_deadline
                            and receipt["started_ns"] == started
                            and type(receipt["finished_ns"]) is int
                            and receipt["finished_ns"] < cleanup_deadline,
                            "exact Linux reap settlement changed")
                except BaseException:
                    self._poison("LINUX_POST_REAP_AUTHORITY")
                    return {"schema": 1,
                            "classification":
                            "LINUX_REAP_UNKNOWN_RETAINED",
                            "leader_reap_confirmed": True,
                            "abort_protocol_verified": False,
                            "status_eof_verified": False,
                            "descendants_qualified": False,
                            "runtime_authorized": False,
                            "storage_authorized": False,
                            "exec_proven": False}
                self._phase("LINUX_LEADER_REAPED_UNQUALIFIED")
                return {"schema": 1,
                        "classification": "LINUX_LEADER_REAPED_UNQUALIFIED",
                        "leader_reap_confirmed": True,
                        "abort_protocol_verified": False,
                        "status_eof_verified": False,
                        "descendants_qualified": False,
                        "runtime_authorized": False,
                        "storage_authorized": False,
                        "exec_proven": False}
            self._phase("LINUX_REAP_UNKNOWN_RETAINED")
            return {"schema": 1,
                    "classification": "LINUX_REAP_UNKNOWN_RETAINED",
                    "leader_reap_confirmed": confirmed,
                    "abort_protocol_verified": False,
                    "status_eof_verified": False,
                    "descendants_qualified": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            self._poison("LINUX_REAP")
            raise

    def _validate_linux_reaped_chain(self, phase, terminal_claimed=False):
        """Pure proof of the original reaped leader and still-owned pipes."""
        import prelive_capture_settlement as CAPTURE
        require(type(terminal_claimed) is bool
                and threading.current_thread() is self.owner_thread
                and type(self.state) is str and self.state == phase
                and self.poisoned is False
                and self.reap_attempted is True
                and self.leader_reap_confirmed is True
                and type(self.cleanup_started_ns) is int
                and type(self.cleanup_deadline_ns) is int
                and self.cleanup_started_ns < self.cleanup_deadline_ns
                and self.status_ready == STATUS_READY,
                "exact reaped Linux abort phase required")
        cap = self.monitor_capability
        settlement = self.settlement_handle
        receipt = self.settlement_receipt
        owned = self.owned_pidfd
        adapter = self.capture_adapter
        capture = self.capture_handle
        require(type(cap) is CAPTURE._NormalCompletionCapability
                and type(settlement) is CAPTURE.SettlementHandle
                and type(settlement.__dict__) is dict
                and all(type(key) is str for key in settlement.__dict__)
                and set(settlement.__dict__) == {"receipt", "child_adapter",
                    "deadline_ns", "leader_reaped", "owner", "probe_used",
                    "owner_thread", "capture_handle",
                    "normal_completion_capability",
                    "descendant_capability"}
                and type(settlement.receipt) is dict
                and all(type(key) is str for key in settlement.receipt)
                and type(settlement.deadline_ns) is int
                and type(settlement.leader_reaped) is bool
                and type(settlement.probe_used) is bool
                and type(settlement.owner) is dict
                and type(receipt) is dict
                and all(type(key) is str for key in receipt),
                "strict reaped settlement containers")
        GRAPH.CONSUMER._builtin_tree(cap.completion,
                                     "reaped monitor completion")
        GRAPH.CONSUMER._builtin_tree(settlement.receipt,
                                     "live reaped settlement receipt")
        GRAPH.CONSUMER._builtin_tree(receipt, "reaped settlement receipt")
        require(settlement.capture_handle is self.capture_handle
                and settlement.child_adapter is self.capture_adapter
                and settlement.normal_completion_capability is cap
                and settlement.owner_thread is self.owner_thread
                and settlement.deadline_ns == self.cleanup_deadline_ns
                and settlement.leader_reaped is True
                and settlement.probe_used is terminal_claimed
                and SUP.canonical(cap.completion)
                == self._monitor_result_bytes
                and type(receipt) is dict
                and SUP.canonical(receipt) == self._settlement_receipt_bytes
                and settlement.receipt == receipt
                and receipt["primary_completion"] == cap.completion
                and receipt["cleanup_outcome"]
                == "LEADER_REAPED_STREAMS_EOF"
                and receipt["error"] is None
                and receipt["cleanup_deadline_ns"]
                == self.cleanup_deadline_ns
                and receipt["finished_ns"] < self.cleanup_deadline_ns
                and receipt["remaining_resources"] == {
                    "leader_may_survive": False,
                    "pidfd_owned": True,
                    "epoll_owned": True,
                    "stdout_read_fd_owned": True,
                    "stderr_read_fd_owned": True},
                "reaped Linux settlement changed")
        enrollment = self.enrollment
        lifecycle = self.lifecycle
        cached = cap.cached_receipt
        descendant = settlement.descendant_capability
        require(type(owned) is OWNED._OwnedPidfdHandle
                and type(owned.__dict__) is dict
                and type(owned.lifecycle) is dict
                and owned.lifecycle is lifecycle
                and type(owned.state) is str
                and type(owned.pidfd) is int
                and type(owned.child) is dict
                and all(type(key) is str for key in owned.child)
                and set(owned.child) == {"pid", "starttime", "boot_id"}
                and type(owned.child["pid"]) is int
                and type(owned.child["starttime"]) is int
                and type(owned.child["boot_id"]) is str
                and type(owned.launcher) is dict
                and type(adapter) is OWNED._CaptureChildAdapter
                and type(adapter.__dict__) is dict
                and adapter.handle is owned
                and type(adapter.state) is str
                and type(adapter.normal_completion_digest) is str
                and adapter.owner_thread is self.owner_thread
                and type(capture) is CAPTURE.CaptureHandle
                and type(capture.streams) is dict
                and all(type(key) is str for key in capture.streams)
                and set(capture.streams) == {"stdout", "stderr"}
                and type(cap.__dict__) is dict
                and all(type(key) is str for key in cap.__dict__)
                and type(cap.completion) is dict
                and type(cap.completion_digest) is str
                and type(cap.execution_deadline_ns) is int
                and type(cap.completed_at_ns) is int
                and type(cap.owner) is dict
                and all(type(key) is str for key in cap.owner)
                and set(cap.owner) == {"pid", "starttime", "boot_id"}
                and type(cap.owner["pid"]) is int
                and type(cap.owner["starttime"]) is int
                and type(cap.owner["boot_id"]) is str
                and type(cap.used) is bool
                and type(cached) is OWNED.ExitReceipt
                and type(cached.lifecycle_token) is str
                and type(cached.child_pid) is int
                and type(cached.child_starttime) is int
                and type(cached.pidfd) is int
                and type(cached.code) is int
                and type(cached.status) is int
                and type(cached.classification) is str
                and type(descendant)
                is CAPTURE._DescendantSettlementCapability
                and type(descendant.__dict__) is dict
                and type(descendant.primary_completion) is dict
                and all(type(key) is str
                        for key in descendant.primary_completion)
                and type(descendant.settlement_kind) is str
                and type(descendant.primary_failure) is type(None)
                and type(descendant.finished_ns) is int
                and type(descendant.deadline_ns) is int
                and type(descendant.owner) is dict
                and type(descendant.used) is bool
                and descendant.used is terminal_claimed
                and type(enrollment.state) is str
                and type(enrollment._poisoned) is bool
                and type(lifecycle) is dict
                and all(type(key) is str for key in lifecycle)
                and set(lifecycle) == {"schema", "state", "token",
                    "supervisor", "owned_child", "origin"}
                and type(lifecycle["schema"]) is int
                and type(lifecycle["state"]) is str
                and type(lifecycle["token"]) is str
                and type(lifecycle["supervisor"]) is dict
                and type(lifecycle["owned_child"]) is dict
                and all(type(key) is str
                        for key in lifecycle["owned_child"])
                and set(lifecycle["owned_child"]) == {"pid", "starttime"}
                and type(lifecycle["owned_child"]["pid"]) is int
                and type(lifecycle["owned_child"]["starttime"]) is int
                and type(lifecycle["origin"]) is OWNED._ForkOrigin
                and type(self._lifecycle_origin.claimed) is bool
                and type(self._lifecycle_origin.pid) is int
                and enrollment.state == "BOUND"
                and enrollment._poisoned is False
                and enrollment.handle is self.handle
                and enrollment.ticket is self.ticket
                and enrollment.bound_capability is self.persisted.bound
                and self.ticket.used is True
                and self.handle._consumer_claim is enrollment
                and self.handle._consumer_claim_started is True
                and lifecycle is self.owned_pidfd.lifecycle
                and lifecycle["state"] == "OWNED_LEADER_REAPED_ONLY"
                and lifecycle["origin"] is self._lifecycle_origin
                and self._lifecycle_origin.claimed is True
                and self._lifecycle_origin.pid == self.returned_pid
                and self._lifecycle_origin.pre_fork_enrollment is self.ticket
                and self.capture_handle.origin is self._capture_origin
                and self.capture_handle.poller is self._capture_poller
                and all(self.capture_handle.streams.get(role)
                        is self._capture_stream_refs[role]
                        for role in ("stdout", "stderr")),
                "reaped Linux authority chain changed")
        GRAPH.CONSUMER._builtin_tree(
            descendant.primary_completion,
            "reaped descendant primary completion")
        CAPTURE._owner(cap.owner)
        self.persisted._validate_frozen("CLAIMED")
        CAPTURE.validate_transferred_normal_completion(
            settlement, capture, adapter)
        require(type(self.handle.state) is str
                and type(self.handle.poisoned) is bool
                and all(type(value) is set for value in (
                    self.handle.owned, self.handle.lost,
                    self.handle.attempted_closes,
                    self.handle.attempted_writes))
                and all(type(role) is str for values in (
                    self.handle.owned, self.handle.lost,
                    self.handle.attempted_closes,
                    self.handle.attempted_writes) for role in values),
                "strict reaped Linux FD sets")
        self.handle._validate_stored_graph()
        require(self.handle.state == "FD_GRAPH_READY"
                and self.handle.poisoned is False
                and self.handle.active is None
                and self.handle.owned
                == {"status_parent", "stdout_parent", "stderr_parent"}
                and self.handle.lost == set()
                and self.handle.attempted_closes
                == set(PARENT_CLOSE_ROLES) | {"grant_parent"}
                and self.handle.attempted_writes == set(),
                "reaped Linux FD graph changed")

    def observe_linux_status_tail_once(self):
        """Observe an empty status tail and real EOF under the same budget."""
        try:
            require(self.state == "LINUX_LEADER_REAPED_UNQUALIFIED"
                    and self.status_tail_attempted is False
                    and self.status_eof_receipt is None,
                    "fresh reaped Linux status observation required")
            self._validate_linux_reaped_chain(
                "LINUX_LEADER_REAPED_UNQUALIFIED")
            self._set("status_tail_attempted", True)
            self._phase("LINUX_OBSERVING_STATUS_TAIL")
            deadline = self.cleanup_deadline_ns
            watermark = max(self.last_clock_ns,
                            self.settlement_receipt["finished_ns"])
            status_fd = self.handle._authority_record("status_parent")["fd"]
            stagnant = 0
            def validate_read_observation():
                value = self.status_read_observation
                require(type(value) is dict
                        and all(type(key) is str for key in value),
                        "strict Linux status read evidence")
                GRAPH.CONSUMER._builtin_tree(
                    value, "frozen Linux status read evidence")
                require(SUP.canonical(value)
                        == self._status_read_observation_bytes,
                        "Linux status read observation changed")
            while True:
                now = time.monotonic_ns()
                require(type(now) is int and now >= watermark,
                        "Linux status clock regressed")
                stagnant = stagnant + 1 if now == watermark else 0
                require(stagnant <= 1024, "Linux status clock stalled")
                watermark = now
                self._set("last_clock_ns", now)
                require(now < deadline, "Linux status deadline reached")
                self._validate_linux_reaped_chain(
                    "LINUX_OBSERVING_STATUS_TAIL")
                raw = self.handle.read_nonblocking("status_parent", 1)
                require(type(raw) is dict and set(raw) == {"kind", "data"}
                        and type(raw["kind"]) is str
                        and raw["kind"] in ("DATA", "EAGAIN", "EOF")
                        and type(raw["data"]) is bytes
                        and (raw["kind"] == "DATA" or raw["data"] == b""),
                        "strict Linux status read result")
                result = {"kind": raw["kind"], "data": bytes(raw["data"])}
                observation = {"schema": 1,
                    "classification": "LINUX_STATUS_READ_OBSERVATION_ONLY",
                    "kind": result["kind"],
                    "data_hex": result["data"].hex(),
                    "data_bytes": len(result["data"]),
                    "before_ns": watermark,
                    "status_eof_verified": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
                GRAPH.CONSUMER._builtin_tree(
                    observation, "Linux status read observation")
                observation_bytes = SUP.canonical(observation)
                self._set("status_read_observation",
                          copy.deepcopy(observation))
                self._set("_status_read_observation_bytes",
                          observation_bytes)
                if result["kind"] == "EOF":
                    self._set("status_eof_observed", True)
                self._validate_linux_reaped_chain(
                    "LINUX_OBSERVING_STATUS_TAIL")
                after = time.monotonic_ns()
                require(type(after) is int and after >= watermark,
                        "Linux status post-read clock regressed")
                watermark = after
                self._set("last_clock_ns", after)
                require(after < deadline,
                        "Linux status read completed after deadline")
                self._validate_linux_reaped_chain(
                    "LINUX_OBSERVING_STATUS_TAIL")
                validate_read_observation()
                if result["kind"] == "DATA":
                    evidence = {"schema": 1,
                        "classification": "LINUX_STATUS_TAIL_NOT_EMPTY",
                        "tail_hex": result["data"].hex(),
                        "tail_bytes": len(result["data"]),
                        "observed_ns": after,
                        "status_eof_verified": False,
                        "abort_protocol_verified": False,
                        "runtime_authorized": False,
                        "storage_authorized": False, "exec_proven": False}
                    self._set("status_eof_receipt", evidence)
                    GRAPH.CONSUMER._builtin_tree(
                        evidence, "Linux status violation evidence")
                    self._set("_status_eof_receipt_bytes",
                              SUP.canonical(evidence))
                    self._phase("LINUX_STATUS_PROTOCOL_VIOLATION")
                    return copy.deepcopy(evidence)
                if result["kind"] == "EOF":
                    evidence = {"schema": 1,
                        "classification":
                        "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED",
                        "tail_bytes": 0, "observed_ns": after,
                        "deadline_ns": deadline,
                        "status_eof_verified": True,
                        "abort_protocol_verified": False,
                        "descendants_qualified": False,
                        "resources_closed": False,
                        "runtime_authorized": False,
                        "storage_authorized": False, "exec_proven": False}
                    frozen = copy.deepcopy(evidence)
                    GRAPH.CONSUMER._builtin_tree(
                        frozen, "Linux status EOF evidence")
                    frozen_bytes = SUP.canonical(frozen)
                    self._set("status_eof_receipt", frozen)
                    self._set("_status_eof_receipt_bytes", frozen_bytes)
                    final = time.monotonic_ns()
                    require(type(final) is int and final >= watermark,
                            "Linux status final clock regressed")
                    self._set("last_clock_ns", final)
                    require(final < deadline,
                            "Linux status EOF completed after deadline")
                    self._validate_linux_reaped_chain(
                        "LINUX_OBSERVING_STATUS_TAIL")
                    validate_read_observation()
                    require(type(self.status_eof_receipt) is dict
                            and all(type(key) is str
                                    for key in self.status_eof_receipt),
                            "strict Linux status EOF evidence")
                    GRAPH.CONSUMER._builtin_tree(
                        self.status_eof_receipt,
                        "frozen Linux status EOF evidence")
                    require(SUP.canonical(self.status_eof_receipt)
                            == self._status_eof_receipt_bytes,
                            "Linux status EOF evidence changed")
                    self._phase("LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED")
                    return copy.deepcopy(frozen)
                remaining = deadline - after
                poller = select.poll()
                poller.register(status_fd,
                                select.POLLIN | select.POLLHUP
                                | select.POLLERR)
                try:
                    events = poller.poll(min(100,
                        max(1, (remaining + 999999) // 1000000)))
                except InterruptedError:
                    events = []
                except OSError as exc:
                    if exc.errno != errno.EINTR:
                        raise
                    events = []
                require(type(events) is list
                        and all(type(item) is tuple and len(item) == 2
                                and type(item[0]) is int
                                and type(item[1]) is int for item in events)
                        and len(events) <= 1
                        and (not events or (events[0][0] == status_fd
                            and events[0][1] != 0
                            and events[0][1]
                            & ~(select.POLLIN | select.POLLHUP) == 0)),
                        "Linux status poll changed")
                self._validate_linux_reaped_chain(
                    "LINUX_OBSERVING_STATUS_TAIL")
        except BaseException:
            self._poison("LINUX_STATUS_TAIL")
            raise

    def verify_linux_abort_protocol_once(self):
        """Purely match frozen exit/status/canary evidence; grant no authority."""
        import prelive_capture_settlement as CAPTURE
        try:
            require(self.state == "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED"
                    and self.poisoned is False
                    and self.protocol_verify_attempted is False
                    and self.protocol_evidence is None
                    and self.status_eof_observed is True,
                    "fresh Linux abort protocol evidence required")
            self._validate_linux_reaped_chain(
                "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED")
            policy = self._protocol_policy
            require(type(policy) is dict
                    and all(type(key) is str for key in policy)
                    and set(policy) == {"schema", "exit_code",
                        "stdout_hex", "stderr_hex"}
                    and type(policy["schema"]) is int
                    and policy["schema"] == 1
                    and type(policy["exit_code"]) is int
                    and policy["exit_code"] == 73
                    and all(type(policy[key]) is str
                            and re.fullmatch(r"[0-9a-f]+", policy[key])
                            and len(policy[key]) % 2 == 0
                            for key in ("stdout_hex", "stderr_hex")),
                    "strict frozen Linux abort protocol policy")
            GRAPH.CONSUMER._builtin_tree(
                policy, "Linux abort protocol policy")
            require(SUP.canonical(policy) == self._protocol_policy_bytes,
                    "Linux abort protocol policy changed")
            for value, frozen, label in (
                    (self.status_read_observation,
                     self._status_read_observation_bytes,
                     "Linux protocol status read"),
                    (self.status_eof_receipt,
                     self._status_eof_receipt_bytes,
                     "Linux protocol status EOF")):
                require(type(value) is dict
                        and all(type(key) is str for key in value),
                        f"strict {label} evidence")
                GRAPH.CONSUMER._builtin_tree(value, label)
                require(SUP.canonical(value) == frozen,
                        f"frozen {label} evidence changed")
            require(self.status_read_observation == {
                        "schema": 1,
                        "classification":
                        "LINUX_STATUS_READ_OBSERVATION_ONLY",
                        "kind": "EOF", "data_hex": "", "data_bytes": 0,
                        "before_ns":
                        self.status_read_observation["before_ns"],
                        "status_eof_verified": False,
                        "runtime_authorized": False,
                        "storage_authorized": False, "exec_proven": False}
                    and type(self.status_read_observation["before_ns"]) is int
                    and self.status_eof_receipt == {
                        "schema": 1,
                        "classification":
                        "LINUX_STATUS_EOF_OBSERVED_UNQUALIFIED",
                        "tail_bytes": 0,
                        "observed_ns": self.status_eof_receipt["observed_ns"],
                        "deadline_ns": self.cleanup_deadline_ns,
                        "status_eof_verified": True,
                        "abort_protocol_verified": False,
                        "descendants_qualified": False,
                        "resources_closed": False,
                        "runtime_authorized": False,
                        "storage_authorized": False, "exec_proven": False}
                    and type(self.status_eof_receipt["observed_ns"]) is int,
                    "exact Linux status EOF proof required")
            self._set("protocol_verify_attempted", True)
            self._phase("LINUX_VERIFYING_ABORT_PROTOCOL")
            cap = self.monitor_capability
            completion = cap.completion
            terminal = completion["leader_observation"]
            reap = self.settlement_receipt["leader_reap"]
            streams = completion["streams"]
            require(type(terminal) is dict
                    and type(reap) is dict and type(streams) is dict
                    and all(type(role) is str for role in streams)
                    and set(streams) == {"stdout", "stderr"},
                    "strict Linux abort protocol containers")
            GRAPH.CONSUMER._builtin_tree(
                terminal, "Linux abort terminal receipt")
            GRAPH.CONSUMER._builtin_tree(reap, "Linux abort reap receipt")
            GRAPH.CONSUMER._builtin_tree(streams,
                                         "Linux abort stream receipts")
            expected_terminal = {
                "classification": "EXITED_NONZERO",
                "lifecycle_token": self.lifecycle["token"],
                "child_pid": self.returned_pid,
                "child_starttime": self.owned_pidfd.child["starttime"],
                "code": os.CLD_EXITED, "status": policy["exit_code"]}
            expected_reap = {
                "classification": "OWNED_LEADER_REAPED_ONLY",
                "lifecycle_token": self.lifecycle["token"],
                "child_pid": self.returned_pid,
                "child_starttime": self.owned_pidfd.child["starttime"],
                "exit": "EXITED_NONZERO", "status": policy["exit_code"],
                "runtime_authorized": False,
                "storage_authorized": False,
                "postcondition_verified": False,
                "descendants_qualified": False,
                "capture_qualified": False}
            reasons = []
            terminal_match = terminal == expected_terminal
            reap_match = reap == expected_reap
            if not terminal_match:
                reasons.append("TERMINAL_NOT_EXIT_73")
            if not reap_match:
                reasons.append("REAP_NOT_EXIT_73")
            expected_data = {
                "stdout": bytes.fromhex(policy["stdout_hex"]),
                "stderr": bytes.fromhex(policy["stderr_hex"])}
            capture = self.capture_handle
            canary_matches = {}
            for role in ("stdout", "stderr"):
                stream = capture.streams[role]
                current_receipt = CAPTURE._strict_stream_snapshot(
                    stream, role)
                require(type(capture.origin.identities) is dict
                        and all(type(key) is str
                                for key in capture.origin.identities)
                        and set(capture.origin.identities)
                        == {"stdout", "stderr"},
                        "strict Linux abort origin identities")
                origin_identity = CAPTURE._identity(
                    capture.origin.identities[role])
                require(current_receipt["pipe_identity"] == origin_identity,
                        "Linux abort stream origin changed")
                data = bytes(stream.stored)
                expected = expected_data[role]
                expected_receipt = {
                    "role": role,
                    "pipe_identity": origin_identity,
                    "eof": True, "truncated": False,
                    "stored_bytes": len(expected),
                    "observed_bytes": len(expected),
                    "prefix_sha256": hashlib.sha256(expected).hexdigest(),
                    "digest_scope": "FULL_CAPTURE"}
                canary_matches[role] = (data == expected
                    and current_receipt == expected_receipt
                    and streams[role] == expected_receipt)
                if not canary_matches[role]:
                    reasons.append(f"{role.upper()}_CANARY_MISMATCH")
            matched = reasons == []
            evidence = {"schema": 1,
                "classification": ("LINUX_ABORT_PROTOCOL_EVIDENCE_MATCH"
                    if matched else "LINUX_ABORT_PROTOCOL_EVIDENCE_MISMATCH"),
                "reasons": reasons,
                "exit_73_verified": terminal_match and reap_match,
                "status_eof_verified": True,
                "stdout_canary_verified": canary_matches["stdout"],
                "stderr_canary_verified": canary_matches["stderr"],
                "abort_protocol_verified": matched,
                "leader_reaped": True,
                "descendants_qualified": False,
                "resources_closed": False,
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
            GRAPH.CONSUMER._builtin_tree(
                evidence, "Linux abort protocol evidence")
            frozen = copy.deepcopy(evidence)
            frozen_bytes = SUP.canonical(frozen)
            self._set("protocol_evidence", frozen)
            self._set("_protocol_evidence_bytes", frozen_bytes)
            self._validate_linux_reaped_chain(
                "LINUX_VERIFYING_ABORT_PROTOCOL")
            require(SUP.canonical(self.status_read_observation)
                    == self._status_read_observation_bytes
                    and SUP.canonical(self.status_eof_receipt)
                    == self._status_eof_receipt_bytes
                    and SUP.canonical(self.protocol_evidence)
                    == self._protocol_evidence_bytes,
                    "Linux abort protocol evidence changed")
            self._phase("LINUX_ABORT_PROTOCOL_VERIFIED" if matched
                        else "LINUX_ABORT_PROTOCOL_MISMATCH")
            return copy.deepcopy(frozen)
        except BaseException:
            self._poison("LINUX_ABORT_PROTOCOL_VERIFY")
            raise

    def _validate_terminal_protocol_current(self, phase):
        """Callback-free exact MATCH/status/canary revalidation."""
        import prelive_capture_settlement as CAPTURE
        self._validate_linux_reaped_chain(phase)
        for value, frozen, label in (
                (self.protocol_evidence, self._protocol_evidence_bytes,
                 "terminal protocol evidence"),
                (self.status_read_observation,
                 self._status_read_observation_bytes,
                 "terminal status read"),
                (self.status_eof_receipt, self._status_eof_receipt_bytes,
                 "terminal status EOF")):
            require(type(value) is dict
                    and all(type(key) is str for key in value),
                    f"strict {label}")
            GRAPH.CONSUMER._builtin_tree(value, label)
            require(SUP.canonical(value) == frozen, f"{label} changed")
        require(self.protocol_evidence == {
                    "schema": 1,
                    "classification": "LINUX_ABORT_PROTOCOL_EVIDENCE_MATCH",
                    "reasons": [], "exit_73_verified": True,
                    "status_eof_verified": True,
                    "stdout_canary_verified": True,
                    "stderr_canary_verified": True,
                    "abort_protocol_verified": True,
                    "leader_reaped": True,
                    "descendants_qualified": False,
                    "resources_closed": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
                and self.status_read_observation["kind"] == "EOF"
                and self.status_read_observation["data_hex"] == ""
                and self.status_eof_receipt["tail_bytes"] == 0
                and self.status_eof_receipt["status_eof_verified"] is True,
                "exact current abort protocol MATCH required")
        streams = self.monitor_capability.completion["streams"]
        require(type(streams) is dict
                and all(type(key) is str for key in streams)
                and set(streams) == {"stdout", "stderr"},
                "strict current terminal streams")
        GRAPH.CONSUMER._builtin_tree(streams, "terminal capture receipts")
        policy = self._protocol_policy
        require(type(policy) is dict
                and all(type(key) is str for key in policy)
                and set(policy) == {"schema", "exit_code", "stdout_hex",
                                    "stderr_hex"},
                "strict terminal protocol policy")
        GRAPH.CONSUMER._builtin_tree(policy, "terminal protocol policy")
        require(type(policy["schema"]) is int and policy["schema"] == 1
                and type(policy["exit_code"]) is int
                and policy["exit_code"] == 73
                and all(type(policy[key]) is str
                        and re.fullmatch(r"[0-9a-f]+", policy[key])
                        and len(policy[key]) % 2 == 0
                        for key in ("stdout_hex", "stderr_hex"))
                and SUP.canonical(policy) == self._protocol_policy_bytes,
                "terminal protocol policy changed")
        for role, expected in (
                ("stdout", bytes.fromhex(policy["stdout_hex"])),
                ("stderr", bytes.fromhex(policy["stderr_hex"]))):
            stream = self.capture_handle.streams[role]
            current = CAPTURE._strict_stream_snapshot(stream, role)
            require(bytes(stream.stored) == expected
                    and current == streams[role]
                    and current["eof"] is True
                    and current["truncated"] is False
                    and current["stored_bytes"] == len(expected)
                    and current["observed_bytes"] == len(expected)
                    and current["prefix_sha256"]
                        == hashlib.sha256(expected).hexdigest()
                    and current["digest_scope"] == "FULL_CAPTURE",
                    f"current {role} canary changed")

    def _terminal_resource_preimage(self):
        roles = ("status_parent", "stdout_parent", "stderr_parent")
        handle = self.handle
        capture = self.capture_handle
        owned = self.owned_pidfd
        require(type(handle.owned) is set and handle.owned == set(roles)
                and type(handle.lost) is set and not handle.lost
                and handle.state == "FD_GRAPH_READY"
                and handle.poisoned is False and handle.active is None
                and handle.attempted_closes
                    == set(PARENT_CLOSE_ROLES) | {"grant_parent"}
                and handle.attempted_writes == set()
                and capture.monitor_active is False
                and capture.poller is self._capture_poller
                and owned.state == "REAPED"
                and owned.pidfd == self._owned_pidfd_fd,
                "exact terminal resource ownership required")
        require(type(capture.poller) is select.epoll
                and capture.poller.closed is False,
                "exact live terminal epoll required")
        poller_fd = capture.poller.fileno()
        require(type(poller_fd) is int and poller_fd >= 3,
                "strict terminal epoll descriptor")
        pipes = tuple((role, handle._authority_record(role)["fd"])
                      for role in roles)
        require(all(type(fd) is int and fd >= 3 for _, fd in pipes),
                "strict terminal pipe authority")
        scalar = (poller_fd, pipes, owned.pidfd)
        return (handle, capture, owned, capture.poller, scalar)

    def _validate_minted_terminal_claim_current(self, claim):
        require(type(claim) is _AbortTerminalReleaseClaim
                and self.terminal_release_claim is claim
                and self.state == "LINUX_ABORT_TERMINAL_RELEASE_CLAIMED"
                and self.poisoned is False,
                "terminal release claim is no longer current")
        self._validate_linux_reaped_chain(
            "LINUX_ABORT_TERMINAL_RELEASE_CLAIMED", terminal_claimed=True)
        current = self._terminal_resource_preimage()
        require(current[0] is claim._handle
                and current[1] is claim._capture
                and current[2] is claim._owned_pidfd
                and current[3] is claim._poller
                and current[4] == claim._resource_scalar,
                "terminal release resources changed")

    def claim_abort_terminal_release_once(self):
        """Freeze exact terminal resources; source-only and no close syscall."""
        try:
            require(self.state == "LINUX_ABORT_PROTOCOL_VERIFIED"
                    and self.poisoned is False
                    and self.terminal_release_claim_attempted is False
                    and self.terminal_release_claim is None
                    and self.enrollment.domain_enrollment is None,
                    "fresh verified terminal release evidence required")
            self._set("terminal_release_claim_attempted", True)
            self._phase("LINUX_CLAIMING_TERMINAL_RELEASE")
            self._validate_terminal_protocol_current(
                "LINUX_CLAIMING_TERMINAL_RELEASE")
            preimage = self._terminal_resource_preimage()
            handle, capture, owned, poller, resource_scalar = preimage
            poller_fd, pipes, pidfd = resource_scalar
            resources = []
            resources.append({"order": 1, "kind": "capture_epoll",
                              "role": "epoll", "owned": True})
            for order, (role, fd) in enumerate(pipes, 2):
                resources.append({"order": order, "kind": "pipe_read_end",
                                  "role": role, "fd": fd,
                                  "owned": True})
            resources.append({"order": 5, "kind": "leader_pidfd",
                              "role": "pidfd", "fd": pidfd,
                              "owned": True})
            now = time.monotonic_ns()
            require(type(now) is int and now >= self.last_clock_ns
                    and now < self.cleanup_deadline_ns,
                    "terminal release admission deadline")
            self._validate_terminal_protocol_current(
                "LINUX_CLAIMING_TERMINAL_RELEASE")
            require(self._terminal_resource_preimage() == preimage,
                    "terminal resources changed after admission clock")
            receipt = {
                "schema": 1,
                "classification": "ABORT_TERMINAL_RELEASE_CLAIMED_SOURCE_ONLY",
                "attempt_id": self.attempt_id,
                "deadline_ns": self.cleanup_deadline_ns,
                "claimed_ns": now,
                "resources": resources,
                "release_order": ["epoll", "status_parent",
                    "stdout_parent", "stderr_parent", "pidfd"],
                "close_syscalls": 0,
                "resources_closed": False,
                "descendants_qualified": False,
                "runtime_authorized": False,
                "storage_authorized": False,
                "exec_proven": False}
            GRAPH.CONSUMER._builtin_tree(
                receipt, "terminal release source claim")
            claim = _AbortTerminalReleaseClaim(
                _TERMINAL_RELEASE_KEY, self, handle, capture, owned, poller,
                resource_scalar, receipt)
            descendant = self.settlement_handle.descendant_capability
            require(self.settlement_handle.probe_used is False
                    and descendant.used is False,
                    "terminal release consumer already used")
            descendant.used = True
            self.settlement_handle.probe_used = True
            self._set("terminal_release_claim", claim)
            self._phase("LINUX_ABORT_TERMINAL_RELEASE_CLAIMED")
            return claim
        except BaseException:
            self._poison("LINUX_ABORT_TERMINAL_RELEASE_CLAIM")
            raise

    def run_mock_fork_split(self, syscalls):
        backend_type = type(syscalls)
        methods = ("monotonic_ns", "current_parent_identity", "fork_once",
                   "parent_closed", "advance_child", "child_status",
                   "wait_parent_readable")
        require(backend_type.__dict__.get("abort_fork_split_mock") is True
                and all(callable(backend_type.__dict__.get(name))
                        for name in methods),
                "closed abort fork split mock required")
        try:
            if self.state != "READY_TO_FORK" or self.poisoned:
                self._poison("MOCK_FORK_SPLIT_REPLAY")
                raise Refusal("fresh abort fork attempt required")
            self._validate("READY_TO_FORK", True)
            self._set("_backend", syscalls)
            self._set("_methods", {name: backend_type.__dict__[name]
                                   for name in methods})
            self._phase("PREFLIGHTING")
            parent = self._invoke("current_parent_identity")
            self._guard("PREFLIGHTING")
            GRAPH.CONSUMER._builtin_tree(parent, "abort parent identity")
            require(SUP.canonical(parent)
                    == self._owner_bytes,
                    "abort parent identity mismatch")
            self.handle.validate()
            self._guard("PREFLIGHTING")
            self.persisted._validate_frozen("CLAIMED")
            self._guard("PREFLIGHTING")
            self._clock("PREFLIGHTING")
            self._final_prefork_admission()
            child_context = {"schema": 1, "parent": copy.deepcopy(self.owner),
                "started_ns": self.last_clock_ns,
                "deadline_ns": self.deadline_ns,
                "descriptor": copy.deepcopy(self.descriptor),
                "descriptor_digest": SUP.digest(self.descriptor)}
            frozen_owner = copy.deepcopy(self.owner)
            self._set("fork_attempted", True)
            self._set("child_may_exist", True)
            self._set("fork_epoch", 1)
            self._phase("FORKING")
            pid = self._invoke("fork_once",
                child_context,
                lambda context: AbortChildRoutine(_CHILD_KEY, context))
            require(_integer(pid, 1) and pid != frozen_owner["pid"],
                    "abort fork PID invalid")
            self._set("returned_pid", pid)
            lifecycle = OWNED._record_owned_fork(
                OWNED._FORK_KEY, pid, frozen_owner, self.attempt_id,
                self.ticket)
            self._set("lifecycle", lifecycle)
            self._set("_lifecycle_origin", lifecycle["origin"])
            self._set("_lifecycle_token", lifecycle["token"])
            self._set("_lifecycle_supervisor_bytes",
                      SUP.canonical(lifecycle["supervisor"]))
            self._record_shared_descendant_fork(lifecycle, "FORKING")
            if self.state != "FORKING" or self.poisoned:
                raise Refusal("abort fork callback changed authority")
            self._phase("PARENT_RECORDED")
            require(self.fork_epoch == 1 and self.ticket.used is True
                    and lifecycle["owned_child"]["pid"] == pid,
                    "abort child lifecycle record failed")
            self._phase("CLOSING_CHILD_ENDPOINTS")
            for role in PARENT_CLOSE_ROLES:
                self._close_parent_role(role, "CLOSING_CHILD_ENDPOINTS")
            self._phase("WAITING_CHILD_READY")
            self._clock("WAITING_CHILD_READY")
            child_progress = self._invoke("advance_child", self.deadline_ns)
            self._guard("WAITING_CHILD_READY")
            child_progress = self._strict_progress(child_progress)
            child_progress = copy.deepcopy(child_progress)
            self._clock("WAITING_CHILD_READY")
            require(child_progress["classification"]
                    == "CHILD_WAITING_GRANT_EOF",
                    "abort child did not reach grant wait")
            self._phase("CLOSING_GRANT_WRITER")
            self._close_parent_role("grant_parent", "CLOSING_GRANT_WRITER")
            self._phase("WAITING_CHILD_EXIT")
            self._clock("WAITING_CHILD_EXIT")
            child_progress = self._invoke("advance_child", self.deadline_ns)
            self._guard("WAITING_CHILD_EXIT")
            child_progress = self._strict_progress(child_progress)
            child_progress = copy.deepcopy(child_progress)
            self._clock("WAITING_CHILD_EXIT")
            require(child_progress["classification"] == "CHILD_EXITED"
                    and child_progress["exit_code"] == EXIT_ABORT_EOF,
                    "abort child EOF exit not proven")
            expected = {"status_parent": STATUS_READY,
                        "stdout_parent": STDOUT_CANARY,
                        "stderr_parent": STDERR_CANARY}
            captures = {}
            self._phase("DRAINING_CAPTURE")
            for role in PARENT_READ_ROLES:
                captures[role] = self._drain_exact(
                    role, expected[role], "DRAINING_CAPTURE")
                self._close_parent_role(role, "DRAINING_CAPTURE")
            self._phase("CHECKING_CHILD_STATUS")
            self._clock("CHECKING_CHILD_STATUS")
            status = self._invoke("child_status")
            self._guard("CHECKING_CHILD_STATUS")
            GRAPH.CONSUMER._builtin_tree(status, "abort child status")
            require(type(status) is dict
                    and type(status.get("pid")) is int
                    and type(status.get("exit_code")) is int
                    and type(status.get("reaped")) is bool
                    and status == {
                        "pid": pid, "exit_code": EXIT_ABORT_EOF,
                        "reaped": False},
                    "abort child status changed")
            self._clock("CHECKING_CHILD_STATUS")
            self._phase("CHILD_EXIT_OBSERVED_UNREAPED")
            return {"schema": 1,
                    "classification": "MOCK_ABORT_CHILD_EXITED_UNREAPED",
                    "pid": pid, "exit_code": EXIT_ABORT_EOF,
                    "captures": captures, "ticket_used": True,
                    "child_may_exist": True, "reaped": False,
                    "grant_bytes": 0, "grant_write_calls": 0,
                    "exec_attempts": 0, "runtime_authorized": False,
                    "storage_authorized": False,
                    "terminal_success": False}
        except BaseException:
            self._poison("MOCK_FORK_SPLIT")
            raise

    def fork_and_bind_linux_pidfd_once(self):
        """Source-staged real fork/pidfd handoff; no capture or grant close."""
        import prelive_abort_linux_child as LINUX
        try:
            if self.state != "READY_TO_FORK" or self.poisoned:
                self._poison("LINUX_FORK_BIND_REPLAY")
                raise Refusal("fresh Linux abort fork attempt required")
            self._validate("READY_TO_FORK", True)
            self._phase("LINUX_PREFLIGHTING")
            fd_calls = FDS.LinuxFDCalls()
            owned_calls = OWNED.LinuxSyscalls()
            parent = fd_calls.owner_identity()
            self._guard("LINUX_PREFLIGHTING")
            GRAPH.CONSUMER._builtin_tree(parent,
                                         "Linux abort parent identity")
            require(SUP.canonical(parent) == self._owner_bytes,
                    "Linux abort parent identity mismatch")
            observed_launcher = owned_calls.launcher(parent["pid"])
            OWNED._validate_launcher(observed_launcher)
            request = GRAPH._strict_bound_request(self.persisted.bound)
            launcher_identity = request["launcher_identity"]
            intent_launcher = {
                "kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                "exe_dev": launcher_identity["interpreter"]["dev"],
                "exe_inode": launcher_identity["interpreter"]["inode"],
                "cmdline_sha256": launcher_identity["inherited"]
                ["proc_cmdline_sha256"]}
            OWNED._validate_launcher(intent_launcher)
            require(observed_launcher == intent_launcher,
                    "parent launcher differs from persisted INTENT")
            expected_launcher = copy.deepcopy(intent_launcher)
            self._set("expected_launcher", copy.deepcopy(expected_launcher))
            self._guard("LINUX_PREFLIGHTING")
            self.handle.validate()
            self._guard("LINUX_PREFLIGHTING")
            self.persisted._validate_frozen("CLAIMED")
            self._guard("LINUX_PREFLIGHTING")
            now = time.monotonic_ns()
            self._guard("LINUX_PREFLIGHTING")
            require(type(now) is int and now >= self.last_clock_ns
                    and now < self.deadline_ns,
                    "Linux abort fork deadline reached or clock regressed")
            self._set("last_clock_ns", now)
            self._final_prefork_admission_for("LINUX_PREFLIGHTING")
            context = {"schema": 1, "parent": copy.deepcopy(self.owner),
                "started_ns": now, "deadline_ns": self.deadline_ns,
                "descriptor": copy.deepcopy(self.descriptor),
                "descriptor_digest": SUP.digest(self.descriptor)}
            frozen_owner = copy.deepcopy(self.owner)
            self._set("fork_attempted", True)
            self._set("child_may_exist", True)
            self._set("fork_epoch", 1)
            self._phase("LINUX_FORKING")
            pid = LINUX.fork_abort_child_once(LINUX._FORK_KEY, context)
            require(type(pid) is int and pid > 0
                    and pid != frozen_owner["pid"],
                    "Linux abort fork PID invalid")
            self._set("returned_pid", pid)
            lifecycle = OWNED._record_owned_fork(
                OWNED._FORK_KEY, pid, frozen_owner, self.attempt_id,
                self.ticket)
            self._set("lifecycle", lifecycle)
            self._set("_lifecycle_origin", lifecycle["origin"])
            self._set("_lifecycle_token", lifecycle["token"])
            self._set("_lifecycle_supervisor_bytes",
                      SUP.canonical(lifecycle["supervisor"]))
            self._record_shared_descendant_fork(
                lifecycle, "LINUX_FORKING")
            require(self.state == "LINUX_FORKING" and self.poisoned is False
                    and self.ticket.used is True,
                    "Linux fork callback changed authority")
            self._phase("LINUX_BINDING_PIDFD")
            owned = OWNED.bind_owned_pidfd(
                lifecycle, copy.deepcopy(expected_launcher), owned_calls)
            self._set("owned_pidfd", owned)
            self._strict_linux_owned_graph(owned)
            require(owned.child == {"pid": pid,
                        "starttime": lifecycle["owned_child"]["starttime"],
                        "boot_id": self.owner["boot_id"]}
                    and owned.launcher == expected_launcher,
                    "Linux abort pidfd bind initial result incomplete")
            self._set("_owned_pidfd_fd", owned.pidfd)
            self._set("_owned_child_bytes", SUP.canonical(owned.child))
            self._set("_owned_launcher_bytes", SUP.canonical(owned.launcher))
            post_bind_now = time.monotonic_ns()
            require(type(post_bind_now) is int
                    and post_bind_now >= self.last_clock_ns
                    and post_bind_now < self.deadline_ns,
                    "Linux abort pidfd bind exceeded deadline")
            self._set("last_clock_ns", post_bind_now)
            self._validate_linux_pidfd_bound(owned)
            self._phase("LINUX_PIDFD_BOUND_UNARMED")
            return {"schema": 1,
                    "classification": "LINUX_ABORT_PIDFD_BOUND_UNARMED",
                    "pid": pid, "pidfd": owned.pidfd,
                    "grant_write_calls": 0, "grant_bytes": 0,
                    "capture_complete": False, "leader_reaped": False,
                    "descendants_qualified": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            self._poison("LINUX_FORK_BIND")
            raise

    def prepare_linux_capture_unarmed_once(self):
        """Close parent child-ends, prove readiness and bind capture only."""
        import prelive_capture_settlement as CAPTURE
        try:
            require(self.state == "LINUX_PIDFD_BOUND_UNARMED"
                    and self.poisoned is False
                    and self.capture_handle is None
                    and self.capture_adapter is None,
                    "fresh Linux unarmed capture preparation required")
            self._validate_linux_pidfd_bound(
                self.owned_pidfd, "LINUX_PIDFD_BOUND_UNARMED")
            self._phase("LINUX_CLOSING_CHILD_ENDPOINTS")
            for role in PARENT_CLOSE_ROLES:
                self.handle.close_role_once(role)
                require(self.state == "LINUX_CLOSING_CHILD_ENDPOINTS"
                        and self.poisoned is False,
                        "Linux parent endpoint close changed authority")
                self._strict_linux_owned_graph(self.owned_pidfd)
            expected_owned = {"grant_parent", "status_parent",
                              "stdout_parent", "stderr_parent"}
            require(self.handle.owned == expected_owned
                    and self.handle.lost == set()
                    and self.handle.attempted_closes
                    == set(PARENT_CLOSE_ROLES)
                    and self.handle.attempted_writes == set()
                    and self.handle.poisoned is False,
                    "Linux parent endpoint close set incomplete")
            self._phase("LINUX_WAITING_READY")
            ready = b""
            status_fd = self.handle.frozen_bundle["status_parent"]["fd"]
            while len(ready) < 1:
                now = time.monotonic_ns()
                require(type(now) is int and now >= self.last_clock_ns
                        and now < self.deadline_ns,
                        "Linux readiness deadline reached")
                self._set("last_clock_ns", now)
                result = self.handle.read_nonblocking(
                    "status_parent", 2 - len(ready))
                require(type(result) is dict
                        and set(result) == {"kind", "data"}
                        and result["kind"] in ("DATA", "EAGAIN", "EOF")
                        and type(result["data"]) is bytes,
                        "strict Linux readiness read")
                if result["kind"] == "DATA":
                    ready += result["data"]
                    require(ready == STATUS_READY,
                            "Linux child readiness bytes changed")
                    continue
                require(result["kind"] != "EOF",
                        "Linux child readiness pipe closed early")
                poll_now = time.monotonic_ns()
                require(type(poll_now) is int
                        and poll_now >= self.last_clock_ns
                        and poll_now < self.deadline_ns,
                        "Linux readiness poll deadline reached")
                self._set("last_clock_ns", poll_now)
                remaining = self.deadline_ns - poll_now
                poller = select.poll()
                poller.register(status_fd,
                                select.POLLIN | select.POLLHUP
                                | select.POLLERR)
                events = poller.poll(
                    max(1, (remaining + 999999) // 1000000))
                require(type(events) is list and len(events) == 1
                        and type(events[0]) is tuple and len(events[0]) == 2
                        and events[0][0] == status_fd
                        and type(events[0][1]) is int
                        and events[0][1] != 0
                        and events[0][1]
                        & ~(select.POLLIN | select.POLLHUP) == 0,
                        "Linux readiness poll changed")
            self._set("status_ready", ready)
            ready_now = time.monotonic_ns()
            require(type(ready_now) is int
                    and ready_now >= self.last_clock_ns
                    and ready_now < self.deadline_ns,
                    "Linux readiness completion exceeded deadline")
            self._set("last_clock_ns", ready_now)
            self._validate_linux_capture_chain("LINUX_WAITING_READY")
            child = {"lifecycle_token": self.lifecycle["token"],
                     "pid": self.owned_pidfd.child["pid"],
                     "starttime": self.owned_pidfd.child["starttime"],
                     "boot_id": self.owned_pidfd.child["boot_id"]}
            def capture_identity(role):
                item = self.handle.frozen_bundle[role]
                return {key: item[key] for key in (
                    "fd", "dev", "inode", "mode", "flags")}
            origin = CAPTURE._record_capture_pipes(
                CAPTURE._PIPE_KEY, self.owner, child,
                capture_identity("stdout_parent"),
                capture_identity("stderr_parent"))
            self._set("_capture_origin", origin)
            self._set("_capture_origin_bytes", SUP.canonical({
                "owner": origin.owner, "child": origin.child,
                "identities": origin.identities}))
            capture_calls = CAPTURE.LinuxCaptureSyscalls()
            request = GRAPH._strict_bound_request(self.persisted.bound)
            capture = CAPTURE.bind_capture_pipes(
                origin, request["capture_limit"], capture_calls)
            self._set("capture_handle", capture)
            self._set("_capture_poller", capture.poller)
            self._set("_capture_stream_refs", {
                role: capture.streams[role] for role in ("stdout", "stderr")})
            owned_calls = OWNED.LinuxSyscalls()
            adapter = OWNED.bind_capture_child_adapter(
                self.owned_pidfd, owned_calls)
            self._set("capture_adapter", adapter)
            CAPTURE.attach_child_adapter(capture, adapter, capture_calls)
            attached_now = time.monotonic_ns()
            require(type(attached_now) is int
                    and attached_now >= self.last_clock_ns
                    and attached_now < self.deadline_ns,
                    "Linux capture bind exceeded deadline")
            self._set("last_clock_ns", attached_now)
            self._validate_linux_capture_chain(
                "LINUX_WAITING_READY", attached=True)
            require(capture.state == "BOUND"
                    and capture.child_adapter is adapter
                    and adapter.handle is self.owned_pidfd
                    and self.handle.owned == expected_owned
                    and self.status_ready == STATUS_READY,
                    "Linux unarmed capture binding incomplete")
            self._phase("LINUX_CAPTURE_BOUND_UNARMED")
            return {"schema": 1,
                    "classification": "LINUX_ABORT_CAPTURE_BOUND_UNARMED",
                    "pid": self.returned_pid,
                    "pidfd": self._owned_pidfd_fd,
                    "status_ready": True, "grant_write_calls": 0,
                    "grant_bytes": 0, "grant_writer_open": True,
                    "leader_reaped": False,
                    "descendants_qualified": False,
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            self._poison("LINUX_CAPTURE_PREPARE")
            raise

    def _strict_progress(self, value):
        GRAPH.CONSUMER._builtin_tree(value, "abort child progress")
        require(type(value) is dict
                and ((set(value) == {"classification"}
                      and value["classification"]
                      == "CHILD_WAITING_GRANT_EOF")
                     or (set(value) == {"classification", "exit_code"}
                         and value["classification"] == "CHILD_EXITED"
                         and type(value["exit_code"]) is int)),
                "strict abort child progress required")
        return value

    def _close_parent_role(self, role, expected_state):
        self._guard(expected_state)
        identity = copy.deepcopy(self.handle.frozen_bundle[role])
        self.handle.close_role_once(role)
        self._guard(expected_state)
        result = self._invoke("parent_closed", role, identity)
        self._guard(expected_state)
        require(result is True, "parent close mirror not confirmed")

    def _drain_exact(self, role, expected, expected_state):
        data = b""
        eof = False
        while not eof:
            self._clock(expected_state)
            result = self.handle.read_nonblocking(role, 65536)
            self._guard(expected_state)
            require(type(result) is dict and set(result) == {"kind", "data"}
                    and result["kind"] in ("EAGAIN", "EOF", "DATA")
                    and type(result["data"]) is bytes,
                    "strict abort parent read result")
            result = {"kind": result["kind"], "data": bytes(result["data"])}
            require(result["kind"] == "DATA" or result["data"] == b"",
                    "non-data read result carried bytes")
            self._clock(expected_state)
            if result["kind"] == "EAGAIN":
                waited = self._invoke(
                    "wait_parent_readable", role, self.deadline_ns)
                self._guard(expected_state)
                require(waited is True,
                        "parent readable wait not confirmed")
                self._clock(expected_state)
                continue
            if result["kind"] == "EOF":
                eof = True
            else:
                data += result["data"]
                require(len(data) <= len(expected),
                        "abort child capture exceeded canary")
        require(data == expected, "abort child capture mismatch")
        return data


def prepare_abort_attempt(persisted, attempt_id, now_ns):
    return AbortForkAttempt(_ATTEMPT_KEY, persisted, attempt_id, now_ns)
