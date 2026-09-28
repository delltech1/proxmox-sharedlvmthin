#!/usr/bin/env python3
"""Durable core for the coordinated offline layout migration runner.

This file deliberately performs no SSH, dpkg, pmxcfs, service or storage
mutation.  It persists the exact phase boundary that a later qualified command
adapter must obey.  An interrupted action is never redispatched automatically.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path


PHASES = (
    "PREPARE_READY", "ALL_UNPACKED", "CONFIG_COMMITTED", "ALL_CONFIGURED",
    "ALL_READY", "ALL_CERTIFIED", "SETTLED",
)
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Refusal(Exception):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def checked_identity(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    info = os.fstat(fd)
    require(stat.S_ISREG(info.st_mode), "identity must be a regular non-symlink file")
    require(0 < info.st_size <= 1024 * 1024, "identity size is invalid")
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(65536, 1024 * 1024 + 1 - total))
        if not chunk:
            break
        chunks.append(chunk); total += len(chunk)
        require(total <= 1024 * 1024, "identity size is invalid")
    after = os.fstat(fd)
    os.close(fd)
    raw = b"".join(chunks)
    require(len(raw) == info.st_size, "identity changed while reading")
    require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns) ==
            (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            "identity changed while reading")
    value = json.loads(raw)
    require(type(value) is dict, "identity must be an object")
    require(set(value) == {"schema", "tx", "participants", "candidate",
                           "baseline_storage_cfg_sha256",
                           "target_storage_cfg_sha256"},
            "identity fields do not match schema")
    require(value["schema"] == "slt-offline-migration/v1", "identity schema invalid")
    require(type(value["tx"]) is str and HEX32.fullmatch(value["tx"]),
            "transaction identity invalid")
    require(type(value["participants"]) is list and len(value["participants"]) >= 3
            and value["participants"] == sorted(set(value["participants"])),
            "participant set invalid")
    require(type(value["candidate"]) is dict, "candidate identity invalid")
    for field in ("baseline_storage_cfg_sha256", "target_storage_cfg_sha256"):
        require(type(value[field]) is str and SHA256.fullmatch(value[field]),
                f"{field} invalid")
    require(value["baseline_storage_cfg_sha256"] != value["target_storage_cfg_sha256"],
            "baseline and target configuration are identical")
    return value, digest(canonical(value))


def record_hash(record):
    body = dict(record)
    body.pop("record_sha256", None)
    return digest(canonical(body))


def read_journal(fd, identity_sha, tx):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks, total = [], 0
    while True:
        chunk = os.read(fd, min(65536, 4 * 1024 * 1024 + 1 - total))
        if not chunk:
            break
        chunks.append(chunk); total += len(chunk)
        require(total <= 4 * 1024 * 1024, "journal exceeds size bound")
    raw = b"".join(chunks)
    require(not raw or raw.endswith(b"\n"), "journal has a torn final record")
    records, previous = [], "0" * 64
    for index, line in enumerate(raw.splitlines(), 1):
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Refusal(f"journal record {index} is invalid") from error
        require(type(value) is dict and set(value) == {
            "schema", "seq", "tx", "identity_sha256", "kind", "phase",
            "evidence_sha256", "intent_record_sha256", "previous_sha256",
            "record_sha256"},
            f"journal record {index} fields invalid")
        require(value["schema"] == "slt-offline-journal/v1"
                and value["seq"] == index and value["tx"] == tx
                and value["identity_sha256"] == identity_sha,
                f"journal record {index} identity invalid")
        require(value["kind"] in ("INTENT", "COMPLETE")
                and value["phase"] in PHASES
                and SHA256.fullmatch(value["evidence_sha256"] or "")
                and SHA256.fullmatch(value["intent_record_sha256"] or "")
                and value["previous_sha256"] == previous
                and value["record_sha256"] == record_hash(value),
                f"journal record {index} chain invalid")
        previous = value["record_sha256"]
        records.append(value)
    return records


def state(records):
    if not records:
        return {"classification": "EMPTY", "next_phase": PHASES[0]}
    completed = -1
    pending = None
    for item in records:
        phase_index = PHASES.index(item["phase"])
        if item["kind"] == "INTENT":
            require(pending is None and phase_index == completed + 1,
                    "journal phase transition invalid")
            pending = item
        else:
            require(pending is not None and item["phase"] == pending["phase"]
                    and item["intent_record_sha256"] == pending["record_sha256"]
                    and item["evidence_sha256"] != pending["evidence_sha256"],
                    "completion does not exactly match pending intent")
            completed, pending = phase_index, None
    if pending:
        return {"classification": "RECOVERY_REQUIRED",
                "pending_phase": pending["phase"], "authorization": "NONE"}
    if completed == len(PHASES) - 1:
        return {"classification": "SETTLED", "authorization": "NONE"}
    return {"classification": "READY_FOR_EXPLICIT_INTENT",
            "completed_phase": PHASES[completed],
            "next_phase": PHASES[completed + 1], "authorization": "NONE"}


def validate_namespace(parent_path, parent_fd, name, anchor_name, fd):
    parent_open = os.fstat(parent_fd)
    parent_named = os.stat(parent_path, follow_symlinks=False)
    require(stat.S_ISDIR(parent_named.st_mode)
            and (parent_named.st_dev, parent_named.st_ino)
                == (parent_open.st_dev, parent_open.st_ino)
            and parent_open.st_uid == os.geteuid()
            and parent_open.st_mode & 0o077 == 0,
            "named journal parent identity changed or is unsafe")
    opened = os.fstat(fd)
    named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    anchor = os.stat(anchor_name, dir_fd=parent_fd, follow_symlinks=False)
    require(stat.S_ISREG(opened.st_mode) and opened.st_nlink == 2
            and opened.st_uid == os.geteuid() and opened.st_mode & 0o077 == 0,
            "journal inode metadata is unsafe")
    require((named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)
            and (anchor.st_dev, anchor.st_ino) == (opened.st_dev, opened.st_ino),
            "journal or anchor namespace identity changed")


def append(fd, parent_path, parent_fd, name, anchor_name, records, identity,
           identity_sha, kind, phase,
           evidence_sha, intent_record_sha="0" * 64):
    value = {
        "schema": "slt-offline-journal/v1", "seq": len(records) + 1,
        "tx": identity["tx"], "identity_sha256": identity_sha,
        "kind": kind, "phase": phase, "evidence_sha256": evidence_sha,
        "intent_record_sha256": intent_record_sha,
        "previous_sha256": records[-1]["record_sha256"] if records else "0" * 64,
    }
    value["record_sha256"] = record_hash(value)
    os.lseek(fd, 0, os.SEEK_END)
    payload = canonical(value) + b"\n"
    offset = 0
    while offset < len(payload):
        written = os.write(fd, payload[offset:])
        require(written > 0, "journal append made no progress")
        offset += written
    os.fsync(fd)
    validate_namespace(parent_path, parent_fd, name, anchor_name, fd)
    os.fsync(parent_fd)
    validate_namespace(parent_path, parent_fd, name, anchor_name, fd)


def operate(args):
    identity, identity_sha = checked_identity(Path(args.identity))
    journal = Path(args.journal)
    require(journal.name not in ("", ".", ".."), "journal name invalid")
    parent_fd = os.open(journal.parent, os.O_RDONLY | os.O_DIRECTORY |
                        getattr(os, "O_NOFOLLOW", 0))
    parent_info = os.fstat(parent_fd)
    require(parent_info.st_uid == os.geteuid() and parent_info.st_mode & 0o077 == 0,
            "journal parent ownership or permissions are unsafe")
    anchor_name = journal.name + ".anchor"
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    if args.command == "init":
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(journal.name, flags, 0o600, dir_fd=parent_fd)
    try:
        if args.command == "init":
            os.link(journal.name, anchor_name, src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd, follow_symlinks=False)
            os.fsync(parent_fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate_namespace(journal.parent, parent_fd, journal.name, anchor_name, fd)
        os.fsync(fd); os.fsync(parent_fd)
        validate_namespace(journal.parent, parent_fd, journal.name, anchor_name, fd)
        records = read_journal(fd, identity_sha, identity["tx"])
        current = state(records)
        if args.command in ("init", "inspect"):
            validate_namespace(journal.parent, parent_fd, journal.name,
                               anchor_name, fd)
            return current
        require(SHA256.fullmatch(args.evidence_sha256 or ""), "evidence hash invalid")
        if args.command == "intent":
            require(current["classification"] in
                    ("EMPTY", "READY_FOR_EXPLICIT_INTENT")
                    and current["next_phase"] == args.phase,
                    "phase is not ready for a new intent")
            append(fd, journal.parent, parent_fd, journal.name, anchor_name,
                   records, identity, identity_sha,
                   "INTENT", args.phase, args.evidence_sha256)
        else:
            require(current["classification"] == "RECOVERY_REQUIRED"
                    and current["pending_phase"] == args.phase,
                    "no exact pending phase to reconcile")
            pending = records[-1]
            require(pending["evidence_sha256"] != args.evidence_sha256,
                    "result evidence must differ from admission evidence")
            append(fd, journal.parent, parent_fd, journal.name, anchor_name,
                   records, identity, identity_sha,
                   "COMPLETE", args.phase, args.evidence_sha256,
                   pending["record_sha256"])
        result = state(read_journal(fd, identity_sha, identity["tx"]))
        validate_namespace(journal.parent, parent_fd, journal.name, anchor_name, fd)
        return result
    finally:
        os.close(fd)
        os.close(parent_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "inspect", "intent", "reconcile"))
    parser.add_argument("--identity", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--phase", choices=PHASES)
    parser.add_argument("--evidence-sha256")
    args = parser.parse_args()
    try:
        if args.command != "inspect":
            require(args.phase is not None, "phase is required")
        print(json.dumps(operate(args), sort_keys=True, indent=2))
        return 0
    except (OSError, ValueError, Refusal) as error:
        print(json.dumps({"classification": "REFUSED", "authorization": "NONE",
                          "reason": str(error)}, sort_keys=True))
        return 2


if __name__ == "__main__":
    sys.exit(main())
