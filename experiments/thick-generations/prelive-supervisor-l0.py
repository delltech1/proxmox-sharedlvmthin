#!/usr/bin/python3
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Read-only feature inventory for a future common pre-live supervisor."""

import argparse
import ctypes
import hashlib
import json
import os
import platform
from pathlib import Path
import resource
import selectors
import signal
import stat
import sys

sys.dont_write_bytecode = True

PR_GET_CHILD_SUBREAPER = 37


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb", buffering=0) as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def decode_mount_path(value):
    for encoded, plain in (("\\040", " "), ("\\011", "\t"),
                           ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, plain)
    return value


def mount_identity(path, text):
    target = os.path.realpath(path)
    matches = []
    for raw in text.splitlines():
        fields = raw.split()
        if "-" not in fields or len(fields) < 10:
            continue
        separator = fields.index("-")
        if separator + 2 >= len(fields):
            continue
        mountpoint = decode_mount_path(fields[4])
        if target == mountpoint or target.startswith(mountpoint.rstrip("/") + "/"):
            matches.append((len(mountpoint), {
                "mountpoint": mountpoint,
                "fstype": fields[separator + 1],
                "source": fields[separator + 2],
                "mount_id": fields[0],
                "parent_id": fields[1],
                "major_minor": fields[2],
                "readonly": "ro" in fields[5].split(","),
            }))
    if not matches:
        raise RuntimeError("journal parent mount is not identifiable")
    return max(matches, key=lambda item: item[0])[1]


def get_subreaper_state():
    libc = ctypes.CDLL(None, use_errno=True)
    value = ctypes.c_int(-1)
    result = libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(value), 0, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        return {"available": False, "errno": error, "value": None}
    return {"available": True, "errno": 0, "value": value.value}


def collect(expect_host, journal_parent="/var/tmp"):
    if platform.node() != expect_host:
        raise RuntimeError("exact hostname confirmation failed")
    executable = Path(sys.executable).resolve(strict=True)
    info = executable.stat()
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError("Python executable is not a regular file")
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()
    mountinfo = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    subreaper = get_subreaper_state()
    features = {
        "pidfd_open": callable(getattr(os, "pidfd_open", None)),
        "pidfd_send_signal": callable(getattr(signal, "pidfd_send_signal", None)),
        "waitid": callable(getattr(os, "waitid", None)),
        "p_pidfd": isinstance(getattr(os, "P_PIDFD", None), int),
        "wnowait": isinstance(getattr(os, "WNOWAIT", None), int),
        "epoll_selector": selectors.DefaultSelector.__name__ == "EpollSelector",
        "subreaper_get": subreaper["available"],
    }
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    ready = all(features.values()) and subreaper["value"] in (0, 1)
    return {
        "schema": 1,
        "classification": "L0_FEATURES_PRESENT_MODEL_REVIEW_ONLY" if ready else "L0_BLOCKED",
        "host": expect_host,
        "boot_id": boot_id,
        "kernel": platform.release(),
        "python": platform.python_version(),
        "python_executable": str(executable),
        "python_sha256": sha256_file(executable),
        "features": features,
        "current_subreaper_state": subreaper,
        "journal_parent": mount_identity(journal_parent, mountinfo),
        "rlimit_nofile": {"soft": soft, "hard": hard},
        "mutation_performed": False,
        "child_spawned": False,
        "runtime_authorized": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-host", required=True)
    parser.add_argument("--journal-parent", default="/var/tmp")
    args = parser.parse_args(argv)
    try:
        result = collect(args.expect_host, args.journal_parent)
    except Exception as exc:
        print(json.dumps({"classification": "L0_BLOCKED", "error": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0 if result["classification"] == "L0_FEATURES_PRESENT_MODEL_REVIEW_ONLY" else 2


if __name__ == "__main__":
    raise SystemExit(main())
