#!/usr/bin/env python3
"""Disposable filesystem model for one local maintenance-hold archive effect.

This module is deliberately not a production CLI.  It qualifies create-only
attempt/receipt records, renameat2(RENAME_NOREPLACE), crash reconciliation and
bounded locking below a caller-created disposable root.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import importlib.util
import json
import os
import re
import stat
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / "layout-migration-release-certificate.py"
SPEC = importlib.util.spec_from_file_location("slt_release_base", BASE_PATH)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

Refusal = BASE.Refusal
require = BASE.require
canonical = BASE.canonical
digest = BASE.digest

SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
RENAME_NOREPLACE = 1


def _fault(callback, point):
    if callback is not None:
        callback(point)


def _process_starttime():
    fields = Path("/proc/self/stat").read_text().split()
    require(len(fields) > 21 and fields[21].isdigit(),
            "executor process starttime is unavailable")
    return int(fields[21])


def _identity(info, data):
    return {
        "dev": info.st_dev, "ino": info.st_ino, "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode), "nlink": info.st_nlink,
        "size": info.st_size, "sha256": digest(data),
    }


def _read_regular(dir_fd, name, expected_uid, *, missing=False,
                  allowed_nlinks=(1,)):
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(name, flags, dir_fd=dir_fd)
    except FileNotFoundError:
        if missing:
            return None, None
        raise Refusal(f"required record is absent: {name}")
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == expected_uid
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink in allowed_nlinks
                and 0 < info.st_size <= 1024 * 1024,
                f"record inode is unsafe: {name}")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            require(chunk, f"record read made no progress: {name}")
            chunks.append(chunk)
            remaining -= len(chunk)
        require(os.read(fd, 1) == b"", f"record grew during read: {name}")
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        require((named.st_dev, named.st_ino, named.st_size, named.st_nlink)
                == (info.st_dev, info.st_ino, info.st_size, info.st_nlink),
                f"record namespace changed during read: {name}")
        return b"".join(chunks), info
    finally:
        os.close(fd)


def _fsync_regular(dir_fd, name, expected_uid, *, allowed_nlinks=(1,)):
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_uid == expected_uid
                and stat.S_IMODE(info.st_mode) == 0o600
                and info.st_nlink in allowed_nlinks
                and 0 < info.st_size <= 1024 * 1024,
                f"record inode is unsafe: {name}")
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        require((named.st_dev, named.st_ino, named.st_size, named.st_nlink)
                == (info.st_dev, info.st_ino, info.st_size, info.st_nlink),
                f"record namespace changed before fsync: {name}")
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_once(dir_fd, name, payload, expected_uid, fault, prefix):
    temporary = f".{name}.tmp"
    try:
        final_info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        temp_info = os.stat(temporary, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        final_info = temp_info = None
    if (final_info is not None and temp_info is not None
            and final_info.st_nlink == 2
            and (final_info.st_dev, final_info.st_ino)
                == (temp_info.st_dev, temp_info.st_ino)):
        current, _ = _read_regular(dir_fd, name, expected_uid,
                                   allowed_nlinks=(2,))
        require(current == payload, f"{prefix} linked record conflicts")
        _fsync_regular(dir_fd, name, expected_uid, allowed_nlinks=(2,))
        os.unlink(temporary, dir_fd=dir_fd)
        os.fsync(dir_fd)
        require(_read_regular(dir_fd, name, expected_uid)[0] == payload,
                f"{prefix} linked record reconciliation failed")
        return "VERIFIED_REPLAY"
    current, _ = _read_regular(dir_fd, name, expected_uid, missing=True)
    if current is not None:
        require(current == payload, f"{prefix} record conflicts")
        _fsync_regular(dir_fd, name, expected_uid)
        os.fsync(dir_fd)
        return "VERIFIED_REPLAY"
    staged, _ = _read_regular(dir_fd, temporary, expected_uid, missing=True)
    require(staged is None or staged == payload,
            f"{prefix} staged record conflicts")
    if staged is None:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(fd, payload[offset:])
                require(written > 0, f"{prefix} write made no progress")
                offset += written
            os.fsync(fd)
            _fault(fault, f"after_{prefix}_file_fsync")
        finally:
            os.close(fd)
    else:
        _fsync_regular(dir_fd, temporary, expected_uid)
    linked = False
    try:
        os.link(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd,
                follow_symlinks=False)
        linked = True
        _fault(fault, f"after_{prefix}_link")
    except FileExistsError:
        require(_read_regular(dir_fd, name, expected_uid)[0] == payload,
                f"{prefix} concurrent record conflicts")
        linked = True
    finally:
        if linked:
            try:
                os.unlink(temporary, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
    os.fsync(dir_fd)
    _fault(fault, f"after_{prefix}_directory_fsync")
    require(_read_regular(dir_fd, name, expected_uid)[0] == payload,
            f"{prefix} published bytes differ")
    return "CREATED"


def _rename_noreplace(source_fd, source, target_fd, target):
    libc = ctypes.CDLL(None, use_errno=True)
    function = getattr(libc, "renameat2", None)
    require(function is not None,
            "renameat2(RENAME_NOREPLACE) is unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                         ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(source_fd, os.fsencode(source), target_fd,
                      os.fsencode(target), RENAME_NOREPLACE)
    if result != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code), target)


def _acquire_lock(fd, timeout_sec):
    require(type(timeout_sec) in (int, float) and type(timeout_sec) is not bool
            and 0 < timeout_sec <= 30, "maintenance lock timeout is invalid")
    deadline = time.monotonic() + timeout_sec
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            require(time.monotonic() < deadline,
                    "maintenance lock acquisition timed out")
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _decode(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(f"record JSON is invalid: {error}") from error


class LocalHoldReleaseLab:
    def __init__(self, root: Path, *, expected_uid=None):
        self.root = Path(root)
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid

    def _open_directories(self):
        descriptors, identities = BASE._open_pinned_directory(
            self.root, self.expected_uid)
        root_fd = descriptors[-1]
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        children = {}
        child_identities = {}
        try:
            for name in ("archive", "release-attempts", "release-certificates"):
                fd = os.open(name, flags, dir_fd=root_fd)
                info = os.fstat(fd)
                require(stat.S_ISDIR(info.st_mode)
                        and info.st_uid == self.expected_uid
                        and stat.S_IMODE(info.st_mode) == 0o700,
                        f"{name} directory is unsafe")
                require(info.st_dev == os.fstat(root_fd).st_dev,
                        "release namespace crosses filesystems")
                children[name] = fd
                child_identities[name] = info
            return descriptors, identities, children, child_identities
        except Exception:
            for fd in children.values():
                os.close(fd)
            for fd in reversed(descriptors):
                os.close(fd)
            raise

    def _require_namespace(self, descriptors, identities, children,
                           child_identities):
        BASE._require_directory_chain(descriptors, identities, self.root,
                                      self.expected_uid)
        root_fd = descriptors[-1]
        root_dev = os.fstat(root_fd).st_dev
        for name, fd in children.items():
            opened = os.fstat(fd)
            named = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            expected = child_identities[name]
            require(stat.S_ISDIR(opened.st_mode)
                    and stat.S_ISDIR(named.st_mode)
                    and (opened.st_dev, opened.st_ino)
                        == (expected.st_dev, expected.st_ino)
                        == (named.st_dev, named.st_ino)
                    and opened.st_dev == root_dev
                    and opened.st_uid == expected.st_uid == named.st_uid
                        == self.expected_uid
                    and stat.S_IMODE(opened.st_mode)
                        == stat.S_IMODE(expected.st_mode)
                        == stat.S_IMODE(named.st_mode) == 0o700,
                    f"release child directory changed: {name}")

    def release(self, request, manifest_bytes, certificate_bytes, *, now=None,
                lock_timeout_sec=1.0, fault=None):
        exact = {
            "tx", "generation", "commit_id", "node", "boot_id",
            "authorization_sha256", "all_certified_plan_sha256",
            "certificate_sha256", "manifest_sha256", "valid_until",
            "source_identity", "certificate_identity",
        }
        require(type(request) is dict and set(request) == exact,
                "release request fields do not match")
        require(type(request["tx"]) is str and HEX32.fullmatch(request["tx"])
                and type(request["commit_id"]) is str
                and HEX32.fullmatch(request["commit_id"])
                and type(request["generation"]) is int
                and request["generation"] > 0
                and type(request["node"]) is str
                and SAFE_NAME.fullmatch(request["node"] or ""),
                "release request identity is invalid")
        for field in ("authorization_sha256", "all_certified_plan_sha256",
                      "certificate_sha256", "manifest_sha256"):
            require(type(request[field]) is str
                    and SHA256.fullmatch(request[field] or ""),
                    f"release request {field} is invalid")
        require(digest(manifest_bytes) == request["manifest_sha256"]
                and digest(certificate_bytes) == request["certificate_sha256"],
                "release request bytes differ")
        require(type(request["valid_until"]) in (int, float)
                and type(request["valid_until"]) is not bool,
                "release request deadline is invalid")
        now = time.time() if now is None else now
        archive_name = (f"{request['tx']}-{request['generation']}-"
                        f"{request['commit_id']}.json")
        decision_name = f"{request['tx']}-{request['generation']}.json"
        attempt_name = f"{request['tx']}-{request['generation']}.json"
        receipt_name = f"{request['tx']}-{request['generation']}-receipt.json"
        descriptors, identities, children, child_identities = \
            self._open_directories()
        root_fd = descriptors[-1]
        archive_fd = children["archive"]
        attempts_fd = children["release-attempts"]
        certificates_fd = children["release-certificates"]
        lock_fd = -1
        try:
            lock_fd = os.open(".maintenance.lock", os.O_RDWR | os.O_CREAT
                              | getattr(os, "O_NOFOLLOW", 0), 0o600,
                              dir_fd=root_fd)
            lock_info = os.fstat(lock_fd)
            require(stat.S_ISREG(lock_info.st_mode)
                    and lock_info.st_uid == self.expected_uid
                    and stat.S_IMODE(lock_info.st_mode) == 0o600
                    and lock_info.st_nlink == 1,
                    "maintenance lock inode is unsafe")
            _acquire_lock(lock_fd, lock_timeout_sec)
            def require_namespace():
                self._require_namespace(descriptors, identities, children,
                                        child_identities)
                opened = os.fstat(lock_fd)
                named = os.stat(".maintenance.lock", dir_fd=root_fd,
                                follow_symlinks=False)
                expected = (lock_info.st_dev, lock_info.st_ino,
                            self.expected_uid, 0o600, 1)
                for info in (opened, named):
                    require(stat.S_ISREG(info.st_mode) and
                            (info.st_dev, info.st_ino, info.st_uid,
                             stat.S_IMODE(info.st_mode), info.st_nlink) == expected,
                            "maintenance lock namespace changed")
            require_namespace()
            stored_certificate, certificate_info = _read_regular(
                certificates_fd, decision_name, self.expected_uid)
            require(stored_certificate == certificate_bytes
                    and _identity(certificate_info, stored_certificate)
                        == request["certificate_identity"],
                    "release certificate bytes or inode differ")
            active, active_info = _read_regular(root_fd, "active.json",
                                                self.expected_uid, missing=True)
            archived, archive_info = _read_regular(
                archive_fd, archive_name, self.expected_uid, missing=True)
            attempt, attempt_info = _read_regular(
                attempts_fd, attempt_name, self.expected_uid, missing=True,
                allowed_nlinks=(1, 2))
            existing_receipt, receipt_info = _read_regular(
                attempts_fd, receipt_name, self.expected_uid, missing=True,
                allowed_nlinks=(1, 2))
            staged_attempt = None
            if attempt is None:
                staged_attempt, _ = _read_regular(
                    attempts_fd, f".{attempt_name}.tmp", self.expected_uid,
                    missing=True)
            staged_receipt = None
            if existing_receipt is None:
                staged_receipt, _ = _read_regular(
                    attempts_fd, f".{receipt_name}.tmp", self.expected_uid,
                    missing=True)
            if attempt_info is not None and attempt_info.st_nlink == 2:
                staged_info = os.stat(f".{attempt_name}.tmp",
                                      dir_fd=attempts_fd,
                                      follow_symlinks=False)
                require((attempt_info.st_dev, attempt_info.st_ino)
                        == (staged_info.st_dev, staged_info.st_ino),
                        "two-link attempt is not the exact staged inode")
            if receipt_info is not None and receipt_info.st_nlink == 2:
                staged_info = os.stat(f".{receipt_name}.tmp",
                                      dir_fd=attempts_fd,
                                      follow_symlinks=False)
                require((receipt_info.st_dev, receipt_info.st_ino)
                        == (staged_info.st_dev, staged_info.st_ino),
                        "two-link receipt is not the exact staged inode")
            source = request["source_identity"]
            require(type(source) is dict
                    and set(source) == {"dev", "ino", "uid", "mode", "nlink",
                                       "size", "sha256"},
                    "source identity fields do not match")
            stable_attempt = {
                "schema": "slt-layout-local-hold-release-attempt/v1",
                "tx": request["tx"], "generation": request["generation"],
                "commit_id": request["commit_id"], "node": request["node"],
                "boot_id": request["boot_id"],
                "authorization_sha256": request["authorization_sha256"],
                "all_certified_plan_sha256":
                    request["all_certified_plan_sha256"],
                "certificate_sha256": request["certificate_sha256"],
                "manifest_sha256": request["manifest_sha256"],
                "source_identity": source, "archive_name": archive_name,
            }
            attempt_record = attempt if attempt is not None else staged_attempt
            if attempt_record is None:
                attempt_body = {**stable_attempt, "executor_pid": os.getpid(),
                                "executor_starttime": _process_starttime(),
                                "started_at": int(time.time())}
                attempt_bytes = canonical(attempt_body) + b"\n"
            else:
                attempt_body = _decode(attempt_record)
                require(set(attempt_body) == (set(stable_attempt)
                        | {"executor_pid", "executor_starttime", "started_at"})
                        and all(attempt_body[key] == value
                                for key, value in stable_attempt.items())
                        and type(attempt_body["executor_pid"]) is int
                        and attempt_body["executor_pid"] > 1
                        and type(attempt_body["executor_starttime"]) is int
                        and attempt_body["executor_starttime"] > 0
                        and type(attempt_body["started_at"]) is int
                        and attempt_body["started_at"] > 0,
                        "durable attempt identity differs")
                attempt_bytes = attempt_record
            # Reconciliation is the only permitted post-expiry path.
            if active is None and archived == manifest_bytes:
                require(attempt is not None,
                        "renamed hold lacks the exact durable attempt")
                _write_once(attempts_fd, attempt_name, attempt_bytes,
                            self.expected_uid, fault, "attempt")
            else:
                require(active == manifest_bytes and archived is None,
                        "active/archive state is conflicting or unknown")
                require(existing_receipt is None and staged_receipt is None,
                        "release receipt exists while active hold is present")
                require(_identity(active_info, active) == source,
                        "active manifest inode differs from authorized source")
                require(now <= request["valid_until"],
                        "release authorization expired before attempt")
                _write_once(attempts_fd, attempt_name, attempt_bytes,
                            self.expected_uid, fault, "attempt")
                require(time.time() <= request["valid_until"],
                        "release authorization expired before rename")
                active2, active2_info = _read_regular(
                    root_fd, "active.json", self.expected_uid)
                require(active2 == manifest_bytes
                        and _identity(active2_info, active2) == source,
                        "active manifest changed before rename")
                _fault(fault, "before_rename")
                require_namespace()
                stored_certificate2, certificate_info2 = _read_regular(
                    certificates_fd, decision_name, self.expected_uid)
                require(stored_certificate2 == certificate_bytes
                        and _identity(certificate_info2, stored_certificate2)
                            == request["certificate_identity"],
                        "release certificate changed immediately before rename")
                active3, active3_info = _read_regular(
                    root_fd, "active.json", self.expected_uid)
                require(active3 == manifest_bytes
                        and _identity(active3_info, active3) == source,
                        "active manifest changed immediately before rename")
                require(time.time() <= request["valid_until"],
                        "release authorization expired immediately before rename")
                try:
                    _rename_noreplace(root_fd, "active.json", archive_fd,
                                      archive_name)
                    _fault(fault, "after_rename")
                except OSError:
                    active3, _ = _read_regular(root_fd, "active.json",
                                               self.expected_uid, missing=True)
                    archive3, archive3_info = _read_regular(
                        archive_fd, archive_name, self.expected_uid,
                        missing=True)
                    require(active3 is None and archive3 == manifest_bytes
                            and _identity(archive3_info, archive3) == source,
                            "rename result is unknown")
            os.fsync(root_fd)
            _fault(fault, "after_active_directory_fsync")
            os.fsync(archive_fd)
            _fault(fault, "after_archive_directory_fsync")
            require_namespace()
            final_active, _ = _read_regular(root_fd, "active.json",
                                            self.expected_uid, missing=True)
            final_archive, final_info = _read_regular(
                archive_fd, archive_name, self.expected_uid)
            require(final_active is None and final_archive == manifest_bytes
                    and _identity(final_info, final_archive) == source,
                    "archived hold postcondition differs")
            stable_receipt = {
                "schema": "slt-layout-local-hold-release-receipt/v1",
                "tx": request["tx"], "generation": request["generation"],
                "commit_id": request["commit_id"], "node": request["node"],
                "boot_id": request["boot_id"],
                "authorization_sha256": request["authorization_sha256"],
                "all_certified_plan_sha256":
                    request["all_certified_plan_sha256"],
                "certificate_sha256": request["certificate_sha256"],
                "manifest_sha256": request["manifest_sha256"],
                "attempt_sha256": digest(attempt_bytes),
                "source_identity": source,
                "archive_identity": _identity(final_info, final_archive),
                "archive_name": archive_name,
                "rename_outcome": "EXACT_ARCHIVE_CONFIRMED",
                "directory_sync_complete": True,
                "started_at": attempt_body["started_at"],
                "classification": "LOCAL_HOLD_ARCHIVED",
                "cluster_released": False,
            }
            receipt_record = (existing_receipt if existing_receipt is not None
                              else staged_receipt)
            if receipt_record is not None:
                _write_once(attempts_fd, receipt_name, receipt_record,
                            self.expected_uid, fault, "receipt")
                receipt_body = _decode(receipt_record)
                require(set(receipt_body) == (set(stable_receipt)
                        | {"finished_at"})
                        and all(receipt_body[key] == value
                                for key, value in stable_receipt.items())
                        and type(receipt_body["finished_at"]) is int
                        and receipt_body["finished_at"]
                            >= receipt_body["started_at"],
                        "durable release receipt identity differs")
                require_namespace()
                final_active2, _ = _read_regular(
                    root_fd, "active.json", self.expected_uid, missing=True)
                final_archive2, final_info2 = _read_regular(
                    archive_fd, archive_name, self.expected_uid)
                require(final_active2 is None
                        and final_archive2 == manifest_bytes
                        and _identity(final_info2, final_archive2) == source,
                        "archived hold changed before receipt replay success")
                return receipt_body
            receipt_body = {
                **stable_receipt, "finished_at": int(time.time()),
            }
            receipt_bytes = canonical(receipt_body) + b"\n"
            _fault(fault, "before_receipt")
            _write_once(attempts_fd, receipt_name, receipt_bytes,
                        self.expected_uid, fault, "receipt")
            require_namespace()
            final_active2, _ = _read_regular(
                root_fd, "active.json", self.expected_uid, missing=True)
            final_archive2, final_info2 = _read_regular(
                archive_fd, archive_name, self.expected_uid)
            require(final_active2 is None and final_archive2 == manifest_bytes
                    and _identity(final_info2, final_archive2) == source,
                    "archived hold changed before receipt success")
            return receipt_body
        finally:
            if lock_fd >= 0:
                os.close(lock_fd)
            for fd in children.values():
                os.close(fd)
            for fd in reversed(descriptors):
                os.close(fd)
