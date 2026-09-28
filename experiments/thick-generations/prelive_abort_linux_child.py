#!/usr/bin/python3
"""Narrow Linux abort child boundary; no exec, grant write or storage API."""

import copy
import errno
import fcntl
import os
from pathlib import Path
import select
import stat
import threading
import time

import prelive_abort_fork_split as SPLIT
import prelive_allocated_graph_enrollment as GRAPH
import prelive_real_fd_adapter as FDS
import prelive_supervisor_model as SUP


_CALLS_KEY = object()
_FORK_KEY = object()


class Refusal(RuntimeError):
    pass


class _RequestedExit(BaseException):
    def __init__(self, code):
        self.code = code


def require(value, message):
    if not value:
        raise Refusal(message)


def _identity(fd):
    info = os.fstat(fd)
    return {"dev": info.st_dev, "inode": info.st_ino,
            "mode": info.st_mode, "flags": fcntl.fcntl(fd, fcntl.F_GETFL),
            "fd_flags": fcntl.fcntl(fd, fcntl.F_GETFD)}


class LinuxAbortChildCalls:
    """Exact child-side syscall surface consumed by AbortChildRoutine."""

    abort_child_split_linux = True

    def __init__(self, key, context):
        require(key is _CALLS_KEY, "private Linux abort child calls")
        descriptor = context["descriptor"]
        self._fds = {item["role"]: item["fd"]
                     for item in descriptor["endpoints"]}
        self._expected = {item["role"]: copy.deepcopy(item)
                          for item in descriptor["endpoints"]}
        self._deadline_ns = context["deadline_ns"]
        self._closed = set()
        self._quarantined = set()
        self._sealed = True

    def __setattr__(self, name, value):
        if "_sealed" in self.__dict__:
            raise Refusal("Linux abort child calls are immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("Linux abort child calls cannot be deleted")

    def monotonic_ns(self):
        del self
        return time.monotonic_ns()

    def current_child_identity(self):
        del self
        pid = os.getpid()
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        closing = raw.rfind(")")
        fields = raw[closing + 2:].split()
        require(closing > 0 and len(fields) >= 20
                and fields[19].isdigit(), "child stat identity")
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii").strip().lower()
        return {"pid": pid, "ppid": os.getppid(),
                "starttime": int(fields[19]), "boot_id": boot_id}

    def validate_child_graph(self, descriptor):
        require(type(descriptor) is dict
                and descriptor == {**descriptor}, "strict child descriptor")
        by_role = {item["role"]: item for item in descriptor["endpoints"]}
        require(set(by_role) == set(self._fds), "child role set changed")
        for role, fd in self._fds.items():
            require(role not in self._closed and role not in self._quarantined,
                    "child endpoint unavailable")
            current = _identity(fd)
            expected = by_role[role]
            require(stat.S_ISFIFO(current["mode"])
                    and all(current[key] == expected[key]
                            for key in ("dev", "inode", "mode", "flags",
                                        "fd_flags")),
                    "child endpoint identity changed")
        return True

    def _validate_role(self, role):
        require(role in self._fds and role in self._expected,
                "child role unavailable")
        current = _identity(self._fds[role])
        expected = self._expected[role]
        require(stat.S_ISFIFO(current["mode"])
                and all(current[key] == expected[key]
                        for key in ("dev", "inode", "mode", "flags",
                                    "fd_flags")),
                "child endpoint changed across wait")

    def close_role_once(self, role, expected_identity):
        require(role in self._fds and role not in self._closed
                and role not in self._quarantined,
                "child close authority unavailable")
        fd = self._fds[role]
        object.__setattr__(self, "_fds", dict(self._fds))
        del self._fds[role]
        object.__setattr__(self, "_closed", set(self._closed) | {role})
        try:
            current = _identity(fd)
            require(all(current[key] == expected_identity[key]
                        for key in ("dev", "inode", "mode", "flags",
                                    "fd_flags")),
                    "child close endpoint identity changed")
        except BaseException:
            object.__setattr__(self, "_quarantined",
                               set(self._quarantined) | {fd})
            raise
        os.close(fd)
        return True

    def wait_writable(self, role, deadline_ns):
        require(role in self._fds and deadline_ns == self._deadline_ns,
                "strict child write wait")
        remaining_ns = deadline_ns - time.monotonic_ns()
        require(remaining_ns > 0,
                "child write deadline reached")
        self._validate_role(role)
        fd = self._fds[role]
        poller = select.poll()
        poller.register(fd, select.POLLOUT)
        events = poller.poll(max(1, (remaining_ns + 999999) // 1000000))
        require(type(events) is list and events == [(fd, select.POLLOUT)]
                and time.monotonic_ns() < deadline_ns,
                "child write wait did not complete before deadline")
        self._validate_role(role)
        return True

    def write_role(self, role, data):
        require(role in ("status_child", "stdout_child", "stderr_child")
                and role in self._fds and type(data) is bytes and data,
                "unauthorized child write")
        self.wait_writable(role, self._deadline_ns)
        return os.write(self._fds[role], data)

    def read_grant(self, maximum):
        require(type(maximum) is int and 0 < maximum <= 2,
                "strict child grant read")
        remaining_ns = self._deadline_ns - time.monotonic_ns()
        require(remaining_ns > 0, "child grant deadline reached")
        self._validate_role("grant_child")
        fd = self._fds["grant_child"]
        poller = select.poll()
        wanted = select.POLLIN | select.POLLHUP | select.POLLERR
        poller.register(fd,
                        select.POLLIN | select.POLLHUP | select.POLLERR)
        events = poller.poll(max(1, (remaining_ns + 999999) // 1000000))
        require(type(events) is list and len(events) == 1
                and type(events[0]) is tuple and len(events[0]) == 2
                and type(events[0][0]) is int and events[0][0] == fd
                and type(events[0][1]) is int
                and events[0][1] != 0
                and events[0][1] & ~wanted == 0
                and events[0][1] & select.POLLNVAL == 0
                and time.monotonic_ns() < self._deadline_ns,
                "child grant wait did not complete before deadline")
        self._validate_role("grant_child")
        return os.read(self._fds["grant_child"], maximum)

    def exit_child(self, code):
        del self
        require(type(code) is int and code in (
            SPLIT.EXIT_ABORT_EOF, SPLIT.EXIT_GRANT_DATA,
            SPLIT.EXIT_CHILD_ERROR), "strict abort exit")
        raise _RequestedExit(code)


def _close_unlisted_fds(allowed):
    require(type(allowed) is set
            and all(type(fd) is int and fd >= 0 for fd in allowed),
            "strict inherited FD allowlist")
    entries = os.listdir("/proc/self/fd")
    require(all(type(item) is str and item.isdigit() for item in entries),
            "unparseable inherited FD inventory")
    for fd in sorted({int(item) for item in entries} - allowed):
        try:
            os.close(fd)
        except OSError as exc:
            require(exc.errno == errno.EBADF,
                    "unauthorized inherited FD close failed")


def _validate_prefork_context(context):
    GRAPH.CONSUMER._builtin_tree(context, "Linux abort fork context")
    require(type(context) is dict and set(context) == {
                "schema", "parent", "started_ns", "deadline_ns",
                "descriptor", "descriptor_digest"}
            and type(context["schema"]) is int and context["schema"] == 1
            and type(context["started_ns"]) is int
            and type(context["deadline_ns"]) is int
            and context["deadline_ns"] > context["started_ns"]
            and type(context["descriptor_digest"]) is str
            and SUP.digest(context["descriptor"])
            == context["descriptor_digest"],
            "strict Linux abort fork context")
    descriptor = context["descriptor"]
    require(type(descriptor) is dict and set(descriptor) == {
                "schema", "kind", "request_id", "graph_id", "supervisor",
                "pre_fork_ticket_id", "phase", "grant_policy", "endpoints",
                "endpoint_count", "all_cloexec", "parent_nonblocking"}
            and type(descriptor["schema"]) is int
            and descriptor["schema"] == 1
            and descriptor["kind"] == "NO_GRANT_ALLOCATED_FD_GRAPH_V1"
            and descriptor["phase"] == "PARENT_PRE_FORK_ALL_ENDPOINTS_OWNED"
            and descriptor["grant_policy"] == FDS.CLOSE_ONLY_NO_WRITE
            and type(descriptor["endpoint_count"]) is int
            and descriptor["endpoint_count"] == 8
            and descriptor["all_cloexec"] is True
            and descriptor["parent_nonblocking"] is True
            and type(descriptor["endpoints"]) is list
            and len(descriptor["endpoints"]) == 8,
            "strict Linux abort graph descriptor")
    by_role = {}
    for item in descriptor["endpoints"]:
        require(type(item) is dict and set(item) == {
                    "role", "fd", "dev", "inode", "mode", "flags",
                    "fd_flags"} and type(item["role"]) is str
                and item["role"] in GRAPH.FD_ROLES
                and item["role"] not in by_role,
                "strict Linux abort endpoint")
        FDS._identity_record({key: item[key] for key in item if key != "role"},
                             item["role"])
        by_role[item["role"]] = item
    require(set(by_role) == set(GRAPH.FD_ROLES)
            and len({item["fd"] for item in by_role.values()}) == 8,
            "complete distinct Linux abort graph")
    for left, right in (("grant_child", "grant_parent"),
                        ("status_parent", "status_child"),
                        ("stdout_parent", "stdout_child"),
                        ("stderr_parent", "stderr_child")):
        require((by_role[left]["dev"], by_role[left]["inode"])
                == (by_role[right]["dev"], by_role[right]["inode"]),
                "Linux abort pipe peers changed")
    require(len({(item["dev"], item["inode"])
                 for item in by_role.values()}) == 4,
            "Linux abort pipes are not distinct")
    return SUP.canonical(context)


def fork_abort_child_once(key, context):
    """Fork once; child cannot return into parent orchestration."""
    require(key is _FORK_KEY, "private abort fork boundary")
    frozen_bytes = _validate_prefork_context(context)
    frozen = copy.deepcopy(context)
    require(SUP.canonical(frozen) == frozen_bytes,
            "Linux abort context changed during copy")
    routine = SPLIT.AbortChildRoutine(SPLIT._CHILD_KEY, frozen)
    require(threading.active_count() == 1
            and len(os.listdir("/proc/self/task")) == 1,
            "abort fork requires an exact single-thread process")
    require(time.monotonic_ns() < frozen["deadline_ns"],
            "abort fork deadline reached")
    pid = os.fork()
    require(type(pid) is int and pid >= 0, "invalid abort fork result")
    if pid != 0:
        return pid
    try:
        allowed = {item["fd"] for item in frozen["descriptor"]["endpoints"]}
        _close_unlisted_fds(allowed)
        calls = LinuxAbortChildCalls(_CALLS_KEY, frozen)
        while True:
            routine.step(calls)
    except _RequestedExit as requested:
        code = requested.code
    except BaseException:
        code = SPLIT.EXIT_CHILD_ERROR
    os._exit(code)
