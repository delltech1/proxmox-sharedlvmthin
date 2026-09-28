#!/usr/bin/python3
"""Prepare and independently witness one controlled lazy-zero reboot checkpoint."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import socket
import subprocess
import sys


def process_starttime(pid):
    text = pathlib.Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    tail = text.rsplit(")", 1)[1].split()
    if len(tail) < 20 or not tail[19].isdigit():
        raise RuntimeError("unreadable process starttime")
    return tail[19]


def exact_fields(path):
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw:
            raise RuntimeError("malformed manifest record")
        key, value = raw.split("=", 1)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise RuntimeError("duplicate or malformed manifest key")
        result[key] = value
    return result


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_durable_exclusive(path, payload):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        view = memoryview(payload)
        while view:
            done = os.write(fd, view)
            if done <= 0:
                raise RuntimeError("short witness write")
            view = view[done:]
        os.fsync(fd)
    finally:
        os.close(fd)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-host", required=True)
    parser.add_argument("--storeid", required=True)
    parser.add_argument("--vg", required=True)
    parser.add_argument("--tx", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--before", required=True)
    parser.add_argument("--data-uuid", required=True)
    parser.add_argument("--meta-uuid", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expect_host:
        raise RuntimeError("host identity mismatch")
    for value in (args.storeid, args.vg):
        if not re.fullmatch(r"[A-Za-z0-9+_.-]+", value):
            raise RuntimeError("invalid storage identity")
    for value in (args.tx, args.nonce, args.before):
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise RuntimeError("invalid transaction identity")
    for value in (args.data_uuid, args.meta_uuid):
        if not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise RuntimeError("invalid LV identity")

    runner = pathlib.Path(__file__).resolve().parent / "lazy-zero-shared-hydrate-lab.sh"
    if not runner.is_file() or runner.is_symlink():
        raise RuntimeError("unsafe runner")
    command = [str(runner), "--expect-host", args.expect_host,
               "--storeid", args.storeid, "--vg", args.vg,
               "--tx", args.tx, "--nonce", args.nonce, "--before", args.before,
               "--data-uuid", args.data_uuid, "--meta-uuid", args.meta_uuid,
               "--data-gib", "8", "--hydration-budget-sec", "900",
               "--fault-mode", "controlled-reboot-prepare"]
    process = subprocess.Popen(command)
    prepare_pid = process.pid
    prepare_start = process_starttime(prepare_pid)
    returncode = process.wait()
    if returncode != 0:
        raise RuntimeError(f"controlled reboot prepare was not terminal-success: rc={returncode}")
    try:
        surviving_start = process_starttime(prepare_pid)
    except FileNotFoundError:
        surviving_start = None
    if surviving_start == prepare_start:
        raise RuntimeError("prepare process identity remains alive after wait")

    root = pathlib.Path(f"/var/tmp/slt-lazy-shared-{args.tx}")
    partial = root / "controlled-reboot-partial.manifest"
    ready = root / "controlled-reboot-ready.manifest"
    for path in (partial, ready):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"unsafe or missing prepare evidence: {path.name}")
    partial_fields = exact_fields(partial)
    ready_fields = exact_fields(ready)
    partial_sha = sha256(partial)
    ready_sha = sha256(ready)
    if (partial_fields.get("KIND") != "CONTROLLED_REBOOT_PARTIAL_V1"
            or partial_fields.get("PHASE") != "CONTROLLED_REBOOT_PARTIAL_FLUSHED"
            or ready_fields.get("KIND") != "CONTROLLED_REBOOT_READY_V1"
            or ready_fields.get("PHASE") != "CONTROLLED_REBOOT_READY"
            or ready_fields.get("PARTIAL_MANIFEST_SHA256") != partial_sha
            or ready_fields.get("PREPARE_PID") != str(prepare_pid)
            or ready_fields.get("PREPARE_STARTTIME") != prepare_start):
        raise RuntimeError("controlled reboot prepare manifest binding mismatch")
    for key, value in (("HOST", args.expect_host), ("STOREID", args.storeid),
                       ("VG", args.vg), ("TX", args.tx), ("NONCE", args.nonce),
                       ("BEFORE", args.before), ("DATA_UUID", args.data_uuid),
                       ("META_UUID", args.meta_uuid)):
        if partial_fields.get(key) != value or ready_fields.get(key) != value:
            raise RuntimeError(f"controlled reboot manifest identity mismatch: {key}")

    witness = root / "controlled-reboot-prepare.observed"
    witness_data = {
        "SCHEMA": "1",
        "EVENT": "CONTROLLED_REBOOT_PREPARE_EXIT0_OBSERVED",
        "PARTIAL_MANIFEST_SHA256": partial_sha,
        "READY_MANIFEST_SHA256": ready_sha,
        "PREPARE_PID": str(prepare_pid),
        "PREPARE_STARTTIME": prepare_start,
        "WAIT_RESULT": "EXIT_0",
        "LAUNCHER_PID": str(os.getpid()),
        "LAUNCHER_STARTTIME": process_starttime(os.getpid()),
    }
    payload = "".join(f"{key}={value}\n" for key, value in witness_data.items()).encode()
    write_durable_exclusive(witness, payload)
    print(f"CONTROLLED_REBOOT_PREPARE_WITNESS={witness}")
    print(f"CONTROLLED_REBOOT_PREPARE_WITNESS_SHA256={hashlib.sha256(payload).hexdigest()}")
    print(f"CONTROLLED_REBOOT_READY_MANIFEST_SHA256={ready_sha}")
    print("RESULT=CONTROLLED_REBOOT_PREPARE_EXIT0_WITNESSED_NO_REBOOT_DISPATCHED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"state": "ERROR", "error": type(exc).__name__,
                          "detail": str(exc)}, sort_keys=True, separators=(",", ":")),
              file=sys.stderr)
        sys.exit(2)
