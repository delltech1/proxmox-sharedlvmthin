#!/usr/bin/python3
"""Deterministic foreground-write plan for one disposable shared lazy lab."""

import argparse
import fcntl
import importlib.util
import json
import mmap
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("lazy_zero_live_io", HERE / "lazy-zero-live-io.py")
IO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IO)


def record(fd, value):
    line = json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    os.write(fd, line)
    os.fsync(fd)


def write_exact(fd, offset, length, value):
    with mmap.mmap(-1, length) as buffer:
        buffer[:] = bytes([value]) * length
        view = memoryview(buffer)
        try:
            done = os.pwritev(fd, [view], offset)
            if done != length:
                raise RuntimeError("short O_DIRECT foreground write")
        finally:
            view.release()


MIB = 1024 * 1024
MIN_BYTES = 1024 * MIB
MAX_BYTES = 512 * 1024 * 1024 * 1024


def build_plan(max_writes, size):
    if size < MIN_BYTES or size > MAX_BYTES or size % MIB:
        raise RuntimeError("writer size is outside the qualified geometry")
    total_regions = size // MIB
    plan = [(1048576 - 4096, 8192, 0x7B)]
    for index in range(1024):
        region = (index * 7919 + 17) % total_regions
        within = ((index * 37) % 255) * 4096
        plan.append((region * 1048576 + within, 4096, (index % 251) + 1))
    plan.extend(((size - MIB - 4096, 8192, 0xD3),
                 (size - 4096, 4096, 0xE7)))
    if max_writes < 1 or max_writes > len(plan):
        raise RuntimeError("max-writes is outside the qualified plan")
    return plan[:max_writes]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", required=True)
    parser.add_argument("--major", required=True, type=int)
    parser.add_argument("--minor", required=True, type=int)
    parser.add_argument("--diskseq", required=True, type=int)
    parser.add_argument("--size", required=True, type=int)
    parser.add_argument("--expected", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--max-writes", type=int, default=1027)
    parser.add_argument("--progress-every", type=int, default=16)
    parser.add_argument("--write-delay-ms", type=int, default=0)
    args = parser.parse_args()
    if args.size < MIN_BYTES or args.size > MAX_BYTES or args.size % MIB:
        raise RuntimeError("writer geometry is outside the qualified range")
    if args.progress_every < 1 or args.progress_every > 16:
        raise RuntimeError("progress-every is outside the qualified range")
    if args.write_delay_ms < 0 or args.write_delay_ms > 1000:
        raise RuntimeError("write-delay-ms is outside the qualified range")
    expected_fd = os.open(args.expected, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    journal_fd = os.open(args.journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    device_fd = -1
    try:
        os.ftruncate(expected_fd, args.size)
        record(journal_fd, {"state": "READY", "pid": os.getpid(), "writes": 0})
        deadline = time.monotonic() + 60
        while not os.path.exists(args.start):
            if time.monotonic() >= deadline:
                raise RuntimeError("start barrier timeout")
            time.sleep(0.01)
        device_fd = os.open(args.device, os.O_RDWR | os.O_CLOEXEC | os.O_DIRECT)
        IO.verify_identity(device_fd, args.major, args.minor, args.diskseq, args.size)
        plan = build_plan(args.max_writes, args.size)
        for index, (offset, length, value) in enumerate(plan, 1):
            write_exact(device_fd, offset, length, value)
            data = bytes([value]) * length
            if os.pwrite(expected_fd, data, offset) != length:
                raise RuntimeError("short expected-image write")
            if index % args.progress_every == 0 or index == len(plan):
                os.fsync(device_fd)
                os.fsync(expected_fd)
                record(journal_fd, {"state": "PROGRESS", "writes": index,
                                    "offset": offset, "length": length})
            if args.write_delay_ms:
                time.sleep(args.write_delay_ms / 1000)
        IO.verify_identity(device_fd, args.major, args.minor, args.diskseq, args.size)
        record(journal_fd, {"state": "COMPLETE", "writes": len(plan), "fsync": True})
        return 0
    finally:
        if device_fd >= 0:
            os.close(device_fd)
        os.close(expected_fd)
        os.close(journal_fd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"state": "ERROR", "error": type(exc).__name__, "detail": str(exc)},
                         sort_keys=True, separators=(",", ":")), file=sys.stderr)
        sys.exit(2)
