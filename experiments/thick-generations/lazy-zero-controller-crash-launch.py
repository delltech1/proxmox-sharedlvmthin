#!/usr/bin/python3
"""Launch and independently witness the exact controller-only SIGKILL checkpoint."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
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


def terminated_by_sigkill(returncode):
    return returncode == -signal.SIGKILL


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expect-host", required=True)
    parser.add_argument("--storeid", required=True)
    parser.add_argument("--vg", required=True)
    parser.add_argument("--tx", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--data-uuid", required=True)
    parser.add_argument("--meta-uuid", required=True)
    args = parser.parse_args()
    if socket.gethostname() != args.expect_host:
        raise RuntimeError("host identity mismatch")
    for value in (args.storeid, args.vg):
        if not re.fullmatch(r"[A-Za-z0-9+_.-]+", value):
            raise RuntimeError("invalid storage identity")
    if not re.fullmatch(r"[0-9a-f]{32}", args.tx) or not re.fullmatch(r"[0-9a-f]{32}", args.nonce):
        raise RuntimeError("invalid transaction identity")
    for value in (args.data_uuid, args.meta_uuid):
        if not re.fullmatch(r"[A-Za-z0-9-]+", value):
            raise RuntimeError("invalid LV identity")

    runner = pathlib.Path(__file__).resolve().parent / "lazy-zero-shared-hydrate-lab.sh"
    if not runner.is_file() or runner.is_symlink():
        raise RuntimeError("unsafe runner")
    command = [str(runner), "--expect-host", args.expect_host, "--storeid", args.storeid,
               "--vg", args.vg, "--tx", args.tx, "--nonce", args.nonce,
               "--data-uuid", args.data_uuid, "--meta-uuid", args.meta_uuid,
               "--data-gib", "8", "--hydration-budget-sec", "900",
               "--fault-mode", "controller-kill-after-partial"]
    process = subprocess.Popen(command)
    controller_pid = process.pid
    controller_start = process_starttime(controller_pid)
    returncode = process.wait()
    if not terminated_by_sigkill(returncode):
        raise RuntimeError(f"controller was not terminated by SIGKILL: returncode={returncode}")
    try:
        surviving_start = process_starttime(controller_pid)
    except FileNotFoundError:
        surviving_start = None
    if surviving_start == controller_start:
        raise RuntimeError("controller identity remains alive after wait")

    root = pathlib.Path(f"/var/tmp/slt-lazy-shared-{args.tx}")
    manifest = root / "controller-crash.manifest"
    if not manifest.is_file() or manifest.is_symlink():
        raise RuntimeError("controller manifest absent or unsafe")
    fields = exact_fields(manifest)
    if (fields.get("CONTROLLER_PID") != str(controller_pid)
            or fields.get("CONTROLLER_STARTTIME") != controller_start
            or fields.get("PHASE") != "CONTROLLER_CRASH_READY"):
        raise RuntimeError("controller manifest process identity mismatch")
    manifest_sha = hashlib.sha256(manifest.read_bytes()).hexdigest()
    witness = root / "controller-crash.observed"
    witness_data = {
        "SCHEMA": "1",
        "EVENT": "CONTROLLER_SIGKILL_OBSERVED",
        "MANIFEST_SHA256": manifest_sha,
        "CONTROLLER_PID": str(controller_pid),
        "CONTROLLER_STARTTIME": controller_start,
        "WAIT_SIGNAL": "SIGKILL",
        "LAUNCHER_PID": str(os.getpid()),
        "LAUNCHER_STARTTIME": process_starttime(os.getpid()),
    }
    payload = "".join(f"{key}={value}\n" for key, value in witness_data.items()).encode()
    write_durable_exclusive(witness, payload)
    witness_sha = hashlib.sha256(payload).hexdigest()
    print(f"CONTROLLER_CRASH_WITNESS={witness}")
    print(f"CONTROLLER_CRASH_WITNESS_SHA256={witness_sha}")
    print(f"CONTROLLER_CRASH_MANIFEST_SHA256={manifest_sha}")
    print("RESULT=CONTROLLER_SIGKILL_OBSERVED_GRAPH_RETAINED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(json.dumps({"state": "ERROR", "error": type(exc).__name__, "detail": str(exc)},
                         sort_keys=True, separators=(",", ":")), file=sys.stderr)
        sys.exit(2)
