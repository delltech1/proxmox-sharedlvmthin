#!/usr/bin/env python3
"""Create-only local request latch. Reopening is inspection only; no live CLI."""
import copy
import hashlib
import importlib.util
import json
import os
import stat
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("node_latch_backend", HERE / "layout-migration-barrier-backend.py")
B = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(B)
BASE = B.JOURNAL.BASE
Refusal = B.Refusal
require = B.require
PRODUCTION_ROOT = Path("/var/lib/pve-sharedlvmthin/barrier-node-attempts")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def request_nonce(request):
    return hashlib.sha256(canonical(request)).hexdigest()[:32]


def _safe_root(root, create=False):
    root = Path(root)
    require(root.is_absolute() and root.name not in ("", ".", ".."), "latch root invalid")
    parent = root.parent
    ancestors = (Path("/"), Path("/var"), Path("/var/lib"), parent) if root == PRODUCTION_ROOT else (parent,)
    for path in ancestors:
        info = path.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid() and not info.st_mode & 0o022,
                "latch ancestor unsafe")
    if create:
        try:
            root.mkdir(mode=0o700)
            pfd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try: os.fsync(pfd)
            finally: os.close(pfd)
        except FileExistsError:
            pass
    info = root.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o700, "latch root unsafe")
    return root


def _write_once(directory_fd, name, raw):
    require(type(raw) is bytes and 0 < len(raw) <= 16 * 1024 * 1024, "latch record size invalid")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            require(type(written) is int and written > 0, "latch write made no progress")
            offset += written
        os.fsync(fd)
        info = os.fstat(fd); named = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        require((info.st_dev, info.st_ino) == (named.st_dev, named.st_ino)
                and stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid()
                and stat.S_IMODE(info.st_mode) == 0o600 and info.st_nlink == 1,
                "latch record identity invalid")
    finally:
        os.close(fd)
    os.fsync(directory_fd)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "file_synced": True,
            "dir_synced": True, "closed": True}


def inspect(request, root=PRODUCTION_ROOT):
    """Read the exact deterministic request directory; never reopen for write."""
    root_path = _safe_root(root)
    parent = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    root = None
    try:
        name = "attempt-" + request_nonce(request)
        root = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        info = os.fstat(root)
        require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                "latch directory owner/mode invalid")
        names = sorted(os.listdir(root))
        require(names in (["exact-event-000000.json"],
                          ["exact-event-000000.json", "exact-event-000001.json"]),
                "latch incomplete or foreign file set")
        records = []
        for filename in names:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            try:
                before = os.fstat(fd)
                require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                        and stat.S_IMODE(before.st_mode) == 0o600 and before.st_nlink == 1
                        and 0 < before.st_size <= 16 * 1024 * 1024, "latch record unsafe")
                raw = bytearray()
                while len(raw) <= before.st_size:
                    chunk = os.read(fd, min(65536, before.st_size + 1 - len(raw)))
                    if not chunk: break
                    raw.extend(chunk)
                after, named = os.fstat(fd), os.stat(filename, dir_fd=root, follow_symlinks=False)
                identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_mode, s.st_nlink)
                require(identity(before) == identity(after) == identity(named) and len(raw) == before.st_size,
                        "latch record changed")
                record = B.JOURNAL.strict(raw)
                require(canonical(record) == raw, "latch record not canonical")
                records.append(record)
            finally: os.close(fd)
        require(records[0].get("request") == request
                and records[0].get("schema") == "slt-barrier-node-attempt/v1"
                and records[0].get("state") == "ATTEMPT_RESERVED", "latch request differs")
        if len(records) == 2:
            require(records[1].get("schema") == "slt-barrier-node-result/v1"
                    and records[1].get("request_sha256") == hashlib.sha256(canonical(request)).hexdigest()
                    and records[1].get("state") == "OBSERVED_COMPLETE"
                    and records[1].get("response", {}).get("request") == request,
                    "latch result differs")
        require(sorted(os.listdir(root)) == names
                and (os.stat(name, dir_fd=parent, follow_symlinks=False).st_dev,
                     os.stat(name, dir_fd=parent, follow_symlinks=False).st_ino) == (info.st_dev, info.st_ino),
                "latch namespace changed")
        return {"state": "COMPLETED_HISTORICAL" if len(records) == 2 else "RECOVERY_REQUIRED",
                "records": records, "retry_authorized": False, "release_authorized": False}
    finally:
        if root is not None: os.close(root)
        os.close(parent)


class DurableNodeAgent:
    """NodeAgent wrapper records request before any possible systemctl effect.

    A pre-existing request directory, partial write or missing receipt is a
    refusal, never permission to repeat. Fresh requests are node/boot/plan
    bound; reboot does not reset the request's deterministic namespace.
    """
    def __init__(self, agent, root=PRODUCTION_ROOT):
        self.agent = agent
        self.root = Path(root)

    def request(self, request):
        B.exact(request, {"tx", "plan_sha256", "node", "operation"}, "request")
        require(request["tx"] == self.agent.plan["tx"]
                and request["plan_sha256"] == self.agent.plan_sha
                and request["node"] == self.agent.node
                and request["operation"] in B.OPS, "request identity differs")
        frozen = copy.deepcopy(request)
        if request["operation"] == "OBSERVE":
            return self.agent.request(frozen)
        # A stable operation path also makes completed operations one-shot.
        root_path = _safe_root(self.root, create=True)
        root_fd = os.open(root_path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        attempt_fd = None
        try:
            attempt_name = "attempt-" + request_nonce(frozen)
            os.mkdir(attempt_name, 0o700, dir_fd=root_fd)
            os.fsync(root_fd)
            attempt_fd = os.open(attempt_name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            attempt_info = os.fstat(attempt_fd)
            require(attempt_info.st_uid == os.geteuid() and stat.S_IMODE(attempt_info.st_mode) == 0o700,
                    "attempt directory unsafe")
            boot = next(p["boot_id"] for p in self.agent.plan["participants"]
                        if p["node"] == self.agent.node)
            intent = {"schema": "slt-barrier-node-attempt/v1", "request": frozen,
                      "boot_id": boot, "state": "ATTEMPT_RESERVED"}
            raw = canonical(intent)
            ack = _write_once(attempt_fd, "exact-event-000000.json", raw)
            require(ack["sha256"] == hashlib.sha256(raw).hexdigest()
                    and ack["file_synced"] is True and ack["dir_synced"] is True
                    and ack["closed"] is True, "request persistence is uncertain")
            response = self.agent.request(frozen)
            raw = canonical({"schema": "slt-barrier-node-result/v1",
                             "request_sha256": hashlib.sha256(canonical(frozen)).hexdigest(),
                             "state": "OBSERVED_COMPLETE", "response": response})
            ack = _write_once(attempt_fd, "exact-event-000001.json", raw)
            require(ack["sha256"] == hashlib.sha256(raw).hexdigest()
                    and ack["file_synced"] is True and ack["dir_synced"] is True
                    and ack["closed"] is True, "result persistence is uncertain")
            return response
        except BaseException:
            self.agent.poisoned = True
            raise
        finally:
            if attempt_fd is not None: os.close(attempt_fd)
            os.close(root_fd)
