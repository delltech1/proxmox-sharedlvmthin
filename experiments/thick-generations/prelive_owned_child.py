#!/usr/bin/python3
"""Source-staged owned-child pidfd lifecycle; not wired to any CLI or runner."""

from dataclasses import dataclass
import copy
import errno
import hashlib
import importlib
import os
from pathlib import Path
import re
import signal
import threading


LIMITS = {"runtime_authorized": False, "storage_authorized": False,
          "postcondition_verified": False, "descendants_qualified": False,
          "capture_qualified": False}
_HANDLE_KEY = object()
_RECEIPT_KEY = object()
_FORK_KEY = object()
_CAPTURE_KEY = object()
_ADAPTER_KEY = object()
_DESCENDANT_KEY = object()
_LEADER_REAP_CAP_KEY = object()
_PREFORK_KEY = object()


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _hex32(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{32}", value) is not None


def _validate_identity(value, label):
    require(type(value) is dict
            and set(value) == {"pid", "starttime", "boot_id"}, label + " schema")
    require(_integer(value["pid"], 1) and _integer(value["starttime"], 1),
            label + " numeric identity")
    require(type(value["boot_id"]) is str
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             value["boot_id"]) is not None, label + " boot identity")


def _validate_launcher(value):
    require(type(value) is dict
            and set(value) == {"exe_dev", "exe_inode", "cmdline_sha256", "kind"},
            "launcher schema")
    require(value["kind"] == "UNARMED_LAUNCHER_NOT_PAYLOAD"
            and _integer(value["exe_dev"], 1)
            and _integer(value["exe_inode"], 1)
            and type(value["cmdline_sha256"]) is str
            and re.fullmatch(r"[0-9a-f]{64}", value["cmdline_sha256"]) is not None,
            "launcher identity")


class LinuxSyscalls:
    """Real Linux boundary, deliberately unused by the default-refusing CLI."""

    @staticmethod
    def boot_id():
        return Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip().lower()

    @staticmethod
    def starttime(pid):
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        fields = raw[closing + 2:].split()
        if closing < 1 or len(fields) < 20 or not fields[19].isdigit():
            raise Refusal("unparseable process starttime")
        return int(fields[19])

    @classmethod
    def current_identity(cls):
        pid = os.getpid()
        return {"pid": pid, "starttime": cls.starttime(pid),
                "boot_id": cls.boot_id()}

    @staticmethod
    def launcher(pid):
        info = os.stat(f"/proc/{pid}/exe")
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        return {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                "exe_dev": info.st_dev, "exe_inode": info.st_ino,
                "cmdline_sha256": hashlib.sha256(cmdline).hexdigest()}

    @staticmethod
    def pidfd_open(pid):
        return os.pidfd_open(pid, 0)

    @staticmethod
    def waitid(pidfd, options):
        return os.waitid(os.P_PIDFD, pidfd, options)

    @classmethod
    def probe_waitable(cls, pidfd):
        return cls.waitid(pidfd, os.WEXITED | os.WNOHANG | os.WNOWAIT)

    @staticmethod
    def close(fd):
        os.close(fd)


class _OwnedPidfdHandle:
    def __init__(self, key, lifecycle, pidfd, child, launcher):
        require(key is _HANDLE_KEY, "handle construction is private")
        self.lifecycle = lifecycle
        self.pidfd = pidfd
        self.child = dict(child)
        self.launcher = dict(launcher)
        self.state = "BOUND"
        self.observation = None
        self.capture_adapter = None
        self.owner_thread = threading.current_thread()

    def __copy__(self):
        raise Refusal("owned pidfd handle is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("owned pidfd handle is noncopyable")


class _PreForkDescendantEnrollment:
    def __init__(self, key, binding, supervisor):
        require(key is _PREFORK_KEY,
                "pre-fork enrollment construction is private")
        self.binding = binding
        self.supervisor = copy.deepcopy(supervisor)
        self.used = False

    def __copy__(self):
        raise Refusal("pre-fork enrollment is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("pre-fork enrollment is noncopyable")


class _ForkOrigin:
    def __init__(self, key, pid, pre_fork_enrollment=None):
        require(key is _FORK_KEY, "fork origin construction is private")
        self.pid = pid
        self.claimed = False
        self.pre_fork_enrollment = pre_fork_enrollment

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


class _ExactLeaderReapCapability:
    """Opaque post-reap authority; never serialized or reconstructed."""
    def __init__(self, key, binding, adapter, handle, receipt):
        require(key is _LEADER_REAP_CAP_KEY,
                "leader reap capability construction is private")
        self.binding = binding
        self.adapter = adapter
        self.handle = handle
        self.fork_origin = handle.lifecycle["origin"]
        self.receipt = receipt
        self.owner_thread = threading.current_thread()
        self.leader = {"pid": receipt.child_pid,
                       "starttime": receipt.child_starttime,
                       "boot_id": handle.child["boot_id"]}
        self.used = False

    def __copy__(self):
        raise Refusal("leader reap capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("leader reap capability is noncopyable")


def prepare_descendant_fork_enrollment(key, binding, supervisor):
    require(key is _PREFORK_KEY and binding is not None,
            "pre-fork enrollment capability")
    _validate_identity(supervisor, "pre-fork supervisor")
    return _PreForkDescendantEnrollment(key, binding, supervisor)


def _record_owned_fork(key, pid, supervisor, token, pre_fork_enrollment=None):
    """Called only by the future parent fork branch; no public adopt API."""
    require(key is _FORK_KEY, "fork record construction is private")
    if pre_fork_enrollment is not None:
        require(type(pre_fork_enrollment) is _PreForkDescendantEnrollment
                and not pre_fork_enrollment.used
                and pre_fork_enrollment.supervisor == supervisor,
                "foreign or reused pre-fork enrollment")
        pre_fork_enrollment.used = True
    return {"schema": 1, "state": "SPAWNED_UNBOUND", "token": token,
            "supervisor": copy.deepcopy(supervisor),
            "owned_child": {"pid": pid, "starttime": None},
            "origin": _ForkOrigin(key, pid, pre_fork_enrollment)}


@dataclass(frozen=True)
class ExitReceipt:
    _key: object
    lifecycle_token: str
    child_pid: int
    child_starttime: int
    pidfd: int
    code: int
    status: int
    classification: str


def _validate_lifecycle(value, syscalls):
    keys = {"schema", "state", "token", "supervisor", "owned_child", "origin"}
    require(type(value) is dict and set(value) == keys, "lifecycle schema")
    require(type(value["schema"]) is int and value["schema"] == 1
            and value["state"] == "SPAWNED_UNBOUND" and _hex32(value["token"]),
            "lifecycle state")
    _validate_identity(value["supervisor"], "supervisor")
    current = syscalls.current_identity()
    _validate_identity(current, "observed supervisor")
    require(value["supervisor"] == current,
            "foreign supervisor lifecycle")
    child = value["owned_child"]
    require(type(child) is dict and set(child) == {"pid", "starttime"},
            "owned child schema")
    require(_integer(child["pid"], 1) and child["starttime"] is None,
            "owned child must be a fresh fork result")
    require(child["pid"] != value["supervisor"]["pid"], "child cannot be supervisor")
    require(type(value["origin"]) is _ForkOrigin
            and value["origin"].pid == child["pid"]
            and value["origin"].claimed is False, "unowned or reused fork origin")


def bind_owned_pidfd(lifecycle, expected_launcher, syscalls):
    """Bind only a caller-owned fresh fork record; never adopt an arbitrary PID."""
    if (type(lifecycle) is dict and type(lifecycle.get("origin")) is _ForkOrigin
            and lifecycle["origin"].claimed):
        lifecycle["state"] = "UNKNOWN_REENTRANT_OR_REUSED_BIND"
        raise Refusal("fork origin is already being bound or was consumed")
    _validate_lifecycle(lifecycle, syscalls)
    expected_launcher = copy.deepcopy(expected_launcher)
    _validate_launcher(expected_launcher)
    lifecycle["origin"].claimed = True
    lifecycle["state"] = "BINDING"
    pid = lifecycle["owned_child"]["pid"]
    pidfd = None
    pidfd_valid = False
    try:
        first_start = syscalls.starttime(pid)
        require(_integer(first_start, 1), "child starttime")
        first_launcher = syscalls.launcher(pid)
        _validate_launcher(first_launcher)
        require(first_launcher == expected_launcher, "unexpected unarmed launcher")
        pidfd = syscalls.pidfd_open(pid)
        require(_integer(pidfd, 0), "invalid pidfd")
        pidfd_valid = True
        waitable = syscalls.probe_waitable(pidfd)
        if waitable is not None:
            require(_integer(waitable.si_pid, 1) and waitable.si_pid == pid,
                    "pidfd waitability PID mismatch")
            require(type(waitable.si_code) is int and type(waitable.si_status) is int
                    and waitable.si_code in (os.CLD_EXITED, os.CLD_KILLED, os.CLD_DUMPED),
                    "pidfd waitability receipt invalid")
        second_start = syscalls.starttime(pid)
        second_launcher = syscalls.launcher(pid)
        require(_integer(second_start, 1), "second child starttime")
        _validate_launcher(second_launcher)
        require(second_start == first_start and second_launcher == first_launcher,
                "child identity changed during pidfd bind")
        require(lifecycle["state"] == "BINDING",
                "bind state changed reentrantly")
    except BaseException:
        lifecycle["state"] = "UNKNOWN_CHILD_MAY_EXIST"
        lifecycle["owned_child"]["starttime"] = locals().get("first_start")
        if pidfd_valid:
            try:
                syscalls.close(pidfd)
            except BaseException:
                pass
        raise
    child = {"pid": pid, "starttime": first_start,
             "boot_id": lifecycle["supervisor"]["boot_id"]}
    lifecycle["owned_child"]["starttime"] = first_start
    lifecycle["state"] = "PIDFD_BOUND"
    return _OwnedPidfdHandle(_HANDLE_KEY, lifecycle, pidfd, child, first_launcher)


def _assert_handle_owner(handle, syscalls):
    require(type(handle) is _OwnedPidfdHandle, "owned pidfd handle required")
    if (handle.state.startswith("UNKNOWN")
            or handle.state in ("PIDFD_CLOSE_UNKNOWN_UNSETTLED",
                                "CLOSED_UNSETTLED_UNKNOWN",
                                "REAPED_PIDFD_CLOSE_UNKNOWN")):
        raise Refusal("pidfd lifecycle is permanently unknown or closed")
    try:
        current = syscalls.current_identity()
        _validate_identity(current, "observed supervisor")
    except BaseException:
        handle.state = "UNKNOWN_OWNER_CHANGED"
        handle.lifecycle["state"] = "UNKNOWN_OWNER_CHANGED"
        raise
    if (threading.current_thread() is not handle.owner_thread
            or current != handle.lifecycle["supervisor"]):
        handle.state = "UNKNOWN_OWNER_CHANGED"
        handle.lifecycle["state"] = "UNKNOWN_OWNER_CHANGED"
        raise Refusal("pidfd handle owner changed")


def _assert_consumer(handle, key):
    if handle.capture_adapter is not None and key is not _CAPTURE_KEY:
        handle.state = "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION"
        handle.lifecycle["state"] = "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION"
        raise Refusal("owned child lifecycle is claimed by capture adapter")


def _decode_wait(result, handle):
    require(result is not None, "terminal result is absent")
    require(_integer(result.si_pid, 1) and result.si_pid == handle.child["pid"],
            "wait receipt PID mismatch")
    require(type(result.si_code) is int and type(result.si_status) is int,
            "wait receipt types")
    if result.si_code == os.CLD_EXITED:
        require(0 <= result.si_status <= 255, "exit status range")
        classification = "EXITED_ZERO" if result.si_status == 0 else "EXITED_NONZERO"
    elif result.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        require(1 <= result.si_status < signal.NSIG, "signal status range")
        classification = "SIGNALED"
    else:
        raise Refusal("wait receipt is not terminal")
    return ExitReceipt(_RECEIPT_KEY, handle.lifecycle["token"],
                       handle.child["pid"], handle.child["starttime"],
                       handle.pidfd, result.si_code, result.si_status,
                       classification)


def observe_exit_nonblocking(handle, syscalls, _consumer=None):
    _assert_handle_owner(handle, syscalls)
    _assert_consumer(handle, _consumer)
    if handle.state in ("OBSERVING", "REAPING", "CLOSING"):
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        handle.lifecycle["state"] = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("reentrant pidfd lifecycle operation")
    require(handle.state == "BOUND", "owned bound handle required")
    options = os.WEXITED | os.WNOHANG | os.WNOWAIT
    handle.state = "OBSERVING"
    try:
        result = syscalls.waitid(handle.pidfd, options)
    except BaseException:
        handle.state = "UNKNOWN_OBSERVE_OUTCOME"
        handle.lifecycle["state"] = "UNKNOWN_OBSERVE_OUTCOME"
        raise
    if result is None:
        if handle.state != "OBSERVING":
            raise Refusal("observation state changed reentrantly")
        handle.state = "BOUND"
        return None
    try:
        receipt = _decode_wait(result, handle)
    except BaseException:
        handle.state = "UNKNOWN_OBSERVE_OUTCOME"
        handle.lifecycle["state"] = "UNKNOWN_OBSERVE_OUTCOME"
        raise
    handle.observation = receipt
    require(handle.state == "OBSERVING", "observation state changed reentrantly")
    handle.state = "TERMINAL_OBSERVED_NOT_REAPED"
    return receipt


def reap_observed_exit(handle, receipt, syscalls, _consumer=None):
    _assert_handle_owner(handle, syscalls)
    _assert_consumer(handle, _consumer)
    if handle.state in ("OBSERVING", "REAPING", "CLOSING"):
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        handle.lifecycle["state"] = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("reentrant pidfd lifecycle operation")
    require(handle.state == "TERMINAL_OBSERVED_NOT_REAPED", "reap state")
    require(type(receipt) is ExitReceipt and receipt._key is _RECEIPT_KEY
            and handle.observation is receipt, "foreign exit receipt")
    handle.state = "REAPING"
    try:
        result = syscalls.waitid(handle.pidfd, os.WEXITED | os.WNOHANG)
        repeated = _decode_wait(result, handle)
        require((repeated.lifecycle_token, repeated.child_pid,
                 repeated.child_starttime, repeated.pidfd,
                 repeated.code, repeated.status, repeated.classification)
                == (receipt.lifecycle_token, receipt.child_pid,
                    receipt.child_starttime, receipt.pidfd,
                    receipt.code, receipt.status, receipt.classification),
                "reap result changed after WNOWAIT")
        require(handle.state == "REAPING", "reap state changed reentrantly")
    except BaseException:
        handle.state = "UNKNOWN_REAP_OUTCOME"
        handle.lifecycle["state"] = "UNKNOWN_REAP_OUTCOME"
        raise
    handle.state = "REAPED"
    handle.lifecycle["state"] = "OWNED_LEADER_REAPED_ONLY"
    return {"classification": "OWNED_LEADER_REAPED_ONLY",
            "exit": receipt.classification, "status": receipt.status, **LIMITS}


def close_pidfd_without_settlement(handle, syscalls, _consumer=None):
    _assert_handle_owner(handle, syscalls)
    _assert_consumer(handle, _consumer)
    if handle.state in ("OBSERVING", "REAPING", "CLOSING"):
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        handle.lifecycle["state"] = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("reentrant pidfd lifecycle operation")
    require(handle.state in ("BOUND", "TERMINAL_OBSERVED_NOT_REAPED"),
            "only an unsettled owned handle may use refusal close")
    owned_fd = handle.pidfd
    handle.pidfd = None
    handle.state = "CLOSING"
    try:
        syscalls.close(owned_fd)
    except BaseException:
        handle.state = "PIDFD_CLOSE_UNKNOWN_UNSETTLED"
        handle.lifecycle["state"] = "PIDFD_CLOSE_UNKNOWN_UNSETTLED"
        raise
    if handle.state != "CLOSING":
        handle.lifecycle["state"] = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("close state changed reentrantly")
    handle.state = "CLOSED_UNSETTLED_UNKNOWN"
    handle.lifecycle["state"] = "CLOSED_UNSETTLED_UNKNOWN"
    return {"classification": "CLOSED_UNSETTLED_UNKNOWN", **LIMITS}


def close_reaped_pidfd(handle, syscalls, _consumer=None):
    _assert_handle_owner(handle, syscalls)
    _assert_consumer(handle, _consumer)
    if handle.state in ("OBSERVING", "REAPING", "CLOSING"):
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        handle.lifecycle["state"] = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("reentrant pidfd lifecycle operation")
    require(handle.state == "REAPED"
            and handle.lifecycle["state"] == "OWNED_LEADER_REAPED_ONLY",
            "only an exactly reaped handle may use terminal close")
    owned_fd = handle.pidfd
    handle.pidfd = None
    handle.state = "CLOSING"
    try:
        syscalls.close(owned_fd)
    except BaseException:
        handle.state = "REAPED_PIDFD_CLOSE_UNKNOWN"
        raise
    if handle.state != "CLOSING":
        handle.lifecycle["state"] = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("terminal close state changed reentrantly")
    handle.state = "REAPED_PIDFD_CLOSED"
    return {"classification": "OWNED_LEADER_REAPED_PIDFD_CLOSED",
            **LIMITS}


def _normalized_exit(receipt, handle):
    require(type(receipt) is ExitReceipt and receipt._key is _RECEIPT_KEY
            and handle.observation is receipt
            and receipt.lifecycle_token == handle.lifecycle["token"]
            and type(receipt.child_pid) is int
            and receipt.child_pid == handle.child["pid"]
            and type(receipt.child_starttime) is int
            and receipt.child_starttime == handle.child["starttime"]
            and type(receipt.pidfd) is int and receipt.pidfd == handle.pidfd,
            "cached exit receipt identity")
    return {"classification": receipt.classification,
            "lifecycle_token": receipt.lifecycle_token,
            "child_pid": receipt.child_pid,
            "child_starttime": receipt.child_starttime,
            "code": receipt.code, "status": receipt.status}


class _CaptureChildAdapter:
    def __init__(self, key, handle, syscalls):
        require(key is _ADAPTER_KEY, "capture adapter construction is private")
        self.handle = handle
        self.syscalls = syscalls
        self.cached = None
        self.state = "PRIMARY"
        self.capture_authority = None
        self.capture_consumer = None
        self.descendant_binding = None
        self.descendant_consumer = None
        self.reap_capability = None
        self.normal_completion_capability = None
        self.normal_completion_digest = None
        self.owner_thread = threading.current_thread()

    def __copy__(self):
        raise Refusal("capture child adapter is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("capture child adapter is noncopyable")

    def _poison(self, state):
        self.state = state
        self.handle.state = "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION"
        self.handle.lifecycle["state"] = "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION"

    def _begin(self, operation, allowed, authority=None, consumer=None,
               claimed=True):
        if self.state.startswith("UNKNOWN"):
            raise Refusal("capture adapter is permanently unknown")
        if self.state in ("CLAIMING", "IDENTIFYING", "CHECKING_PIDFD",
                          "OBSERVING", "VALIDATING", "HANDING_OFF", "REAPING",
                          "BINDING_NORMAL_COMPLETION"):
            self._poison("UNKNOWN_REENTRANT_OPERATION")
            raise Refusal("reentrant capture adapter operation")
        if (threading.current_thread() is not self.owner_thread
                or self.handle.capture_adapter is not self):
            self._poison("UNKNOWN_OWNER_CHANGED")
            raise Refusal("capture adapter ownership changed")
        if claimed and (self.capture_authority is None
                        or self.capture_consumer is None
                        or self.capture_authority is not authority
                        or self.capture_consumer is not consumer):
            self._poison("UNKNOWN_CAPTURE_CONSUMER_CHANGED")
            raise Refusal("capture adapter consumer changed")
        require(self.state in allowed, "capture adapter operation state")
        prior = self.state
        self.state = operation
        try:
            _assert_handle_owner(self.handle, self.syscalls)
            require(self.state == operation, "capture adapter state changed")
        except BaseException:
            self._poison("UNKNOWN_OWNER_OR_REENTRANT")
            raise
        return prior

    def _phase(self, condition, message):
        if not condition:
            self._poison("UNKNOWN_CAPTURE_PHASE")
            raise Refusal(message)

    def identity(self, authority=None, consumer=None):
        claimed = self.capture_consumer is not None
        prior = self._begin("IDENTIFYING", ("PRIMARY", "SETTLEMENT"),
                            authority, consumer, claimed)
        result = {"lifecycle_token": self.handle.lifecycle["token"],
                  "pid": self.handle.child["pid"],
                  "starttime": self.handle.child["starttime"],
                  "boot_id": self.handle.child["boot_id"]}
        require(self.state == "IDENTIFYING", "identity state changed")
        self.state = prior
        return result

    def claim_capture(self, authority, consumer):
        if authority is None or consumer is None:
            self._poison("UNKNOWN_EMPTY_CAPTURE_CLAIM")
            raise Refusal("capture claim capability")
        if self.capture_consumer is not None:
            self._poison("UNKNOWN_DUPLICATE_CAPTURE_CLAIM")
            raise Refusal("capture adapter is already attached")
        prior = self._begin("CLAIMING", ("PRIMARY",), claimed=False)
        self.capture_authority = authority
        self.capture_consumer = consumer
        require(self.state == "CLAIMING", "capture claim state changed")
        self.state = prior

    def pidfd_owned(self, authority, consumer):
        prior = self._begin("CHECKING_PIDFD", ("PRIMARY", "SETTLEMENT"),
                            authority, consumer)
        result = type(self.handle.pidfd) is int and self.handle.pidfd >= 0
        require(self.state == "CHECKING_PIDFD", "pidfd check state changed")
        self.state = prior
        return result

    def observe_or_cached(self, authority, consumer):
        prior = self._begin("OBSERVING", ("PRIMARY", "SETTLEMENT"),
                            authority, consumer)
        expected_capture_state = "BOUND" if prior == "PRIMARY" else "SETTLING"
        self._phase(getattr(consumer, "state", None) == expected_capture_state,
                    "capture consumer phase")
        if prior == "PRIMARY":
            self._phase(getattr(consumer, "monitor_active", None) is True,
                        "primary monitor is not active")
        if self.cached is not None:
            try:
                require(self.handle.state == "TERMINAL_OBSERVED_NOT_REAPED",
                        "cached terminal lifecycle state")
                result = _normalized_exit(self.cached, self.handle)
                require(self.state == "OBSERVING", "cached state changed")
            except BaseException:
                self._poison("UNKNOWN_CACHED_RECEIPT")
                raise
            self.state = prior
            return result
        try:
            receipt = observe_exit_nonblocking(
                self.handle, self.syscalls, _CAPTURE_KEY)
            require(self.state == "OBSERVING",
                    "capture adapter observe state changed")
            if receipt is not None:
                require(self.handle.observation is receipt,
                        "capture adapter lost exact receipt")
                self.cached = receipt
        except BaseException:
            self._poison("UNKNOWN_OBSERVE")
            raise
        self.state = prior
        return None if receipt is None else _normalized_exit(receipt, self.handle)

    def validate_cached(self, authority, consumer):
        prior = self._begin("VALIDATING", ("PRIMARY",), authority, consumer)
        self._phase(getattr(consumer, "state", None) == "BOUND"
                    and getattr(consumer, "monitor_active", None) is True,
                    "cached validation is outside primary monitor")
        try:
            if self.cached is None:
                require(self.handle.state == "BOUND",
                        "unobserved child lifecycle changed")
                result = None
            else:
                require(self.handle.state == "TERMINAL_OBSERVED_NOT_REAPED",
                        "cached child lifecycle changed")
                result = _normalized_exit(self.cached, self.handle)
            require(self.state == "VALIDATING", "cached validation state changed")
        except BaseException:
            self._poison("UNKNOWN_CACHED_VALIDATION")
            raise
        self.state = prior
        return result

    def assert_current_no_callback(self, authority, consumer):
        """Pure in-memory authority check; deliberately performs no syscall."""
        valid = (threading.current_thread() is self.owner_thread
                 and self.handle.capture_adapter is self
                 and self.capture_authority is authority
                 and self.capture_consumer is consumer
                 and self.state == "PRIMARY"
                 and not self.handle.lifecycle["state"].startswith("UNKNOWN"))
        if self.cached is None:
            valid = valid and self.handle.state == "BOUND"
            result = None
        else:
            valid = (valid
                     and self.handle.state == "TERMINAL_OBSERVED_NOT_REAPED"
                     and self.handle.observation is self.cached)
            result = (_normalized_exit(self.cached, self.handle)
                      if valid else None)
        if not valid:
            self._poison("UNKNOWN_INTERNAL_AUTHORITY_CHECK")
            raise Refusal("capture adapter internal authority changed")
        return result

    def enter_settlement(self, authority, consumer):
        self._begin("HANDING_OFF", ("PRIMARY",), authority, consumer)
        self._phase(getattr(consumer, "state", None) == "SETTLING"
                    and getattr(consumer, "primary_failure", None) is not None
                    and getattr(consumer, "settlement_used", None) is True
                    and getattr(consumer, "monitor_active", None) is False,
                    "capture consumer is not settling a primary failure")
        require(self.state == "HANDING_OFF", "capture handoff state changed")
        self.state = "SETTLEMENT"

    def bind_normal_completion(self, authority, consumer, capability, digest):
        try:
            capture_module = importlib.import_module("prelive_capture_settlement")
            require(threading.current_thread() is self.owner_thread
                    and self.state == "PRIMARY"
                    and self.capture_authority is authority
                    and self.capture_consumer is consumer
                    and self.handle.capture_adapter is self
                    and type(capability)
                    is capture_module._NormalCompletionCapability
                    and getattr(consumer, "state", None) == "BOUND"
                    and getattr(consumer, "monitor_active", None) is True
                    and getattr(consumer, "primary_failure", None) is None
                    and getattr(consumer, "normal_completion_capability", None)
                    is capability
                    and capability.capture_handle is consumer
                    and capability.child_adapter is self
                    and type(self.cached) is ExitReceipt
                    and capability.cached_receipt is self.cached
                    and self.handle.observation is self.cached
                    and type(digest) is str and len(digest) == 64
                    and capability.completion_digest == digest
                    and capture_module._completion_digest(capability.completion)
                    == digest
                    and self.normal_completion_capability is None,
                    "exact normal completion binding")
            self.state = "BINDING_NORMAL_COMPLETION"
            self.normal_completion_capability = capability
            self.normal_completion_digest = digest
            require(self.state == "BINDING_NORMAL_COMPLETION",
                    "normal completion binding state changed")
            self.state = "PRIMARY"
        except BaseException:
            self._poison("UNKNOWN_NORMAL_COMPLETION_BIND")
            raise

    def enter_completed_settlement(self, authority, consumer, capability):
        self._begin("HANDING_OFF", ("PRIMARY",), authority, consumer)
        self._phase(getattr(consumer, "state", None) == "SETTLING"
                    and getattr(consumer, "primary_failure", None) is None
                    and getattr(consumer, "settlement_used", None) is True
                    and getattr(consumer, "monitor_active", None) is False
                    and getattr(consumer, "normal_completion_capability", None)
                    is capability
                    and getattr(capability, "used", None) is True
                    and getattr(capability, "capture_handle", None) is consumer
                    and getattr(capability, "child_adapter", None) is self
                    and getattr(capability, "cached_receipt", None) is self.cached,
                    "capture consumer lacks exact normal completion")
        self._phase(capability is self.normal_completion_capability
                    and type(self.normal_completion_digest) is str
                    and getattr(capability, "completion_digest", None)
                    == self.normal_completion_digest
                    and getattr(consumer, "normal_handoff_active", None) is True
                    and getattr(consumer, "normal_handoff_poisoned", None) is False,
                    "normal completion claim is not active")
        require(self.state == "HANDING_OFF",
                "completed capture handoff state changed")
        self.state = "SETTLEMENT"

    def reap_cached_once(self, authority, consumer):
        self._begin("REAPING", ("SETTLEMENT",), authority, consumer)
        self._phase(getattr(consumer, "state", None) == "SETTLING"
                    and getattr(consumer, "settlement_used", None) is True,
                    "capture consumer is not in settlement")
        require(self.cached is not None, "settlement requires cached terminal receipt")
        receipt = self.cached
        require(self.handle.observation is receipt,
                "settlement exact receipt changed")
        try:
            result = reap_observed_exit(
                self.handle, receipt, self.syscalls, _CAPTURE_KEY)
            require(self.state == "REAPING", "capture adapter reap state changed")
        except BaseException:
            self._poison("UNKNOWN_REAP")
            raise
        if self.descendant_binding is not None:
            self.reap_capability = _ExactLeaderReapCapability(
                _LEADER_REAP_CAP_KEY, self.descendant_binding, self,
                self.handle, receipt)
        self.state = "REAPED"
        return {**result,
                "lifecycle_token": receipt.lifecycle_token,
                "child_pid": receipt.child_pid,
                "child_starttime": receipt.child_starttime}

    def enroll_descendant(self, key, binding, consumer):
        if key is not _DESCENDANT_KEY or binding is None or consumer is None:
            self._poison("UNKNOWN_DESCENDANT_ENROLLMENT")
            raise Refusal("descendant enrollment capability")
        if (threading.current_thread() is not self.owner_thread
                or self.state != "PRIMARY"
                or self.cached is not None
                or self.descendant_binding is not None
                or self.capture_consumer is not consumer
                or getattr(consumer, "child_adapter", None) is not self
                or getattr(consumer, "state", None) != "BOUND"
                or getattr(consumer, "monitor_active", None) is not False
                or self.handle.state != "BOUND"):
            self._poison("UNKNOWN_DESCENDANT_ENROLLMENT")
            raise Refusal("descendant enrollment phase")
        self.descendant_binding = binding
        self.descendant_consumer = consumer

    def take_exact_leader_reap(self, key, binding, consumer):
        if (key is not _DESCENDANT_KEY
                or threading.current_thread() is not self.owner_thread
                or self.state != "REAPED"
                or self.descendant_binding is not binding
                or self.descendant_consumer is not consumer
                or self.capture_consumer is not consumer
                or getattr(consumer, "child_adapter", None) is not self
                or self.reap_capability is None
                or self.reap_capability.used
                or self.reap_capability.binding is not binding
                or self.reap_capability.adapter is not self
                or self.reap_capability.handle is not self.handle
                or self.reap_capability.receipt is not self.cached
                or self.handle.state != "REAPED"
                or self.handle.lifecycle["state"] != "OWNED_LEADER_REAPED_ONLY"
                or self.handle.observation is not self.cached):
            self._poison("UNKNOWN_DESCENDANT_HANDOFF")
            raise Refusal("exact leader reap handoff unavailable")
        self.state = "DESCENDANT_HANDOFF"
        return self.reap_capability

    def validate_transferred_reap(self, key, capability, binding, consumer):
        valid = (key is _DESCENDANT_KEY
                 and threading.current_thread() is self.owner_thread
                 and self.state == "DESCENDANT_HANDOFF"
                 and self.descendant_binding is binding
                 and self.descendant_consumer is consumer
                 and self.capture_consumer is consumer
                 and getattr(consumer, "child_adapter", None) is self
                 and capability is self.reap_capability
                 and type(capability) is _ExactLeaderReapCapability
                 and capability.used is True
                 and capability.binding is binding
                 and capability.adapter is self
                 and capability.handle is self.handle
                 and capability.fork_origin is self.handle.lifecycle["origin"]
                 and capability.receipt is self.cached
                 and capability.owner_thread is self.owner_thread
                 and self.handle.capture_adapter is self
                 and self.handle.state == "REAPED"
                 and self.handle.lifecycle["state"] == "OWNED_LEADER_REAPED_ONLY"
                 and self.handle.observation is self.cached)
        if not valid:
            self._poison("UNKNOWN_TRANSFERRED_REAP")
            raise Refusal("transferred leader reap authority changed")


def bind_capture_child_adapter(handle, syscalls):
    """Claim one BOUND owned-child handle for capture through settlement."""
    _assert_handle_owner(handle, syscalls)
    require(handle.state == "BOUND", "capture adapter requires bound child")
    if handle.capture_adapter is not None:
        handle.state = "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION"
        handle.lifecycle["state"] = "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION"
        raise Refusal("owned child already has a capture adapter")
    adapter = _CaptureChildAdapter(_ADAPTER_KEY, handle, syscalls)
    handle.capture_adapter = adapter
    return adapter


def enroll_descendant_bridge(adapter, binding, capture_consumer):
    """Internal source-stage enrollment; no serialized authority accepted."""
    require(type(adapter) is _CaptureChildAdapter,
            "exact capture adapter required")
    adapter.enroll_descendant(_DESCENDANT_KEY, binding, capture_consumer)


def take_exact_leader_reap(adapter, binding, capture_consumer):
    """Transfer the one opaque post-reap capability without another wait."""
    require(type(adapter) is _CaptureChildAdapter,
            "exact capture adapter required")
    return adapter.take_exact_leader_reap(
        _DESCENDANT_KEY, binding, capture_consumer)


def validate_transferred_leader_reap(adapter, capability, binding,
                                      capture_consumer):
    require(type(adapter) is _CaptureChildAdapter,
            "exact capture adapter required")
    adapter.validate_transferred_reap(
        _DESCENDANT_KEY, capability, binding, capture_consumer)
