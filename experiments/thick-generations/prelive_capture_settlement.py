#!/usr/bin/python3
"""Source-staged bounded capture core; no process or storage wiring."""

import copy
import errno
import fcntl
import hashlib
import importlib
import json
import os
import re
import select
import signal
import stat
import threading
import time
from pathlib import Path


_PIPE_KEY = object()
MAX_READ = 65536
MAX_READS_PER_STREAM_TURN = 4
ALLOWED_MASK = select.EPOLLIN | select.EPOLLHUP | select.EPOLLERR
_MONITOR_KEY = object()
_SETTLE_KEY = object()
_SETTLEMENT_HANDLE_KEY = object()
_DESCENDANT_SETTLEMENT_KEY = object()
_NORMAL_COMPLETION_KEY = object()
_CHILD_ADAPTER_KEY = object()
MAX_POLL_MS = 100
MAX_STAGNANT_TURNS = 8
RESULT_LIMITS = {"runtime_authorized": False, "storage_authorized": False,
                 "postcondition_verified": False,
                 "leader_reaped": False, "descendants_qualified": False}


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def _integer(value, minimum=0):
    return type(value) is int and value >= minimum


def _identity(value):
    keys = {"fd", "dev", "inode", "mode", "flags"}
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == keys, "pipe identity schema")
    require(_integer(value["fd"], 3) and _integer(value["dev"])
            and _integer(value["inode"], 1) and _integer(value["mode"])
            and _integer(value["flags"]), "pipe identity types")
    require(stat.S_ISFIFO(value["mode"]), "capture FD is not a pipe")
    require(value["flags"] & os.O_NONBLOCK, "capture pipe is blocking")
    require(value["flags"] & os.O_ACCMODE == os.O_RDONLY,
            "capture pipe is not read-only")
    return copy.deepcopy(value)


def _owner(value):
    keys = {"pid", "starttime", "boot_id"}
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == keys, "capture owner schema")
    require(_integer(value["pid"], 1) and _integer(value["starttime"], 1)
            and type(value["boot_id"]) is str
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             value["boot_id"]) is not None, "capture owner identity")
    return copy.deepcopy(value)


def _child(value):
    keys = {"lifecycle_token", "pid", "starttime", "boot_id"}
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == keys, "capture child schema")
    require(type(value["lifecycle_token"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", value["lifecycle_token"]) is not None
            and _integer(value["pid"], 1) and _integer(value["starttime"], 1)
            and type(value["boot_id"]) is str
            and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                             value["boot_id"]) is not None,
            "capture child identity")
    return copy.deepcopy(value)


class LinuxCaptureSyscalls:
    @staticmethod
    def owner_identity():
        pid = os.getpid()
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        end = raw.rfind(")")
        fields = raw[end + 2:].split()
        boot = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip().lower()
        return {"pid": pid, "starttime": int(fields[19]), "boot_id": boot}

    @staticmethod
    def fd_identity(fd):
        info = os.fstat(fd)
        return {"fd": fd, "dev": info.st_dev, "inode": info.st_ino,
                "mode": info.st_mode, "flags": fcntl.fcntl(fd, fcntl.F_GETFL)}

    @staticmethod
    def epoll_create():
        return select.epoll()

    @staticmethod
    def epoll_register(poller, fd, mask):
        poller.register(fd, mask)

    @staticmethod
    def epoll_unregister(poller, fd):
        poller.unregister(fd)

    @staticmethod
    def epoll_close(poller):
        poller.close()

    @staticmethod
    def read(fd, maximum):
        return os.read(fd, maximum)

    @staticmethod
    def monotonic_ns():
        return time.monotonic_ns()

    @staticmethod
    def epoll_poll(poller, timeout_ms):
        require(_integer(timeout_ms, 1), "epoll timeout")
        return poller.poll(timeout_ms / 1000.0)

    @staticmethod
    def wait_all_nohang_wnowait():
        return os.waitid(os.P_ALL, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)


class _PipeOrigin:
    def __init__(self, key, owner, child, identities):
        require(key is _PIPE_KEY, "pipe origin construction is private")
        self.owner = _owner(owner)
        self.child = _child(child)
        self.identities = copy.deepcopy(identities)
        self.claimed = False

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


def _record_capture_pipes(key, owner, child, stdout_identity, stderr_identity):
    """Future fork parent records its two read ends here; no public adopt API."""
    require(key is _PIPE_KEY, "pipe record construction is private")
    identities = {"stdout": _identity(stdout_identity),
                  "stderr": _identity(stderr_identity)}
    require(identities["stdout"]["fd"] != identities["stderr"]["fd"],
            "capture pipe FD alias")
    require((identities["stdout"]["dev"], identities["stdout"]["inode"])
            != (identities["stderr"]["dev"], identities["stderr"]["inode"]),
            "capture pipe identity alias")
    return _PipeOrigin(key, owner, child, identities)


class _Stream:
    def __init__(self, role, identity, limit):
        self.role = role
        self.identity = copy.deepcopy(identity)
        self.limit = limit
        self.stored = bytearray()
        self.observed_bytes = 0
        self.eof = False
        self.truncated = False

    def add(self, data):
        require(type(data) is bytes and data != b"", "capture data chunk")
        self.observed_bytes += len(data)
        available = max(0, self.limit - len(self.stored))
        self.stored.extend(data[:available])
        if len(data) > available:
            self.truncated = True

    def receipt(self):
        complete = self.eof and not self.truncated
        return {"role": self.role, "pipe_identity": copy.deepcopy(self.identity),
                "eof": self.eof, "truncated": self.truncated,
                "stored_bytes": len(self.stored),
                "observed_bytes": self.observed_bytes,
                "prefix_sha256": hashlib.sha256(self.stored).hexdigest(),
                "digest_scope": "FULL_CAPTURE" if complete else "CAPTURED_PREFIX"}


class CaptureHandle:
    def __init__(self, key, origin, poller, limit):
        require(key is _PIPE_KEY, "capture handle construction is private")
        self.origin = origin
        self.poller = poller
        self.owner_thread = threading.current_thread()
        self.state = "BOUND"
        self.streams = {role: _Stream(role, identity, limit)
                        for role, identity in origin.identities.items()}
        self.fd_roles = {stream.identity["fd"]: role
                         for role, stream in self.streams.items()}
        self.monitor_active = False
        self.primary_failure = None
        self.settlement_used = False
        self.child_adapter = None
        self.child_adapter_type = None
        self.execution_deadline_ns = None
        self.normal_completion = None
        self.normal_completion_capability = None
        self.normal_handoff_active = False
        self.normal_handoff_poisoned = False

    def __copy__(self):
        raise Refusal("capture handle is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("capture handle is noncopyable")


def bind_capture_pipes(origin, limit, syscalls):
    require(type(origin) is _PipeOrigin and origin.claimed is False,
            "fresh internal capture-pipe origin required")
    require(_integer(limit, 1) and limit <= 1048576, "capture limit")
    origin.claimed = True
    current = _owner(syscalls.owner_identity())
    require(current == origin.owner, "capture owner changed")
    poller = None
    registered = []
    try:
        observed = {role: _identity(syscalls.fd_identity(identity["fd"]))
                    for role, identity in origin.identities.items()}
        require(observed == origin.identities, "capture pipe identity changed")
        poller = syscalls.epoll_create()
        for role in ("stdout", "stderr"):
            fd = origin.identities[role]["fd"]
            syscalls.epoll_register(poller, fd, ALLOWED_MASK)
            registered.append(fd)
    except BaseException:
        if poller is not None:
            for fd in reversed(registered):
                try:
                    syscalls.epoll_unregister(poller, fd)
                except BaseException:
                    pass
            try:
                syscalls.epoll_close(poller)
            except BaseException:
                pass
        raise
    return CaptureHandle(_PIPE_KEY, origin, poller, limit)


def attach_child_adapter(handle, child_adapter, syscalls):
    """Attach exactly one already-claimed owned-child adapter before monitor."""
    _assert_owner(handle, syscalls)
    owned_module = importlib.import_module("prelive_owned_child")
    require(type(child_adapter) is owned_module._CaptureChildAdapter,
            "exact owned child adapter required")
    require(handle.state == "BOUND" and handle.child_adapter is None
            and handle.monitor_active is False, "capture adapter attach state")
    handle.state = "ATTACHING_CHILD_ADAPTER"
    handle.child_adapter = child_adapter
    try:
        child_adapter.claim_capture(_CHILD_ADAPTER_KEY, handle)
        identity = _child(child_adapter.identity(_CHILD_ADAPTER_KEY, handle))
        require(handle.state == "ATTACHING_CHILD_ADAPTER"
                and handle.child_adapter is child_adapter,
                "capture adapter attach changed reentrantly")
        require(identity == handle.origin.child, "capture adapter child mismatch")
        handle.child_adapter_type = type(child_adapter)
    except BaseException:
        handle.state = "UNKNOWN_CHILD_ADAPTER_ATTACH"
        raise
    handle.state = "BOUND"


def _assert_owner(handle, syscalls, _key=None):
    require(type(handle) is CaptureHandle, "capture handle")
    if handle.state.startswith("UNKNOWN") or handle.state == "CLOSED":
        raise Refusal("capture handle is permanently unavailable")
    if (handle.state in ("DRAINING", "CLOSING")
            or (handle.state == "SETTLING" and _key is not _SETTLE_KEY)):
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("reentrant capture operation")
    try:
        current = _owner(syscalls.owner_identity())
    except BaseException:
        handle.state = "UNKNOWN_OWNER"
        raise
    if threading.current_thread() is not handle.owner_thread or current != handle.origin.owner:
        handle.state = "UNKNOWN_OWNER"
        raise Refusal("capture owner changed")


def _verify_pipes(handle, syscalls):
    try:
        observed = {role: _identity(syscalls.fd_identity(stream.identity["fd"]))
                    for role, stream in handle.streams.items()}
    except BaseException:
        handle.state = "UNKNOWN_PIPE_IDENTITY"
        raise
    expected = {role: stream.identity for role, stream in handle.streams.items()}
    if observed != expected:
        handle.state = "UNKNOWN_PIPE_IDENTITY"
        raise Refusal("capture pipe identity changed")


def drain_ready_turn(handle, events, syscalls, _key=None):
    """Drain one fair level-triggered turn; HUP alone is never EOF proof."""
    _assert_owner(handle, syscalls, _key)
    if handle.monitor_active and _key is not _MONITOR_KEY:
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("capture monitor owns the drain operation")
    expected_state = "SETTLING" if _key is _SETTLE_KEY else "BOUND"
    require(handle.state == expected_state, "capture busy state")
    require(type(events) is list, "epoll event list")
    handle.state = "DRAINING"
    try:
        _verify_pipes(handle, syscalls)
        seen = set()
        for item in events:
            require(type(item) is tuple and len(item) == 2, "epoll event schema")
            fd, mask = item
            require(_integer(fd, 3) and type(mask) is int, "epoll event types")
            require(fd in handle.fd_roles and fd not in seen,
                    "foreign or duplicate event FD")
            require(mask != 0 and mask & ~ALLOWED_MASK == 0, "unknown epoll mask")
            require(mask & select.EPOLLERR == 0, "epoll error event")
            seen.add(fd)
        events = tuple((fd, mask) for fd, mask in events)
        seen.clear()
        for item in events:
            fd, mask = item
            seen.add(fd)
            stream = handle.streams[handle.fd_roles[fd]]
            require(not stream.eof, "event after stream EOF")
            for unused in range(MAX_READS_PER_STREAM_TURN):
                try:
                    _verify_pipes(handle, syscalls)
                    data = syscalls.read(fd, MAX_READ)
                except BlockingIOError as exc:
                    require(exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK),
                            "unexpected nonblocking read error")
                    break
                require(type(data) is bytes and len(data) <= MAX_READ,
                        "invalid bounded read result")
                if data == b"":
                    stream.eof = True
                    syscalls.epoll_unregister(handle.poller, fd)
                    break
                stream.add(data)
        _verify_pipes(handle, syscalls)
        require(handle.state == "DRAINING", "capture state changed reentrantly")
    except BaseException:
        handle.state = "UNKNOWN_DRAIN"
        raise
    handle.state = expected_state
    return {role: stream.receipt() for role, stream in handle.streams.items()}


def _terminal_receipt(value, expected_child):
    keys = {"classification", "lifecycle_token", "child_pid",
            "child_starttime", "code", "status"}
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == keys, "terminal receipt schema")
    require(type(value["classification"]) is str
            and value["classification"] in (
                "EXITED_ZERO", "EXITED_NONZERO", "SIGNALED")
            and type(value["lifecycle_token"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", value["lifecycle_token"]) is not None
            and _integer(value["child_pid"], 1)
            and _integer(value["child_starttime"], 1)
            and type(value["code"]) is int and type(value["status"]) is int,
            "terminal receipt values")
    require((value["lifecycle_token"], value["child_pid"], value["child_starttime"])
            == (expected_child["lifecycle_token"], expected_child["pid"],
                expected_child["starttime"]), "terminal receipt child mismatch")
    if value["classification"] == "EXITED_ZERO":
        require(value["code"] == os.CLD_EXITED and value["status"] == 0,
                "exit-zero receipt mismatch")
    elif value["classification"] == "EXITED_NONZERO":
        require(value["code"] == os.CLD_EXITED and 1 <= value["status"] <= 255,
                "nonzero receipt mismatch")
    else:
        require(value["code"] in (os.CLD_KILLED, os.CLD_DUMPED)
                and 1 <= value["status"] < signal.NSIG, "signal receipt mismatch")
    return copy.deepcopy(value)


class _NormalCompletionCapability:
    def __init__(self, key, handle, adapter, cached_receipt, completion,
                 deadline_ns, completed_at_ns):
        require(key is _NORMAL_COMPLETION_KEY,
                "normal completion capability construction is private")
        self.capture_handle = handle
        self.child_adapter = adapter
        self.cached_receipt = cached_receipt
        self.completion = copy.deepcopy(completion)
        self.completion_digest = hashlib.sha256(json.dumps(
            self.completion, sort_keys=True, separators=(",", ":"),
            ensure_ascii=True).encode("ascii")).hexdigest()
        self.execution_deadline_ns = deadline_ns
        self.completed_at_ns = completed_at_ns
        self.owner = copy.deepcopy(handle.origin.owner)
        self.owner_thread = threading.current_thread()
        self.used = False

    def __copy__(self):
        raise Refusal("normal completion capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("normal completion capability is noncopyable")


def _capture_until_deadline(handle, deadline_ns, terminal_observer, syscalls,
                             terminal_validator=None,
                             terminal_internal_validator=None,
                             attached_adapter=None, minimum_ns=None):
    """Observe streams and leader until one absolute deadline; never reap."""
    _assert_owner(handle, syscalls)
    if handle.monitor_active:
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("reentrant capture monitor")
    require(handle.state == "BOUND" and handle.monitor_active is False,
            "capture monitor state")
    require(type(deadline_ns) is int and deadline_ns > 0, "absolute deadline")
    require(minimum_ns is None
            or (type(minimum_ns) is int
                and 0 <= minimum_ns < deadline_ns),
            "capture minimum clock watermark")
    require(callable(terminal_observer), "terminal observer")
    handle.monitor_active = True
    require(handle.execution_deadline_ns is None,
            "capture execution deadline already assigned")
    handle.execution_deadline_ns = deadline_ns
    observation = None
    last = minimum_ns
    stagnant = 0
    def sample_clock():
        nonlocal last, stagnant
        now = syscalls.monotonic_ns()
        require(handle.monitor_active is True and handle.state == "BOUND",
                "capture monitor changed during clock observation")
        require(type(now) is int and now >= 0, "monotonic clock type")
        if last is not None:
            require(now >= last, "monotonic clock regressed")
            stagnant = stagnant + 1 if now == last else 0
            require(stagnant <= MAX_STAGNANT_TURNS, "monotonic clock stalled")
        last = now
        return now
    def validate_internal():
        nonlocal observation
        if terminal_internal_validator is None:
            return
        validated = terminal_internal_validator()
        require(handle.monitor_active is True and handle.state == "BOUND",
                "capture monitor changed during internal authority validation")
        if validated is not None:
            validated = _terminal_receipt(validated, handle.origin.child)
            require(observation is None or observation == validated,
                    "internal cached terminal validation changed")
            observation = validated
    try:
        while True:
            now = sample_clock()
            validate_internal()
            if terminal_validator is not None:
                validated = terminal_validator()
                require(handle.monitor_active is True and handle.state == "BOUND",
                        "capture monitor changed during terminal validation")
                if validated is not None:
                    validated = _terminal_receipt(validated, handle.origin.child)
                    require(observation is None or observation == validated,
                            "cached terminal validation changed")
                    observation = validated
                now = sample_clock()
                validate_internal()
            receipts = {role: stream.receipt()
                        for role, stream in handle.streams.items()}
            if any(receipt["truncated"] for receipt in receipts.values()):
                handle.state = "CAPTURE_TRUNCATED_UNSETTLED"
                result = {"classification": "CAPTURE_TRUNCATED_UNSETTLED",
                        "leader_observation": observation,
                        "streams": receipts, **RESULT_LIMITS}
                handle.primary_failure = copy.deepcopy(result)
                return result
            if now >= deadline_ns:
                handle.state = "EXECUTION_TIMEOUT_UNSETTLED"
                result = {"classification": "EXECUTION_TIMEOUT_UNSETTLED",
                        "leader_observation": observation,
                        "streams": receipts, **RESULT_LIMITS}
                handle.primary_failure = copy.deepcopy(result)
                return result
            if observation is None:
                candidate = terminal_observer()
                require(handle.monitor_active is True and handle.state == "BOUND",
                        "capture monitor changed during terminal observation")
                if candidate is not None:
                    observation = _terminal_receipt(candidate, handle.origin.child)
                now = sample_clock()
                validate_internal()
                if now >= deadline_ns:
                    handle.state = "EXECUTION_TIMEOUT_UNSETTLED"
                    result = {"classification": "EXECUTION_TIMEOUT_UNSETTLED",
                            "leader_observation": observation,
                            "streams": receipts, **RESULT_LIMITS}
                    handle.primary_failure = copy.deepcopy(result)
                    return result
            if (observation is not None
                    and all(receipt["eof"] for receipt in receipts.values())):
                validate_internal()
                result = {
                    "classification": "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED",
                    "leader_observation": observation,
                    "streams": receipts, **RESULT_LIMITS}
                if attached_adapter is not None:
                    require(set(receipts) == {"stdout", "stderr"}
                            and all(item["eof"] is True
                                    and item["truncated"] is False
                                    for item in receipts.values())
                            and attached_adapter is handle.child_adapter
                            and attached_adapter.cached is not None,
                            "exact attached normal completion")
                    handle.normal_completion = copy.deepcopy(result)
                    handle.normal_completion_capability = _NormalCompletionCapability(
                        _NORMAL_COMPLETION_KEY, handle, attached_adapter,
                        attached_adapter.cached, result, deadline_ns, now)
                    attached_adapter.bind_normal_completion(
                        _CHILD_ADAPTER_KEY, handle,
                        handle.normal_completion_capability,
                        handle.normal_completion_capability.completion_digest)
                    require(attached_adapter.state == "PRIMARY"
                            and attached_adapter.normal_completion_capability
                            is handle.normal_completion_capability,
                            "normal completion binding changed")
                handle.state = "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED"
                return result
            remaining = deadline_ns - now
            timeout_ms = min(MAX_POLL_MS, max(1, (remaining + 999999) // 1000000))
            try:
                events = syscalls.epoll_poll(handle.poller, timeout_ms)
            except InterruptedError:
                continue
            require(handle.monitor_active is True and handle.state == "BOUND",
                    "capture monitor changed during poll")
            require(type(events) is list, "epoll poll result")
            drain_ready_turn(handle, events, syscalls, _MONITOR_KEY)
            require(handle.monitor_active is True and handle.state == "BOUND",
                    "capture monitor state changed")
    except BaseException:
        if not handle.state.startswith("UNKNOWN"):
            handle.state = "UNKNOWN_MONITOR"
        raise
    finally:
        handle.monitor_active = False


def capture_until_deadline(handle, deadline_ns, terminal_observer, syscalls):
    """Legacy source-stage observer path; cannot use an attached adapter."""
    require(type(handle) is CaptureHandle and handle.child_adapter is None,
            "attached child adapter requires its owned monitor path")
    return _capture_until_deadline(handle, deadline_ns, terminal_observer, syscalls)


def capture_attached_until_deadline(handle, deadline_ns, syscalls,
                                    minimum_ns=None):
    """Monitor through the single adapter attached before the first observe."""
    require(type(handle) is CaptureHandle and handle.child_adapter is not None,
            "owned child adapter is not attached")
    adapter = handle.child_adapter
    return _capture_until_deadline(
        handle, deadline_ns,
        lambda: adapter.observe_or_cached(_CHILD_ADAPTER_KEY, handle), syscalls,
        lambda: adapter.validate_cached(_CHILD_ADAPTER_KEY, handle),
        lambda: adapter.assert_current_no_callback(
            _CHILD_ADAPTER_KEY, handle), adapter, minimum_ns)


class _DescendantSettlementCapability:
    def __init__(self, key, capture_handle, child_adapter, receipt, deadline_ns,
                 owner, normal_completion_capability=None):
        require(key is _DESCENDANT_SETTLEMENT_KEY,
                "descendant settlement capability construction is private")
        self.capture_handle = capture_handle
        self.child_adapter = child_adapter
        self.primary_failure = copy.deepcopy(receipt["primary_failure"])
        self.primary_completion = copy.deepcopy(receipt["primary_completion"])
        self.settlement_kind = receipt["settlement_kind"]
        self.normal_completion_capability = normal_completion_capability
        self.finished_ns = receipt["finished_ns"]
        self.deadline_ns = deadline_ns
        self.owner = copy.deepcopy(owner)
        self.owner_thread = threading.current_thread()
        self.used = False

    def __copy__(self):
        raise Refusal("descendant settlement capability is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("descendant settlement capability is noncopyable")


class SettlementHandle:
    def __init__(self, key, receipt, child_adapter, deadline_ns, leader_reaped,
                 owner, capture_handle, normal_completion_capability=None):
        require(key is _SETTLEMENT_HANDLE_KEY, "settlement handle construction is private")
        self.receipt = copy.deepcopy(receipt)
        self.child_adapter = child_adapter
        self.deadline_ns = deadline_ns
        self.leader_reaped = leader_reaped
        self.owner = _owner(owner)
        self.probe_used = False
        self.owner_thread = threading.current_thread()
        self.capture_handle = capture_handle
        self.normal_completion_capability = normal_completion_capability
        self.descendant_capability = None
        if (receipt.get("cleanup_outcome") == "LEADER_REAPED_STREAMS_EOF"
                and leader_reaped is True
                and set(receipt.get("streams", {})) == {"stdout", "stderr"}
                and all(item.get("eof") is True
                        for item in receipt["streams"].values())
                and ((receipt.get("settlement_kind") == "PRIMARY_FAILURE"
                      and receipt.get("primary_failure") is not None
                      and receipt.get("primary_completion") is None)
                     or (receipt.get("settlement_kind") == "NORMAL_COMPLETION"
                         and receipt.get("primary_failure") is None
                         and receipt.get("primary_completion") is not None))):
            self.descendant_capability = _DescendantSettlementCapability(
                _DESCENDANT_SETTLEMENT_KEY, capture_handle, child_adapter,
                receipt, deadline_ns, self.owner,
                normal_completion_capability)

    def __copy__(self):
        raise Refusal("settlement handle is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("settlement handle is noncopyable")


def _remaining_resources(handle, pidfd_owned, leader_reaped):
    return {
        "leader_may_survive": not leader_reaped,
        "pidfd_owned": pidfd_owned,
        "epoll_owned": handle.poller is not None,
        # EOF is stream evidence, never evidence that this API closed a borrowed FD.
        "stdout_read_fd_owned": True,
        "stderr_read_fd_owned": True,
    }


def _settlement_receipt(handle, pidfd_owned, deadline_ns, started, finished,
                        outcome, observation, reap, error,
                        settlement_kind="PRIMARY_FAILURE",
                        primary_completion=None):
    streams = {role: stream.receipt() for role, stream in handle.streams.items()}
    leader_reaped = reap is not None
    return {
        "schema": 1,
        "classification": "PASSIVE_SETTLEMENT_OBSERVATION_ONLY",
        "owner": copy.deepcopy(handle.origin.owner),
        "child": copy.deepcopy(handle.origin.child),
        "primary_failure": copy.deepcopy(handle.primary_failure),
        "primary_completion": copy.deepcopy(primary_completion),
        "settlement_kind": settlement_kind,
        "cleanup_deadline_ns": deadline_ns,
        "started_ns": started,
        "finished_ns": finished,
        "cleanup_outcome": outcome,
        "leader_observation": copy.deepcopy(observation),
        "leader_reap": copy.deepcopy(reap),
        "streams": streams,
        "remaining_resources": _remaining_resources(
            handle, pidfd_owned, leader_reaped),
        "error": error,
        "runtime_authorized": False,
        "storage_authorized": False,
        "postcondition_verified": False,
        "descendants_qualified": False,
    }


def _validate_reap(value, expected_child, observation):
    keys = {"classification", "lifecycle_token", "child_pid", "child_starttime",
            "exit", "status", "runtime_authorized", "storage_authorized",
            "postcondition_verified", "descendants_qualified", "capture_qualified"}
    require(type(value) is dict and set(value) == keys, "leader reap schema")
    require(value["classification"] == "OWNED_LEADER_REAPED_ONLY"
            and type(value["lifecycle_token"]) is str
            and type(value["child_pid"]) is int
            and type(value["child_starttime"]) is int
            and type(value["exit"]) is str
            and (value["lifecycle_token"], value["child_pid"], value["child_starttime"])
            == (expected_child["lifecycle_token"], expected_child["pid"],
                expected_child["starttime"])
            and value["exit"] == observation["classification"]
            and type(value["status"]) is int
            and value["status"] == observation["status"]
            and all(value[key] is False for key in
                    ("runtime_authorized", "storage_authorized",
                     "postcondition_verified", "descendants_qualified",
                     "capture_qualified")), "leader reap identity")
    return copy.deepcopy(value)


def _settle_passively(handle, child_adapter, cleanup_deadline_ns, syscalls,
                      attached):
    """Single-use bounded observation/drain/reap; never signals or closes FDs."""
    _assert_owner(handle, syscalls)
    require(handle.state in ("EXECUTION_TIMEOUT_UNSETTLED",
                             "CAPTURE_TRUNCATED_UNSETTLED"),
            "passive settlement requires a primary failure")
    require(handle.primary_failure is not None and handle.settlement_used is False,
            "passive settlement is single-use")
    require(type(cleanup_deadline_ns) is int and cleanup_deadline_ns > 0,
            "cleanup deadline")
    require((attached and handle.child_adapter is child_adapter)
            or (not attached and handle.child_adapter is None),
            "settlement child adapter authority")
    handle.settlement_used = True
    handle.state = "SETTLING"
    observation = copy.deepcopy(handle.primary_failure["leader_observation"])
    reap = None
    last = None
    stagnant = 0
    started = None
    pidfd_owned = "UNKNOWN"
    stage = "clock"
    adapter_confirmed = False
    def clock():
        nonlocal last, stagnant, started
        now = syscalls.monotonic_ns()
        require(handle.state == "SETTLING", "settlement state changed")
        require(type(now) is int and now >= 0, "settlement clock type")
        if last is not None:
            require(now >= last, "settlement clock regressed")
            stagnant = stagnant + 1 if now == last else 0
            require(stagnant <= MAX_STAGNANT_TURNS, "settlement clock stalled")
        last = now
        if started is None:
            started = now
        return now
    def finish_deadline(now):
        receipt = _settlement_receipt(
            handle, pidfd_owned, cleanup_deadline_ns, started, now,
            "CLEANUP_DEADLINE_UNSETTLED", observation, reap, None)
        handle.state = "SETTLEMENT_FINISHED"
        return SettlementHandle(_SETTLEMENT_HANDLE_KEY, receipt,
                                child_adapter, cleanup_deadline_ns,
                                reap is not None, handle.origin.owner, handle)
    try:
        stage = "child_identity"
        identity = (child_adapter.identity(_CHILD_ADAPTER_KEY, handle) if attached
                    else child_adapter.identity())
        require(_child(identity) == handle.origin.child,
                "settlement child identity mismatch")
        stage = "pidfd_ownership"
        owned = (child_adapter.pidfd_owned(_CHILD_ADAPTER_KEY, handle) if attached
                 else child_adapter.pidfd_owned())
        require(type(owned) is bool, "pidfd ownership result")
        pidfd_owned = owned
        if attached:
            stage = "child_handoff"
            child_adapter.enter_settlement(_CHILD_ADAPTER_KEY, handle)
            require(handle.state == "SETTLING",
                    "settlement handoff changed capture state")
        while True:
            stage = "clock"
            now = clock()
            if now >= cleanup_deadline_ns:
                return finish_deadline(now)
            if attached and not adapter_confirmed:
                stage = "child_observe"
                normalized = child_adapter.observe_or_cached(
                    _CHILD_ADAPTER_KEY, handle)
                require(handle.state == "SETTLING", "settlement observe reentrancy")
                if normalized is not None:
                    candidate = _terminal_receipt(normalized, handle.origin.child)
                    require(observation is None or observation == candidate,
                            "primary and cached terminal evidence differ")
                    observation = candidate
                    adapter_confirmed = True
                elif observation is not None:
                    raise Refusal("primary terminal has no exact cached receipt")
            elif not attached and observation is None and reap is None:
                stage = "child_observe"
                opaque = child_adapter.observe()
                require(handle.state == "SETTLING", "settlement observe reentrancy")
                if opaque is not None:
                    stage = "child_normalize"
                    normalized = child_adapter.normalize(opaque)
                    observation = _terminal_receipt(normalized, handle.origin.child)
                    stage = "clock"
                    now = clock()
                    if now >= cleanup_deadline_ns:
                        return finish_deadline(now)
                    stage = "child_reap"
                    reaped = child_adapter.reap_once(opaque)
                    require(handle.state == "SETTLING", "settlement reap reentrancy")
                    reap = _validate_reap(reaped, handle.origin.child, observation)
            if attached and observation is not None and reap is None and adapter_confirmed:
                stage = "clock"
                now = clock()
                if now >= cleanup_deadline_ns:
                    return finish_deadline(now)
                stage = "child_reap"
                reaped = child_adapter.reap_cached_once(
                    _CHILD_ADAPTER_KEY, handle)
                require(handle.state == "SETTLING", "settlement reap reentrancy")
                reap = _validate_reap(reaped, handle.origin.child, observation)
            streams = {role: stream.receipt() for role, stream in handle.streams.items()}
            if reap is not None and all(item["eof"] for item in streams.values()):
                stage = "clock"
                now = clock()
                if now >= cleanup_deadline_ns:
                    return finish_deadline(now)
                outcome = "LEADER_REAPED_STREAMS_EOF"
                receipt = _settlement_receipt(
                    handle, pidfd_owned, cleanup_deadline_ns, started, now,
                    outcome, observation, reap, None)
                handle.state = "SETTLEMENT_FINISHED"
                return SettlementHandle(_SETTLEMENT_HANDLE_KEY, receipt,
                                        child_adapter, cleanup_deadline_ns, True,
                                        handle.origin.owner, handle)
            stage = "clock"
            now = clock()
            if now >= cleanup_deadline_ns:
                return finish_deadline(now)
            remaining = cleanup_deadline_ns - now
            timeout_ms = min(MAX_POLL_MS, max(1, (remaining + 999999) // 1000000))
            try:
                stage = "capture_poll"
                events = syscalls.epoll_poll(handle.poller, timeout_ms)
            except InterruptedError:
                continue
            require(handle.state == "SETTLING", "settlement poll reentrancy")
            require(type(events) is list, "settlement poll result")
            stage = "capture_drain"
            drain_ready_turn(handle, events, syscalls, _SETTLE_KEY)
            require(handle.state == "SETTLING", "settlement drain state")
    except BaseException as exc:
        if stage == "clock":
            outcome = "UNKNOWN_CLOCK_OR_OWNERSHIP"
        elif stage.startswith("child_") or stage == "pidfd_ownership":
            outcome = "UNKNOWN_CHILD_API"
        else:
            outcome = "UNKNOWN_CAPTURE_API"
        finished = last if last is not None else 0
        receipt = _settlement_receipt(
            handle, pidfd_owned, cleanup_deadline_ns,
            started if started is not None else finished, finished,
            outcome, observation, reap, type(exc).__name__)
        handle.state = "UNKNOWN_SETTLEMENT"
        return SettlementHandle(_SETTLEMENT_HANDLE_KEY, receipt, child_adapter,
                                cleanup_deadline_ns, reap is not None,
                                handle.origin.owner, handle)


def settle_passively(handle, child_adapter, cleanup_deadline_ns, syscalls):
    """Legacy mocked adapter settlement; attached adapters use owned path."""
    return _settle_passively(
        handle, child_adapter, cleanup_deadline_ns, syscalls, False)


def settle_attached_passively(handle, cleanup_deadline_ns, syscalls):
    """Settle through the exact adapter attached before primary observation."""
    require(type(handle) is CaptureHandle and handle.child_adapter is not None,
            "owned child adapter is not attached")
    return _settle_passively(
        handle, handle.child_adapter, cleanup_deadline_ns, syscalls, True)


def _completion_digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"),
        ensure_ascii=True).encode("ascii")).hexdigest()


def _strict_stream_snapshot(stream, role):
    require(type(stream) is _Stream and type(role) is str
            and role in ("stdout", "stderr")
            and type(stream.role) is str and stream.role == role
            and type(stream.identity) is dict
            and type(stream.limit) is int and stream.limit >= 1
            and type(stream.stored) is bytearray
            and type(stream.observed_bytes) is int
            and stream.observed_bytes == len(stream.stored)
            and len(stream.stored) <= stream.limit
            and type(stream.eof) is bool and stream.eof is True
            and type(stream.truncated) is bool
            and stream.truncated is False,
            "strict completed capture stream")
    identity = _identity(stream.identity)
    stored = bytes(stream.stored)
    return {"role": role, "pipe_identity": identity,
            "eof": True, "truncated": False,
            "stored_bytes": len(stored),
            "observed_bytes": stream.observed_bytes,
            "prefix_sha256": hashlib.sha256(stored).hexdigest(),
            "digest_scope": "FULL_CAPTURE"}


def _strict_builtin_tree(value, label):
    if type(value) in (str, int, bool) or value is None:
        return
    if type(value) is list:
        for item in value:
            _strict_builtin_tree(item, label)
        return
    if type(value) is dict:
        require(all(type(key) is str for key in value),
                label + " keys")
        for item in value.values():
            _strict_builtin_tree(item, label)
        return
    raise Refusal(label + " contains a custom value")


def _strict_completion(value, child, streams):
    keys = {"classification", "leader_observation", "streams",
            *RESULT_LIMITS}
    require(type(value) is dict
            and all(type(key) is str for key in value)
            and set(value) == keys
            and type(value["classification"]) is str
            and value["classification"]
            == "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED"
            and type(value["streams"]) is dict
            and all(type(role) is str for role in value["streams"])
            and set(value["streams"]) == {"stdout", "stderr"}
            and all(type(value[key]) is bool
                    and value[key] is RESULT_LIMITS[key]
                    for key in RESULT_LIMITS),
            "strict attached completion schema")
    terminal = _terminal_receipt(value["leader_observation"], child)
    for role in ("stdout", "stderr"):
        receipt = value["streams"][role]
        expected = streams[role]
        require(type(receipt) is dict and set(receipt) == set(expected)
                and all(type(key) is str for key in receipt),
                "strict completion stream receipt")
        _identity(receipt["pipe_identity"])
        require(type(receipt["role"]) is str
                and type(receipt["eof"]) is bool
                and type(receipt["truncated"]) is bool
                and type(receipt["stored_bytes"]) is int
                and type(receipt["observed_bytes"]) is int
                and type(receipt["prefix_sha256"]) is str
                and re.fullmatch(r"[0-9a-f]{64}",
                                 receipt["prefix_sha256"]) is not None
                and type(receipt["digest_scope"]) is str
                and receipt == expected,
                "completed capture stream changed")
    return {"classification": value["classification"],
            "leader_observation": terminal,
            "streams": copy.deepcopy(value["streams"]), **RESULT_LIMITS}


def validate_attached_completion_unreaped(handle):
    """Pure proof of the original attached completion; consumes nothing."""
    owned_module = importlib.import_module("prelive_owned_child")
    require(type(handle) is CaptureHandle
            and type(handle.origin) is _PipeOrigin
            and type(handle.child_adapter) is owned_module._CaptureChildAdapter
            and type(handle.normal_completion_capability)
            is _NormalCompletionCapability,
            "exact unreaped completion objects")
    adapter = handle.child_adapter
    owned = adapter.handle
    cap = handle.normal_completion_capability
    require(type(owned) is owned_module._OwnedPidfdHandle
            and type(cap.cached_receipt) is owned_module.ExitReceipt
            and type(handle.streams) is dict
            and all(type(role) is str for role in handle.streams)
            and set(handle.streams) == {"stdout", "stderr"}
            and type(handle.normal_completion) is dict
            and type(cap.completion) is dict
            and type(cap.owner) is dict
            and type(owned.child) is dict
            and type(owned.lifecycle) is dict
            and type(handle.origin.identities) is dict
            and all(type(role) is str for role in handle.origin.identities)
            and set(handle.origin.identities) == {"stdout", "stderr"}
            and type(handle.fd_roles) is dict
            and all(type(fd) is int and type(role) is str
                    for fd, role in handle.fd_roles.items())
            and type(handle.state) is str
            and type(adapter.state) is str
            and type(owned.state) is str
            and type(adapter.normal_completion_digest) is str,
            "strict unreaped completion containers")
    require(all(type(key) is str for key in owned.lifecycle)
            and set(owned.lifecycle) == {"schema", "state", "token",
                "supervisor", "owned_child", "origin"}
            and type(owned.lifecycle["schema"]) is int
            and type(owned.lifecycle["state"]) is str
            and type(owned.lifecycle["token"]) is str
            and type(owned.lifecycle["supervisor"]) is dict
            and type(owned.lifecycle["owned_child"]) is dict
            and type(owned.lifecycle["origin"])
            is owned_module._ForkOrigin
            and all(type(key) is str for key in owned.child)
            and set(owned.child) == {"pid", "starttime", "boot_id"}
            and type(owned.child["pid"]) is int
            and type(owned.child["starttime"]) is int
            and type(owned.child["boot_id"]) is str
            and type(owned.pidfd) is int and owned.pidfd >= 0
            and all(type(key) is str for key in cap.owner),
            "strict owned completion preflight")
    cap_owner = _owner(cap.owner)
    _owner(owned.lifecycle["supervisor"])
    lifecycle_child = owned.lifecycle["owned_child"]
    require(all(type(key) is str for key in lifecycle_child)
            and set(lifecycle_child) == {"pid", "starttime"}
            and type(lifecycle_child["pid"]) is int
            and type(lifecycle_child["starttime"]) is int,
            "strict owned lifecycle child preflight")
    _strict_builtin_tree(handle.normal_completion,
                         "capture normal completion")
    _strict_builtin_tree(cap.completion, "capability completion")
    owner = _owner(handle.origin.owner)
    child = _child(handle.origin.child)
    identities = {role: _identity(handle.origin.identities[role])
                  for role in ("stdout", "stderr")}
    stream_snapshots = {role: _strict_stream_snapshot(
        handle.streams[role], role) for role in ("stdout", "stderr")}
    completion = _strict_completion(cap.completion, child, stream_snapshots)
    _strict_completion(handle.normal_completion, child, stream_snapshots)
    receipt = cap.cached_receipt
    require(receipt._key is owned_module._RECEIPT_KEY
            and type(receipt.lifecycle_token) is str
            and type(receipt.child_pid) is int
            and type(receipt.child_starttime) is int
            and type(receipt.pidfd) is int
            and type(receipt.code) is int
            and type(receipt.status) is int
            and type(receipt.classification) is str,
            "strict opaque exit receipt")
    normalized = owned_module._normalized_exit(receipt, owned)
    _strict_builtin_tree(normalized, "normalized opaque exit")
    require(type(cap.completion_digest) is str
            and re.fullmatch(r"[0-9a-f]{64}", cap.completion_digest) is not None
            and cap.completion_digest == _completion_digest(completion)
            and handle.normal_completion == completion
            and cap.completion == completion
            and completion["leader_observation"] == normalized
            and type(cap.execution_deadline_ns) is int
            and type(handle.execution_deadline_ns) is int
            and cap.execution_deadline_ns == handle.execution_deadline_ns
            and type(cap.completed_at_ns) is int
            and 0 <= cap.completed_at_ns < cap.execution_deadline_ns
            and handle.state
            == "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED"
            and handle.monitor_active is False
            and handle.primary_failure is None
            and handle.settlement_used is False
            and handle.normal_handoff_active is False
            and handle.normal_handoff_poisoned is False
            and threading.current_thread() is handle.owner_thread
            and adapter.owner_thread is handle.owner_thread
            and owned.owner_thread is handle.owner_thread
            and cap.owner_thread is handle.owner_thread
            and handle.origin.claimed is True
            and handle.child_adapter_type is owned_module._CaptureChildAdapter
            and all(handle.streams[role].identity == identities[role]
                    for role in ("stdout", "stderr"))
            and handle.fd_roles == {
                identities[role]["fd"]: role
                for role in ("stdout", "stderr")}
            and cap.used is False
            and cap.capture_handle is handle
            and cap.child_adapter is adapter
            and cap.cached_receipt is adapter.cached
            and cap_owner == owner
            and cap.owner_thread is handle.owner_thread
            and adapter.capture_consumer is handle
            and adapter.capture_authority is _CHILD_ADAPTER_KEY
            and adapter.normal_completion_capability is cap
            and adapter.normal_completion_digest == cap.completion_digest
            and adapter.state == "PRIMARY"
            and owned.capture_adapter is adapter
            and owned.observation is cap.cached_receipt
            and owned.state == "TERMINAL_OBSERVED_NOT_REAPED"
            and owned.lifecycle["state"] == "PIDFD_BOUND",
            "attached unreaped completion authority changed")
    _validate_owned_normal_identity(adapter, cap, False)
    return cap


def _validate_owned_normal_identity(adapter, cap, reaped):
    """Pure exact identity proof for the owned child behind a completion."""
    owned_module = importlib.import_module("prelive_owned_child")
    owned = adapter.handle
    receipt = cap.cached_receipt
    expected_lifecycle = ("OWNED_LEADER_REAPED_ONLY" if reaped
                          else "PIDFD_BOUND")
    expected_handle = "REAPED" if reaped else "TERMINAL_OBSERVED_NOT_REAPED"
    supervisor = _owner(owned.lifecycle.get("supervisor"))
    capture_child = _child(cap.capture_handle.origin.child)
    require(type(owned) is owned_module._OwnedPidfdHandle
            and type(receipt) is owned_module.ExitReceipt
            and owned.capture_adapter is adapter
            and owned.observation is receipt
            and owned.state == expected_handle
            and type(owned.pidfd) is int and owned.pidfd >= 0
            and type(owned.child) is dict
            and set(owned.child) == {"pid", "starttime", "boot_id"}
            and type(owned.child["pid"]) is int
            and owned.child["pid"] > 0
            and type(owned.child["starttime"]) is int
            and owned.child["starttime"] > 0
            and type(owned.child["boot_id"]) is str
            and owned.child["boot_id"] == capture_child["boot_id"]
            and type(owned.lifecycle) is dict
            and set(owned.lifecycle)
            == {"schema", "state", "token", "supervisor", "owned_child",
                "origin"}
            and type(owned.lifecycle["schema"]) is int
            and owned.lifecycle["schema"] == 1
            and owned.lifecycle["state"] == expected_lifecycle
            and supervisor == cap.owner
            and supervisor == cap.capture_handle.origin.owner
            and type(owned.lifecycle["token"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", owned.lifecycle["token"])
            and type(owned.lifecycle["owned_child"]) is dict
            and set(owned.lifecycle["owned_child"]) == {"pid", "starttime"}
            and type(owned.lifecycle["owned_child"]["pid"]) is int
            and type(owned.lifecycle["owned_child"]["starttime"]) is int
            and owned.lifecycle["owned_child"]["pid"] == owned.child["pid"]
            and owned.lifecycle["owned_child"]["starttime"]
            == owned.child["starttime"]
            and capture_child["lifecycle_token"]
            == owned.lifecycle["token"]
            and capture_child["pid"] == owned.child["pid"]
            and capture_child["starttime"] == owned.child["starttime"]
            and type(receipt.lifecycle_token) is str
            and receipt.lifecycle_token == owned.lifecycle["token"]
            and type(receipt.child_pid) is int
            and receipt.child_pid == owned.child["pid"]
            and type(receipt.child_starttime) is int
            and receipt.child_starttime == owned.child["starttime"]
            and type(receipt.pidfd) is int
            and receipt.pidfd == owned.pidfd,
            "owned normal completion identity changed")


def _validate_normal_handoff(handle, cap, adapter_states, child_states):
    require(type(handle) is CaptureHandle
            and handle.normal_handoff_active is True
            and handle.normal_handoff_poisoned is False
            and threading.current_thread() is handle.owner_thread
            and handle.state == "SETTLING" and handle.monitor_active is False
            and handle.primary_failure is None and handle.settlement_used is True
            and type(cap) is _NormalCompletionCapability and cap.used is True
            and handle.normal_completion_capability is cap
            and cap.capture_handle is handle
            and cap.child_adapter is handle.child_adapter
            and type(handle.child_adapter) is handle.child_adapter_type
            and cap.owner_thread is handle.owner_thread
            and cap.owner == handle.origin.owner
            and cap.cached_receipt is handle.child_adapter.cached
            and cap.execution_deadline_ns == handle.execution_deadline_ns
            and cap.completion == handle.normal_completion
            and cap.completion_digest == _completion_digest(cap.completion)
            and set(cap.completion["streams"]) == {"stdout", "stderr"}
            and all(item["eof"] is True and item["truncated"] is False
                    for item in cap.completion["streams"].values())
            and {role: stream.receipt() for role, stream in handle.streams.items()}
            == cap.completion["streams"]
            and handle.child_adapter.capture_authority is _CHILD_ADAPTER_KEY
            and handle.child_adapter.capture_consumer is handle
            and handle.child_adapter.owner_thread is handle.owner_thread
            and handle.child_adapter.normal_completion_capability is cap
            and handle.child_adapter.normal_completion_digest
            == cap.completion_digest
            and handle.child_adapter.handle.capture_adapter
            is handle.child_adapter
            and handle.child_adapter.state in adapter_states
            and handle.child_adapter.handle.state in child_states
            and handle.child_adapter.handle.observation is cap.cached_receipt,
            "normal completion authority changed")
    _validate_owned_normal_identity(
        handle.child_adapter, cap, "REAPED" in adapter_states)


def validate_transferred_normal_completion(settlement, capture, adapter):
    """Pure bridge-side proof of the original normal-completion capability."""
    require(type(settlement) is SettlementHandle
            and settlement.capture_handle is capture
            and settlement.child_adapter is adapter
            and settlement.receipt.get("settlement_kind") == "NORMAL_COMPLETION"
            and settlement.receipt.get("primary_failure") is None,
            "normal settlement identity")
    cap = settlement.normal_completion_capability
    desc = settlement.descendant_capability
    require(type(cap) is _NormalCompletionCapability and cap.used is True
            and cap is capture.normal_completion_capability
            and cap.capture_handle is capture and cap.child_adapter is adapter
            and cap.cached_receipt is adapter.cached
            and adapter.normal_completion_capability is cap
            and adapter.normal_completion_digest == cap.completion_digest
            and adapter.handle.capture_adapter is adapter
            and adapter.handle.lifecycle["state"] == "OWNED_LEADER_REAPED_ONLY"
            and adapter.handle.state == "REAPED"
            and cap.completion_digest == _completion_digest(cap.completion)
            and cap.completion == capture.normal_completion
            and settlement.receipt.get("primary_completion") == cap.completion
            and type(desc) is _DescendantSettlementCapability
            and desc.normal_completion_capability is cap
            and desc.primary_completion == cap.completion
            and desc.settlement_kind == "NORMAL_COMPLETION",
            "transferred normal completion changed")
    _validate_owned_normal_identity(adapter, cap, True)
    return cap


def reap_attached_completion_once(handle, cleanup_deadline_ns, syscalls,
                                  minimum_ns=None):
    """Consume exact attached normal completion and reap its leader once."""
    if type(handle) is not CaptureHandle:
        raise Refusal("exact capture handle required")
    if (handle.normal_handoff_active
            or threading.current_thread() is not handle.owner_thread):
        handle.normal_handoff_poisoned = True
        handle.state = "UNKNOWN_NORMAL_COMPLETION_REENTRANT"
        raise Refusal("normal completion handoff ownership")
    try:
        require(handle.state == "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED"
                and handle.monitor_active is False
                and handle.primary_failure is None
                and handle.settlement_used is False
                and handle.child_adapter is not None
                and type(handle.child_adapter) is handle.child_adapter_type,
                "exact attached normal completion required")
    except BaseException:
        handle.normal_handoff_poisoned = True
        handle.state = "UNKNOWN_NORMAL_COMPLETION_ENTRY"
        raise
    cap = handle.normal_completion_capability
    require(type(cap) is _NormalCompletionCapability and cap.used is False
            and cap.capture_handle is handle
            and cap.child_adapter is handle.child_adapter
            and cap.owner_thread is handle.owner_thread
            and cap.owner == handle.origin.owner
            and cap.cached_receipt is handle.child_adapter.cached
            and cap.execution_deadline_ns == handle.execution_deadline_ns
            and cap.completion == handle.normal_completion
            and set(cap.completion["streams"]) == {"stdout", "stderr"},
            "normal completion capability unavailable")
    require(type(cleanup_deadline_ns) is int and cleanup_deadline_ns > 0,
            "cleanup deadline")
    require(minimum_ns is None
            or (type(minimum_ns) is int
                and cap.completed_at_ns <= minimum_ns < cleanup_deadline_ns),
            "cleanup minimum clock watermark")
    cap.used = True
    handle.settlement_used = True
    handle.normal_handoff_active = True
    handle.state = "SETTLING"
    adapter = handle.child_adapter
    observation = copy.deepcopy(cap.completion["leader_observation"])
    reap = None
    now = cap.completed_at_ns if minimum_ns is None else minimum_ns
    last = now
    started = now
    pidfd_owned = "UNKNOWN"
    def clock(adapter_states, child_states):
        nonlocal now, last
        now = syscalls.monotonic_ns()
        _validate_normal_handoff(handle, cap, adapter_states, child_states)
        require(type(now) is int and now >= last,
                "normal completion clock regressed")
        last = now
        return now
    try:
        _assert_owner(handle, syscalls, _SETTLE_KEY)
        _validate_normal_handoff(handle, cap, ("PRIMARY",),
                                 ("TERMINAL_OBSERVED_NOT_REAPED",))
        clock(("PRIMARY",), ("TERMINAL_OBSERVED_NOT_REAPED",))
        require(now < cleanup_deadline_ns, "normal completion handoff deadline")
        identity = _child(adapter.identity(_CHILD_ADAPTER_KEY, handle))
        _validate_normal_handoff(handle, cap, ("PRIMARY",),
                                 ("TERMINAL_OBSERVED_NOT_REAPED",))
        require(identity == handle.origin.child, "completion child identity")
        pidfd_owned = adapter.pidfd_owned(_CHILD_ADAPTER_KEY, handle)
        _validate_normal_handoff(handle, cap, ("PRIMARY",),
                                 ("TERMINAL_OBSERVED_NOT_REAPED",))
        require(type(pidfd_owned) is bool and pidfd_owned,
                "completion pidfd ownership")
        adapter.enter_completed_settlement(
            _CHILD_ADAPTER_KEY, handle, cap)
        _validate_normal_handoff(handle, cap, ("SETTLEMENT",),
                                 ("TERMINAL_OBSERVED_NOT_REAPED",))
        clock(("SETTLEMENT",), ("TERMINAL_OBSERVED_NOT_REAPED",))
        require(now < cleanup_deadline_ns, "normal completion pre-reap deadline")
        reaped = adapter.reap_cached_once(_CHILD_ADAPTER_KEY, handle)
        reap = _validate_reap(reaped, handle.origin.child, observation)
        _validate_normal_handoff(handle, cap, ("REAPED",), ("REAPED",))
        clock(("REAPED",), ("REAPED",))
        if now >= cleanup_deadline_ns:
            raise Refusal("normal completion post-reap deadline")
        _validate_normal_handoff(handle, cap, ("REAPED",), ("REAPED",))
        receipt = _settlement_receipt(
            handle, pidfd_owned, cleanup_deadline_ns, started, now,
            "LEADER_REAPED_STREAMS_EOF", observation, reap, None,
            "NORMAL_COMPLETION", cap.completion)
        handle.state = "SETTLEMENT_FINISHED"
        handle.normal_handoff_active = False
        return SettlementHandle(_SETTLEMENT_HANDLE_KEY, receipt, adapter,
                                cleanup_deadline_ns, True,
                                handle.origin.owner, handle, cap)
    except BaseException as exc:
        receipt = _settlement_receipt(
            handle, pidfd_owned, cleanup_deadline_ns, started, now,
            "UNKNOWN_NORMAL_COMPLETION", observation, reap,
            type(exc).__name__, "NORMAL_COMPLETION", cap.completion)
        handle.state = "UNKNOWN_SETTLEMENT"
        handle.normal_handoff_poisoned = True
        handle.normal_handoff_active = False
        return SettlementHandle(_SETTLEMENT_HANDLE_KEY, receipt, adapter,
                                cleanup_deadline_ns, reap is not None,
                                handle.origin.owner, handle, cap)


def take_descendant_settlement_capability(settlement, capture, adapter):
    """Claim exact settlement completion for the sole descendant consumer."""
    require(type(settlement) is SettlementHandle
            and threading.current_thread() is settlement.owner_thread
            and settlement.capture_handle is capture
            and settlement.child_adapter is adapter
            and settlement.leader_reaped is True
            and settlement.probe_used is False,
            "exact descendant settlement required")
    cap = settlement.descendant_capability
    require(type(cap) is _DescendantSettlementCapability and not cap.used
            and cap.capture_handle is capture and cap.child_adapter is adapter
            and cap.owner_thread is settlement.owner_thread
            and cap.deadline_ns == settlement.deadline_ns,
            "descendant settlement capability unavailable")
    cap.used = True
    settlement.probe_used = True
    return cap


def probe_children_after_reap(settlement, syscalls):
    """One P_ALL WNOWAIT observation; never reaps an unexpected child."""
    require(type(settlement) is SettlementHandle
            and threading.current_thread() is settlement.owner_thread,
            "owned settlement handle required")
    require(settlement.leader_reaped and settlement.probe_used is False,
            "post-reap probe is unavailable")
    settlement.probe_used = True
    now = 0
    finished = 0
    outcome = "UNKNOWN"
    stage = "owner"
    try:
        require(_owner(syscalls.owner_identity()) == settlement.owner,
                "post-reap owner mismatch")
        stage = "initial_clock"
        now = syscalls.monotonic_ns()
        require(type(now) is int and now >= 0, "post-reap clock")
        if now >= settlement.deadline_ns:
            outcome = "NOT_RUN_DEADLINE"
        else:
            stage = "wait_all"
            result = syscalls.wait_all_nohang_wnowait()
            stage = "finishing_clock"
            finished = syscalls.monotonic_ns()
            require(type(finished) is int and finished >= now,
                    "post-reap clock changed")
            if finished >= settlement.deadline_ns:
                outcome = "UNKNOWN_DEADLINE_CROSSED"
            elif result is None:
                outcome = "CHILDREN_PRESENT_NONE_WAITABLE"
            else:
                require(_integer(result.si_pid, 1)
                        and type(result.si_code) is int
                        and type(result.si_status) is int,
                        "post-reap wait receipt")
                outcome = "UNEXPECTED_WAITABLE_CHILD"
    except ChildProcessError as exc:
        if stage != "wait_all":
            outcome = "UNKNOWN"
            exc = None
        if exc is None:
            pass
        else:
            try:
                finished = syscalls.monotonic_ns()
                require(type(finished) is int and finished >= now,
                        "post-reap exception clock changed")
            except BaseException:
                outcome = "UNKNOWN"
            else:
                outcome = ("ECHILD_OBSERVED_AFTER_EXACT_REAP"
                           if exc.errno == errno.ECHILD
                           and finished < settlement.deadline_ns else "UNKNOWN")
    except BaseException:
        outcome = "UNKNOWN"
    return {"schema": 1, "classification": outcome,
            "deadline_ns": settlement.deadline_ns,
            "observed_ns": finished or now, "descendants_qualified": False,
            "dedicated_subreaper_qualified": False,
            "runtime_authorized": False, "storage_authorized": False,
            "postcondition_verified": False}


def close_capture_epoll(handle, syscalls):
    """Close only the internally owned epoll object, never borrowed pipe FDs."""
    _assert_owner(handle, syscalls)
    if handle.monitor_active:
        handle.state = "UNKNOWN_REENTRANT_OPERATION"
        raise Refusal("capture monitor owns the epoll resource")
    require(handle.state in ("BOUND", "SETTLEMENT_FINISHED"),
            "capture close state")
    poller = handle.poller
    handle.poller = None
    handle.state = "CLOSING"
    try:
        syscalls.epoll_close(poller)
    except BaseException:
        handle.state = "UNKNOWN_EPOLL_CLOSE"
        raise
    require(handle.state == "CLOSING", "capture close changed reentrantly")
    handle.state = "CLOSED"
