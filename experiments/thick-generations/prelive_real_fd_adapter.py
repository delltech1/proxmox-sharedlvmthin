#!/usr/bin/python3
"""Real Linux FD boundary for the inert pre-live process qualification.

This module allocates and validates pipes only.  It never forks, executes,
signals, touches storage, or grants runtime authority.
"""

import copy
import errno
import fcntl
import os
import stat
import threading


class Refusal(RuntimeError):
    pass


class AllocationRefusal(Refusal):
    def __init__(self, message, cleanup, cause):
        super().__init__(message)
        self.cleanup = copy.deepcopy(cleanup)
        self.cause = type(cause).__name__


_ALLOCATE_KEY = object()
_PAIR_ROLES = (
    ("grant_child", "grant_parent"),
    ("status_parent", "status_child"),
    ("stdout_parent", "stdout_child"),
    ("stderr_parent", "stderr_child"),
)
_READ_ROLES = frozenset(("grant_child", "status_parent", "stdout_parent",
                         "stderr_parent"))
_NONBLOCK_ROLES = frozenset(("grant_parent", "status_parent",
                             "stdout_parent", "stderr_parent"))
_PARENT_ROLES = frozenset(("grant_parent", "status_parent",
                           "stdout_parent", "stderr_parent"))
_ALL_ROLES = frozenset(role for pair in _PAIR_ROLES for role in pair)
_MAX_IO = 65536
_IDENTITY_KEYS = ("fd", "dev", "inode", "mode", "flags", "fd_flags")
GRANT_ONCE = "GRANT_ONCE"
CLOSE_ONLY_NO_WRITE = "CLOSE_ONLY_NO_WRITE"
_GRANT_POLICIES = frozenset((GRANT_ONCE, CLOSE_ONLY_NO_WRITE))


def require(condition, message):
    if not condition:
        raise Refusal(message)


def _strict_int(value, message):
    require(type(value) is int and value >= 0, message)
    return value


def _owner(value):
    require(type(value) is dict
            and set(value) == {"pid", "starttime", "boot_id"}
            and type(value["pid"]) is int and value["pid"] > 0
            and type(value["starttime"]) is int and value["starttime"] > 0
            and type(value["boot_id"]) is str and len(value["boot_id"]) == 36,
            "strict owner identity")
    return copy.deepcopy(value)


def _identity(calls, fd, role):
    _strict_int(fd, "strict file descriptor")
    expected_access = os.O_RDONLY if role in _READ_ROLES else os.O_WRONLY
    flags = calls.fcntl(fd, fcntl.F_GETFL)
    fd_flags = calls.fcntl(fd, fcntl.F_GETFD)
    info = calls.fstat(fd)
    require(type(flags) is int and type(fd_flags) is int,
            "strict descriptor flags")
    expected_flags = expected_access | (os.O_NONBLOCK
                                        if role in _NONBLOCK_ROLES else 0)
    require(flags == expected_flags, "pipe endpoint flags mismatch")
    require(fd_flags == fcntl.FD_CLOEXEC,
            "pipe endpoint close-on-exec flags mismatch")
    require(stat.S_ISFIFO(info.st_mode)
            and type(info.st_dev) is int and type(info.st_ino) is int,
            "pipe endpoint identity")
    return {"fd": fd, "dev": info.st_dev, "inode": info.st_ino,
            "mode": info.st_mode, "flags": flags, "fd_flags": fd_flags}


def _identity_record(value, role):
    require(type(value) is dict
            and set(value) == {"fd", "dev", "inode", "mode", "flags",
                               "fd_flags"},
            "strict endpoint identity schema")
    for key in value:
        _strict_int(value[key], "strict endpoint identity value")
    expected_access = os.O_RDONLY if role in _READ_ROLES else os.O_WRONLY
    expected_flags = expected_access | (os.O_NONBLOCK
                                        if role in _NONBLOCK_ROLES else 0)
    require(stat.S_ISFIFO(value["mode"])
            and value["flags"] == expected_flags
            and value["fd_flags"] == fcntl.FD_CLOEXEC,
            "endpoint identity semantics")
    return copy.deepcopy(value)


def _allocation_identity(calls, fd, role):
    """Capture the newly returned object before any flag mutation."""
    _strict_int(fd, "strict allocated descriptor")
    flags = calls.fcntl(fd, fcntl.F_GETFL)
    fd_flags = calls.fcntl(fd, fcntl.F_GETFD)
    info = calls.fstat(fd)
    expected_access = os.O_RDONLY if role in _READ_ROLES else os.O_WRONLY
    require(type(flags) is int and type(fd_flags) is int
            and flags == expected_access
            and fd_flags == fcntl.FD_CLOEXEC
            and stat.S_ISFIFO(info.st_mode), "new pipe endpoint identity")
    return {"fd": fd, "dev": info.st_dev, "inode": info.st_ino,
            "mode": info.st_mode, "flags": flags, "fd_flags": fd_flags}


def _same_allocated_object(calls, original, role, allowed_flags):
    try:
        fd = original["fd"]
        flags = calls.fcntl(fd, fcntl.F_GETFL)
        fd_flags = calls.fcntl(fd, fcntl.F_GETFD)
        info = calls.fstat(fd)
    except BaseException:
        return False
    return (type(flags) is int and type(fd_flags) is int
            and info.st_dev == original["dev"]
            and info.st_ino == original["inode"]
            and info.st_mode == original["mode"]
            and fd_flags == original["fd_flags"]
            and type(allowed_flags) is frozenset
            and flags in allowed_flags)


class RealFDHandle:
    def __init__(self, key, calls, owner, bundle, grant_policy):
        require(key is _ALLOCATE_KEY, "private real FD handle")
        require(type(grant_policy) is str and grant_policy in _GRANT_POLICIES,
                "strict immutable grant policy")
        object.__setattr__(self, "_grant_policy", grant_policy)
        object.__setattr__(self, "_grant_policy_sealed", True)
        object.__setattr__(self, "_consumer_claim", None)
        object.__setattr__(self, "_consumer_claim_started", False)
        object.__setattr__(self, "_consumer_claim_sealed", True)
        self.calls = calls
        self.owner = _owner(owner)
        self.owner_thread = threading.current_thread()
        self.bundle = copy.deepcopy(bundle)
        self.frozen_bundle = copy.deepcopy(bundle)
        self._allocation_authority = tuple(
            (role, tuple(bundle[role][key] for key in _IDENTITY_KEYS))
            for role in sorted(_ALL_ROLES))
        self.owned = set(_ALL_ROLES)
        self.lost = set()
        self.attempted_writes = set()
        self.attempted_closes = set()
        self.poisoned = False
        self.poison_reasons = []
        self.active = None
        self.active_poison_count = 0
        self.state = "FD_GRAPH_READY"
        self.runtime_authorized = False
        self.storage_authorized = False
        self.exec_proven = False

    def __setattr__(self, name, value):
        if ((name in {"_grant_policy", "_grant_policy_sealed"}
             and getattr(self, "_grant_policy_sealed", False))
                or (name in {"_consumer_claim", "_consumer_claim_started",
                             "_consumer_claim_sealed"}
                    and getattr(self, "_consumer_claim_sealed", False))):
            raise Refusal("FD handle construction authority is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if name in {"_grant_policy", "_grant_policy_sealed",
                    "_consumer_claim", "_consumer_claim_started",
                    "_consumer_claim_sealed"}:
            raise Refusal("FD handle construction authority is immutable")
        object.__delattr__(self, name)

    def __copy__(self):
        raise Refusal("real FD handle is noncopyable")

    def __deepcopy__(self, memo):
        raise Refusal("real FD handle is noncopyable")

    def _assert_owner(self):
        if threading.current_thread() is not self.owner_thread:
            self._poison("UNKNOWN_OWNER")
            raise Refusal("real FD handle crossed owner thread")
        if _owner(self.calls.owner_identity()) != self.owner:
            self._poison("UNKNOWN_OWNER")
            raise Refusal("real FD handle crossed owner process")

    def _authority_record(self, role):
        matches = [values for name, values in self._allocation_authority
                   if name == role]
        require(len(matches) == 1 and len(matches[0]) == len(_IDENTITY_KEYS),
                "allocation authority changed")
        value = dict(zip(_IDENTITY_KEYS, matches[0]))
        return _identity_record(value, role)

    def _validate_stored_role(self, role):
        authority = self._authority_record(role)
        require(_identity_record(self.frozen_bundle[role], role) == authority
                and _identity_record(self.bundle[role], role) == authority,
                "stored endpoint authority changed")
        return authority

    def _validate_stored_graph(self):
        # Pure, callback-free closing check after the last observable syscall.
        return {role: self._validate_stored_role(role)
                for role in sorted(_ALL_ROLES)}

    def _poison(self, state="UNKNOWN_FD_GRAPH"):
        self.poisoned = True
        self.poison_reasons.append(state)
        self.state = state

    def _begin(self, operation, allow_poison=False):
        if self.active is not None:
            self._poison("UNKNOWN_REENTRANT_OPERATION")
            raise Refusal("real FD operation is already active")
        if self.poisoned and not allow_poison:
            raise Refusal("poisoned real FD handle")
        self.active = operation
        self.active_poison_count = len(self.poison_reasons)
        try:
            self._assert_owner()
            self._checkpoint(operation, allow_poison)
        except BaseException:
            self._poison("UNKNOWN_OPERATION_ENTRY")
            self.active = None
            raise

    def _checkpoint(self, operation, allow_poison=False):
        require(self.active == operation, "real FD operation changed reentrantly")
        require(len(self.poison_reasons) == self.active_poison_count,
                "real FD operation was poisoned reentrantly")
        if self.poisoned and not allow_poison:
            raise Refusal("real FD authority is poisoned")

    def _finish(self, operation):
        require(self.active == operation, "real FD operation completion mismatch")
        self.active = None
        self.active_poison_count = 0

    def _validate_current(self, operation, allow_poison=False):
        self._checkpoint(operation, allow_poison)
        self._assert_owner()
        self._checkpoint(operation, allow_poison)
        require(self.state != "FD_GRAPH_CLOSED", "real FD graph unavailable")
        require(set(self.bundle) == _ALL_ROLES
                and set(self.frozen_bundle) == _ALL_ROLES,
                "real FD graph roles changed")
        current = {}
        for role in sorted(_ALL_ROLES):
            expected = self._validate_stored_role(role)
            if role in self.owned:
                observed = _identity(self.calls, expected["fd"], role)
                self._checkpoint(operation, allow_poison)
                require(observed == expected, "real FD endpoint drift")
                current[role] = observed
            else:
                require(role in self.attempted_closes,
                        "real FD ownership disappeared without close attempt")
                current[role] = expected
        fds = [value["fd"] for value in current.values()]
        require(len(set(fds)) == 8 and all(fd > 2 for fd in fds),
                "real FD graph aliases reserved or peer descriptor")
        pipes = set()
        for read_role, write_role in _PAIR_ROLES:
            left = current[read_role]
            right = current[write_role]
            require((left["dev"], left["inode"])
                    == (right["dev"], right["inode"]),
                    "pipe peer identity mismatch")
            pipes.add((left["dev"], left["inode"]))
        require(len(pipes) == 4, "pipe identities are not distinct")
        self._validate_stored_graph()

    def validate(self):
        self._begin("VALIDATING")
        try:
            self._validate_current("VALIDATING")
            result = self.snapshot()
            self._checkpoint("VALIDATING")
            self._finish("VALIDATING")
            return result
        except BaseException:
            self._poison("UNKNOWN_VALIDATE")
            self.active = None
            raise

    def snapshot(self):
        return {"classification": self.state,
                "grant_policy": self._grant_policy,
                "consumer_claimed": self._consumer_claim is not None,
                "owner": copy.deepcopy(self.owner),
                "bundle": copy.deepcopy(self.frozen_bundle),
                "owned_roles": sorted(self.owned),
                "lost_roles": sorted(self.lost),
                "poisoned": self.poisoned,
                "poison_reasons": list(self.poison_reasons),
                "runtime_authorized": False,
                "storage_authorized": False,
                "exec_proven": False}

    def claim_close_only_once(self, consumer):
        if (self._consumer_claim_started
                or self._consumer_claim is not None or consumer is None):
            self._poison("UNKNOWN_CONSUMER_CLAIM_REPLAY")
            raise Refusal("fresh close-only consumer claim required")
        object.__setattr__(self, "_consumer_claim_started", True)
        object.__setattr__(self, "_consumer_claim", consumer)
        self._begin("CLAIMING_CONSUMER")
        try:
            require(self._grant_policy == CLOSE_ONLY_NO_WRITE
                    and self.state == "FD_GRAPH_READY"
                    and self.poisoned is False
                    and self.owned == _ALL_ROLES
                    and not self.lost and not self.attempted_closes
                    and not self.attempted_writes,
                    "fresh complete close-only FD graph required")
            self._validate_current("CLAIMING_CONSUMER")
            result = self.snapshot()
            self._checkpoint("CLAIMING_CONSUMER")
            self._finish("CLAIMING_CONSUMER")
            return result
        except BaseException:
            self._poison("UNKNOWN_CONSUMER_CLAIM")
            self.active = None
            raise

    def read_nonblocking(self, role, maximum):
        self._begin("READING")
        try:
            self._validate_current("READING")
            require(role in ("status_parent", "stdout_parent", "stderr_parent")
                    and role in self.owned and role not in self.attempted_closes
                    and type(maximum) is int and 0 < maximum <= _MAX_IO,
                    "bounded parent read")
            fd = self._authority_record(role)["fd"]
            try:
                data = self.calls.read(fd, maximum)
            except BlockingIOError as exc:
                require(exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK),
                        "unexpected nonblocking read error")
                data = None
            self._checkpoint("READING")
            self._validate_current("READING")
            require(data is None or type(data) is bytes, "strict read result")
            if data is None:
                result = {"kind": "EAGAIN", "data": b""}
            elif data == b"":
                result = {"kind": "EOF", "data": b""}
            else:
                result = {"kind": "DATA", "data": data}
            self._checkpoint("READING")
            self._finish("READING")
            return result
        except BaseException:
            self._poison("UNKNOWN_READ")
            self.active = None
            raise

    def write_grant_once(self, data):
        role = "grant_parent"
        if self._grant_policy != GRANT_ONCE:
            self._poison("UNKNOWN_FORBIDDEN_GRANT_WRITE")
            raise Refusal("grant write forbidden by immutable policy")
        if role in self.attempted_writes:
            self._poison("UNKNOWN_WRITE_REPLAY")
            raise Refusal("grant write is one-shot")
        self._begin("WRITING")
        try:
            self._validate_current("WRITING")
            require(role in self.owned and role not in self.attempted_closes,
                    "grant writer is not owned")
            require(type(data) is bytes and data == b"G", "exact grant byte")
            fd = self._authority_record(role)["fd"]
            self.attempted_writes.add(role)
            written = self.calls.write(fd, data)
            self._checkpoint("WRITING")
            require(type(written) is int and written == 1,
                    "grant write outcome is ambiguous")
            self._validate_current("WRITING")
            result = {"classification": "ONE_GRANT_BYTE_WRITE_CONFIRMED",
                    "bytes_written": 1, "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
            self._finish("WRITING")
            return result
        except BaseException:
            self._poison("UNKNOWN_GRANT_WRITE")
            self.active = None
            raise

    def _close_role_internal(self, role, operation):
        self._checkpoint(operation, True)
        require(role in _ALL_ROLES, "known FD role")
        if role in self.attempted_closes or role not in self.owned:
            self._poison("UNKNOWN_CLOSE_REPLAY")
            raise Refusal("FD close is one-shot")
        authority = self._validate_stored_role(role)
        self.attempted_closes.add(role)
        fd = authority["fd"]
        try:
            observed = _identity(self.calls, fd, role)
            self._checkpoint(operation, True)
            require(self._validate_stored_role(role) == authority
                    and observed == authority,
                    "close target endpoint drift")
        except BaseException:
            self.owned.remove(role)
            self.lost.add(role)
            self._poison("UNKNOWN_CLOSE_TARGET")
            raise
        self.owned.remove(role)
        try:
            result = self.calls.close(fd)
            self._checkpoint(operation, True)
            require(result is None, "strict close result")
        except BaseException:
            self._poison("UNKNOWN_CLOSE")
            raise
        if not self.owned and not self.poisoned:
            self.state = "FD_GRAPH_CLOSED"
        return {"classification": "FD_CLOSE_CONFIRMED", "role": role,
                "remaining": sorted(self.owned), "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}

    def close_role_once(self, role):
        self._begin("CLOSING", allow_poison=True)
        try:
            result = self._close_role_internal(role, "CLOSING")
            self._finish("CLOSING")
            return result
        except BaseException:
            self._poison("UNKNOWN_CLOSE_OPERATION")
            self.active = None
            raise

    def close_all_once(self):
        self._begin("RELEASING", allow_poison=True)
        try:
            require(self.state != "FD_GRAPH_CLOSED" and self.owned,
                    "bulk close unavailable")
            outcomes = []
            for role in sorted(tuple(self.owned), reverse=True):
                try:
                    outcomes.append(self._close_role_internal(role, "RELEASING"))
                except BaseException as exc:
                    outcomes.append({"classification": "UNKNOWN_CLOSE",
                                     "role": role, "error": type(exc).__name__})
                    self.active_poison_count = len(self.poison_reasons)
            classification = ("FD_GRAPH_CLOSED" if not self.owned
                              and not self.lost and not self.poisoned
                              and all(item["classification"]
                                      == "FD_CLOSE_CONFIRMED"
                                      for item in outcomes)
                              else "UNKNOWN_CLOSE")
            if classification == "FD_GRAPH_CLOSED":
                self.state = classification
            else:
                self._poison("UNKNOWN_RELEASE")
                self.active_poison_count = len(self.poison_reasons)
            self._finish("RELEASING")
            return {"classification": classification, "outcomes": outcomes,
                    "remaining": sorted(self.owned), "lost": sorted(self.lost),
                    "runtime_authorized": False,
                    "storage_authorized": False, "exec_proven": False}
        except BaseException:
            self._poison("UNKNOWN_RELEASE_EXCEPTION")
            self.active = None
            self.active_poison_count = 0
            raise


def allocate_real_fd_graph(calls, grant_policy=GRANT_ONCE):
    """Allocate four exact pipes; on any ambiguity, never return a handle."""
    require(type(grant_policy) is str and grant_policy in _GRANT_POLICIES,
            "strict grant policy")
    owner = _owner(calls.owner_identity())
    require(threading.active_count() == 1, "dedicated single-thread allocation")
    raw = {}
    owned_fds = []
    allocation_identities = {}
    cleanup_allowed_flags = {}
    try:
        for std_fd in (0, 1, 2):
            calls.fstat(std_fd)
        for read_role, write_role in _PAIR_ROLES:
            pair = calls.pipe2(os.O_CLOEXEC)
            prior_fds = set(owned_fds)
            if type(pair) is tuple:
                for fd in pair:
                    if type(fd) is int and fd >= 0 and fd not in owned_fds:
                        owned_fds.append(fd)
            require(type(pair) is tuple and len(pair) == 2
                    and all(type(fd) is int and fd >= 0 for fd in pair),
                    "strict pipe2 result")
            read_fd, write_fd = pair
            require(read_fd != write_fd and read_fd not in prior_fds
                    and write_fd not in prior_fds, "duplicate pipe descriptor")
            raw[read_role] = read_fd
            raw[write_role] = write_fd
            read_identity = _allocation_identity(calls, read_fd, read_role)
            allocation_identities[read_fd] = (read_role, read_identity)
            cleanup_allowed_flags[read_fd] = {
                read_identity["flags"]}
            write_identity = _allocation_identity(calls, write_fd, write_role)
            allocation_identities[write_fd] = (write_role, write_identity)
            cleanup_allowed_flags[write_fd] = {
                write_identity["flags"]}
            require(read_fd > 2 and write_fd > 2,
                    "pipe reused a standard descriptor")
        for role in _NONBLOCK_ROLES:
            fd = raw[role]
            flags = calls.fcntl(fd, fcntl.F_GETFL)
            original_flags = allocation_identities[fd][1]["flags"]
            require(type(flags) is int and flags == original_flags,
                    "strict initial descriptor flags")
            # The exact old/new values are both cleanup-safe once this owned
            # transition is authorized; an ambiguous F_SETFL may leave either.
            cleanup_allowed_flags[fd].add(original_flags | os.O_NONBLOCK)
            result = calls.fcntl(
                fd, fcntl.F_SETFL, original_flags | os.O_NONBLOCK)
            require(type(result) is int and result == 0,
                    "nonblocking flag update not confirmed")
        bundle = {role: _identity(calls, fd, role)
                  for role, fd in raw.items()}
        for role, final in bundle.items():
            original = allocation_identities[final["fd"]][1]
            require(all(final[key] == original[key]
                        for key in ("fd", "dev", "inode", "mode", "fd_flags"))
                    and (final["flags"] & os.O_ACCMODE)
                    == (original["flags"] & os.O_ACCMODE),
                    "allocated endpoint object changed during setup")
        handle = RealFDHandle(_ALLOCATE_KEY, calls, owner, bundle,
                              grant_policy)
        handle.validate()
        return handle
    except BaseException as exc:
        # Ownership is removed before every single close attempt.  A close error
        # is ambiguous and is never retried, but construction still refuses.
        cleanup = []
        for fd in reversed(owned_fds):
            captured = allocation_identities.get(fd)
            if (captured is None
                    or not _same_allocated_object(
                        calls, captured[1], captured[0],
                        frozenset(cleanup_allowed_flags.get(fd, ())))):
                cleanup.append({"fd": fd, "outcome": "QUARANTINED_UNKNOWN"})
                continue
            try:
                result = calls.close(fd)
                require(result is None, "strict allocation cleanup close")
                cleanup.append({"fd": fd, "outcome": "CLOSE_CONFIRMED"})
            except BaseException as close_exc:
                cleanup.append({"fd": fd, "outcome": "UNKNOWN_CLOSE",
                                "error": type(close_exc).__name__})
        raise AllocationRefusal("real FD graph allocation refused", cleanup,
                                exc) from exc


class LinuxFDCalls:
    """Thin syscall boundary.  It deliberately exposes no fork/exec/signal."""

    def owner_identity(self):
        with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as stream:
            boot_id = stream.read().strip().lower()
        with open(f"/proc/{os.getpid()}/stat", "r", encoding="ascii") as stream:
            raw = stream.read()
        closing = raw.rfind(")")
        fields = raw[closing + 2:].split()
        require(closing > 0 and len(fields) >= 20 and fields[19].isdigit(),
                "owner starttime unavailable")
        return {"pid": os.getpid(), "starttime": int(fields[19]),
                "boot_id": boot_id}

    pipe2 = staticmethod(os.pipe2)
    fcntl = staticmethod(fcntl.fcntl)
    fstat = staticmethod(os.fstat)
    read = staticmethod(os.read)
    write = staticmethod(os.write)
    close = staticmethod(os.close)
