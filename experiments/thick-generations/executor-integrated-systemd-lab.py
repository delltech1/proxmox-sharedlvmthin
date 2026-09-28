#!/usr/bin/python3
"""Model-ledger plus inert systemd runner integration qualification.

The only dispatched operation is creation of an identity-bound JSON marker in
a disposable runner directory.  The runner cannot write the separate ledger
directory.  No LVM, device-mapper, PVE configuration, or guest storage command
is available in this script.
"""

import argparse
import hashlib
import json
import os
import pathlib
import re
import signal
import stat
import subprocess
import sys
import time
import types
import uuid


ACK = "DISPOSABLE-INTEGRATED-SYSTEMD-LAB"
PARENT = pathlib.Path("/var/tmp")
RUNNER_PREFIX = "slt-executor-systemd-lab-integrated-"
CODE_PREFIX = "slt-integrated-executor-code-lab-"
CONTROL_PREFIX = "slt-integrated-executor-control-lab-"
UNIT_PREFIX = "slt-thick-lab-exec-"
HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
EXEC_TYPE = "a(sasbttttuii)"


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def read_canonical_json(path):
    raw = path.read_bytes()
    value = json.loads(raw)
    if raw != canonical(value):
        raise RuntimeError(f"{path.name} is not canonical JSON")
    return value, hashlib.sha256(raw).hexdigest()


def directory_fd(root):
    return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def exclusive_json(root, name, value):
    root_fd = directory_fd(root)
    temporary = f".{name}.tmp.{uuid.uuid4().hex}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root_fd)
        try:
            payload = canonical(value)
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd,
                    follow_symlinks=False)
        finally:
            os.unlink(temporary, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def validate_runner_root(root):
    root = pathlib.Path(root)
    if not root.is_absolute() or root.parent != PARENT \
            or not root.name.startswith(RUNNER_PREFIX):
        raise RuntimeError("runner root must be a direct integrated lab path")
    return root


def require_private_root(root, mode):
    info = os.lstat(root)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 \
            or stat.S_IMODE(info.st_mode) != mode:
        raise RuntimeError(f"{root} must be a root-owned mode {mode:04o} directory")


def validate_code_root(root):
    root = pathlib.Path(root)
    if not root.is_absolute() or root.parent != PARENT \
            or not root.name.startswith(CODE_PREFIX):
        raise RuntimeError("code root must be a direct integrated lab path")
    return root


def validate_control_root(root):
    root = pathlib.Path(root)
    if not root.is_absolute() or root.parent != PARENT \
            or not root.name.startswith(CONTROL_PREFIX):
        raise RuntimeError("control root must be a direct integrated lab path")
    return root


def read_no_follow(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        expected = os.fstat(fd).st_size
        chunks = []
        remaining = expected
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                raise RuntimeError("source changed or truncated during snapshot read")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            raise RuntimeError("source grew during snapshot read")
        return b"".join(chunks)
    finally:
        os.close(fd)


def create_code_snapshot(root, sources):
    root = validate_code_root(root)
    parent_fd = directory_fd(PARENT)
    try:
        os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    root_fd = directory_fd(root)
    result = {}
    try:
        for name, source in sources.items():
            payload = read_no_follow(pathlib.Path(source).resolve(strict=True))
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=root_fd)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
            result[name] = {"path": root / name,
                            "sha256": hashlib.sha256(payload).hexdigest()}
        os.fsync(root_fd)
        os.fchmod(root_fd, 0o500)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return result


def proc_start_ticks(pid):
    raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    close = raw.rfind(")")
    if close < 2 or close + 2 >= len(raw):
        raise RuntimeError("process stat has no valid parenthesized comm boundary")
    fields_from_three = raw[close + 2:].split()
    if len(fields_from_three) < 20 or not fields_from_three[19].isdigit():
        raise RuntimeError("process stat start ticks are unavailable")
    return int(fields_from_three[19])


def proc_state(pid):
    raw = pathlib.Path(f"/proc/{pid}/stat").read_text()
    close = raw.rfind(")")
    if close < 2 or close + 2 >= len(raw):
        raise RuntimeError("process stat has no valid state boundary")
    fields_from_three = raw[close + 2:].split()
    if not fields_from_three or len(fields_from_three[0]) != 1:
        raise RuntimeError("process state is unavailable")
    return fields_from_three[0]


def proc_argv(pid):
    raw = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    if not raw.endswith(b"\0"):
        raise RuntimeError("process command line is not NUL terminated")
    values = raw[:-1].split(b"\0")
    if not values or any(not value for value in values):
        raise RuntimeError("process command line contains an empty argument")
    return [value.decode("utf-8", errors="strict") for value in values]


def exec_descriptor(path, argv, ignore_failure=False):
    if path != "/usr/bin/python3" or not isinstance(argv, list) \
            or not argv or argv[0] != path or type(ignore_failure) is not bool:
        raise RuntimeError("typed ExecStart descriptor is invalid")
    return {"schema": 1, "path": path, "argv": argv,
            "ignore_failure": ignore_failure}


def busctl_json(arguments):
    result = subprocess.run(
        ["/usr/bin/busctl", "--system", "--json=short", *arguments],
        check=True, capture_output=True, timeout=5,
    )
    value = json.loads(result.stdout)
    if not isinstance(value, dict) or set(value) != {"type", "data"}:
        raise RuntimeError("busctl returned an invalid typed envelope")
    return value


def unit_object_path(unit):
    value = busctl_json([
        "call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager", "GetUnit", "s", unit,
    ])
    if value.get("type") != "o" or not isinstance(value.get("data"), list) \
            or len(value["data"]) != 1 or not isinstance(value["data"][0], str) \
            or not value["data"][0].startswith("/org/freedesktop/systemd1/unit/"):
        raise RuntimeError("GetUnit returned an invalid object path")
    return value["data"][0]


def typed_exec_start(unit):
    object_path = unit_object_path(unit)
    hooks = {}
    for name in ("ExecStart", "ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost"):
        value = busctl_json([
            "get-property", "org.freedesktop.systemd1", object_path,
            "org.freedesktop.systemd1.Service", name,
        ])
        if value.get("type") != EXEC_TYPE or not isinstance(value.get("data"), list):
            raise RuntimeError(f"typed systemd {name} has an unexpected signature")
        hooks[name] = value["data"]
    if any(hooks[name] for name in ("ExecStartPre", "ExecStartPost", "ExecStop", "ExecStopPost")):
        raise RuntimeError("unexpected systemd execution hook is configured")
    if len(hooks["ExecStart"]) != 1:
        raise RuntimeError("systemd must expose exactly one ExecStart")
    entry = hooks["ExecStart"][0]
    if not isinstance(entry, list) or len(entry) != 10:
        raise RuntimeError("typed ExecStart entry is malformed")
    path, argv, ignore_failure = entry[:3]
    return exec_descriptor(path, argv, ignore_failure), object_path


def show(unit):
    fields = (
        "InvocationID", "MainPID", "ControlGroup", "ActiveState", "SubState",
        "Result", "ExecMainCode", "ExecMainStatus", "Job", "Id", "Type",
        "ExitType", "Restart", "RemainAfterExit", "KillMode", "Delegate",
        "NoNewPrivileges", "PrivateDevices", "ProtectSystem", "ProtectHome",
        "ProtectControlGroups", "CapabilityBoundingSet", "TasksMax", "MemoryMax",
        "ReadWritePaths", "LoadState",
    )
    result = subprocess.run(
        ["/usr/bin/systemctl", "show", unit, "--property=" + ",".join(fields)],
        check=True, capture_output=True, text=True, timeout=5,
    )
    values = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def wait_for(predicate, timeout, description):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(0.05)
    raise RuntimeError(f"timed out waiting for {description}")


def phase_stop(root, scenario, phase, attempt, invocation):
    exclusive_json(root, f"phase-{scenario}.json", {
        "schema": 1, "scenario": scenario, "phase": phase,
        "attempt": attempt, "invocation_id": invocation, "pid": os.getpid(),
        "start_ticks": proc_start_ticks(os.getpid()),
    })
    os.kill(os.getpid(), signal.SIGSTOP)


def runner(script, args):
    root = validate_runner_root(args.runner_root)
    require_private_root(root, 0o700)
    fields = (args.authority_id, args.enrollment_epoch, args.transaction,
              args.attempt, args.run_nonce, args.code_digest, args.policy_digest,
              args.inert_operation_digest)
    if any(not HEX32.fullmatch(value) for value in (
            args.authority_id, args.transaction, args.attempt, args.run_nonce)) \
            or any(not HEX64.fullmatch(value) for value in (
                args.enrollment_epoch, args.code_digest, args.policy_digest,
                args.inert_operation_digest)):
        raise RuntimeError("runner immutable identity is invalid")
    del fields
    invocation = os.environ.get("INVOCATION_ID", "")
    if not HEX32.fullmatch(invocation):
        raise RuntimeError("runner InvocationID is invalid")
    pid = os.getpid()
    actual_argv = proc_argv(pid)
    descriptor = exec_descriptor(actual_argv[0], actual_argv, False)
    command_sha256 = digest(descriptor)
    boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    unit = f"{UNIT_PREFIX}{args.attempt}.service"
    cgroup_raw = pathlib.Path("/proc/self/cgroup").read_text().strip()
    unified = [line[3:] for line in cgroup_raw.splitlines() if line.startswith("0::")]
    if unified != [f"/system.slice/{unit}"]:
        raise RuntimeError("runner is outside its exact systemd cgroup")
    operation, operation_file_sha256 = read_canonical_json(root / "operation.json")
    expected_operation = {
        "schema": 1, "domain": "INERT_EXECUTOR_LAB",
        "authority_id": args.authority_id,
        "enrollment_epoch": args.enrollment_epoch,
        "attempt": args.attempt, "transaction": args.transaction,
        "run_nonce": args.run_nonce, "runner_root": str(root),
        "marker": "dispatch-marker.json",
        "marker_contract": "EXACT_GRANT_SHA_AND_IDENTITY_V1",
        "scenario": args.scenario, "code_digest": args.code_digest,
        "policy_digest": args.policy_digest,
    }
    if operation != expected_operation or operation_file_sha256 != args.inert_operation_digest:
        exclusive_json(root, "refusal.json", {
            "reason": "EXACT_OPERATION_DESCRIPTOR_MISMATCH",
            "attempt": args.attempt, "invocation_id": invocation,
            "received_operation_sha256": operation_file_sha256,
        })
        return 41
    startup = {
        "attempt": args.attempt, "unit": unit, "invocation_id": invocation,
        "boot_id": boot_id, "run_nonce": args.run_nonce, "pid": pid,
        "start_ticks": proc_start_ticks(pid),
        "runner_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "command_sha256": command_sha256, "control_group": unified[0],
        "inert_operation_digest": args.inert_operation_digest,
    }
    exclusive_json(root, "startup.json", startup)
    if args.scenario == "S1":
        phase_stop(root, args.scenario, "STARTUP_REPORTED_PRE_BIND",
                   args.attempt, invocation)
    if args.scenario in ("C3", "C4", "C5", "C6"):
        phase_stop(root, args.scenario, "STARTUP_REPORTED_HELD",
                   args.attempt, invocation)
    grant_path = root / "grant.json"
    grant, grant_file_sha256 = wait_for(
        lambda: read_canonical_json(grant_path) if grant_path.exists() else None,
        30, "persisted exact grant publication",
    )
    expected = {
        "schema": 1, "authority_id": args.authority_id,
        "enrollment_epoch": args.enrollment_epoch,
        "attempt": args.attempt, "transaction": args.transaction,
        "unit": unit, "invocation_id": invocation, "boot_id": boot_id,
        "run_nonce": args.run_nonce, "pid": pid,
        "start_ticks": startup["start_ticks"], "code_digest": args.code_digest,
        "policy_digest": args.policy_digest,
        "runner_sha256": startup["runner_sha256"],
        "command_sha256": command_sha256,
        "control_group": startup["control_group"],
        "inert_operation_digest": args.inert_operation_digest,
    }
    if grant != expected or grant_file_sha256 != digest(expected):
        exclusive_json(root, "refusal.json", {
            "reason": "EXACT_INTEGRATED_GRANT_MISMATCH",
            "attempt": args.attempt, "invocation_id": invocation,
            "received_grant_sha256": grant_file_sha256,
        })
        return 42
    if args.scenario == "S3":
        phase_stop(root, args.scenario, "GRANT_VALIDATED_PRE_MARKER",
                   args.attempt, invocation)
    exclusive_json(root, "dispatch-marker.json", {
        "schema": 1, "attempt": args.attempt, "transaction": args.transaction,
        "invocation_id": invocation, "grant_sha256": grant_file_sha256,
        "inert_operation_digest": args.inert_operation_digest,
    })
    if args.scenario in ("S4", "C7"):
        phase_stop(root, args.scenario, "MARKER_PERSISTED_PRE_EXIT",
                   args.attempt, invocation)
    return 0


def load_verified_module(path, expected_sha256, name):
    path = pathlib.Path(path).resolve(strict=True)
    payload = read_no_follow(path)
    if hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RuntimeError("verified helper snapshot digest mismatch")
    sys.dont_write_bytecode = True
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    code = compile(payload, str(path), "exec", dont_inherit=True)
    exec(code, module.__dict__)
    return module


def qualify(script, args):
    runner_root = validate_runner_root(args.runner_root)
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(runner_root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    snapshot = create_code_snapshot(args.code_root, {
        "admission-adapter.pl": args.adapter,
        "SharedLvmAdmission.pm": args.module,
        "executor-integrated-admission-lab.py": args.integrated_ledger_script,
        "executor-integrated-systemd-lab.py": script,
    })
    require_private_root(validate_code_root(args.code_root), 0o500)
    integrated_path = snapshot["executor-integrated-admission-lab.py"]["path"]
    integrated = load_verified_module(
        integrated_path,
        snapshot["executor-integrated-admission-lab.py"]["sha256"],
        "slt_integrated_ledger",
    )
    model = integrated.Model(
        snapshot["admission-adapter.pl"]["path"],
        snapshot["SharedLvmAdmission.pm"]["path"],
    )
    authority = integrated.authority()
    ledger = integrated.Ledger.initialize(args.ledger_root, authority, model)
    attempt = uuid.uuid4().hex
    transaction = uuid.uuid4().hex
    run_nonce = uuid.uuid4().hex
    unit = f"{UNIT_PREFIX}{attempt}.service"
    runner_script = snapshot["executor-integrated-systemd-lab.py"]["path"]
    runner_sha256 = snapshot["executor-integrated-systemd-lab.py"]["sha256"]
    manifest = {
        "adapter": snapshot["admission-adapter.pl"]["sha256"],
        "model": snapshot["SharedLvmAdmission.pm"]["sha256"],
        "ledger": snapshot["executor-integrated-admission-lab.py"]["sha256"],
        "runner": runner_sha256,
    }
    code_digest = digest(manifest)
    properties = {
        "ExitType": "cgroup", "Restart": "no", "RemainAfterExit": "yes",
        "KillMode": "control-group", "NoNewPrivileges": "yes",
        "PrivateDevices": "yes", "ProtectSystem": "strict", "ProtectHome": "yes",
        "ProtectControlGroups": "yes", "Delegate": "no",
        "CapabilityBoundingSet": "", "TasksMax": "16", "MemoryMax": "67108864",
        "ReadWritePaths": str(runner_root),
    }
    policy_digest = digest(properties)
    operation = {
        "schema": 1, "domain": "INERT_EXECUTOR_LAB",
        "authority_id": authority["authority_id"],
        "enrollment_epoch": authority["enrollment_epoch"],
        "attempt": attempt, "transaction": transaction, "run_nonce": run_nonce,
        "runner_root": str(runner_root), "marker": "dispatch-marker.json",
        "marker_contract": "EXACT_GRANT_SHA_AND_IDENTITY_V1", "scenario": args.scenario,
        "code_digest": code_digest, "policy_digest": policy_digest,
    }
    operation_digest = digest(operation)
    exclusive_json(runner_root, "operation.json", operation)
    stored_operation, stored_operation_sha = read_canonical_json(runner_root / "operation.json")
    if stored_operation != operation or stored_operation_sha != operation_digest:
        raise RuntimeError("published operation descriptor differs before launch")
    runner_argv = [
        "/usr/bin/python3", "-I", "-B", str(runner_script),
        "--runner-root", str(runner_root), "--attempt", attempt,
        "--run-nonce", run_nonce, "--authority-id", authority["authority_id"],
        "--enrollment-epoch", authority["enrollment_epoch"],
        "--transaction", transaction, "--code-digest", code_digest,
        "--policy-digest", policy_digest,
        "--inert-operation-digest", operation_digest,
        "--scenario", args.scenario, "runner",
    ]
    descriptor = exec_descriptor(runner_argv[0], runner_argv, False)
    command_sha256 = digest(descriptor)
    vg_uuid = "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF"
    volume = "vm-900001-disk-0"
    record = {
        "schema": 1, "kind": "THICK_EXECUTOR_LAB",
        "enrollment_epoch": authority["enrollment_epoch"], "vg_uuid": vg_uuid,
        "volume": volume, "object_key": integrated.volume_key(vg_uuid, volume),
        "transaction": transaction, "attempt": attempt,
        "node": authority["authority_node"], "boot_id": authority["authority_boot_id"],
        "unit": unit, "code_digest": code_digest, "policy_digest": policy_digest,
        "state": "RESERVED",
    }
    systemd_command = [
        "/usr/bin/systemd-run", "--quiet", f"--unit={unit}", "--service-type=exec",
        "--property=ExitType=cgroup", "--property=Restart=no",
        "--property=RemainAfterExit=yes", "--property=KillMode=control-group",
        "--property=NoNewPrivileges=yes", "--property=PrivateDevices=yes",
        "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
        "--property=ProtectControlGroups=yes",
        f"--property=ReadWritePaths={runner_root}", "--property=Delegate=no",
        "--property=CapabilityBoundingSet=", "--property=TasksMax=16",
        "--property=MemoryMax=64M", "--property=StandardOutput=null",
        "--property=StandardError=null", "--property=TimeoutStopSec=10s",
        *runner_argv,
    ]
    submission_attempted = False
    submission_outcome = "NOT_ATTEMPTED"
    owned = False
    owned_invocation = None
    evidence = {"record": record, "operation": operation, "manifest": manifest,
                "command_descriptor": descriptor}
    boundary_results = []

    def cleanup_owned():
        nonlocal owned
        if not HEX32.fullmatch(owned_invocation or ""):
            raise RuntimeError("integrated unit InvocationID is unpinned; cleanup refused")
        current = show(unit)
        if current.get("Id") != unit:
            raise RuntimeError("exact integrated unit identity changed; cleanup refused")
        observed, _ = typed_exec_start(unit)
        if observed != descriptor:
            raise RuntimeError("integrated unit ExecStart changed; cleanup refused")
        if current.get("InvocationID") != owned_invocation:
            raise RuntimeError("integrated unit InvocationID changed; cleanup refused")
        stopped = subprocess.run(
            ["/usr/bin/systemctl", "stop", unit], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
        )
        if stopped.returncode != 0:
            raise RuntimeError("exact integrated unit stop failed")
        post = show(unit)
        reset = None
        if post.get("ActiveState") == "failed":
            if post.get("InvocationID") != owned_invocation:
                raise RuntimeError("failed integrated unit identity changed before reset")
            reset_result = subprocess.run(
                ["/usr/bin/systemctl", "reset-failed", unit], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
            )
            reset = reset_result.returncode
            if reset_result.returncode != 0:
                raise RuntimeError("exact integrated unit reset-failed failed")
            post = show(unit)
        inactive = post.get("LoadState") == "not-found" or (
            post.get("LoadState") == "loaded" and post.get("ActiveState") == "inactive"
            and post.get("SubState") == "dead"
        )
        if not inactive or post.get("Job") not in ("", "0"):
            raise RuntimeError("exact integrated unit is not proven inactive after cleanup")
        owned = False
        return {"stop_returncode": stopped.returncode,
                "reset_failed_returncode": reset, "post_stop": post}

    def require_exact_process(expected_startup):
        current = show(unit)
        if current.get("InvocationID") != expected_startup["invocation_id"] \
                or current.get("MainPID") != str(expected_startup["pid"]) \
                or current.get("ControlGroup") != expected_startup["control_group"] \
                or proc_start_ticks(expected_startup["pid"]) != expected_startup["start_ticks"]:
            raise RuntimeError("exact runner identity changed at fault boundary")
        return current

    def signal_exact(expected_startup, selected_signal):
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            raise RuntimeError("pidfd signaling is unavailable; numeric PID fallback is forbidden")
        try:
            pidfd = os.pidfd_open(expected_startup["pid"], 0)
        except OSError as error:
            raise RuntimeError("exact runner pidfd could not be opened") from error
        try:
            require_exact_process(expected_startup)
            signal.pidfd_send_signal(pidfd, selected_signal, None, 0)
        except OSError as error:
            raise RuntimeError("exact runner pidfd signal outcome is unknown") from error
        finally:
            os.close(pidfd)

    def wait_exact_stopped(expected_startup, phase_name=None, phase_value=None):
        phase = None
        if phase_name is not None:
            phase, _ = wait_for(
                lambda: read_canonical_json(runner_root / phase_name)
                if (runner_root / phase_name).exists() else None,
                5, f"{args.scenario} boundary report",
            )
            expected_phase = {
                "schema": 1, "scenario": args.scenario, "phase": phase_value,
                "attempt": attempt, "invocation_id": expected_startup["invocation_id"],
                "pid": expected_startup["pid"],
                "start_ticks": expected_startup["start_ticks"],
            }
            if phase != expected_phase:
                raise RuntimeError("fault boundary phase identity mismatch")
        wait_for(
            lambda: True if proc_state(expected_startup["pid"]) in ("T", "t") else None,
            5, f"{args.scenario} exact stopped runner",
        )
        manager = require_exact_process(expected_startup)
        return {"phase": phase, "process_state": proc_state(expected_startup["pid"]),
                "manager": {key: manager.get(key) for key in
                            ("InvocationID", "MainPID", "ControlGroup", "ActiveState", "SubState")}}

    def refuse_competing_attempt(boundary):
        ledger_path = pathlib.Path(args.ledger_root) / "ledger.json"
        before, before_sha = read_canonical_json(ledger_path)
        contender_attempt = uuid.uuid4().hex
        contender = {
            **record, "transaction": record["transaction"], "attempt": contender_attempt,
            "unit": f"{UNIT_PREFIX}{contender_attempt}.service", "state": "RESERVED",
        }
        independent = integrated.Ledger(args.ledger_root, model)
        try:
            refusal, observed = integrated.Protocol(independent, model).reserve(contender)
        finally:
            independent.close()
        after, after_sha = read_canonical_json(ledger_path)
        if refusal.get("allowed") != 0 or before_sha != after_sha or before != after \
                or observed["revision"] != before["revision"]:
            raise RuntimeError(f"competing attempt changed ledger at {boundary}")
        result = {
            "boundary": boundary, "refusal": refusal,
            "ledger_revision": before["revision"], "ledger_sha256": before_sha,
            "contender_attempt": contender_attempt,
            "transaction_relationship": "SAME_TRANSACTION_NEW_ATTEMPT",
        }
        boundary_results.append(result)
        return result

    def crash_checkpoint(boundary):
        if args.scenario != boundary[:2]:
            return
        control_root = validate_control_root(args.control_root)
        require_private_root(control_root, 0o700)
        ledger_value, ledger_sha256 = read_canonical_json(
            pathlib.Path(args.ledger_root) / "ledger.json"
        )
        artifacts = {}
        for name in ("operation.json", "startup.json", "grant.json",
                     "dispatch-marker.json"):
            path = runner_root / name
            if path.exists():
                value, sha256 = read_canonical_json(path)
                artifacts[name] = {"sha256": sha256, "value": value}
        launch_owner = None
        launch_owner_path = control_root / "launch-owner.json"
        if launch_owner_path.exists():
            value, sha256 = read_canonical_json(launch_owner_path)
            launch_owner = {"sha256": sha256, "value": value}
        ready = {
            "schema": 1, "scenario": args.scenario, "boundary": boundary,
            "controller_pid": os.getpid(),
            "controller_start_ticks": proc_start_ticks(os.getpid()),
            "controller_argv": proc_argv(os.getpid()),
            "boot_id": authority["authority_boot_id"], "attempt": attempt,
            "unit": unit, "transaction": transaction,
            "ledger_revision": ledger_value["revision"],
            "ledger_sha256": ledger_sha256,
            "record_state": ledger_value["slots"][record["object_key"]]["record"]["state"],
            "code_manifest": manifest, "code_digest": code_digest,
            "launch_owner": launch_owner, "artifacts": artifacts,
        }
        exclusive_json(control_root, "crash-ready.json", ready)
        os.kill(os.getpid(), signal.SIGSTOP)
        raise RuntimeError("controller crash checkpoint resumed without SIGKILL")
    try:
        reserve, _ = integrated.Protocol(ledger, model).reserve(record)
        if reserve.get("allowed") != 1:
            raise RuntimeError("ledger reserve did not succeed exactly once")
        crash_checkpoint("C1_RESERVED_PRE_LAUNCH")
        launch, _ = integrated.Protocol(ledger, model).issue_launch(record, command_sha256)
        if launch.get("allowed") != 1:
            raise RuntimeError("ledger reserve/launch did not succeed exactly once")
        crash_checkpoint("C2_LAUNCH_PERSISTED_PRE_SUBMIT")
        if args.scenario.startswith("C"):
            control_root = validate_control_root(args.control_root)
            exclusive_json(control_root, "launch-owner.json", {
                "schema": 1, "attempt": attempt, "transaction": transaction,
                "unit": unit, "command_sha256": command_sha256,
                "runner_root": str(runner_root),
                "boot_id": authority["authority_boot_id"],
                "control_root": str(control_root), "controller_pid": os.getpid(),
                "controller_start_ticks": proc_start_ticks(os.getpid()),
                "controller_argv": proc_argv(os.getpid()),
                "supervisor_run_id": args.supervisor_run_id,
            })
        absent = subprocess.run(
            ["/usr/bin/systemctl", "show", unit, "--property=LoadState", "--value"],
            check=False, capture_output=True, text=True, timeout=5,
        )
        if absent.returncode != 0 or absent.stdout.strip() != "not-found":
            raise RuntimeError("exact integrated lab unit is not absent before launch")
        submission_attempted = True
        submission_outcome = "UNKNOWN"
        subprocess.run(systemd_command, check=True, timeout=10)
        submission_outcome = "ACKNOWLEDGED"
        launch_manager = show(unit)
        launch_descriptor, _ = typed_exec_start(unit)
        candidate_invocation = launch_manager.get("InvocationID", "")
        if launch_manager.get("Id") != unit or not HEX32.fullmatch(candidate_invocation) \
                or launch_descriptor != descriptor:
            raise RuntimeError("submitted unit ownership is ambiguous; cleanup refused")
        owned_invocation = candidate_invocation
        owned = True
        startup, _ = wait_for(
            lambda: read_canonical_json(runner_root / "startup.json")
            if (runner_root / "startup.json").exists() else None,
            15, "integrated runner startup report",
        )
        integrated.validate_startup(startup)
        manager_one = show(unit)
        if manager_one.get("InvocationID") != owned_invocation:
            raise RuntimeError("systemd InvocationID changed after ownership pin")
        observed_descriptor, object_path = typed_exec_start(unit)
        if observed_descriptor != descriptor or digest(observed_descriptor) != command_sha256:
            raise RuntimeError("typed systemd ExecStart differs from launch descriptor")
        if proc_argv(startup["pid"]) != runner_argv:
            raise RuntimeError("runner /proc argv differs from launch descriptor")
        expected_properties = {"Id": unit, "Type": "exec", **properties}
        if any(manager_one.get(key) != value for key, value in expected_properties.items()):
            raise RuntimeError("effective systemd properties differ from policy")
        if startup != {
            "attempt": attempt, "unit": unit,
            "invocation_id": manager_one.get("InvocationID"),
            "boot_id": authority["authority_boot_id"], "run_nonce": run_nonce,
            "pid": int(manager_one.get("MainPID", "0")),
            "start_ticks": proc_start_ticks(int(manager_one.get("MainPID", "0"))),
            "runner_sha256": runner_sha256, "command_sha256": command_sha256,
            "control_group": manager_one.get("ControlGroup"),
            "inert_operation_digest": operation_digest,
        }:
            raise RuntimeError("runner, manager and process startup identities disagree")
        manager_two = show(unit)
        for field in ("InvocationID", "MainPID", "ControlGroup"):
            if manager_two.get(field) != manager_one.get(field):
                raise RuntimeError("systemd identity changed during startup observation")
        crash_checkpoint("C3_RUNNER_OBSERVED_PRE_BIND")
        if args.scenario == "S1":
            stopped = wait_exact_stopped(
                startup, f"phase-{args.scenario}.json", "STARTUP_REPORTED_PRE_BIND"
            )
            refusal = refuse_competing_attempt("S1_STARTUP_REPORTED_PRE_BIND")
            signal_exact(startup, signal.SIGCONT)
            boundary_results[-1].update({"stopped": stopped, "resumed": True,
                                         "refusal": refusal["refusal"]})
        pre_bind_marker_absent = not (runner_root / "dispatch-marker.json").exists()
        if not pre_bind_marker_absent:
            raise RuntimeError("runner created marker before bind")
        protocol = integrated.Protocol(ledger, model)
        bind, after_bind = protocol.bind(record, startup)
        if bind.get("allowed") != 1:
            raise RuntimeError("exact startup observation did not bind")
        bound = after_bind["slots"][record["object_key"]]["record"]
        crash_checkpoint("C4_BOUND_PRE_DISPATCH")
        if args.scenario == "S2":
            exclusive_json(runner_root, "phase-S2.json", {
                "schema": 1, "scenario": "S2", "phase": "BOUND_PRE_DISPATCH",
                "attempt": attempt, "invocation_id": startup["invocation_id"],
                "pid": startup["pid"], "start_ticks": startup["start_ticks"],
                "ledger_revision": after_bind["revision"],
            })
            signal_exact(startup, signal.SIGSTOP)
            stopped = wait_exact_stopped(startup)
            refusal = refuse_competing_attempt("S2_BOUND_PRE_DISPATCH")
            signal_exact(startup, signal.SIGCONT)
            boundary_results[-1].update({"stopped": stopped, "resumed": True,
                                         "phase_file": "phase-S2.json",
                                         "refusal": refusal["refusal"]})
        grant = {
            "schema": 1, "authority_id": authority["authority_id"],
            "enrollment_epoch": authority["enrollment_epoch"],
            "attempt": attempt, "transaction": transaction, "unit": unit,
            "invocation_id": startup["invocation_id"], "boot_id": startup["boot_id"],
            "run_nonce": run_nonce, "pid": startup["pid"],
            "start_ticks": startup["start_ticks"], "code_digest": code_digest,
            "policy_digest": policy_digest, "runner_sha256": runner_sha256,
            "command_sha256": command_sha256,
            "control_group": startup["control_group"],
            "inert_operation_digest": operation_digest,
        }
        dispatch, after_dispatch = protocol.issue_dispatch(bound, grant)
        if dispatch.get("allowed") != 1:
            raise RuntimeError("exact persisted BOUND invocation did not issue dispatch")
        stored = after_dispatch["slots"][record["object_key"]]["dispatch_issued"]
        if stored["grant"] != grant or stored["grant_sha256"] != digest(grant):
            raise RuntimeError("persisted dispatch grant differs before publication")
        crash_checkpoint("C5_DISPATCH_PERSISTED_PRE_GRANT")
        pre_grant_marker_absent = not (runner_root / "dispatch-marker.json").exists()
        if not pre_grant_marker_absent:
            raise RuntimeError("runner created marker before grant publication")
        exclusive_json(runner_root, "grant.json", grant)
        published, published_sha = read_canonical_json(runner_root / "grant.json")
        if published != stored["grant"] or published_sha != stored["grant_sha256"]:
            raise RuntimeError("published grant bytes differ from persisted dispatch")
        crash_checkpoint("C6_GRANT_PUBLISHED")
        if args.scenario == "S3":
            stopped = wait_exact_stopped(
                startup, "phase-S3.json", "GRANT_VALIDATED_PRE_MARKER"
            )
            marker_absent_before_refusal = not (runner_root / "dispatch-marker.json").exists()
            if not marker_absent_before_refusal:
                raise RuntimeError("S3 runner created marker before its stopped boundary")
            refusal = refuse_competing_attempt("S3_GRANT_VALIDATED_PRE_MARKER")
            marker_absent_after_refusal = not (runner_root / "dispatch-marker.json").exists()
            if not marker_absent_after_refusal:
                raise RuntimeError("S3 marker appeared while exact runner remained stopped")
            signal_exact(startup, signal.SIGCONT)
            boundary_results[-1].update({"stopped": stopped, "resumed": True,
                                         "refusal": refusal["refusal"],
                                         "marker_absent_before_refusal": marker_absent_before_refusal,
                                         "marker_absent_after_refusal": marker_absent_after_refusal})
        marker, _ = wait_for(
            lambda: read_canonical_json(runner_root / "dispatch-marker.json")
            if (runner_root / "dispatch-marker.json").exists() else None,
            15, "integrated inert marker",
        )
        if marker != {
            "schema": 1, "attempt": attempt, "transaction": transaction,
            "invocation_id": startup["invocation_id"],
            "grant_sha256": stored["grant_sha256"],
            "inert_operation_digest": operation_digest,
        }:
            raise RuntimeError("integrated inert marker identity mismatch")
        crash_checkpoint("C7_MARKER_OBSERVED_PRE_TERMINAL")
        if args.scenario == "S4":
            stopped = wait_exact_stopped(
                startup, "phase-S4.json", "MARKER_PERSISTED_PRE_EXIT"
            )
            refusal = refuse_competing_attempt("S4_MARKER_PERSISTED_PRE_EXIT")
            signal_exact(startup, signal.SIGCONT)
            boundary_results[-1].update({"stopped": stopped, "resumed": True,
                                         "refusal": refusal["refusal"]})
        terminal = wait_for(
            lambda: (state if (state := show(unit)).get("SubState") == "exited" else None),
            15, "integrated runner terminal state",
        )
        if terminal.get("Result") != "success" or terminal.get("ExecMainStatus") != "0" \
                or terminal.get("InvocationID") != startup["invocation_id"] \
                or terminal.get("Job") not in ("", "0") \
                or terminal.get("MainPID") not in ("", "0"):
            raise RuntimeError("integrated runner terminal evidence is incomplete")
        if pathlib.Path(f"/proc/{startup['pid']}").exists():
            raise RuntimeError("integrated runner PID still exists at terminal classification")
        cgroup_path = pathlib.Path("/sys/fs/cgroup" + startup["control_group"])
        if cgroup_path.exists():
            procs = cgroup_path.joinpath("cgroup.procs").read_text().split()
            events = dict(line.split(maxsplit=1) for line in
                          cgroup_path.joinpath("cgroup.events").read_text().splitlines())
            if procs or events.get("populated") != "0":
                raise RuntimeError("integrated terminal cgroup is not empty")
            cgroup_terminal = "DIRECT_EMPTY"
        else:
            if terminal.get("ControlGroup") not in ("", startup["control_group"]):
                raise RuntimeError("pruned integrated cgroup identity changed")
            cgroup_terminal = "PRUNED_AFTER_SAME_LIFECYCLE_TERMINAL"
        cleanup = cleanup_owned()
        evidence.update({"object_path": object_path, "startup": startup,
                         "grant_sha256": stored["grant_sha256"], "marker": marker,
                         "terminal": terminal, "ledger_revision": after_dispatch["revision"],
                         "ledger_sha256": digest(after_dispatch),
                         "cgroup_terminal": cgroup_terminal, "cleanup": cleanup,
                         "pre_bind_marker_absent": pre_bind_marker_absent,
                         "pre_grant_marker_absent": pre_grant_marker_absent,
                         "boundary_results": boundary_results,
                         "code_snapshot": {key: {"path": str(value["path"]),
                                                   "sha256": value["sha256"]}
                                           for key, value in snapshot.items()}})
        exclusive_json(runner_root, "evidence.json", evidence)
        print(json.dumps({
            "classification": "INTEGRATED_INERT_DISPATCH_PASS"
            if args.scenario == "normal" else "INTEGRATED_INERT_SIGSTOP_PASS",
            "attempt": attempt, "unit": unit,
            "ledger_revision": after_dispatch["revision"],
            "ledger_sha256": digest(after_dispatch),
            "evidence_sha256": hashlib.sha256((runner_root / "evidence.json").read_bytes()).hexdigest(),
            "scenario": args.scenario,
            "limitations": ["ledger finish/close is intentionally absent",
                            "no controller crash, reboot, cross-node or storage I/O"],
        }, sort_keys=True))
    except Exception as error:
        try:
            exclusive_json(runner_root, "failure.json", {
                "error": str(error), "unit": unit, "attempt": attempt,
                "submission_attempted": submission_attempted,
                "submission_outcome": submission_outcome,
                "ownership_pinned": owned,
                "owned_invocation": owned_invocation,
            })
        except Exception:
            pass
        raise
    finally:
        if owned:
            try:
                cleanup_owned()
            except Exception as cleanup_error:
                try:
                    exclusive_json(runner_root, "cleanup-failure.json", {
                        "error": str(cleanup_error), "unit": unit,
                        "attempt": attempt, "invocation_id": owned_invocation,
                    })
                except Exception:
                    pass
        ledger.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runner-root", required=True)
    parser.add_argument("--ledger-root")
    parser.add_argument("--code-root")
    parser.add_argument("--control-root")
    parser.add_argument("--supervisor-run-id")
    parser.add_argument("--integrated-ledger-script")
    parser.add_argument("--adapter")
    parser.add_argument("--module")
    parser.add_argument("--ack")
    parser.add_argument("--authority-id")
    parser.add_argument("--enrollment-epoch")
    parser.add_argument("--transaction")
    parser.add_argument("--attempt")
    parser.add_argument("--run-nonce")
    parser.add_argument("--code-digest")
    parser.add_argument("--policy-digest")
    parser.add_argument("--inert-operation-digest")
    parser.add_argument("--scenario", choices=(
        "normal", "S1", "S2", "S3", "S4", "C1", "C2", "C3", "C4", "C5", "C6", "C7"
    ),
                        default="normal")
    parser.add_argument("action", choices=("qualify", "runner"))
    args = parser.parse_args()
    script = pathlib.Path(__file__).resolve(strict=True)
    if args.action == "runner":
        raise SystemExit(runner(script, args))
    if args.ack != ACK or os.geteuid() != 0 or not all((
            args.ledger_root, args.code_root, args.integrated_ledger_script,
            args.adapter, args.module)):
        raise SystemExit("qualification requires root, exact paths and disposable acknowledgement")
    if args.scenario.startswith("C") and not args.control_root:
        raise SystemExit("controller-crash scenario requires an exact control root")
    if args.scenario.startswith("C") and not HEX32.fullmatch(args.supervisor_run_id or ""):
        raise SystemExit("controller-crash scenario requires an exact supervisor run id")
    qualify(script, args)


if __name__ == "__main__":
    main()
