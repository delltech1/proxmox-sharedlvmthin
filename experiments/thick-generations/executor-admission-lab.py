#!/usr/bin/python3
"""Disposable, non-storage-mutating executor-admission backend qualification.

This exercises local serialization and conditional state transitions only.  It
does not touch LVM, device-mapper, PVE configuration, or guest storage and does
not establish power-loss durability.
"""

import argparse
import fcntl
import json
import os
import pathlib
import re
import selectors
import signal
import stat
import sys
import time
import uuid


ACK = "DISPOSABLE-NONSTORAGE-ADMISSION-LAB"
ROOT_PARENT = pathlib.Path("/var/tmp")
ROOT_PREFIX = "slt-executor-admission-lab-"
HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX24 = re.compile(r"^[a-f0-9]{24}$")


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


class Backend:
    @staticmethod
    def validate_root_name(root):
        root = pathlib.Path(root)
        if not root.is_absolute() or root.parent != ROOT_PARENT \
                or not root.name.startswith(ROOT_PREFIX):
            raise RuntimeError("lab root must be a direct /var/tmp/slt-executor-admission-lab-* path")
        return root

    @classmethod
    def initialize(cls, root):
        root = cls.validate_root_name(root)
        parent_fd = os.open(ROOT_PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        backend = cls(root, allow_uninitialized=True)
        try:
            backend._acquire_lock()
            initial = {"schema": 1, "revision": 0, "consumed_attempts": [], "slots": {}}
            backend._store(initial)
            backend.allow_uninitialized = False
        finally:
            fcntl.flock(backend.lock_fd, fcntl.LOCK_UN)
        return backend

    def __init__(self, root, allow_uninitialized=False):
        self.root = self.validate_root_name(root)
        self.allow_uninitialized = allow_uninitialized
        parent_fd = os.open(
            ROOT_PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
        )
        try:
            self.root_fd = os.open(
                self.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent_fd,
            )
        finally:
            os.close(parent_fd)
        root_stat = os.fstat(self.root_fd)
        if root_stat.st_uid != 0 or stat.S_IMODE(root_stat.st_mode) != 0o700:
            os.close(self.root_fd)
            raise RuntimeError("lab root must be root-owned mode 0700")
        lock_flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if allow_uninitialized:
            lock_flags |= os.O_CREAT | os.O_EXCL
        try:
            self.lock_fd = os.open("ledger.lock", lock_flags, 0o600, dir_fd=self.root_fd)
        except FileNotFoundError:
            os.close(self.root_fd)
            raise RuntimeError("stable lock inode is missing; authority state is UNKNOWN")
        lock_stat = os.fstat(self.lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != 0 \
                or stat.S_IMODE(lock_stat.st_mode) != 0o600:
            os.close(self.lock_fd)
            os.close(self.root_fd)
            raise RuntimeError("ledger lock must be root-owned mode 0600")

    def close(self):
        os.close(self.lock_fd)
        os.close(self.root_fd)

    def _acquire_lock(self, timeout=10):
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("authority lock acquisition timed out; no takeover attempted")
                time.sleep(0.01)

    def _load(self):
        try:
            fd = os.open("ledger.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=self.root_fd)
        except FileNotFoundError:
            if self.allow_uninitialized:
                return {"schema": 1, "revision": 0, "consumed_attempts": [], "slots": {}}
            raise RuntimeError("ledger history is missing; authority state is UNKNOWN")
        try:
            ledger_stat = os.fstat(fd)
            if not stat.S_ISREG(ledger_stat.st_mode) or ledger_stat.st_uid != 0 \
                    or stat.S_IMODE(ledger_stat.st_mode) != 0o600:
                raise RuntimeError("ledger must be a root-owned mode 0600 regular file")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                data = json.load(stream, object_pairs_hook=reject_duplicate_keys)
        finally:
            if fd >= 0:
                os.close(fd)
        if not isinstance(data, dict) or set(data) != {
            "schema", "revision", "consumed_attempts", "slots"
        }:
            raise RuntimeError("ledger is malformed")
        if type(data["schema"]) is not int or data["schema"] != 1:
            raise RuntimeError("ledger schema is invalid")
        if type(data["revision"]) is not int \
                or data["revision"] < 1:
            raise RuntimeError("ledger revision is invalid")
        attempts = data["consumed_attempts"]
        if not isinstance(attempts, list) or len(set(attempts)) != len(attempts) \
                or any(not isinstance(item, str) or not HEX32.fullmatch(item) for item in attempts):
            raise RuntimeError("consumed-attempt history is invalid")
        if not isinstance(data["slots"], dict):
            raise RuntimeError("ledger slots are invalid")
        for key, active in data["slots"].items():
            if not isinstance(key, str) or not HEX24.fullmatch(key) or not isinstance(active, dict):
                raise RuntimeError("active slot identity is invalid")
            if active.get("object_key") != key or active.get("attempt") not in attempts \
                    or active.get("state") not in ("RESERVED", "BOUND", "TERMINAL", "UNKNOWN"):
                raise RuntimeError("active slot is inconsistent with authority history")
        return data

    def _store(self, data):
        data = dict(data)
        data["revision"] += 1
        temporary = "ledger.tmp." + uuid.uuid4().hex
        fd = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600, dir_fd=self.root_fd,
        )
        try:
            payload = canonical(data)
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, "ledger.json", src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
        os.fsync(self.root_fd)
        return data

    def transact(self, callback):
        self._acquire_lock()
        try:
            before = self._load()
            after, result = callback(before)
            if after is not None:
                after = self._store(after)
            return result, after if after is not None else before
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)

    @staticmethod
    def _key(record):
        key = record.get("object_key", "")
        if not HEX24.fullmatch(key):
            raise RuntimeError("invalid object key")
        return key

    def reserve(self, record):
        def operation(data):
            key = self._key(record)
            attempt = record.get("attempt", "")
            if not HEX32.fullmatch(attempt):
                raise RuntimeError("invalid attempt")
            if key in data["slots"]:
                return None, {"allowed": False, "reason": "slot occupied"}
            if attempt in data["consumed_attempts"]:
                return None, {"allowed": False, "reason": "attempt already consumed"}
            updated = json.loads(json.dumps(data))
            candidate = dict(record)
            candidate["state"] = "RESERVED"
            updated["slots"][key] = candidate
            updated["consumed_attempts"].append(attempt)
            return updated, {"allowed": True, "state": "RESERVED"}

        return self.transact(operation)

    def bind(self, expected, invocation_id):
        def operation(data):
            key = self._key(expected)
            current = data["slots"].get(key)
            if current != expected or current.get("state") != "RESERVED":
                return None, {"allowed": False, "reason": "conditional bind lost"}
            if not HEX32.fullmatch(invocation_id):
                raise RuntimeError("invalid invocation ID")
            updated = json.loads(json.dumps(data))
            updated["slots"][key] = {**current, "state": "BOUND", "invocation_id": invocation_id}
            return updated, {"allowed": True, "state": "BOUND"}

        return self.transact(operation)

    def transition(self, expected, state, result=None, terminal_evidence_proven=False):
        if state not in ("TERMINAL", "UNKNOWN"):
            raise RuntimeError("invalid terminal transition")

        def operation(data):
            key = self._key(expected)
            current = data["slots"].get(key)
            if current != expected or current.get("state") != "BOUND":
                return None, {"allowed": False, "reason": "conditional finish lost"}
            updated = json.loads(json.dumps(data))
            next_record = {**current, "state": state}
            if state == "TERMINAL":
                if terminal_evidence_proven is not True:
                    raise RuntimeError("terminal evidence is not positively proven")
                if result not in ("SUCCESS", "FAILED"):
                    raise RuntimeError("terminal result is invalid")
                next_record["executor_result"] = result
            updated["slots"][key] = next_record
            return updated, {"allowed": True, "state": state}

        return self.transact(operation)

    def close_slot(self, expected):
        def operation(data):
            key = self._key(expected)
            current = data["slots"].get(key)
            if current != expected or current.get("state") != "TERMINAL":
                return None, {"allowed": False, "reason": "conditional close lost"}
            updated = json.loads(json.dumps(data))
            del updated["slots"][key]
            return updated, {"allowed": True, "state": "CLOSED"}

        return self.transact(operation)


def record(object_key, transaction, attempt):
    return {
        "schema": 1,
        "kind": "THICK_EXECUTOR",
        "object_key": object_key,
        "vg_uuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
        "volume": "vm-900001-disk-0",
        "transaction": transaction,
        "attempt": attempt,
        "node": "qualification-node",
        "boot_id": "11111111-2222-3333-4444-555555555555",
        "unit": f"slt-thick-exec-{attempt}.service",
        "code_digest": "2" * 64,
        "policy_digest": "3" * 64,
        "enrollment_epoch": "e" * 64,
    }


def qualify(root):
    backend = Backend.initialize(root)
    checks = []

    def check(condition, name):
        if not condition:
            raise RuntimeError("qualification failed: " + name)
        checks.append(name)

    try:
        # Every contender opens its own lock file description.  Sharing a
        # pre-fork descriptor would share one flock owner and invalidate this
        # concurrency test.
        backend.close()
        backend = None
        readers = []
        children = []
        start_read, start_write = os.pipe()
        for index in range(8):
            read_fd, write_fd = os.pipe()
            pid = os.fork()
            if pid == 0:
                os.close(read_fd)
                os.close(start_write)
                try:
                    if os.read(start_read, 1) != b"S":
                        raise RuntimeError("concurrency barrier was not released")
                    os.close(start_read)
                    child_backend = Backend(root)
                    contender = record("f" * 24, "0" * 32, f"{index + 1:032x}")
                    outcome, _ = child_backend.reserve(contender)
                    os.write(write_fd, b"1" if outcome["allowed"] else b"0")
                    child_backend.close()
                    os._exit(0)
                except BaseException:
                    os.write(write_fd, b"E")
                    os._exit(1)
            os.close(write_fd)
            readers.append(read_fd)
            children.append(pid)
        os.close(start_read)
        os.write(start_write, b"S" * len(children))
        os.close(start_write)
        contender_results = []
        selector = selectors.DefaultSelector()
        for read_fd in readers:
            selector.register(read_fd, selectors.EVENT_READ)
        deadline = time.monotonic() + 10
        while selector.get_map():
            ready = selector.select(max(0, deadline - time.monotonic()))
            if not ready:
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                raise RuntimeError("concurrent reservation qualification timed out")
            for key, _ in ready:
                contender_results.append(os.read(key.fd, 1))
                selector.unregister(key.fd)
                os.close(key.fd)
        selector.close()
        child_statuses = []
        remaining = set(children)
        while remaining and time.monotonic() < deadline:
            for pid in tuple(remaining):
                waited, status = os.waitpid(pid, os.WNOHANG)
                if waited:
                    child_statuses.append(status)
                    remaining.remove(pid)
            if remaining:
                time.sleep(0.01)
        if remaining:
            for pid in remaining:
                os.kill(pid, signal.SIGKILL)
            cleanup_deadline = time.monotonic() + 1
            while remaining and time.monotonic() < cleanup_deadline:
                for pid in tuple(remaining):
                    waited, _ = os.waitpid(pid, os.WNOHANG)
                    if waited:
                        remaining.remove(pid)
                if remaining:
                    time.sleep(0.01)
            raise RuntimeError(
                "concurrent contender exit timed out; SIGKILL is not terminal proof; "
                f"unreaped={sorted(remaining)}"
            )
        check(all(status == 0 for status in child_statuses), "all concurrent contenders completed")
        check(contender_results.count(b"1") == 1 and contender_results.count(b"0") == 7,
              "eight concurrent attempts elect exactly one reservation")
        backend = Backend(root)

        first = record("a" * 24, "b" * 32, "1" * 32)
        second = record("a" * 24, "b" * 32, "4" * 32)
        outcome, ledger = backend.reserve(first)
        check(outcome["allowed"], "initial reserve")
        reserved = ledger["slots"][first["object_key"]]
        outcome, _ = backend.reserve(second)
        check(not outcome["allowed"], "same-transaction second attempt refused")

        invocation = "5" * 32
        outcome, ledger = backend.bind(reserved, invocation)
        check(outcome["allowed"], "exact conditional bind")
        bound = ledger["slots"][first["object_key"]]
        outcome, _ = backend.bind(reserved, "6" * 32)
        check(not outcome["allowed"], "losing invocation refused")
        outcome, ledger = backend.transition(bound, "UNKNOWN")
        check(outcome["allowed"], "ambiguous executor becomes UNKNOWN")
        unknown = ledger["slots"][first["object_key"]]
        outcome, _ = backend.close_slot(unknown)
        check(not outcome["allowed"], "UNKNOWN cannot close")

        old = record("c" * 24, "d" * 32, "7" * 32)
        outcome, ledger = backend.reserve(old)
        check(outcome["allowed"], "independent volume reserve")
        old_reserved = ledger["slots"][old["object_key"]]
        outcome, ledger = backend.bind(old_reserved, "8" * 32)
        check(outcome["allowed"], "independent exact bind")
        old_bound = ledger["slots"][old["object_key"]]
        outcome, ledger = backend.transition(
            old_bound, "TERMINAL", "SUCCESS", terminal_evidence_proven=True
        )
        check(outcome["allowed"], "exact terminal transition")
        old_terminal = ledger["slots"][old["object_key"]]
        outcome, _ = backend.close_slot(old_terminal)
        check(outcome["allowed"], "exact conditional close")
        outcome, _ = backend.reserve(old)
        check(not outcome["allowed"], "closed attempt replay refused")

        new = record("c" * 24, "d" * 32, "9" * 32)
        outcome, _ = backend.reserve(new)
        check(outcome["allowed"], "new attempt after close")
        outcome, _ = backend.close_slot(old_terminal)
        check(not outcome["allowed"], "delayed stale close refused")

        final = backend._load()
        check(first["attempt"] in final["consumed_attempts"], "first attempt tombstone retained")
        check(old["attempt"] in final["consumed_attempts"], "closed attempt tombstone retained")
        check(new["attempt"] in final["consumed_attempts"], "new attempt tombstone retained")
        print(json.dumps({
            "kind": "executor-admission-python-cas-backend-qualification",
            "result": "PASS",
            "checks": checks,
            "ledger_revision": final["revision"],
            "limitations": [
                "no LVM, device-mapper, PVE configuration or guest storage was touched",
                "systemd/cgroup invocation binding was not exercised by this phase",
                "the Perl admission decision model was not invoked by this phase",
                "power-loss durability and cross-node authority were not tested",
            ],
        }, sort_keys=True, indent=2))
    finally:
        if backend is not None:
            backend.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--ack", required=True)
    parser.add_argument("qualify", choices=("qualify",))
    args = parser.parse_args()
    if args.ack != ACK:
        raise SystemExit("explicit disposable-lab acknowledgement is required")
    qualify(args.root)


if __name__ == "__main__":
    main()
