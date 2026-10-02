#!/usr/bin/env python3
"""Unshipped, local-only durable NONE-authority adapter; no effect/transport API.

Production root is fixed. for_test is an explicit disposable-filesystem seam,
not an environment/CLI override. No CLI is provided. Unknown/partial records
are retained; never overwritten, deleted, adopted or repaired automatically.
"""
import contextlib
import copy
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import stat

ROOT = Path("/var/lib/pve-sharedlvmthin/maintenance-coordinator")
LIMIT = 2 * 1024 * 1024
MAX_RECORDS = 512
_spec = importlib.util.spec_from_file_location("exact_local_journal_model",
    Path(__file__).with_name("layout-migration-exact-restore-model.py"))
MODEL = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(MODEL)


class Refusal(ValueError): pass


def need(value, message):
    if not value: raise Refusal(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(value): return hashlib.sha256(canonical(value)).hexdigest()


def decode(raw):
    def pairs(rows):
        value = {}
        for key, item in rows:
            need(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    value = json.loads(raw, object_pairs_hook=pairs,
                       parse_constant=lambda _: (_ for _ in ()).throw(Refusal("nonfinite JSON")))
    need(canonical(value) + b"\n" == raw, "noncanonical record")
    return value


def identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class Journal:
    def __init__(self, plan, *, create=False, fault=None):
        need(os.getuid() == os.geteuid() == 0, "root required")
        self._open(plan, ROOT, create, fault, test_anchor=None)

    @classmethod
    def for_test(cls, plan, root, *, create=False, fault=None):
        obj = object.__new__(cls)
        obj._open(plan, Path(root), create, fault, test_anchor=Path(root).parent)
        return obj

    def _fault(self, point):
        if self.fault: self.fault(point)

    def _dir(self, parent, name, *, private=False):
        fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
        try:
            info = os.fstat(fd)
            need(stat.S_ISDIR(info.st_mode) and info.st_uid == self.uid
                 and (stat.S_IMODE(info.st_mode) == 0o700 if private else not info.st_mode & 0o022),
                 "unsafe directory")
            self.chain.append((parent, name, fd, info.st_dev, info.st_ino, private))
            return fd
        except BaseException:
            os.close(fd); raise

    def _open(self, plan, root, create, fault, test_anchor):
        MODEL.validate_plan(plan, plan.get("issued_at") if type(plan) is dict else None)
        need(type(plan) is dict and type(plan.get("tx")) is str
             and re.fullmatch(r"[0-9a-f]{32}", plan["tx"]), "invalid transaction")
        need(root.is_absolute() and ".." not in root.parts, "invalid root")
        self.plan, self.tx, self.hash = copy.deepcopy(plan), plan["tx"], digest(plan)
        self.uid, self.chain, self.fault, self.poisoned, self.closed = os.geteuid(), [], fault, False, False
        self.lock = None
        try:
            if test_anchor is None:
                parent = self._dir(None, "/")
                for name in root.parent.parts[1:]: parent = self._dir(parent, name)
            else:
                parent = self._dir(None, str(test_anchor), private=True)
            if create:
                try:
                    os.mkdir(root.name, 0o700, dir_fd=parent)
                    os.fsync(parent); self._fault("root-parent-fsync")
                except FileExistsError: pass
            self.parent = parent
            self.root = self._dir(parent, root.name, private=True)
            if create:
                os.mkdir(self.tx, 0o700, dir_fd=self.root)
                os.fsync(self.root); self._fault("transaction-parent-fsync")
            self.directory = self._dir(self.root, self.tx, private=True)
            flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | (os.O_CREAT | os.O_EXCL if create else 0)
            self.lock = os.open(".journal.lock", flags, 0o600, dir_fd=self.directory)
            self.lock_identity = identity(os.fstat(self.lock))
            self._regular(os.fstat(self.lock), allow_empty=True)
            if create:
                os.fsync(self.lock); os.fsync(self.directory)
                with self._locked():
                    self._write("identity.json", {"schema": "slt-exact-local-journal/v1", "authority": "NONE",
                        "tx": self.tx, "plan_sha256": self.hash, "plan": self.plan})
            with self._locked(): self._validate_identity()
        except BaseException:
            self.close(); raise

    def close(self):
        if self.lock is not None:
            os.close(self.lock); self.lock = None
        for _, _, fd, _, _, _ in reversed(self.chain): os.close(fd)
        self.chain = []; self.closed = True

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

    def _regular(self, info, *, allow_empty=False):
        need(stat.S_ISREG(info.st_mode) and info.st_uid == self.uid and info.st_nlink == 1
             and stat.S_IMODE(info.st_mode) == 0o600 and 0 <= info.st_size <= LIMIT
             and (allow_empty or info.st_size > 0), "unsafe record")

    def _recheck(self):
        need(not self.closed and digest(self.plan) == self.hash, "closed or changed journal")
        for parent, name, fd, dev, ino, private in self.chain:
            for info in (os.fstat(fd), os.stat(name, dir_fd=parent, follow_symlinks=False)):
                need(stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (dev, ino)
                     and info.st_uid == self.uid
                     and (stat.S_IMODE(info.st_mode) == 0o700 if private else not info.st_mode & 0o022),
                     "directory namespace drift")
        for info in (os.fstat(self.lock), os.stat(".journal.lock", dir_fd=self.directory, follow_symlinks=False)):
            self._regular(info, allow_empty=True)
            need(identity(info) == self.lock_identity, "lock namespace drift")
        names = os.listdir(self.directory)
        need(len(names) <= MAX_RECORDS + 2 and all(name in ("identity.json", ".journal.lock")
             or re.fullmatch(r"[0-9a-f]{64}\.json", name) for name in names), "foreign journal entry or record budget")

    @contextlib.contextmanager
    def _locked(self):
        self._recheck()
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            self._recheck()
            yield
            self._recheck()
        finally: fcntl.flock(self.lock, fcntl.LOCK_UN)

    def _read(self, name, *, missing=False):
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=self.directory)
        except FileNotFoundError:
            need(missing, "required identity absent"); return None
        try:
            before = os.fstat(fd); self._regular(before)
            raw = bytearray()
            while len(raw) <= LIMIT:
                block = os.read(fd, min(65536, LIMIT + 1 - len(raw)))
                if not block: break
                raw.extend(block)
            need(len(raw) == before.st_size and len(raw) <= LIMIT, "record size changed")
            value = decode(bytes(raw))
            os.fsync(fd); self._fault("read-file-fsync")
            need(identity(before) == identity(os.fstat(fd)) == identity(os.stat(name, dir_fd=self.directory, follow_symlinks=False)),
                 "record namespace drift")
            return value
        finally: os.close(fd)

    def _write(self, name, value):
        raw = canonical(value) + b"\n"
        need(0 < len(raw) <= LIMIT, "record size budget")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     0o600, dir_fd=self.directory)
        try:
            self._fault("file-created")
            offset = 0
            while offset < len(raw):
                count = os.write(fd, raw[offset:]); need(count > 0, "short write")
                offset += count
            self._fault("file-written")
            os.fsync(fd); self._fault("file-fsync")
            self._regular(os.fstat(fd))
        finally: os.close(fd)
        os.fsync(self.directory); self._fault("directory-fsync")
        os.fsync(self.root); self._fault("parent-fsync")
        os.fsync(self.parent); self._fault("root-parent-record-fsync")
        need(self._read(name) == value, "stored readback differs")
        self._recheck(); self._fault("before-ack")

    def _validate_identity(self):
        need(self._read("identity.json") == {"schema": "slt-exact-local-journal/v1", "authority": "NONE",
            "tx": self.tx, "plan_sha256": self.hash, "plan": self.plan}, "journal identity differs")

    def _name(self, tx, key):
        need(tx == self.tx and type(key) is str, "journal transaction/key differs")
        # Every name comes from the fixed plan cohort and restoration vocabulary.
        # File naming, descriptor checks and create-only publication stay shared.
        nodes = [row["node"] for row in self.plan["participants"]]
        parts = key.split(":")
        allowed = key in {"baseline", "flow:started", "flow:held", "flow:release", "flow:complete"}
        if len(parts) == 2 and parts[0] == "node-baseline": allowed = parts[1] in nodes
        if len(parts) == 3 and parts[0] == "barrier":
            allowed = (parts[1] in {f"{i:02d}" for i in range(3 * len(nodes))}
                       and parts[2] in ("INTENT_DURABLE", "ATTEMPTED", "OBSERVED_COMPLETE"))
        if len(parts) == 3 and parts[0] == "archive":
            allowed = (parts[1] in {r["node"] for r in self.plan["participants"] if r["role"] == "SAN"}
                       and parts[2] in ("intent", "done"))
        effect = None
        if len(parts) == 4 and parts[3] in ("grant", "intent", "done"): effect = parts[:3]
        if len(parts) == 4 and parts[0] == "receipt": effect = parts[1:]
        if len(parts) == 5 and parts[0] == "transport" and parts[4] in ("intent", "done"): effect = parts[1:4]
        if effect is not None:
            allowed = effect[0] in nodes and effect[1] in MODEL.MASKED_BY_BARRIER and effect[2] in ("UNMASK", "START")
        need(allowed, "key outside closed model vocabulary")
        return hashlib.sha256(key.encode("ascii")).hexdigest() + ".json"

    def create_once(self, tx, key, value):
        need(not self.poisoned, "uncertain publication; new reader required")
        name = self._name(tx, key)
        record = {"schema": "slt-exact-local-record/v1", "authority": "NONE", "tx": tx,
                  "plan_sha256": self.hash, "key": key, "value": copy.deepcopy(value), "value_sha256": digest(value)}
        with self._locked():
            self._validate_identity()
            self.poisoned = True
            self._write(name, record)
            self.poisoned = False
        return {"durable": True, "sha256": digest(value)}

    def read(self, tx, key):
        name = self._name(tx, key)
        with self._locked():
            self._validate_identity()
            value = self._read(name, missing=True)
            os.fsync(self.directory); os.fsync(self.root); os.fsync(self.parent)
            if value is None: return None
            need(type(value) is dict and set(value) == {"schema", "authority", "tx", "plan_sha256", "key", "value", "value_sha256"}
                 and value["schema"] == "slt-exact-local-record/v1" and value["authority"] == "NONE"
                 and value["tx"] == tx and value["plan_sha256"] == self.hash and value["key"] == key
                 and value["value_sha256"] == digest(value["value"]), "record binding differs")
            return copy.deepcopy(value["value"])

    def reconcile(self, tx, key, expected):
        need(self.read(tx, key) == expected, "exact expected publication not found")
        return {"authority": "NONE", "state": "DURABLE_RECORD_OBSERVED", "sha256": digest(expected),
                "effect_retry_allowed": False}
