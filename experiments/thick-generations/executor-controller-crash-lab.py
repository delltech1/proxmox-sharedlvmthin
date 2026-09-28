#!/usr/bin/python3
"""Supervisor for post-persistence controller-crash refusal qualification."""

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
import types
import uuid
import fcntl


ACK = "DISPOSABLE-INTEGRATED-CONTROLLER-CRASH-LAB"
PARENT = pathlib.Path("/var/tmp")
CONTROL_PREFIX = "slt-integrated-executor-control-lab-"
UNIT_PREFIX = "slt-thick-lab-exec-"
HEX64 = re.compile(r"^[a-f0-9]{64}$")
SCENARIOS = {
    "C1": {"boundary": "C1_RESERVED_PRE_LAUNCH", "revision": 2, "state": "RESERVED", "unit": False,
           "launch": False, "dispatch": False, "grant": False, "marker": False},
    "C2": {"boundary": "C2_LAUNCH_PERSISTED_PRE_SUBMIT", "revision": 3, "state": "RESERVED", "unit": False,
           "launch": True, "dispatch": False, "grant": False, "marker": False},
    "C3": {"boundary": "C3_RUNNER_OBSERVED_PRE_BIND", "revision": 3, "state": "RESERVED", "unit": True,
           "launch": True, "dispatch": False, "grant": False, "marker": False},
    "C4": {"boundary": "C4_BOUND_PRE_DISPATCH", "revision": 4, "state": "BOUND", "unit": True,
           "launch": True, "dispatch": False, "grant": False, "marker": False},
    "C5": {"boundary": "C5_DISPATCH_PERSISTED_PRE_GRANT", "revision": 5, "state": "BOUND", "unit": True,
           "launch": True, "dispatch": True, "grant": False, "marker": False},
    "C6": {"boundary": "C6_GRANT_PUBLISHED", "revision": 5, "state": "BOUND", "unit": True,
           "launch": True, "dispatch": True, "grant": True, "marker": False},
    "C7": {"boundary": "C7_MARKER_OBSERVED_PRE_TERMINAL", "revision": 5, "state": "BOUND", "unit": True,
           "launch": True, "dispatch": True, "grant": True, "marker": True},
}


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def file_sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_exact(path, name, expected_sha256):
    path = pathlib.Path(path).resolve(strict=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    payload = b"".join(chunks)
    captured_sha256 = hashlib.sha256(payload).hexdigest()
    if captured_sha256 != expected_sha256:
        raise RuntimeError("captured module bytes differ before execution")
    sys.dont_write_bytecode = True
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(payload, str(path), "exec", dont_inherit=True), module.__dict__)
    return module, captured_sha256


def exact_ledger_load(ledger, ledger_module):
    """Read, validate and hash the exact canonical ledger bytes via its pinned dirfd."""
    fd = os.open("ledger.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=ledger.root_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 \
                or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("ledger must be root-owned mode 0600")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    data = json.loads(raw, object_pairs_hook=ledger_module.duplicate_keys)
    if raw != ledger_module.canonical(data):
        raise RuntimeError("ledger bytes are not canonical JSON")
    validated = ledger._validate(data)
    identity = {field: validated[field] for field in (
        "authority_id", "authority_node", "authority_boot_id", "enrollment_epoch"
    )}
    if ledger.pinned_authority is None:
        ledger.pinned_authority = identity
    elif identity != ledger.pinned_authority:
        raise RuntimeError("ledger authority changed within one open instance")
    return validated, hashlib.sha256(raw).hexdigest()


def locked_exact_ledger(ledger, ledger_module):
    ledger._lock()
    try:
        return exact_ledger_load(ledger, ledger_module)
    finally:
        fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)


def verify_code_manifest(code_root, manifest, expected_digest):
    if set(manifest) != {"adapter", "model", "ledger", "runner"} \
            or any(not HEX64.fullmatch(value or "") for value in manifest.values()) \
            or hashlib.sha256(canonical(manifest)).hexdigest() != expected_digest:
        raise RuntimeError("finish child code manifest digest mismatch")
    paths = {
        "adapter": code_root / "admission-adapter.pl",
        "model": code_root / "SharedLvmAdmission.pm",
        "ledger": code_root / "executor-integrated-admission-lab.py",
        "runner": code_root / "executor-integrated-systemd-lab.py",
    }
    if {name: file_sha(path) for name, path in paths.items()} != manifest:
        raise RuntimeError("finish child code snapshot differs from pinned manifest")
    return paths


def fresh_control_root(root):
    root = pathlib.Path(root)
    if not root.is_absolute() or root.parent != PARENT \
            or not root.name.startswith(CONTROL_PREFIX):
        raise RuntimeError("control root is outside the disposable namespace")
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return root


def copy_controller(root, source, name="controller.py"):
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        payload = b""
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            payload += chunk
    finally:
        os.close(source_fd)
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=root_fd)
        try:
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    return root / name, hashlib.sha256(payload).hexdigest()


def bounded_wait_child(child, timeout):
    try:
        return child.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("controller child did not terminate within deadline") from error


def capture_child_pipes(child, timeout=1):
    try:
        stdout, stderr = child.communicate(timeout=timeout)
        return stdout or b"", stderr or b"", None
    except subprocess.TimeoutExpired as error:
        stdout = error.output if isinstance(error.output, bytes) else b""
        stderr = error.stderr if isinstance(error.stderr, bytes) else b""
        return stdout, stderr, "PIPE_DRAIN_TIMEOUT_AFTER_CONTROLLER_REAP"


def exact_artifacts(controller, runner_root, ready):
    actual = {}
    for name in ("operation.json", "startup.json",
                 "grant.json", "dispatch-marker.json"):
        path = runner_root / name
        if path.exists():
            value, sha256 = controller.read_canonical_json(path)
            actual[name] = {"sha256": sha256, "value": value}
    if actual != ready.get("artifacts"):
        raise RuntimeError("runtime artifacts differ from durable crash checkpoint")
    return actual


def terminal_proof(controller, unit, startup):
    terminal = controller.wait_for(
        lambda: (state if (state := controller.show(unit)).get("SubState") == "exited" else None),
        10, "authorized runner terminal state after controller crash",
    )
    if terminal.get("InvocationID") != startup["invocation_id"] \
            or terminal.get("Result") != "success" \
            or terminal.get("ExecMainStatus") != "0" \
            or terminal.get("Job") not in ("", "0") \
            or terminal.get("MainPID") not in ("", "0"):
        raise RuntimeError("resumed authorized runner terminal evidence is incomplete")
    if pathlib.Path(f"/proc/{startup['pid']}").exists():
        raise RuntimeError("resumed authorized runner PID still exists")
    cgroup_path = pathlib.Path("/sys/fs/cgroup" + startup["control_group"])
    if cgroup_path.exists():
        procs = cgroup_path.joinpath("cgroup.procs").read_text().split()
        events = dict(line.split(maxsplit=1) for line in
                      cgroup_path.joinpath("cgroup.events").read_text().splitlines())
        if procs or events.get("populated") != "0":
            raise RuntimeError("resumed authorized runner cgroup is not empty")
        cgroup_terminal = "DIRECT_EMPTY"
    else:
        if terminal.get("ControlGroup") not in ("", startup["control_group"]):
            raise RuntimeError("pruned runner cgroup identity changed")
        cgroup_terminal = "PRUNED_AFTER_SAME_LIFECYCLE_TERMINAL"
    return terminal, cgroup_terminal


def expected_marker(record, startup, stored, artifacts):
    operation_sha = artifacts["operation.json"]["sha256"]
    if stored["grant"].get("inert_operation_digest") != operation_sha \
            or startup.get("inert_operation_digest") != operation_sha:
        raise RuntimeError("grant/startup operation identity differs from checkpoint")
    return {
        "schema": 1, "attempt": record["attempt"],
        "transaction": record["transaction"],
        "invocation_id": startup["invocation_id"],
        "grant_sha256": stored["grant_sha256"],
        "inert_operation_digest": stored["grant"]["inert_operation_digest"],
    }


def build_c6_terminal_proof(controller, command, child_pid, ready, authority,
                            record, slot, runtime, cleanup):
    terminal = runtime["terminal"]
    startup = runtime["startup"]
    proof = {
        "schema": 1, "scope": "INERT_MARKER_ONLY", "origin": "RUNTIME_COLLECTED",
        "identity": {
            "authority_id": authority["authority_id"],
            "enrollment_epoch": record["enrollment_epoch"],
            "object_key": record["object_key"], "transaction": record["transaction"],
            "attempt": record["attempt"], "unit": record["unit"],
            "invocation_id": record["invocation_id"], "boot_id": record["boot_id"],
        },
        "dispatch_grant_sha256": slot["dispatch_issued"]["grant_sha256"],
        "operation_sha256": slot["dispatch_issued"]["grant"]["inert_operation_digest"],
        "marker": {"sha256": runtime["marker_sha256"], "value": runtime["marker"]},
        "controller": {
            "pid": child_pid, "start_ticks": ready["controller_start_ticks"],
            "wait_status": -signal.SIGKILL, "reaped": 1,
            "checkpoint_boundary": ready["boundary"],
            "command_sha256": controller.digest(command),
        },
        "terminal": {
            "invocation_id": startup["invocation_id"], "pid": startup["pid"],
            "start_ticks": startup["start_ticks"],
            "result": terminal["Result"],
            "exec_main_status": terminal["ExecMainStatus"],
            "main_pid": terminal["MainPID"], "job": terminal["Job"],
            "pid_absent": 1, "cgroup_terminal": runtime["cgroup_terminal"],
        },
        "cleanup": {
            "unit_absent": 1 if cleanup["post_stop"].get("LoadState") == "not-found" else 0,
            "job_absent": 1 if cleanup["post_stop"].get("Job") in ("", "0") else 0,
        },
    }
    observations = {key: value for key, value in proof.items() if key != "verifier"}
    proof["verifier"] = {
        "kind": "C6_POST_CRASH_TERMINAL_V1", "version": 1,
        "observations_sha256": controller.digest(observations),
    }
    return proof


def cleanup_exact_unit(controller, unit, runner_root, owner_path, owner_identity):
    startup_path = pathlib.Path(runner_root) / "startup.json"
    owner_path = pathlib.Path(owner_path)
    if not owner_path.exists():
        return {"attempted": False, "reason": "NO_DURABLE_LAUNCH_OWNER"}
    owner, _ = controller.read_canonical_json(owner_path)
    if any(owner.get(key) != value for key, value in owner_identity.items()) \
            or owner.get("unit") != unit:
        return {"attempted": False, "reason": "CURRENT_RUN_OWNER_MISMATCH"}
    startup = None
    if startup_path.exists():
        startup, _ = controller.read_canonical_json(startup_path)
    manager = controller.show(unit)
    if manager.get("LoadState") == "not-found":
        return {"attempted": True, "reason": "EXACT_UNIT_ABSENT", "post_stop": manager}
    typed, _ = controller.typed_exec_start(unit)
    if manager.get("Id") != unit \
            or owner.get("unit") != unit \
            or controller.digest(typed) != owner.get("command_sha256") \
            or (startup is not None and
                manager.get("InvocationID") != startup.get("invocation_id")):
        return {"attempted": False, "reason": "EXACT_OWNERSHIP_NOT_PROVEN"}
    stopped = subprocess.run(
        ["/usr/bin/systemctl", "stop", unit], check=False,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
    )
    if stopped.returncode != 0:
        raise RuntimeError("exact owned unit stop failed during failure cleanup")
    post = controller.show(unit)
    reset_returncode = None
    if post.get("LoadState") == "loaded" and post.get("ActiveState") == "failed":
        if startup is None or post.get("InvocationID") != startup.get("invocation_id"):
            raise RuntimeError("failed unit lifecycle changed before reset")
        reset = subprocess.run(
            ["/usr/bin/systemctl", "reset-failed", unit], check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
        )
        reset_returncode = reset.returncode
        if reset.returncode != 0:
            raise RuntimeError("exact owned unit reset failed during failure cleanup")
        post = controller.show(unit)
    inactive = post.get("LoadState") == "not-found" or (
        post.get("LoadState") == "loaded" and post.get("ActiveState") == "inactive"
        and post.get("SubState") == "dead"
    )
    if not inactive or post.get("Job") not in ("", "0"):
        raise RuntimeError("failure cleanup did not prove exact unit inactive")
    return {"attempted": True, "stop_returncode": stopped.returncode,
            "reset_returncode": reset_returncode, "post_stop": post}


def execute_finish_store_boundary(scenario, data, store, publish_checkpoint, stop_self):
    """Place the injected stop strictly before or after the one durable store call."""
    if scenario == "F1":
        publish_checkpoint(None)
        stop_self()
        raise RuntimeError("F1 finish child resumed unexpectedly")
    if scenario != "F2":
        raise RuntimeError("unsupported finish store boundary")
    stored = store(data)
    publish_checkpoint(stored)
    stop_self()
    raise RuntimeError("F2 finish child resumed unexpectedly")


def finish_child(args):
    control_root = pathlib.Path(args.control_root).resolve(strict=True)
    code_root = pathlib.Path(args.code_root).resolve(strict=True)
    manifest = {
        "adapter": args.expected_adapter_sha256,
        "model": args.expected_model_sha256,
        "ledger": args.expected_ledger_helper_sha256,
        "runner": args.expected_runner_sha256,
    }
    paths = verify_code_manifest(code_root, manifest, args.expected_code_digest)
    controller_module, _ = load_exact(
        paths["runner"],
        "slt_finish_controller_helpers", manifest["runner"],
    )
    proof, proof_sha = controller_module.read_canonical_json(control_root / "finish-proof.json")
    if proof_sha != args.finish_proof_sha256:
        raise RuntimeError("finish child proof bytes differ from pinned SHA")
    ledger_module, _ = load_exact(
        paths["ledger"],
        "slt_finish_ledger_helpers", manifest["ledger"],
    )
    ledger_module.validate_terminal_proof(proof)
    if proof.get("origin") != "RUNTIME_COLLECTED":
        raise RuntimeError("finish child refuses non-runtime proof")
    model = ledger_module.Model(
        paths["adapter"], paths["model"]
    )
    if model.module_sha256 != manifest["model"]:
        raise RuntimeError("finish child model bytes changed before use")
    expected_key = proof["identity"]["object_key"]

    class FaultLedger(ledger_module.Ledger):
        def __init__(self, *values, **keywords):
            super().__init__(*values, **keywords)
            self.store_calls = 0

        def _load(self):
            loaded, loaded_sha = exact_ledger_load(self, ledger_module)
            if loaded_sha != args.expected_ledger_sha256 \
                    or loaded["revision"] != 5 \
                    or set(loaded["slots"]) != {expected_key} \
                    or loaded["slots"][expected_key]["record"].get("code_digest") \
                    != args.expected_code_digest:
                raise RuntimeError("finish child pre-ledger identity changed under lock")
            return loaded

        def _store(self, data):
            self.store_calls += 1
            if self.store_calls != 1 or data.get("revision") != 5 \
                    or set(data.get("slots", {})) != {expected_key}:
                raise RuntimeError("finish child store target/revision is not exact")
            slot = data["slots"][expected_key]
            if slot["record"].get("state") != "TERMINAL" \
                    or slot["record"].get("executor_result") != "SUCCESS" \
                    or slot["finish_observed"].get("proof_sha256") != proof_sha:
                raise RuntimeError("finish child store is not exact TERMINAL/SUCCESS")
            checkpoint = {
                "schema": 1, "scenario": args.finish_child,
                "boundary": "PRE_FINISH_STORE" if args.finish_child == "F1"
                else "POST_FINISH_STORE_PRE_ACK",
                "run_id": args.supervisor_run_id, "pid": os.getpid(),
                "start_ticks": controller_module.proc_start_ticks(os.getpid()),
                "argv": controller_module.proc_argv(os.getpid()),
                "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "proof_sha256": proof_sha, "pre_ledger_sha256": args.expected_ledger_sha256,
                "expected_pre_revision": 5, "expected_post_revision": 6,
                "object_key": expected_key, "transaction": proof["identity"]["transaction"],
                "attempt": proof["identity"]["attempt"],
                "invocation_id": proof["identity"]["invocation_id"],
            }
            def publish(stored):
                if stored is not None:
                    checkpoint["post_ledger_sha256"] = ledger_module.digest(stored)
                controller_module.exclusive_json(control_root, "finish-crash-ready.json", checkpoint)
            return execute_finish_store_boundary(
                args.finish_child, data, super()._store, publish,
                lambda: os.kill(os.getpid(), signal.SIGSTOP),
            )

    ledger = FaultLedger(args.ledger_root, model)
    try:
        before, observed = ledger.transaction(lambda data: (None, data))
        if before != observed:
            raise RuntimeError("finish child readonly transaction changed ledger")
        record = before["slots"][expected_key]["record"]
        ledger_module.Protocol(ledger, model).finish_exact(record, proof)
        raise RuntimeError("finish child returned without fault checkpoint")
    finally:
        ledger.close()


def qualify_finish_crash(args, controller, control_root, proof, proof_sha, before,
                         before_sha, record, slot, ledger_module, model, manifest,
                         code_digest):
    controller.exclusive_json(control_root, "finish-proof.json", proof)
    finish_script, finish_script_sha = copy_controller(
        control_root, pathlib.Path(__file__).resolve(), "finish-child.py"
    )
    finish_run_id = uuid.uuid4().hex
    command = [
        "/usr/bin/python3", "-I", "-B", str(finish_script),
        "--scenario", "C6", "--control-root", str(control_root),
        "--runner-root", args.runner_root, "--ledger-root", args.ledger_root,
        "--code-root", args.code_root, "--controller", args.controller,
        "--integrated-ledger-script", args.integrated_ledger_script,
        "--adapter", args.adapter, "--module", args.module, "--ack", ACK,
        "--finish-child", args.finish_crash,
        "--finish-proof-sha256", proof_sha,
        "--expected-ledger-sha256", before_sha,
        "--supervisor-run-id", finish_run_id,
        "--expected-code-digest", code_digest,
        "--expected-adapter-sha256", manifest["adapter"],
        "--expected-model-sha256", manifest["model"],
        "--expected-ledger-helper-sha256", manifest["ledger"],
        "--expected-runner-sha256", manifest["runner"],
    ]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_reaped = False
    pipes_captured = False
    completed = False
    try:
        checkpoint, checkpoint_sha = controller.wait_for(
            lambda: controller.read_canonical_json(control_root / "finish-crash-ready.json")
            if (control_root / "finish-crash-ready.json").exists() else None,
            15, f"{args.finish_crash} finish crash checkpoint",
        )
        controller.wait_for(
            lambda: True if controller.proc_state(child.pid) in ("T", "t") else None,
            5, f"{args.finish_crash} exact stopped finish child",
        )
        expected_boundary = "PRE_FINISH_STORE" if args.finish_crash == "F1" \
            else "POST_FINISH_STORE_PRE_ACK"
        expected_checkpoint_keys = {
            "schema", "scenario", "boundary", "run_id", "pid", "start_ticks",
            "argv", "boot_id", "proof_sha256", "pre_ledger_sha256",
            "expected_pre_revision", "expected_post_revision", "object_key",
            "transaction", "attempt", "invocation_id",
        }
        if args.finish_crash == "F2":
            expected_checkpoint_keys.add("post_ledger_sha256")
        identity = proof["identity"]
        if set(checkpoint) != expected_checkpoint_keys \
                or type(checkpoint.get("schema")) is not int or checkpoint["schema"] != 1 \
                or checkpoint.get("scenario") != args.finish_crash \
                or checkpoint.get("boundary") != expected_boundary \
                or checkpoint.get("run_id") != finish_run_id \
                or type(checkpoint.get("pid")) is not int or checkpoint["pid"] != child.pid \
                or checkpoint.get("argv") != command \
                or controller.proc_argv(child.pid) != command \
                or type(checkpoint.get("start_ticks")) is not int \
                or checkpoint["start_ticks"] != controller.proc_start_ticks(child.pid) \
                or checkpoint.get("boot_id") != pathlib.Path(
                    "/proc/sys/kernel/random/boot_id").read_text().strip() \
                or checkpoint.get("proof_sha256") != proof_sha \
                or checkpoint.get("pre_ledger_sha256") != before_sha \
                or type(checkpoint.get("expected_pre_revision")) is not int \
                or checkpoint["expected_pre_revision"] != 5 \
                or type(checkpoint.get("expected_post_revision")) is not int \
                or checkpoint["expected_post_revision"] != 6 \
                or checkpoint.get("object_key") != identity["object_key"] \
                or checkpoint.get("transaction") != identity["transaction"] \
                or checkpoint.get("attempt") != identity["attempt"] \
                or checkpoint.get("invocation_id") != identity["invocation_id"]:
            raise RuntimeError("finish crash checkpoint identity is not exact")
        pidfd = os.pidfd_open(child.pid, 0)
        try:
            signal.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
        finally:
            os.close(pidfd)
        wait_status = bounded_wait_child(child, 5)
        child_reaped = True
        stdout, stderr, capture_error = capture_child_pipes(child)
        pipes_captured = True
        if wait_status != -signal.SIGKILL:
            raise RuntimeError("finish child was not terminated by injected SIGKILL")

        ledger = ledger_module.Ledger(args.ledger_root, model)
        try:
            observed, observed_sha = locked_exact_ledger(ledger, ledger_module)
            observed_slot = observed["slots"][record["object_key"]]
            if args.finish_crash == "F1":
                if observed != before or observed_sha != before_sha \
                        or observed["revision"] != 5 \
                        or observed_slot["record"]["state"] != "BOUND" \
                        or observed_slot["finish_observed"] is not None:
                    raise RuntimeError("F1 changed ledger before finish store")
                duplicate_finish = None
            else:
                if observed["revision"] != 6 \
                        or observed_slot["record"].get("state") != "TERMINAL" \
                        or observed_slot["record"].get("executor_result") != "SUCCESS" \
                        or observed_slot["finish_observed"]["proof"] != proof \
                        or observed_slot["finish_observed"]["proof_sha256"] != proof_sha \
                        or checkpoint.get("post_ledger_sha256") != observed_sha:
                    raise RuntimeError("F2 did not persist exact terminal finish")
                duplicate_finish, duplicate_state = ledger_module.Protocol(
                    ledger, model
                ).finish_exact(record, proof)
                if duplicate_finish.get("allowed") != 0 or duplicate_state != observed:
                    raise RuntimeError("F2 duplicate finish changed terminal ledger")
                after_duplicate, after_duplicate_sha = locked_exact_ledger(
                    ledger, ledger_module
                )
                if after_duplicate != observed or after_duplicate_sha != observed_sha:
                    raise RuntimeError("F2 duplicate finish changed exact ledger bytes")
            retry_attempt = uuid.uuid4().hex
            retry = {**record, "attempt": retry_attempt, "state": "RESERVED"}
            retry.pop("invocation_id", None)
            retry["unit"] = f"{UNIT_PREFIX}{retry_attempt}.service"
            retry_result, retry_state = ledger_module.Protocol(ledger, model).reserve(retry)
            if retry_result.get("allowed") != 0 or retry_state != observed:
                raise RuntimeError("post-finish-crash contender changed ledger")
            after_retry, after_retry_sha = locked_exact_ledger(ledger, ledger_module)
            if after_retry != observed or after_retry_sha != observed_sha:
                raise RuntimeError("post-finish-crash contender changed exact ledger bytes")
        finally:
            ledger.close()
        result = {
            "scenario": args.finish_crash, "boundary": expected_boundary,
            "finish_script_sha256": finish_script_sha,
            "checkpoint": checkpoint, "checkpoint_sha256": checkpoint_sha,
            "wait_status": wait_status, "stdout": stdout.decode("utf-8", "replace"),
            "stderr": stderr.decode("utf-8", "replace"),
            "capture_error": capture_error, "ledger_revision": observed["revision"],
            "ledger_sha256": observed_sha, "record_state": observed_slot["record"]["state"],
            "duplicate_finish": duplicate_finish, "contender_refusal": retry_result,
        }
        completed = True
        return result
    finally:
        if child.poll() is None:
            pidfd = os.pidfd_open(child.pid, 0)
            try:
                signal.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
            finally:
                os.close(pidfd)
        if not child_reaped:
            bounded_wait_child(child, 5)
            child_reaped = True
        if not pipes_captured:
            cleanup_stdout, cleanup_stderr, cleanup_capture_error = capture_child_pipes(child)
            try:
                controller.exclusive_json(control_root, "finish-child-cleanup.json", {
                    "schema": 1, "scenario": args.finish_crash, "pid": child.pid,
                    "reaped": 1 if child_reaped else 0, "returncode": child.poll(),
                    "completed": 1 if completed else 0,
                    "stdout": cleanup_stdout.decode("utf-8", "replace"),
                    "stderr": cleanup_stderr.decode("utf-8", "replace"),
                    "capture_error": cleanup_capture_error,
                })
            except Exception:
                # Never replace the original qualification failure with evidence I/O.
                pass


def qualify(args):
    control_root = fresh_control_root(args.control_root)
    controller_path, controller_sha = copy_controller(control_root, args.controller)
    controller, loaded_sha = load_exact(
        controller_path, "slt_crash_controller_snapshot", controller_sha
    )
    if loaded_sha != controller_sha:
        raise RuntimeError("captured controller bytes changed before supervisor load")
    supervisor_run_id = uuid.uuid4().hex
    command = [
        "/usr/bin/python3", "-I", "-B", str(controller_path),
        "--runner-root", args.runner_root, "--ledger-root", args.ledger_root,
        "--code-root", args.code_root, "--control-root", str(control_root),
        "--supervisor-run-id", supervisor_run_id,
        "--integrated-ledger-script", args.integrated_ledger_script,
        "--adapter", args.adapter, "--module", args.module,
        "--scenario", args.scenario,
        "--ack", "DISPOSABLE-INTEGRATED-SYSTEMD-LAB", "qualify",
    ]
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    child_reaped = False
    child_stdout = b""
    child_stderr = b""
    child_capture_error = None
    owner_identity = {
        "runner_root": str(pathlib.Path(args.runner_root)),
        "control_root": str(control_root), "controller_pid": child.pid,
        "controller_argv": command, "supervisor_run_id": supervisor_run_id,
        "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }
    evidence = {"schema": 1, "scenario": args.scenario,
                "controller_command": command, "controller_sha256": controller_sha,
                "controller_pid": child.pid}
    ready = None
    try:
        expectation = SCENARIOS[args.scenario]
        ready_path = control_root / "crash-ready.json"
        ready, ready_sha = controller.wait_for(
            lambda: controller.read_canonical_json(ready_path) if ready_path.exists() else None,
            20, "controller crash-ready descriptor",
        )
        controller.wait_for(
            lambda: True if controller.proc_state(child.pid) in ("T", "t") else None,
            5, "exact stopped controller after crash-ready publication",
        )
        pidfd = os.pidfd_open(child.pid, 0)
        try:
            if ready.get("scenario") != args.scenario \
                    or ready.get("boundary") != expectation["boundary"] \
                    or ready.get("controller_pid") != child.pid \
                    or ready.get("controller_argv") != command \
                    or controller.proc_argv(child.pid) != command \
                    or ready.get("controller_start_ticks") != controller.proc_start_ticks(child.pid) \
                    or ready.get("boot_id") != pathlib.Path(
                        "/proc/sys/kernel/random/boot_id").read_text().strip() \
                    or controller.proc_state(child.pid) not in ("T", "t"):
                raise RuntimeError("controller crash-ready identity is not exact")
            signal.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
        finally:
            os.close(pidfd)
        wait_status = bounded_wait_child(child, 5)
        child_reaped = True
        child_stdout, child_stderr, child_capture_error = capture_child_pipes(child)
        if wait_status != -signal.SIGKILL:
            raise RuntimeError("controller was not terminated by the injected SIGKILL")
        code_root = pathlib.Path(args.code_root)
        manifest = {
            "adapter": file_sha(code_root / "admission-adapter.pl"),
            "model": file_sha(code_root / "SharedLvmAdmission.pm"),
            "ledger": file_sha(code_root / "executor-integrated-admission-lab.py"),
            "runner": file_sha(code_root / "executor-integrated-systemd-lab.py"),
        }
        if manifest != ready.get("code_manifest") \
                or controller.digest(manifest) != ready.get("code_digest"):
            raise RuntimeError("code snapshot differs from durable crash checkpoint")
        ledger_module_path = code_root / "executor-integrated-admission-lab.py"
        ledger_module, ledger_helper_sha = load_exact(
            ledger_module_path, f"slt_crash_ledger_{args.scenario.lower()}",
            manifest["ledger"],
        )
        model = ledger_module.Model(
            code_root / "admission-adapter.pl", code_root / "SharedLvmAdmission.pm"
        )
        ledger = ledger_module.Ledger(args.ledger_root, model)
        try:
            before, before_sha = controller.read_canonical_json(
                pathlib.Path(args.ledger_root) / "ledger.json"
            )
            if before["revision"] != expectation["revision"] \
                    or before["revision"] != ready["ledger_revision"] \
                    or before_sha != ready["ledger_sha256"]:
                raise RuntimeError("reopened ledger differs from crash-ready authority")
            if len(before["slots"]) != 1:
                raise RuntimeError("crash ledger does not contain exactly one authority slot")
            slot_key, slot = next(iter(before["slots"].items()))
            record = slot["record"]
            if record["state"] != expectation["state"] \
                    or record["object_key"] != slot_key \
                    or record["attempt"] != ready.get("attempt") \
                    or record["transaction"] != ready.get("transaction") \
                    or record["unit"] != ready.get("unit") \
                    or record["state"] != ready.get("record_state") \
                    or record["code_digest"] != ready.get("code_digest") \
                    or (slot["launch_issued"] is not None) != expectation["launch"] \
                    or (slot["dispatch_issued"] is not None) != expectation["dispatch"]:
                raise RuntimeError("reopened ledger state differs from crash boundary")
            contender_attempt = uuid.uuid4().hex
            contender = {**record, "attempt": contender_attempt, "state": "RESERVED"}
            if contender["transaction"] != record["transaction"]:
                raise RuntimeError("contender is not the same transaction")
            contender.pop("invocation_id", None)
            contender.pop("executor_result", None)
            contender["unit"] = f"{UNIT_PREFIX}{contender_attempt}.service"
            refusal, observed = ledger_module.Protocol(ledger, model).reserve(contender)
            after, after_sha = controller.read_canonical_json(
                pathlib.Path(args.ledger_root) / "ledger.json"
            )
            if refusal.get("allowed") != 0 or before != after or before_sha != after_sha \
                    or observed["revision"] != before["revision"]:
                raise RuntimeError("post-crash competing attempt changed the ledger")
        finally:
            ledger.close()

        runner_root = pathlib.Path(args.runner_root)
        artifacts = exact_artifacts(controller, runner_root, ready)
        owner_path = control_root / "launch-owner.json"
        current_owner = None
        if owner_path.exists():
            owner_value, owner_sha = controller.read_canonical_json(owner_path)
            current_owner = {"sha256": owner_sha, "value": owner_value}
        if current_owner != ready.get("launch_owner") \
                or (current_owner is not None) != expectation["unit"]:
            raise RuntimeError("current-run launch owner differs from crash checkpoint")
        grant_present = (runner_root / "grant.json").exists()
        marker_present = (runner_root / "dispatch-marker.json").exists()
        if grant_present != expectation["grant"] or marker_present != expectation["marker"]:
            raise RuntimeError("runner artifacts differ from crash boundary")
        runtime = {"unit_expected": expectation["unit"], "grant_present": grant_present,
                   "marker_present": marker_present}
        cleanup = None
        if not expectation["unit"]:
            absent = subprocess.run(
                ["/usr/bin/systemctl", "show", ready["unit"],
                 "--property=LoadState", "--value"],
                check=False, capture_output=True, text=True, timeout=5,
            )
            if absent.returncode != 0 or absent.stdout.strip() != "not-found":
                raise RuntimeError("unexpected unit exists after pre-submit controller crash")
            runtime["unit_absent"] = True
        else:
            startup, _ = controller.read_canonical_json(runner_root / "startup.json")
            if artifacts.get("startup.json", {}).get("value") != startup:
                raise RuntimeError("startup is not bound to crash checkpoint")
            manager = controller.show(ready["unit"])
            typed, _ = controller.typed_exec_start(ready["unit"])
            if manager.get("InvocationID") != startup["invocation_id"] \
                    or manager.get("MainPID") != str(startup["pid"]) \
                    or manager.get("ControlGroup") != startup["control_group"] \
                    or controller.proc_start_ticks(startup["pid"]) != startup["start_ticks"] \
                    or controller.proc_state(startup["pid"]) not in ("T", "t") \
                    or controller.digest(typed) != startup["command_sha256"]:
                raise RuntimeError("surviving runner identity is not exact and stopped")
            runtime.update({"startup": startup, "stopped_state": controller.proc_state(startup["pid"])})
            phase_name = f"phase-{args.scenario}.json"
            phase, _ = controller.read_canonical_json(runner_root / phase_name)
            expected_phase = "MARKER_PERSISTED_PRE_EXIT" if args.scenario == "C7" \
                else "STARTUP_REPORTED_HELD"
            if phase != {
                    "schema": 1, "scenario": args.scenario, "phase": expected_phase,
                    "attempt": record["attempt"], "invocation_id": startup["invocation_id"],
                    "pid": startup["pid"], "start_ticks": startup["start_ticks"]}:
                raise RuntimeError("surviving runner phase identity is not exact")
            runtime["phase"] = phase
            if args.scenario in ("C6", "C7"):
                stored = slot["dispatch_issued"]
                grant, grant_sha = controller.read_canonical_json(runner_root / "grant.json")
                if grant != stored["grant"] or grant_sha != stored["grant_sha256"]:
                    raise RuntimeError("published grant differs from persisted dispatch")
                marker_expected = expected_marker(record, startup, stored, artifacts)
                if args.scenario == "C7" and artifacts["dispatch-marker.json"]["value"] != marker_expected:
                    raise RuntimeError("pre-resume marker differs from persisted dispatch")
                pidfd = os.pidfd_open(startup["pid"], 0)
                try:
                    current = controller.show(ready["unit"])
                    if current.get("InvocationID") != startup["invocation_id"] \
                            or current.get("MainPID") != str(startup["pid"]) \
                            or controller.proc_start_ticks(startup["pid"]) != startup["start_ticks"]:
                        raise RuntimeError("runner identity changed before pidfd resume")
                    signal.pidfd_send_signal(pidfd, signal.SIGCONT, None, 0)
                finally:
                    os.close(pidfd)
                marker, marker_sha = controller.wait_for(
                    lambda: controller.read_canonical_json(runner_root / "dispatch-marker.json")
                    if (runner_root / "dispatch-marker.json").exists() else None,
                    10, "authorized runner marker after controller crash",
                )
                if marker != marker_expected:
                    raise RuntimeError("post-resume marker differs from persisted dispatch")
                terminal, cgroup_terminal = terminal_proof(controller, ready["unit"], startup)
                runtime.update({"marker": marker, "marker_sha256": marker_sha,
                                "terminal": terminal, "cgroup_terminal": cgroup_terminal})
            # Exact lab-only cleanup after either refusal-only or authorized completion.
            cleanup = cleanup_exact_unit(
                controller, ready["unit"], runner_root, owner_path, owner_identity
            )
            if not cleanup.get("attempted"):
                raise RuntimeError("supervisor could not prove exact unit ownership for cleanup")
        finish = None
        if args.persist_finish or args.finish_crash:
            proof = build_c6_terminal_proof(
                controller, command, child.pid, ready, before, record, slot, runtime, cleanup
            )
            ledger_module.validate_terminal_proof(proof)
            proof_sha = controller.digest(proof)
            if args.finish_crash:
                crash_finish = qualify_finish_crash(
                    args, controller, control_root, proof, proof_sha, before, before_sha,
                    record, slot, ledger_module, model, manifest, ready["code_digest"],
                )
                finish = {"proof": proof, "proof_sha256": proof_sha,
                          "crash": crash_finish,
                          "ledger_revision": crash_finish["ledger_revision"],
                          "ledger_sha256": crash_finish["ledger_sha256"]}
            else:
                finish_ledger = ledger_module.Ledger(args.ledger_root, model)
                try:
                    finish_decision, finish_state = ledger_module.Protocol(
                        finish_ledger, model
                    ).finish_exact(record, proof)
                    retry_attempt = uuid.uuid4().hex
                    retry = {**record, "attempt": retry_attempt, "state": "RESERVED"}
                    retry.pop("invocation_id", None)
                    retry["unit"] = f"{UNIT_PREFIX}{retry_attempt}.service"
                    retry_result, retry_state = ledger_module.Protocol(
                        finish_ledger, model
                    ).reserve(retry)
                finally:
                    finish_ledger.close()
                if finish_decision.get("action") != "MARK_TERMINAL" \
                        or finish_state["revision"] != before["revision"] + 1 \
                        or retry_result.get("allowed") != 0 or retry_state != finish_state:
                    raise RuntimeError("exact finish persistence or terminal retry refusal failed")
                finish = {
                    "proof": proof, "proof_sha256": proof_sha,
                    "decision": finish_decision, "ledger_revision": finish_state["revision"],
                    "ledger_sha256": controller.digest(finish_state),
                    "post_terminal_retry": retry_result,
                }
        if args.finish_crash:
            final_classification = f"INERT_FINISH_{args.finish_crash}_CRASH_PASS"
        elif finish is not None:
            final_classification = "INTEGRATED_INERT_CONTROLLER_CRASH_FINISH_PASS"
        else:
            final_classification = "INTEGRATED_INERT_CONTROLLER_CRASH_REFUSAL_PASS"
        final_revision = finish["ledger_revision"] if finish else before["revision"]
        final_ledger_sha = finish["ledger_sha256"] if finish else before_sha
        result = {
            **evidence, "classification": final_classification,
            "crash_ready_sha256": ready_sha, "crash_ready": ready,
            "controller_wait_status": wait_status,
            "controller_stdout": child_stdout.decode("utf-8", "replace"),
            "controller_stderr": child_stderr.decode("utf-8", "replace"),
            "controller_capture_error": child_capture_error,
            "ledger_revision": final_revision, "ledger_sha256": final_ledger_sha,
            "record_state": finish["crash"]["record_state"] if args.finish_crash \
                else ("TERMINAL" if finish is not None else slot["record"]["state"]),
            "pre_finish": {
                "ledger_revision": before["revision"], "ledger_sha256": before_sha,
                "record_state": slot["record"]["state"],
            } if finish is not None else None,
            "contender_attempt": contender_attempt, "contender_refusal": refusal,
            "transaction_relationship": "SAME_TRANSACTION_NEW_ATTEMPT",
            "runtime": runtime, "cleanup": cleanup, "finish": finish,
            "ledger_helper_sha256": ledger_helper_sha,
            "limitations": ["post-persistence process crash only", "no takeover or replay",
                            "no power loss, reboot, cross-node or storage I/O"],
        }
        controller.exclusive_json(control_root, "supervisor-evidence.json", result)
        print(json.dumps({
            "classification": result["classification"],
            "scenario": args.scenario,
            "ledger_revision": result["ledger_revision"],
            "ledger_sha256": result["ledger_sha256"],
            "evidence_sha256": file_sha(control_root / "supervisor-evidence.json"),
        }, sort_keys=True))
    except Exception as error:
        try:
            if child.poll() is None:
                pidfd = os.pidfd_open(child.pid, 0)
                try:
                    signal.pidfd_send_signal(pidfd, signal.SIGKILL, None, 0)
                finally:
                    os.close(pidfd)
            if not child_reaped:
                bounded_wait_child(child, 5)
                child_reaped = True
            child_stdout, child_stderr, child_capture_error = capture_child_pipes(child)
            failure_cleanup = None
            cleanup_unit = ready.get("unit") if ready is not None else None
            owner_path = control_root / "launch-owner.json"
            if owner_path.exists():
                owner_for_cleanup, _ = controller.read_canonical_json(owner_path)
                if cleanup_unit is None:
                    cleanup_unit = owner_for_cleanup.get("unit")
            if cleanup_unit:
                try:
                    failure_cleanup = cleanup_exact_unit(
                        controller, cleanup_unit, pathlib.Path(args.runner_root),
                        owner_path, owner_identity,
                    )
                except Exception as cleanup_error:
                    failure_cleanup = {"attempted": True, "error": str(cleanup_error)}
            controller.exclusive_json(control_root, "supervisor-failure.json", {
                "error": str(error), "scenario": args.scenario,
                "controller_pid": child.pid, "controller_poll": child.poll(),
                "controller_reaped": child_reaped,
                "controller_stdout": child_stdout.decode("utf-8", "replace"),
                "controller_stderr": child_stderr.decode("utf-8", "replace"),
                "controller_capture_error": child_capture_error,
                "failure_cleanup": failure_cleanup,
            })
        except Exception:
            pass
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", required=True, choices=tuple(SCENARIOS))
    parser.add_argument("--control-root", required=True)
    parser.add_argument("--runner-root", required=True)
    parser.add_argument("--ledger-root", required=True)
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--controller", required=True)
    parser.add_argument("--integrated-ledger-script", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("--ack", required=True)
    parser.add_argument("--persist-finish", action="store_true")
    parser.add_argument("--finish-crash", choices=("F1", "F2"))
    parser.add_argument("--finish-child", choices=("F1", "F2"))
    parser.add_argument("--finish-proof-sha256")
    parser.add_argument("--expected-ledger-sha256")
    parser.add_argument("--supervisor-run-id")
    parser.add_argument("--expected-code-digest")
    parser.add_argument("--expected-adapter-sha256")
    parser.add_argument("--expected-model-sha256")
    parser.add_argument("--expected-ledger-helper-sha256")
    parser.add_argument("--expected-runner-sha256")
    args = parser.parse_args()
    if args.ack != ACK or os.geteuid() != 0:
        raise SystemExit("supervisor requires root and explicit disposable acknowledgement")
    if args.finish_child:
        if not HEX64.fullmatch(args.finish_proof_sha256 or "") \
                or not HEX64.fullmatch(args.expected_ledger_sha256 or "") \
                or any(not HEX64.fullmatch(value or "") for value in (
                    args.expected_code_digest, args.expected_adapter_sha256,
                    args.expected_model_sha256, args.expected_ledger_helper_sha256,
                    args.expected_runner_sha256,
                )) \
                or not re.fullmatch(r"[a-f0-9]{32}", args.supervisor_run_id or ""):
            raise SystemExit("finish child requires exact pinned identities")
        finish_child(args)
        return
    if args.persist_finish and args.scenario != "C6":
        raise SystemExit("finish qualification is restricted to exact C6")
    if args.finish_crash and (args.scenario != "C6" or args.persist_finish):
        raise SystemExit("finish crash qualification requires exact C6 without direct finish")
    qualify(args)


if __name__ == "__main__":
    main()
