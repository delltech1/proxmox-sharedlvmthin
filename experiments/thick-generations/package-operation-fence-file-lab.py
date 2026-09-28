#!/usr/bin/env python3
"""Disposable model for a durable fence between Debian maintainer scripts.

This is deliberately not package payload and cannot release a maintenance
hold.  It models only cooperating participants below a caller-owned fixture.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
from pathlib import Path


HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / "local-hold-release-file-lab.py"
SPEC = importlib.util.spec_from_file_location("slt_local_release_lab", BASE_PATH)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

Refusal = BASE.Refusal
require = BASE.require
digest = BASE.digest

HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FENCE_NAME = "package-operation.json"
CONFIG_NAME = "package-configured.json"


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=True) + "\n").encode("ascii")


def decode(data):
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


def exact(value, keys, label):
    require(type(value) is dict and set(value) == set(keys),
            f"{label} schema is not exact")


class PackageOperationFenceLab:
    def __init__(self, root: Path, *, expected_uid=None):
        self.root = Path(root)
        self.expected_uid = os.geteuid() if expected_uid is None else expected_uid

    def _open(self):
        descriptors, identities = BASE.BASE._open_pinned_directory(
            self.root, self.expected_uid)
        root_fd = descriptors[-1]
        flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fence_fd = os.open("package-fences", flags, dir_fd=root_fd)
            info = os.fstat(fence_fd)
            require(stat.S_ISDIR(info.st_mode)
                    and info.st_uid == self.expected_uid
                    and stat.S_IMODE(info.st_mode) == 0o700,
                    "package-fences directory is unsafe")
            return descriptors, identities, fence_fd, info
        except Exception:
            for fd in reversed(descriptors):
                os.close(fd)
            raise

    def _revalidate(self, descriptors, identities, fence_fd, fence_info,
                    lock_fd=None, lock_info=None):
        BASE.BASE._require_directory_chain(
            descriptors, identities, self.root, self.expected_uid)
        named = os.stat("package-fences", dir_fd=descriptors[-1],
                        follow_symlinks=False)
        opened = os.fstat(fence_fd)
        require((named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)
                == (fence_info.st_dev, fence_info.st_ino)
                and stat.S_ISDIR(named.st_mode)
                and named.st_uid == self.expected_uid
                and stat.S_IMODE(named.st_mode) == 0o700,
                "package-fences namespace changed")
        if lock_fd is not None:
            opened_lock = os.fstat(lock_fd)
            named_lock = os.stat(".maintenance.lock",
                                 dir_fd=descriptors[-1],
                                 follow_symlinks=False)
            require(lock_info is not None
                    and stat.S_ISREG(named_lock.st_mode)
                    and named_lock.st_uid == self.expected_uid
                    and stat.S_IMODE(named_lock.st_mode) == 0o600
                    and named_lock.st_nlink == 1
                    and (named_lock.st_dev, named_lock.st_ino)
                        == (opened_lock.st_dev, opened_lock.st_ino)
                        == (lock_info.st_dev, lock_info.st_ino),
                    "maintenance lock namespace changed")

    def _locked(self, timeout_sec):
        descriptors, identities, fence_fd, fence_info = self._open()
        root_fd = descriptors[-1]
        lock_fd = None
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
            BASE._acquire_lock(lock_fd, timeout_sec)
            self._revalidate(descriptors, identities, fence_fd, fence_info,
                             lock_fd, lock_info)
            return (descriptors, identities, fence_fd, fence_info,
                    lock_fd, lock_info)
        except Exception:
            if lock_fd is not None:
                os.close(lock_fd)
            os.close(fence_fd)
            for fd in reversed(descriptors):
                os.close(fd)
            raise

    @staticmethod
    def _close(context):
        descriptors, _identities, fence_fd, _fence_info, lock_fd, _lock_info = context
        os.close(lock_fd)
        os.close(fence_fd)
        for fd in reversed(descriptors):
            os.close(fd)

    @staticmethod
    def _validate_request(request):
        exact(request, {"attempt_id", "node", "boot_id", "package", "flavor",
                        "version", "artifact_sha256"}, "package attempt")
        require(type(request["attempt_id"]) is str
                and HEX32.fullmatch(request["attempt_id"] or ""),
                "package attempt identity is invalid")
        require(request["package"] in ("pve-sharedlvmthin",
                                       "pve-sharedlvmthin-thick")
                and request["flavor"] in ("dual", "thick-only")
                and ((request["package"] == "pve-sharedlvmthin")
                     == (request["flavor"] == "dual")),
                "package and flavor identity differ")
        for key in ("node", "boot_id", "version"):
            require(type(request[key]) is str and 0 < len(request[key]) <= 128,
                    f"package attempt {key} is invalid")
        require(type(request["artifact_sha256"]) is str
                and SHA256.fullmatch(request["artifact_sha256"] or ""),
                "package artifact identity is invalid")

    def _manifest(self, root_fd, *, missing):
        data, info = BASE._read_regular(root_fd, "active.json",
                                        self.expected_uid, missing=missing)
        if data is None:
            return None, None
        value = decode(data)
        exact(value, {"schema", "tx", "generation", "phase"},
              "maintenance manifest")
        require(value["schema"] == "slt-package-maintenance/v1"
                and type(value["tx"]) is str and HEX32.fullmatch(value["tx"])
                and type(value["generation"]) is int
                and value["generation"] > 0
                and value["phase"] in ("PREPARE_READY", "CONFIG_COMMITTED"),
                "maintenance manifest is invalid")
        return {"value": value, "sha256": digest(data),
                "dev": info.st_dev, "ino": info.st_ino}, data

    def _durable_active(self, root_fd):
        BASE._fsync_regular(root_fd, "active.json", self.expected_uid)
        os.fsync(root_fd)

    def admit(self, request, *, fault=None, lock_timeout_sec=1.0):
        self._validate_request(request)
        context = self._locked(lock_timeout_sec)
        descriptors, identities, fence_fd, fence_info, lock_fd, lock_info = context
        root_fd = descriptors[-1]
        try:
            hold, _raw = self._manifest(root_fd, missing=True)
            if hold is not None:
                require(hold["value"]["phase"] == "PREPARE_READY",
                        "maintenance admission requires PREPARE_READY")
                hold_binding = {"mode": "MAINTENANCE", **hold}
            else:
                hold_binding = {"mode": "ABSENT"}
            record = {
                "schema": "slt-package-operation-fence/v1",
                "state": "PACKAGE_IN_PROGRESS",
                "request": request,
                "hold": hold_binding,
            }
            payload = canonical(record)
            outcome = BASE._write_once(fence_fd, FENCE_NAME, payload,
                                       self.expected_uid, fault, "fence")
            if fault is not None:
                fault("after_fence_durable")
            self._revalidate(descriptors, identities, fence_fd, fence_info,
                             lock_fd, lock_info)
            current, _ = self._manifest(root_fd, missing=True)
            if hold is None:
                require(current is None,
                        "hold appeared during ordinary package admission")
            else:
                require(current == hold,
                        "maintenance hold changed during package admission")
            return {"classification": "PACKAGE_IN_PROGRESS",
                    "publication": outcome, "release_authorized": False}
        finally:
            self._close(context)

    def record_configured(self, request, *, fault=None, lock_timeout_sec=1.0):
        self._validate_request(request)
        context = self._locked(lock_timeout_sec)
        descriptors, identities, fence_fd, fence_info, lock_fd, lock_info = context
        root_fd = descriptors[-1]
        try:
            raw, _ = BASE._read_regular(fence_fd, FENCE_NAME,
                                        self.expected_uid)
            fence = decode(raw)
            exact(fence, {"schema", "state", "request", "hold"},
                  "package fence")
            require(fence["schema"] == "slt-package-operation-fence/v1"
                    and fence["state"] == "PACKAGE_IN_PROGRESS"
                    and fence["request"] == request,
                    "package fence predecessor differs")
            if fence["hold"].get("mode") == "ABSENT":
                exact(fence["hold"], {"mode"}, "ordinary hold binding")
            else:
                exact(fence["hold"], {"mode", "value", "sha256", "dev", "ino"},
                      "maintenance hold binding")
                require(fence["hold"]["mode"] == "MAINTENANCE",
                        "package fence hold mode is invalid")
            current, _ = self._manifest(root_fd, missing=True)
            if fence["hold"].get("mode") == "ABSENT":
                require(current is None,
                        "ordinary package attempt gained a maintenance hold")
            else:
                initial = fence["hold"]
                require(current is not None
                        and current["value"]["tx"] == initial["value"]["tx"]
                        and current["value"]["generation"]
                            == initial["value"]["generation"]
                        and current["value"]["phase"] == "CONFIG_COMMITTED",
                        "maintenance transition is not exact")
            configured = {
                "schema": "slt-package-operation-configured/v1",
                "state": "CONFIG_RECORDED_RETAINED",
                "fence_sha256": digest(raw),
                "request": request,
            }
            outcome = BASE._write_once(fence_fd, CONFIG_NAME,
                                       canonical(configured), self.expected_uid,
                                       fault, "configured")
            self._revalidate(descriptors, identities, fence_fd, fence_info,
                             lock_fd, lock_info)
            after, _ = self._manifest(root_fd, missing=True)
            require(after == current,
                    "maintenance hold changed while recording configuration")
            require(BASE._read_regular(fence_fd, FENCE_NAME,
                                       self.expected_uid)[0] == raw,
                    "package fence changed after configuration")
            return {"classification": "CONFIG_RECORDED_RETAINED",
                    "publication": outcome, "release_authorized": False}
        finally:
            self._close(context)

    def cooperating_transition(self, tx, generation, *, fault=None,
                               lock_timeout_sec=1.0):
        context = self._locked(lock_timeout_sec)
        descriptors, identities, fence_fd, fence_info, lock_fd, lock_info = context
        root_fd = descriptors[-1]
        try:
            raw, _ = BASE._read_regular(fence_fd, FENCE_NAME,
                                        self.expected_uid)
            fence = decode(raw)
            hold = fence.get("hold", {})
            require(hold.get("mode") == "MAINTENANCE",
                    "cooperating writer cannot create a hold during ordinary package operation")
            value = hold["value"]
            require(value["tx"] == tx and value["generation"] == generation
                    and value["phase"] == "PREPARE_READY",
                    "cooperating writer transition identity differs")
            current, _ = self._manifest(root_fd, missing=False)
            successor = dict(value)
            successor["phase"] = "CONFIG_COMMITTED"
            if current["value"] == successor:
                self._durable_active(root_fd)
                self._revalidate(descriptors, identities, fence_fd, fence_info,
                                 lock_fd, lock_info)
                replay, _ = self._manifest(root_fd, missing=False)
                require(replay == current,
                        "maintenance replay binding changed during fsync")
                require(BASE._read_regular(fence_fd, FENCE_NAME,
                                           self.expected_uid)[0] == raw,
                        "package fence changed during maintenance replay")
                return {"classification": "CONFIG_COMMITTED",
                        "publication": "VERIFIED_REPLAY",
                        "release_authorized": False}
            require(current == {key: hold[key] for key in
                                ("value", "sha256", "dev", "ino")},
                    "maintenance source changed before transition")
            payload = canonical(successor)
            temporary = ".active.json.package-transition"
            BASE._write_once(root_fd, temporary, payload, self.expected_uid,
                             fault, "transition")
            self._revalidate(descriptors, identities, fence_fd, fence_info,
                             lock_fd, lock_info)
            require(BASE._read_regular(fence_fd, FENCE_NAME,
                                       self.expected_uid)[0] == raw,
                    "package fence changed before maintenance transition")
            before_replace, _ = self._manifest(root_fd, missing=False)
            require(before_replace == current,
                    "maintenance source changed before transition effect")
            os.replace(temporary, "active.json", src_dir_fd=root_fd,
                       dst_dir_fd=root_fd)
            self._durable_active(root_fd)
            if fault is not None:
                fault("after_active_transition_directory_fsync")
            self._revalidate(descriptors, identities, fence_fd, fence_info,
                             lock_fd, lock_info)
            result, _ = self._manifest(root_fd, missing=False)
            require(result["value"] == successor,
                    "maintenance transition postcondition differs")
            return {"classification": "CONFIG_COMMITTED",
                    "publication": "CREATED",
                    "release_authorized": False}
        finally:
            self._close(context)

    def release_admission(self):
        context = self._locked(1.0)
        _descriptors, _identities, fence_fd, _fence_info, _lock_fd, _lock_info = context
        try:
            names = (FENCE_NAME, CONFIG_NAME,
                     f".{FENCE_NAME}.tmp", f".{CONFIG_NAME}.tmp")
            records = [BASE._read_regular(fence_fd, name, self.expected_uid,
                                          missing=True)[0] for name in names]
            require(all(item is None for item in records),
                    "package-operation fence is retained; release refused")
            return {"classification": "NO_PACKAGE_FENCE",
                    "release_authorized": False}
        finally:
            self._close(context)
