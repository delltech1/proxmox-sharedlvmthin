#!/usr/bin/python3
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Default-refusing source scaffold for a fixed non-storage process fixture."""

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import select
import signal
import stat
import sys
import threading
import time

sys.dont_write_bytecode = True

ACK = "I_UNDERSTAND_FIXTURE_PROCESS_EXECUTION_IS_NOT_YET_QUALIFIED"
TRUE_PATH = "/usr/bin/true"
CLEAN_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
PR_GET_CHILD_SUBREAPER = 37
PR_SET_CHILD_SUBREAPER = 36
JOURNAL_PARENT = "/var/tmp"
JOURNAL_PREFIX = "slt-prelive-fixture-"
JOURNAL_EVENT_KINDS = frozenset({
    "INTENT", "CHILD_BOUND", "EXEC_ISSUED", "PROCESS_TERMINAL",
    "UNKNOWN_PRESERVE",
})
MAX_JOURNAL_RECORD_BYTES = 16384


class Refusal(RuntimeError):
    pass


def _canonical_record(value):
    data = (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")
    if len(data) > MAX_JOURNAL_RECORD_BYTES:
        raise Refusal("journal record exceeds the fixed size limit")
    return data


def _write_all(fd, data):
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        if type(written) is not int or written <= 0:
            raise OSError("journal write made no progress")
        offset += written


def _process_starttime(pid):
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = raw.rfind(")")
    fields = raw[closing + 2:].split()
    if closing < 1 or len(fields) < 20 or not fields[19].isdigit():
        raise Refusal("cannot prove journal owner process starttime")
    return int(fields[19])


def _boot_id():
    value = Path("/proc/sys/kernel/random/boot_id").read_text(
        encoding="ascii").strip().lower()
    if len(value) != 36 or value.count("-") != 4:
        raise Refusal("cannot prove journal owner boot identity")
    return value


class FixtureJournal:
    """Fresh, append-only source fixture; deliberately has no reopen path."""

    def __init__(self, request_id):
        if (type(request_id) is not str or len(request_id) != 32
                or any(ch not in "0123456789abcdef" for ch in request_id)):
            raise Refusal("request ID must be exactly 32 lowercase hex characters")
        self._state = "CREATING"
        self._poisoned = False
        self._sequence = 0
        self._root_fd = None
        self._parent_fd = None
        self._owner_pid = os.getpid()
        self._owner_thread = threading.current_thread()
        self._request_id = request_id
        self._root_name = JOURNAL_PREFIX + request_id
        self._boot_id = _boot_id()
        self._owner_starttime = _process_starttime(self._owner_pid)
        try:
            self._create_fresh()
            if self._poisoned or self._state != "APPENDING":
                self._poison()
                raise Refusal("journal creation state became ambiguous")
            self._state = "READY"
        except BaseException:
            self._poison()
            self._close_owned_fds()
            raise

    def _poison(self):
        self._poisoned = True
        self._state = "POISONED"

    def _assert_owner(self):
        if (os.getpid() != self._owner_pid
                or threading.current_thread() is not self._owner_thread):
            self._poison()
            raise Refusal("journal use crossed its owning process or thread")

    def _close_owned_fds(self):
        failure = None
        for name in ("_root_fd", "_parent_fd"):
            fd = getattr(self, name, None)
            if fd is not None:
                setattr(self, name, None)
                try:
                    os.close(fd)
                except OSError as exc:
                    failure = failure or exc
        return failure

    def _verify_namespace(self):
        try:
            parent_fd = os.fstat(self._parent_fd)
            parent_name = os.stat(JOURNAL_PARENT, follow_symlinks=False)
            root_fd = os.fstat(self._root_fd)
            root_name = os.stat(self._root_name, dir_fd=self._parent_fd,
                                follow_symlinks=False)
        except (OSError, TypeError):
            self._poison()
            raise Refusal("journal namespace continuity is unprovable")
        expected_parent = (self._parent_identity["dev"],
                           self._parent_identity["inode"])
        expected_root = (self._root_identity["dev"], self._root_identity["inode"])
        if ((parent_fd.st_dev, parent_fd.st_ino) != expected_parent
                or (parent_name.st_dev, parent_name.st_ino) != expected_parent
                or (root_fd.st_dev, root_fd.st_ino) != expected_root
                or (root_name.st_dev, root_name.st_ino) != expected_root
                or not stat.S_ISDIR(parent_fd.st_mode)
                or not stat.S_ISDIR(parent_name.st_mode)
                or parent_fd.st_uid != self._parent_identity["uid"]
                or parent_name.st_uid != self._parent_identity["uid"]
                or stat.S_IMODE(parent_fd.st_mode) != self._parent_identity["mode"]
                or stat.S_IMODE(parent_name.st_mode) != self._parent_identity["mode"]
                or not stat.S_ISDIR(root_fd.st_mode)
                or not stat.S_ISDIR(root_name.st_mode)
                or root_fd.st_uid != self._root_identity["uid"]
                or root_name.st_uid != self._root_identity["uid"]
                or stat.S_IMODE(root_fd.st_mode) != self._root_identity["mode"]
                or stat.S_IMODE(root_name.st_mode) != self._root_identity["mode"]):
            self._poison()
            raise Refusal("journal namespace identity changed")

    def _persist_new_file(self, name, data):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(name, flags, 0o600, dir_fd=self._root_fd)
        failure = None
        def verify_record():
            info = os.fstat(fd)
            named = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)
                    or not stat.S_ISREG(named.st_mode)
                    or named.st_uid != os.geteuid()
                    or stat.S_IMODE(named.st_mode) != 0o600
                    or named.st_nlink != 1):
                raise Refusal("journal record identity or mode is unsafe")
        try:
            verify_record()
            _write_all(fd, data)
            os.fsync(fd)
            os.fsync(self._root_fd)
            verify_record()
        except BaseException as exc:
            failure = exc
        try:
            os.close(fd)
        except BaseException as exc:
            failure = failure or exc
        if failure is not None:
            raise failure

    def _create_fresh(self):
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        self._parent_fd = os.open(JOURNAL_PARENT, flags)
        parent = os.fstat(self._parent_fd)
        if not stat.S_ISDIR(parent.st_mode):
            raise Refusal("journal parent is not a pinned directory")
        self._parent_identity = {"dev": parent.st_dev, "inode": parent.st_ino,
                                 "uid": parent.st_uid,
                                 "mode": stat.S_IMODE(parent.st_mode)}
        os.mkdir(self._root_name, 0o700, dir_fd=self._parent_fd)
        os.fsync(self._parent_fd)
        self._root_fd = os.open(self._root_name, flags, dir_fd=self._parent_fd)
        root = os.fstat(self._root_fd)
        named = os.stat(self._root_name, dir_fd=self._parent_fd,
                        follow_symlinks=False)
        if (not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid()
                or stat.S_IMODE(root.st_mode) != 0o700
                or (root.st_dev, root.st_ino) != (named.st_dev, named.st_ino)):
            raise Refusal("fresh journal root identity or mode is unsafe")
        self._root_identity = {"dev": root.st_dev, "inode": root.st_ino,
                               "uid": root.st_uid, "mode": 0o700}
        owner = self._base_record()
        owner.update({"schema": 1, "record": "OWNER"})
        self._state = "APPENDING"
        self._persist_new_file("journal-owner.json", _canonical_record(owner))

    def _base_record(self):
        return {
            "request_id": self._request_id,
            "boot_id": self._boot_id,
            "owner_pid": self._owner_pid,
            "owner_starttime": self._owner_starttime,
            "parent": dict(self._parent_identity),
            "root": dict(self._root_identity),
        }

    def append(self, kind):
        self._assert_owner()
        if self._poisoned:
            raise Refusal("poisoned journal cannot append")
        if self._state == "APPENDING":
            self._poison()
            raise Refusal("reentrant journal append makes persistence ambiguous")
        if self._state != "READY":
            raise Refusal("journal is not ready for a new immutable event")
        if type(kind) is not str or kind not in JOURNAL_EVENT_KINDS:
            raise Refusal("journal event kind is not in the closed schema")
        sequence = self._sequence + 1
        record = self._base_record()
        record.update({"schema": 1, "record": "EVENT",
                       "sequence": sequence, "kind": kind})
        data = _canonical_record(record)
        self._verify_namespace()
        self._state = "APPENDING"
        self._sequence = sequence
        try:
            self._persist_new_file(f"event-{sequence:06d}.json", data)
        except BaseException:
            self._poison()
            raise
        if self._poisoned:
            raise Refusal("journal was poisoned while persistence was in progress")
        self._verify_namespace()
        if self._poisoned or self._state != "APPENDING":
            self._poison()
            raise Refusal("journal state changed while persistence was in progress")
        self._state = "READY"
        return hashlib.sha256(data).hexdigest()

    def close(self):
        self._assert_owner()
        if self._poisoned:
            self._close_owned_fds()
            raise Refusal("poisoned journal cannot transition to closed-safe")
        if self._state in ("APPENDING", "CLOSING"):
            self._poison()
            raise Refusal("cannot close a journal with ambiguous persistence")
        if self._state != "READY":
            raise Refusal("journal is not open for a clean close")
        self._state = "CLOSING"
        failure = self._close_owned_fds()
        if failure is not None or self._poisoned:
            self._poison()
            raise Refusal("journal descriptor close outcome is ambiguous") from failure
        self._state = "CLOSED"


def plan():
    return {
        "schema": 1,
        "classification": "SOURCE_STAGED_EXECUTION_DISABLED",
        "argv": [TRUE_PATH],
        "environment": CLEAN_ENV,
        "storage_authorized": False,
        "postcondition_verified": False,
        "runtime_authorized": False,
        "implemented_scope": "pinned-fd unarmed pre-grant launcher only",
        "missing_scope": ["reviewed journal", "monitor", "settlement", "reap"],
    }


def _sha256_fd(fd):
    digest = hashlib.sha256()
    offset = 0
    while True:
        block = os.pread(fd, 1024 * 1024, offset)
        if not block:
            return digest.hexdigest()
        digest.update(block)
        offset += len(block)


def open_pinned_executable(path=TRUE_PATH):
    if path != TRUE_PATH:
        raise Refusal("fixture executable must be exactly /usr/bin/true")
    require_standard_fds()
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if type(fd) is not int or fd <= 2:
            raise Refusal("fixture executable returned an unsafe FD")
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise Refusal("fixture executable identity or permissions are unsafe")
        return fd, {"path": TRUE_PATH, "sha256": _sha256_fd(fd),
                    "dev": info.st_dev, "inode": info.st_ino,
                    "size": info.st_size, "mode": stat.S_IMODE(info.st_mode)}
    except BaseException:
        os.close(fd)
        raise


def prctl_subreaper(command, value=0):
    if command not in (PR_GET_CHILD_SUBREAPER, PR_SET_CHILD_SUBREAPER):
        raise Refusal("unsupported prctl operation")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong,
                           ctypes.c_ulong, ctypes.c_ulong]
    libc.prctl.restype = ctypes.c_int
    if command == PR_GET_CHILD_SUBREAPER:
        output = ctypes.c_int(-1)
        pointer = ctypes.cast(ctypes.byref(output), ctypes.c_void_p).value
        result = libc.prctl(ctypes.c_int(command), ctypes.c_void_p(pointer),
                            ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
        if result != 0:
            raise OSError(ctypes.get_errno(), "PR_GET_CHILD_SUBREAPER")
        return output.value
    if type(value) is not int or value not in (0, 1):
        raise Refusal("invalid subreaper value")
    result = libc.prctl(ctypes.c_int(command), ctypes.c_void_p(value),
                        ctypes.c_ulong(0), ctypes.c_ulong(0), ctypes.c_ulong(0))
    if result != 0:
        raise OSError(ctypes.get_errno(), "PR_SET_CHILD_SUBREAPER")
    return None


def _pipe():
    return os.pipe2(os.O_CLOEXEC)


def require_standard_fds():
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError as exc:
            raise Refusal(f"standard FD {fd} is not open") from exc


def create_fd_bundle():
    require_standard_fds()
    allocated = []
    try:
        pairs = []
        for unused in range(4):
            pair = _pipe()
            if type(pair) is not tuple or len(pair) != 2:
                raise Refusal("pipe returned an unsafe FD")
            allocated.extend(pair)
            if any(type(fd) is not int or fd <= 2 for fd in pair):
                raise Refusal("pipe returned an unsafe FD")
            pairs.append(pair)
        null_fd = os.open("/dev/null", os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        allocated.append(null_fd)
        if type(null_fd) is not int or null_fd <= 2:
            raise Refusal("null device returned an unsafe FD")
        values = list(allocated)
        if len(values) != len(set(values)):
            raise Refusal("fixture FD allocation aliased")
        (grant_r, grant_w), (status_r, status_w), \
            (stdout_r, stdout_w), (stderr_r, stderr_w) = pairs
        return {"grant_r": grant_r, "grant_w": grant_w,
                "status_r": status_r, "status_w": status_w,
                "stdout_r": stdout_r, "stdout_w": stdout_w,
                "stderr_r": stderr_r, "stderr_w": stderr_w,
                "null_fd": null_fd}
    except BaseException:
        for fd in reversed(allocated):
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _close_all_except(keep, inherited):
    for fd in inherited:
        if fd not in keep:
            try:
                os.close(fd)
            except OSError:
                pass


def snapshot_open_fds():
    values = []
    with os.scandir("/proc/self/fd") as entries:
        for entry in entries:
            if entry.name.isdigit():
                values.append(int(entry.name))
    return sorted(set(values))


def _child_fail(status_fd, message, code):
    try:
        os.write(status_fd, ("E:" + message)[:1024].encode("ascii", "replace"))
    finally:
        os._exit(code)


def _child_launcher(bundle, executable_fd, inherited_fds, unarmed_deadline_ms):
    try:
        keep = {bundle["grant_r"], bundle["status_w"], bundle["stdout_w"],
                bundle["stderr_w"], bundle["null_fd"], executable_fd}
        _close_all_except(keep, inherited_fds)
        os.dup2(bundle["null_fd"], 0)
        os.dup2(bundle["stdout_w"], 1)
        os.dup2(bundle["stderr_w"], 2)
        for fd in (bundle["stdout_w"], bundle["stderr_w"], bundle["null_fd"]):
            if fd > 2:
                os.close(fd)
        os.write(bundle["status_w"], b"R")
        poller = select.poll()
        poller.register(bundle["grant_r"], select.POLLIN | select.POLLHUP | select.POLLERR)
        deadline = time.monotonic() + unarmed_deadline_ms / 1000
        grant = b""
        saw_eof = False
        while time.monotonic() < deadline and not saw_eof:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            events = poller.poll(remaining)
            if not events:
                continue
            chunk = os.read(bundle["grant_r"], 2 - len(grant))
            if chunk:
                grant += chunk
                if len(grant) > 1:
                    _child_fail(bundle["status_w"], "extra grant bytes", 121)
            else:
                saw_eof = True
        if grant != b"G" or not saw_eof:
            _child_fail(bundle["status_w"], "missing exact grant and EOF", 122)
        os.close(bundle["grant_r"])
        # status_w is CLOEXEC: EOF plus exact terminal receipt is later evidence
        # for the pinned-FD protocol, never a standalone proof of exec.
        os.execve(executable_fd, [TRUE_PATH], dict(CLEAN_ENV))
        _child_fail(bundle["status_w"], "execve returned", 123)
    except BaseException as exc:
        _child_fail(bundle["status_w"], type(exc).__name__, 124)


def spawn_unarmed(bundle, executable_fd, lifecycle, unarmed_deadline_ms=5000):
    if type(unarmed_deadline_ms) is not int or not 1 <= unarmed_deadline_ms <= 10000:
        raise Refusal("unarmed deadline is invalid")
    if type(lifecycle) is not dict or lifecycle:
        raise Refusal("fresh caller-owned lifecycle record is required")
    if (type(executable_fd) is not int or executable_fd <= 2
            or executable_fd in bundle.values()):
        raise Refusal("executable FD is unsafe or aliases the pipe bundle")
    inherited = snapshot_open_fds()
    pid = os.fork()
    if pid == 0:
        _child_launcher(bundle, executable_fd, inherited, unarmed_deadline_ms)
        os._exit(125)
    # This assignment is intentionally the first parent-side operation after
    # fork. Later failures must retain this exact owned PID for settlement.
    lifecycle["owned_child"] = {"pid": pid, "pidfd": None,
                                "grant_sent": False, "settled": False}
    for name in ("grant_r", "status_w", "stdout_w", "stderr_w", "null_fd"):
        os.close(bundle[name])
    for name in ("status_r", "stdout_r", "stderr_r"):
        os.set_blocking(bundle[name], False)
    lifecycle["parent_fds"] = {
        "grant_w": bundle["grant_w"], "status_r": bundle["status_r"],
        "stdout_r": bundle["stdout_r"], "stderr_r": bundle["stderr_r"],
        "executable_fd": executable_fd,
    }
    return lifecycle


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute-token")
    args = parser.parse_args(argv)
    value = plan()
    if args.execute_token is not None:
        value["classification"] = "REFUSED_LIVE_BACKEND_NOT_QUALIFIED"
        value["token_valid"] = args.execute_token == ACK
        print(json.dumps(value, sort_keys=True))
        return 2
    print(json.dumps(value, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
