#!/usr/bin/python3
"""Small stdlib I/O oracle for the disposable lazy-zero loop qualification."""

import argparse
import errno
import fcntl
import hashlib
import json
import mmap
import os
import stat
import struct
import sys

BLKDISCARD = 0x1277
BLKGETSIZE64 = 0x80081272
BLKSSZGET = 0x1268
BLKGETDISKSEQ = 0x80081280


def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def pattern_block(seed, index):
    if not seed or len(seed) > 128 or not seed.isascii():
        raise RuntimeError("invalid deterministic pattern seed")
    return hashlib.shake_256(seed.encode("ascii") + index.to_bytes(8, "big")).digest(1048576)


def block_number(fd, request, fmt):
    buf = bytearray(struct.calcsize(fmt))
    fcntl.ioctl(fd, request, buf, True)
    return struct.unpack(fmt, buf)[0]


def verify_identity(fd, major, minor, diskseq, expected_size):
    info = os.fstat(fd)
    if not stat.S_ISBLK(info.st_mode):
        raise RuntimeError("I/O oracle requires a block device")
    if (os.major(info.st_rdev), os.minor(info.st_rdev)) != (major, minor):
        raise RuntimeError("block devno mismatch")
    if block_number(fd, BLKGETDISKSEQ, "=Q") != diskseq:
        raise RuntimeError("block diskseq mismatch")
    if block_number(fd, BLKGETSIZE64, "=Q") != expected_size:
        raise RuntimeError("block size mismatch")


def direct_digest(path, expected_byte, major, minor, diskseq, expected_size,
                  offset=0, length=None):
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECT
    fd = os.open(path, flags)
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        size = block_number(fd, BLKGETSIZE64, "=Q")
        logical = block_number(fd, BLKSSZGET, "=I")
        if size <= 0 or size > 134217728 or size % 4096 or logical not in (512, 4096):
            raise RuntimeError("unqualified block geometry")
        if length is None:
            length = size - offset
        if offset < 0 or length <= 0 or offset % 4096 or length % 4096 or offset + length > size:
            raise RuntimeError("invalid direct oracle range")
        digest = hashlib.sha256()
        chunk = min(1048576, length)
        with mmap.mmap(-1, chunk) as buffer:
            for position in range(offset, offset + length, chunk):
                count = min(chunk, offset + length - position)
                view = memoryview(buffer)[:count]
                try:
                    got = os.preadv(fd, [view], position)
                    if got != count:
                        raise RuntimeError("short O_DIRECT read")
                    if view != bytes([expected_byte]) * count:
                        raise RuntimeError("direct oracle byte mismatch")
                    digest.update(view)
                finally:
                    view.release()
        return {"device_bytes": size, "offset": offset, "bytes": length,
                "expected_byte": expected_byte,
                "logical_sector": logical, "sha256": digest.hexdigest(),
                "direct": True}
    finally:
        os.close(fd)


def direct_pattern_digest(path, seed, major, minor, diskseq, expected_size,
                          offset=0, length=None):
    if offset % 1048576 or (length is not None and length % 1048576):
        raise RuntimeError("pattern oracle range must be MiB aligned")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECT)
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        if length is None:
            length = expected_size - offset
        if length <= 0 or offset + length > expected_size:
            raise RuntimeError("invalid pattern oracle range")
        digest = hashlib.sha256()
        with mmap.mmap(-1, 1048576) as buffer:
            for position in range(offset, offset + length, 1048576):
                view = memoryview(buffer)
                try:
                    got = os.preadv(fd, [view], position)
                    if got != 1048576:
                        raise RuntimeError("short O_DIRECT pattern read")
                    expected = pattern_block(seed, position // 1048576)
                    if view != expected:
                        raise RuntimeError("direct pattern oracle mismatch")
                    digest.update(view)
                finally:
                    view.release()
        return {"device_bytes": expected_size, "offset": offset, "bytes": length,
                "pattern_seed": seed, "sha256": digest.hexdigest(), "direct": True}
    finally:
        os.close(fd)


def fill_file(path, size, value, require_allocated=True):
    if size <= 0 or size > 134217728 or size % 4096 or not 0 <= value <= 255:
        raise RuntimeError("invalid fill geometry")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
    fd = os.open(path, flags, 0o600)
    digest = hashlib.sha256()
    chunk = bytes([value]) * 1048576
    try:
        remaining = size
        while remaining:
            data = chunk[:min(len(chunk), remaining)]
            view = memoryview(data)
            written = 0
            while written < len(data):
                count = os.write(fd, view[written:])
                if count <= 0:
                    raise RuntimeError("short fill write")
                written += count
            digest.update(data)
            remaining -= len(data)
        os.fsync(fd)
    finally:
        os.close(fd)
    info = os.stat(path, follow_symlinks=False)
    if info.st_size != size or (require_allocated and info.st_blocks * 512 < size):
        raise RuntimeError("file is short or sparse")
    verify = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    try:
        remaining = size
        while remaining:
            data = os.read(fd, min(1048576, remaining))
            if not data or data != bytes([value]) * len(data):
                raise RuntimeError("fill readback mismatch")
            verify.update(data)
            remaining -= len(data)
        if os.read(fd, 1):
            raise RuntimeError("fill file exceeds exact size")
    finally:
        os.close(fd)
    if verify.hexdigest() != digest.hexdigest():
        raise RuntimeError("fill digest mismatch")
    return {"bytes": size, "fill_byte": value, "sha256": digest.hexdigest(),
            "fully_allocated": info.st_blocks * 512 >= size,
            "allocation_required": require_allocated, "full_readback": True}


def fill_pattern_file(path, size, seed):
    if size <= 0 or size > 134217728 or size % 1048576:
        raise RuntimeError("invalid pattern geometry")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    digest = hashlib.sha256()
    try:
        for index in range(size // 1048576):
            data = pattern_block(seed, index)
            view = memoryview(data)
            done = 0
            while done < len(data):
                count = os.write(fd, view[done:])
                if count <= 0:
                    raise RuntimeError("short pattern write")
                done += count
            digest.update(data)
        os.fsync(fd)
    finally:
        os.close(fd)
    info = os.stat(path, follow_symlinks=False)
    if info.st_size != size:
        raise RuntimeError("pattern file is short")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    verify = hashlib.sha256()
    try:
        for index in range(size // 1048576):
            data = os.read(fd, 1048576)
            if data != pattern_block(seed, index):
                raise RuntimeError("pattern readback mismatch")
            verify.update(data)
        if os.read(fd, 1):
            raise RuntimeError("pattern file exceeds exact size")
    finally:
        os.close(fd)
    if verify.hexdigest() != digest.hexdigest():
        raise RuntimeError("pattern digest mismatch")
    return {"bytes": size, "pattern_seed": seed, "sha256": digest.hexdigest(),
            "full_readback": True, "logical_holes_excluded_by_pattern": True,
            "stat_blocks": info.st_blocks}


def discard(path, offset, length, expectation, major, minor, diskseq, expected_size):
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        size = block_number(fd, BLKGETSIZE64, "=Q")
        logical = block_number(fd, BLKSSZGET, "=I")
        if offset < 0 or length <= 0 or offset % logical or length % logical or offset + length > size:
            raise RuntimeError("invalid discard range")
        observed = 0
        try:
            fcntl.ioctl(fd, BLKDISCARD, struct.pack("=QQ", offset, length))
        except OSError as exc:
            observed = exc.errno
        wanted = 0 if expectation == "success" else errno.EOPNOTSUPP
        result = {"offset": offset, "length": length, "errno": observed,
                  "expected_errno": wanted, "exact": observed == wanted}
        emit(result)
        return 0 if result["exact"] else 3
    finally:
        os.close(fd)


def write_byte(path, offset, length, value, major, minor, diskseq, expected_size):
    if offset < 0 or length <= 0 or offset % 4096 or length % 4096 or offset + length > expected_size:
        raise RuntimeError("invalid direct write range")
    if not 0 <= value <= 255:
        raise RuntimeError("invalid write byte")
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_DIRECT)
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        with mmap.mmap(-1, length) as buffer:
            buffer[:] = bytes([value]) * length
            view = memoryview(buffer)
            try:
                written = os.pwritev(fd, [view], offset)
                if written != length:
                    raise RuntimeError("short O_DIRECT write")
            finally:
                view.release()
        os.fsync(fd)
        verify_identity(fd, major, minor, diskseq, expected_size)
        return {"offset": offset, "bytes": length, "value": value,
                "direct": True, "fsync": True}
    finally:
        os.close(fd)


def direct_hash(path, major, minor, diskseq, expected_size, progress_bytes=0):
    if expected_size <= 0 or expected_size > 512 * 1024 * 1024 * 1024 or expected_size % 1048576:
        raise RuntimeError("unqualified direct hash geometry")
    if progress_bytes < 0 or progress_bytes % 1048576:
        raise RuntimeError("direct hash progress interval must be MiB aligned")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECT)
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        digest = hashlib.sha256()
        next_progress = progress_bytes
        with mmap.mmap(-1, 1048576) as buffer:
            for offset in range(0, expected_size, 1048576):
                view = memoryview(buffer)
                try:
                    got = os.preadv(fd, [view], offset)
                    if got != 1048576:
                        raise RuntimeError("short O_DIRECT hash read")
                    digest.update(view)
                finally:
                    view.release()
                completed = offset + 1048576
                if progress_bytes and (completed >= next_progress or completed == expected_size):
                    print(json.dumps({"action": "direct-hash-progress",
                                      "bytes": completed,
                                      "total_bytes": expected_size},
                                     sort_keys=True, separators=(",", ":")),
                          file=sys.stderr, flush=True)
                    while next_progress <= completed:
                        next_progress += progress_bytes
        verify_identity(fd, major, minor, diskseq, expected_size)
        return {"bytes": expected_size, "sha256": digest.hexdigest(), "direct": True}
    finally:
        os.close(fd)


def direct_verify_zero(path, major, minor, diskseq, expected_size, progress_bytes=0):
    """Read every byte once and prove it is zero without doing a SHA-256 pass."""
    if expected_size <= 0 or expected_size > 512 * 1024 * 1024 * 1024 or expected_size % 1048576:
        raise RuntimeError("unqualified direct zero geometry")
    if progress_bytes < 0 or progress_bytes % 1048576:
        raise RuntimeError("direct zero progress interval must be MiB aligned")
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECT)
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        zero = bytes(1048576)
        next_progress = progress_bytes
        with mmap.mmap(-1, 1048576) as buffer:
            for offset in range(0, expected_size, 1048576):
                view = memoryview(buffer)
                try:
                    got = os.preadv(fd, [view], offset)
                    if got != 1048576:
                        raise RuntimeError("short O_DIRECT zero read")
                    if view != zero:
                        raise RuntimeError("nonzero byte in direct zero scan")
                finally:
                    view.release()
                completed = offset + 1048576
                if progress_bytes and (completed >= next_progress or completed == expected_size):
                    print(json.dumps({"action": "direct-verify-zero-progress",
                                      "bytes": completed,
                                      "total_bytes": expected_size},
                                     sort_keys=True, separators=(",", ":")),
                          file=sys.stderr, flush=True)
                    while next_progress <= completed:
                        next_progress += progress_bytes
        verify_identity(fd, major, minor, diskseq, expected_size)
        return {"bytes": expected_size, "all_zero": True, "direct": True,
                "full_scan": True}
    finally:
        os.close(fd)


def fill_block(path, mode, value, major, minor, diskseq, expected_size):
    if expected_size <= 0 or expected_size > 512 * 1024 * 1024 * 1024 or expected_size % 1048576:
        raise RuntimeError("unqualified block fill geometry")
    if mode not in ("random", "byte") or not 0 <= value <= 255:
        raise RuntimeError("invalid block fill mode")
    fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_DIRECT)
    digest = hashlib.sha256()
    try:
        verify_identity(fd, major, minor, diskseq, expected_size)
        with mmap.mmap(-1, 1048576) as buffer:
            for offset in range(0, expected_size, 1048576):
                data = os.urandom(1048576) if mode == "random" else bytes([value]) * 1048576
                buffer[:] = data
                view = memoryview(buffer)
                try:
                    done = os.pwritev(fd, [view], offset)
                    if done != 1048576:
                        raise RuntimeError("short O_DIRECT block fill")
                finally:
                    view.release()
                digest.update(data)
        os.fsync(fd)
        verify_identity(fd, major, minor, diskseq, expected_size)
        return {"bytes": expected_size, "sha256": digest.hexdigest(),
                "direct": True, "fsync": True, "mode": mode,
                "value": value if mode == "byte" else None}
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    fill = sub.add_parser("fill")
    fill.add_argument("path")
    fill.add_argument("size", type=int)
    fill.add_argument("byte", type=lambda value: int(value, 0))
    fill.add_argument("--allow-compressed", action="store_true")
    fill_pattern = sub.add_parser("fill-pattern")
    fill_pattern.add_argument("path")
    fill_pattern.add_argument("size", type=int)
    fill_pattern.add_argument("seed")
    check = sub.add_parser("check")
    check.add_argument("path")
    check.add_argument("byte", type=lambda value: int(value, 0))
    check.add_argument("major", type=int)
    check.add_argument("minor", type=int)
    check.add_argument("diskseq", type=int)
    check.add_argument("size", type=int)
    check.add_argument("--offset", type=int, default=0)
    check.add_argument("--length", type=int)
    check_pattern = sub.add_parser("check-pattern")
    check_pattern.add_argument("path")
    check_pattern.add_argument("seed")
    check_pattern.add_argument("major", type=int)
    check_pattern.add_argument("minor", type=int)
    check_pattern.add_argument("diskseq", type=int)
    check_pattern.add_argument("size", type=int)
    check_pattern.add_argument("--offset", type=int, default=0)
    check_pattern.add_argument("--length", type=int)
    drop = sub.add_parser("discard")
    drop.add_argument("path")
    drop.add_argument("offset", type=int)
    drop.add_argument("length", type=int)
    drop.add_argument("expect", choices=("success", "eopnotsupp"))
    drop.add_argument("major", type=int)
    drop.add_argument("minor", type=int)
    drop.add_argument("diskseq", type=int)
    drop.add_argument("size", type=int)
    write = sub.add_parser("write-byte")
    write.add_argument("path")
    write.add_argument("offset", type=int)
    write.add_argument("length", type=int)
    write.add_argument("byte", type=lambda value: int(value, 0))
    write.add_argument("major", type=int)
    write.add_argument("minor", type=int)
    write.add_argument("diskseq", type=int)
    write.add_argument("size", type=int)
    hash_parser = sub.add_parser("direct-hash")
    hash_parser.add_argument("path")
    hash_parser.add_argument("major", type=int)
    hash_parser.add_argument("minor", type=int)
    hash_parser.add_argument("diskseq", type=int)
    hash_parser.add_argument("size", type=int)
    hash_parser.add_argument("--progress-bytes", type=int, default=0)
    zero_parser = sub.add_parser("direct-verify-zero")
    zero_parser.add_argument("path")
    zero_parser.add_argument("major", type=int)
    zero_parser.add_argument("minor", type=int)
    zero_parser.add_argument("diskseq", type=int)
    zero_parser.add_argument("size", type=int)
    zero_parser.add_argument("--progress-bytes", type=int, default=0)
    fill_block_parser = sub.add_parser("fill-block")
    fill_block_parser.add_argument("path")
    fill_block_parser.add_argument("mode", choices=("random", "byte"))
    fill_block_parser.add_argument("value", type=lambda item: int(item, 0))
    fill_block_parser.add_argument("major", type=int)
    fill_block_parser.add_argument("minor", type=int)
    fill_block_parser.add_argument("diskseq", type=int)
    fill_block_parser.add_argument("size", type=int)
    args = parser.parse_args()
    if args.action == "fill":
        emit(fill_file(args.path, args.size, args.byte, not args.allow_compressed))
        return 0
    if args.action == "fill-pattern":
        emit(fill_pattern_file(args.path, args.size, args.seed))
        return 0
    if args.action == "check":
        emit(direct_digest(args.path, args.byte, args.major, args.minor,
                           args.diskseq, args.size, args.offset, args.length))
        return 0
    if args.action == "check-pattern":
        emit(direct_pattern_digest(args.path, args.seed, args.major, args.minor,
                                   args.diskseq, args.size, args.offset, args.length))
        return 0
    if args.action == "write-byte":
        emit(write_byte(args.path, args.offset, args.length, args.byte,
                        args.major, args.minor, args.diskseq, args.size))
        return 0
    if args.action == "direct-hash":
        emit(direct_hash(args.path, args.major, args.minor, args.diskseq, args.size,
                         args.progress_bytes))
        return 0
    if args.action == "direct-verify-zero":
        emit(direct_verify_zero(args.path, args.major, args.minor, args.diskseq,
                                args.size, args.progress_bytes))
        return 0
    if args.action == "fill-block":
        emit(fill_block(args.path, args.mode, args.value, args.major, args.minor,
                        args.diskseq, args.size))
        return 0
    return discard(args.path, args.offset, args.length, args.expect,
                   args.major, args.minor, args.diskseq, args.size)


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        emit({"error": type(exc).__name__, "detail": str(exc)})
        result = 2
    sys.exit(result)
