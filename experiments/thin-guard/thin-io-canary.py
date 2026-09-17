#!/usr/bin/python3
"""Crash-consistency canary for a disposable SharedLvmThin test LV.

This helper is never installed by the DEB. It writes alternating 4 KiB records
to the first 8 KiB of an explicitly supplied disposable block device. Never
point it at a filesystem, guest disk or non-disposable LV.
"""

import argparse
import hashlib
import os
import stat
import struct
import time
from pathlib import Path


BLOCK = 4096
MAGIC = b"SLTIOCANARYv1\0\0\0"
HEADER = struct.Struct(">16sQ")
DIGEST = 32


def record(sequence: int) -> bytes:
    seed = hashlib.sha256(f"BASTRIX-THIN-CANARY-{sequence}".encode()).digest()
    body_len = BLOCK - HEADER.size - DIGEST
    body = (seed * ((body_len + len(seed) - 1) // len(seed)))[:body_len]
    prefix = HEADER.pack(MAGIC, sequence) + body
    return prefix + hashlib.sha256(prefix).digest()


def decode(data: bytes):
    if len(data) != BLOCK:
        return None
    prefix, digest = data[:-DIGEST], data[-DIGEST:]
    if hashlib.sha256(prefix).digest() != digest:
        return None
    magic, sequence = HEADER.unpack(prefix[: HEADER.size])
    if magic != MAGIC or sequence < 1:
        return None
    if data != record(sequence):
        return None
    return sequence


def open_target(path: Path, allow_regular_test: bool):
    resolved = path.resolve(strict=True)
    mode = resolved.stat().st_mode
    if not stat.S_ISBLK(mode) and not (allow_regular_test and stat.S_ISREG(mode)):
        raise SystemExit("target is not a block device")
    if stat.S_ISBLK(mode) and not str(resolved).startswith("/dev/"):
        raise SystemExit("block target is outside /dev")
    return resolved, os.open(resolved, os.O_RDWR | os.O_CLOEXEC)


def durable_journal(path: Path, sequence: int, digest: str):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, f"{sequence} {digest}\n".encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def write_loop(target: Path, journal: Path, interval: float, count: int, allow_regular: bool):
    _, fd = open_target(target, allow_regular)
    sequence = 1
    try:
        while count == 0 or sequence <= count:
            payload = record(sequence)
            offset = (sequence & 1) * BLOCK
            written = os.pwrite(fd, payload, offset)
            if written != BLOCK:
                raise OSError(f"short canary write: {written}")
            os.fsync(fd)
            durable_journal(journal, sequence, hashlib.sha256(payload).hexdigest())
            print(f"CANARY_COMMITTED={sequence}", flush=True)
            sequence += 1
            time.sleep(interval)
    finally:
        os.close(fd)


def verify(target: Path, journal: Path, allow_regular: bool):
    _, fd = open_target(target, allow_regular)
    try:
        states = [decode(os.pread(fd, BLOCK, offset)) for offset in (0, BLOCK)]
    finally:
        os.close(fd)
    valid = [value for value in states if value is not None]
    if not valid:
        raise SystemExit("CANARY_VERIFY=FAIL no valid on-storage generation")
    words = journal.read_text(encoding="ascii").strip().split()
    if len(words) != 2 or not words[0].isdigit() or len(words[1]) != 64:
        raise SystemExit("CANARY_VERIFY=FAIL malformed durable journal")
    journal_sequence = int(words[0])
    storage_sequence = max(valid)
    if storage_sequence < journal_sequence:
        raise SystemExit(
            f"CANARY_VERIFY=FAIL storage={storage_sequence} journal={journal_sequence}"
        )
    print(f"CANARY_VERIFY=PASS storage={storage_sequence} journal={journal_sequence}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("write", "verify"))
    parser.add_argument("target", type=Path)
    parser.add_argument("journal", type=Path)
    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--count", type=int, default=0)
    parser.add_argument("--allow-regular-test", action="store_true")
    args = parser.parse_args()
    if args.interval < 0 or args.interval > 60 or args.count < 0:
        raise SystemExit("invalid interval or count")
    if args.mode == "write":
        write_loop(args.target, args.journal, args.interval, args.count, args.allow_regular_test)
    else:
        verify(args.target, args.journal, args.allow_regular_test)


if __name__ == "__main__":
    main()
