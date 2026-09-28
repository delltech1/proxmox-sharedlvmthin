#!/usr/bin/env python3
"""Create-only persistence for a RELEASE_COMMITTED certificate.

This deliberately does not remove or rename the maintenance hold.  It
re-evaluates the complete ALL_READY input set, consumes a separate exact
release authorization, then publishes one byte-identical certificate on a
local filesystem.  Replays accept only the exact existing bytes.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import importlib.util
import json
import os
import re
import stat
import sys
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / "layout-migration-all-ready-plan.py"
SPEC = importlib.util.spec_from_file_location("slt_all_ready_base", BASE_PATH)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

Refusal = BASE.Refusal
require = BASE.require
read_json = BASE.read_json
exact = BASE.exact
digest = BASE.digest
canonical = BASE.canonical

HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def validate_all_ready_plan(plan: dict, computed: dict) -> None:
    exact(plan, set(computed), "ALL_READY plan")
    require(plan == computed,
            "stored ALL_READY plan differs from fresh evaluation")
    require(plan["schema"] == "slt-layout-all-ready-plan/v1"
            and plan["phase"] == "ALL_READY"
            and plan["verdict"] == "READY_FOR_RELEASE_COMMIT"
            and plan["authorization"] == "NONE"
            and plan["mutation_performed"] is False
            and plan["next_phase"] == "EXPLICIT_RELEASE_COMMIT_REQUIRED",
            "ALL_READY plan is not a non-authorizing release checkpoint")


def validate_release_authorization(value: dict, *, manifest: dict,
                                   manifest_sha: str, plan: dict,
                                   plan_sha: str, now: int) -> None:
    exact(value, {
        "schema", "tx", "generation", "manifest_sha256",
        "all_ready_plan_sha256", "evidence_set_sha256", "candidate",
        "participant_boots", "authorization_id", "commit_id",
        "coordinator", "issued_at", "expires_at", "committed_at",
        "release_not_after", "allowed_effects",
    }, "release authorization")
    require(value["schema"] == "slt-layout-release-authorization/v1"
            and value["tx"] == manifest["tx"]
            and type(value["generation"]) is int
            and value["generation"] == manifest["generation"]
            and value["manifest_sha256"] == manifest_sha
            and value["all_ready_plan_sha256"] == plan_sha
            and value["evidence_set_sha256"] == plan["evidence_set_sha256"]
            and value["candidate"] == manifest["candidate"],
            "release authorization identity differs from ALL_READY")
    require(type(value["authorization_id"]) is str
            and HEX32.fullmatch(value["authorization_id"] or "")
            and type(value["commit_id"]) is str
            and HEX32.fullmatch(value["commit_id"] or ""),
            "release authorization or commit identity is invalid")
    require(value["allowed_effects"] == ["persist-release-certificate"],
            "release authorization effects are not exact")
    require(type(value["issued_at"]) is int
            and type(value["expires_at"]) is int
            and type(value["committed_at"]) is int
            and type(value["release_not_after"]) is int
            and value["issued_at"] <= value["committed_at"] <= now
            and now <= value["expires_at"]
            and now <= value["release_not_after"]
            and value["issued_at"] >= plan["ready_observed_at_max"]
            and value["committed_at"] <= value["release_not_after"]
            and value["release_not_after"] <= value["expires_at"]
            and 0 < value["expires_at"] - value["issued_at"] <= 900,
            "release authorization interval is stale, future or overlong")
    names = [row["name"] for row in manifest["nodes"]]
    boots = {row["name"]: row["boot_id"] for row in manifest["nodes"]}
    require(value["participant_boots"] == boots,
            "release participant boot identities differ")
    coordinator = value["coordinator"]
    exact(coordinator, {"node", "boot_id"}, "release coordinator")
    require(coordinator["node"] in names
            and coordinator["boot_id"] == boots[coordinator["node"]],
            "release coordinator is not an exact participant")


def certificate(manifest: dict, manifest_sha: str, plan: dict,
                plan_sha: str, release: dict, release_sha: str) -> dict:
    boots = {row["name"]: row["boot_id"] for row in manifest["nodes"]}
    participants = [
        {"node": node, "boot_id": boots[node],
         "all_ready_evidence_sha256": plan["node_evidence_sha256"][node]}
        for node in plan["nodes"]
    ]
    return {
        "schema": "slt-layout-release-commit/v1",
        "phase": "RELEASE_COMMITTED",
        "commit_id": release["commit_id"],
        "tx": manifest["tx"], "generation": manifest["generation"],
        "manifest_sha256": manifest_sha,
        "all_configured_plan_sha256": plan["all_configured_plan_sha256"],
        "all_ready_plan_sha256": plan_sha,
        "refresh_authorization_sha256":
            plan["refresh_authorization_sha256"],
        "release_authorization_sha256": release_sha,
        "candidate": manifest["candidate"],
        "cluster_name": manifest["cluster_name"],
        "corosync_conf_sha256": manifest["corosync_conf_sha256"],
        "target_storage_cfg_sha256":
            manifest["target_storage_cfg_sha256"],
        "participants": participants,
        "evidence_set_sha256": plan["evidence_set_sha256"],
        "authorization_id": release["authorization_id"],
        "coordinator": release["coordinator"],
        "committed_at": release["committed_at"],
        "release_not_after": release["release_not_after"],
        "allowed_effects": ["archive-exact-active-manifest"],
    }


def _write_all(fd: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(fd, data[offset:])
        require(type(written) is int and written > 0,
                "certificate write made no progress")
        offset += written


def _read_exact(dir_fd: int, name: str, expected_uid: int,
                *, allowed_nlinks=(1,)) -> bytes:
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=dir_fd)
    try:
        info = os.fstat(fd)
        require(stat.S_ISREG(info.st_mode) and info.st_nlink in allowed_nlinks
                and info.st_uid == expected_uid and info.st_size <= 1024 * 1024,
                "stored certificate inode is unsafe")
        require(stat.S_IMODE(info.st_mode) == 0o600,
                "stored certificate mode is unsafe")
        chunks = []
        remaining = info.st_size
        while remaining:
            chunk = os.read(fd, min(remaining, 65536))
            require(chunk, "stored certificate changed during read")
            chunks.append(chunk)
            remaining -= len(chunk)
        require(os.read(fd, 1) == b"", "stored certificate grew during read")
        named = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        require((named.st_dev, named.st_ino, named.st_size, named.st_nlink)
                == (info.st_dev, info.st_ino, info.st_size, info.st_nlink),
                "stored certificate namespace changed during read")
        os.fsync(fd)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _safe_directory(info, expected_uid: int) -> bool:
    mode = stat.S_IMODE(info.st_mode)
    private = info.st_uid in (0, expected_uid) and mode & 0o022 == 0
    root_sticky = info.st_uid == 0 and mode & stat.S_ISVTX != 0
    return stat.S_ISDIR(info.st_mode) and (private or root_sticky)


def _open_pinned_directory(directory: Path, expected_uid: int):
    absolute = Path(os.path.abspath(directory))
    require(directory.is_absolute() and directory == absolute
            and directory.parts[:1] == (os.sep,)
            and len(directory.parts) > 1,
            "certificate path is not absolute and canonical")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) \
        | getattr(os, "O_NOFOLLOW", 0)
    descriptors = [os.open(os.sep, flags)]
    identities = [os.fstat(descriptors[0])]
    try:
        for component in directory.parts[1:]:
            require(component not in ("", ".", ".."),
                    "certificate path component is unsafe")
            fd = os.open(component, flags, dir_fd=descriptors[-1])
            descriptors.append(fd)
            identities.append(os.fstat(fd))
        require(all(_safe_directory(item, expected_uid)
                    for item in identities),
                "certificate directory chain is unsafe")
        final = identities[-1]
        require(final.st_uid == expected_uid
                and stat.S_IMODE(final.st_mode) & 0o022 == 0,
                "certificate final directory is unsafe")
        return descriptors, identities
    except Exception:
        for fd in reversed(descriptors):
            os.close(fd)
        raise


def _require_directory_chain(descriptors, identities, directory: Path,
                             expected_uid: int) -> None:
    for index, component in enumerate(directory.parts[1:], 1):
        opened = os.fstat(descriptors[index])
        named = os.stat(component, dir_fd=descriptors[index - 1],
                        follow_symlinks=False)
        expected = identities[index]
        require(stat.S_ISDIR(named.st_mode)
                and (opened.st_dev, opened.st_ino)
                    == (expected.st_dev, expected.st_ino)
                    == (named.st_dev, named.st_ino)
                and opened.st_uid == expected.st_uid == named.st_uid
                and stat.S_IMODE(opened.st_mode)
                    == stat.S_IMODE(expected.st_mode)
                    == stat.S_IMODE(named.st_mode)
                and _safe_directory(opened, expected_uid),
                "certificate directory chain changed")


def _acquire_bounded_lock(fd: int, *, valid_until: float,
                          timeout_sec: float) -> None:
    require(type(valid_until) in (int, float) and type(valid_until) is not bool,
            "certificate validity deadline is invalid")
    require(type(timeout_sec) in (int, float) and type(timeout_sec) is not bool
            and 0 < timeout_sec <= 30,
            "certificate lock timeout is invalid")
    deadline = time.monotonic() + timeout_sec
    while True:
        require(time.time() <= valid_until,
                "release authorization expired while waiting for lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            require(time.monotonic() < deadline,
                    "certificate lock acquisition timed out")
            time.sleep(min(0.02, max(0.0, deadline - time.monotonic())))
    require(time.time() <= valid_until,
            "release authorization expired after lock acquisition")


def persist_create_only(directory: Path, name: str, payload: bytes,
                        *, expected_uid: int, valid_until: float,
                        lock_timeout_sec: float = 5.0) -> str:
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,191}", name) is not None,
            "certificate filename is unsafe")
    try:
        descriptors, identities = _open_pinned_directory(directory, expected_uid)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refusal("certificate path contains a symlink or non-directory") from error
        raise
    parent_fd = descriptors[-2]
    dfd = descriptors[-1]
    lock_fd = -1
    temp_name = f".{name}.tmp"
    try:
        _require_directory_chain(descriptors, identities, directory,
                                 expected_uid)
        os.fsync(parent_fd)
        lock_fd = os.open(".release.lock", os.O_RDWR | os.O_CREAT
                          | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dfd)
        lock_info = os.fstat(lock_fd)
        require(stat.S_ISREG(lock_info.st_mode) and lock_info.st_nlink == 1
                and lock_info.st_uid == expected_uid
                and stat.S_IMODE(lock_info.st_mode) == 0o600,
                "certificate lock inode is unsafe")
        _acquire_bounded_lock(lock_fd, valid_until=valid_until,
                              timeout_sec=lock_timeout_sec)
        final_info = None
        staged_info = None
        try:
            final_info = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        try:
            staged_info = os.stat(temp_name, dir_fd=dfd,
                                  follow_symlinks=False)
        except FileNotFoundError:
            pass
        if final_info is not None and final_info.st_nlink == 2:
            require(staged_info is not None
                    and stat.S_ISREG(final_info.st_mode)
                    and stat.S_ISREG(staged_info.st_mode)
                    and (final_info.st_dev, final_info.st_ino)
                        == (staged_info.st_dev, staged_info.st_ino),
                    "two-link certificate is not the exact staged inode")
            require(_read_exact(dfd, name, expected_uid,
                                allowed_nlinks=(2,)) == payload,
                    "linked certificate has different bytes")
            os.unlink(temp_name, dir_fd=dfd)
            os.fsync(dfd)
            require(_read_exact(dfd, name, expected_uid) == payload,
                    "reconciled certificate bytes differ")
            os.fsync(dfd)
            _require_directory_chain(descriptors, identities, directory,
                                     expected_uid)
            return "VERIFIED_REPLAY"
        try:
            existing = _read_exact(dfd, name, expected_uid)
        except FileNotFoundError:
            existing = None
        if existing is not None:
            require(existing == payload,
                    "existing certificate has different bytes")
            os.fsync(dfd)
            _require_directory_chain(descriptors, identities, directory,
                                     expected_uid)
            return "VERIFIED_REPLAY"
        try:
            staged = _read_exact(dfd, temp_name, expected_uid)
        except FileNotFoundError:
            staged = None
        require(staged is None or staged == payload,
                "staged certificate has different bytes")
        if staged is None:
            temp_fd = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                              | getattr(os, "O_NOFOLLOW", 0), 0o600,
                              dir_fd=dfd)
            try:
                _write_all(temp_fd, payload)
                os.fsync(temp_fd)
            finally:
                os.close(temp_fd)
        else:
            require(_read_exact(dfd, temp_name, expected_uid) == payload,
                    "staged certificate changed before publication")
        publication_observed = False
        require(time.time() <= valid_until,
                "release authorization expired before publication")
        try:
            os.link(temp_name, name, src_dir_fd=dfd, dst_dir_fd=dfd,
                    follow_symlinks=False)
            publication_observed = True
        except FileExistsError:
            require(_read_exact(dfd, name, expected_uid) == payload,
                    "concurrent certificate has different bytes")
            publication_observed = True
        except OSError as link_error:
            try:
                linked = _read_exact(dfd, name, expected_uid,
                                     allowed_nlinks=(1, 2))
            except (OSError, Refusal):
                raise link_error
            require(linked == payload,
                    "ambiguous publication produced different bytes")
            publication_observed = True
        finally:
            if publication_observed:
                try:
                    os.unlink(temp_name, dir_fd=dfd)
                except FileNotFoundError:
                    pass
        os.fsync(dfd)
        require(_read_exact(dfd, name, expected_uid) == payload,
                "published certificate bytes differ")
        os.fsync(dfd)
        _require_directory_chain(descriptors, identities, directory,
                                 expected_uid)
        os.fsync(parent_fd)
        return "CERTIFICATE_DURABLE_LOCAL"
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.EMLINK, errno.EXDEV):
            raise Refusal(f"unsafe certificate namespace: {error}") from error
        raise
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        for fd in reversed(descriptors):
            os.close(fd)


def evaluate(args) -> tuple[dict, bytes]:
    manifest, manifest_raw = read_json(Path(args.manifest),
                                       "CONFIG_COMMITTED manifest", 1024 * 1024)
    BASE.validate_manifest(manifest)
    manifest_sha = digest(manifest_raw)
    computed = BASE.evaluate(argparse.Namespace(
        manifest=args.manifest, all_configured=args.all_configured,
        authorization=args.refresh_authorization,
        node_evidence=args.node_evidence, max_age_sec=args.max_age_sec,
        max_skew_sec=args.max_skew_sec, now=args.now))
    require(computed["manifest_sha256"] == manifest_sha
            and computed["tx"] == manifest["tx"]
            and computed["generation"] == manifest["generation"]
            and computed["candidate"] == manifest["candidate"]
            and computed["nodes"] == [row["name"] for row in manifest["nodes"]],
            "fresh ALL_READY evaluation used a different manifest")
    plan, plan_raw = read_json(Path(args.all_ready), "ALL_READY plan", 1024 * 1024)
    validate_all_ready_plan(plan, computed)
    plan_sha = digest(plan_raw)
    release, release_raw = read_json(Path(args.release_authorization),
                                     "release authorization", 1024 * 1024)
    now = args.now if args.now is not None else int(time.time())
    validate_release_authorization(release, manifest=manifest,
                                   manifest_sha=manifest_sha, plan=plan,
                                   plan_sha=plan_sha, now=now)
    result = certificate(manifest, manifest_sha, plan, plan_sha, release,
                         digest(release_raw))
    payload = canonical(result) + b"\n"
    return result, payload


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", required=True)
    result.add_argument("--all-configured", required=True)
    result.add_argument("--refresh-authorization", required=True)
    result.add_argument("--all-ready", required=True)
    result.add_argument("--release-authorization", required=True)
    result.add_argument("--node-evidence", action="append", required=True)
    result.add_argument("--certificate-dir", required=True)
    result.add_argument("--max-age-sec", type=int, default=300)
    result.add_argument("--max-skew-sec", type=int, default=60)
    result.add_argument("--now", type=int, help=argparse.SUPPRESS)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        require(os.geteuid() == 0, "certificate persistence requires root")
        require(30 <= args.max_age_sec <= 900, "evidence age bound is unsafe")
        require(0 <= args.max_skew_sec <= 120, "evidence skew bound is unsafe")
        result, payload = evaluate(args)
        name = f"{result['tx']}-{result['generation']}.json"
        state = persist_create_only(Path(args.certificate_dir), name, payload,
                                    expected_uid=0,
                                    valid_until=result["release_not_after"])
    except (OSError, Refusal) as error:
        print(json.dumps({"schema": 1, "verdict": "REFUSED",
                          "authorization": "NONE",
                          "hold_released": False,
                          "reason": str(error)}, sort_keys=True))
        return 2
    print(json.dumps({"schema": 1, "verdict": state,
                      "authorization": "NONE", "hold_released": False,
                      "certificate_sha256": digest(payload),
                      "certificate": result}, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
