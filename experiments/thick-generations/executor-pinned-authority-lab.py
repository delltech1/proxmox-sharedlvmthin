#!/usr/bin/python3
"""Disposable single-authority/two-client admission qualification harness.

This file has no storage, systemd, PVE configuration, network fallback, close,
TTL or takeover surface.  One-shot enrolled request handlers serialize client
attempts through one ledger.  The qualifier is an explicit initialization and
read-only evidence observer; normal request handling opens no fallback ledger.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import select
import signal
import stat
import subprocess
import sys
import time
import types
import uuid


ACK = "DISPOSABLE-PINNED-AUTHORITY-TWO-CLIENT-LAB"
PARENT = pathlib.Path("/var/tmp")
AUTHORITY_PREFIX = "slt-integrated-executor-lab-pinned-authority-"
CONTROL_PREFIX = "slt-pinned-authority-control-lab-"
CODE_PREFIX = "slt-pinned-authority-code-lab-"
HEX24 = re.compile(r"^[a-f0-9]{24}$")
HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
BOOT_ID = re.compile(r"^[a-f0-9-]{36}$")
VG_UUID = "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF"
VOLUME = "vm-910001-disk-0"
PINNED_BOOTSTRAP = (
    "import hashlib,os,sys;"
    "p=sys.argv[1];e=sys.argv[2];"
    "f=os.open(p,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC);"
    "b=b'';"
    "\nwhile True:\n c=os.read(f,1048576)\n if not c: break\n b+=c\n"
    "os.close(f);"
    "\nif hashlib.sha256(b).hexdigest()!=e: raise SystemExit('helper digest mismatch')\n"
    "sys.argv=[p]+sys.argv[3:];"
    "g={'__name__':'__main__','__file__':p,'__package__':''};"
    "exec(compile(b,p,'exec',dont_inherit=True),g)"
)


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def read_canonical(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0:
            raise RuntimeError("pinned artifact is not a root-owned regular file")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    value = json.loads(raw, object_pairs_hook=duplicate_keys)
    if raw != canonical(value):
        raise RuntimeError("pinned artifact is not canonical JSON")
    return value, hashlib.sha256(raw).hexdigest()


def read_pinned_canonical(path, expected_sha, label):
    value, actual_sha = read_canonical(path)
    if actual_sha != expected_sha:
        raise RuntimeError(f"{label} bytes differ from argv-pinned digest")
    return value


def read_stdin_canonical(limit=1024 * 1024):
    raw = sys.stdin.buffer.read(limit + 1)
    if len(raw) > limit:
        raise RuntimeError("request exceeds bounded input size")
    value = json.loads(raw, object_pairs_hook=duplicate_keys)
    if raw != canonical(value):
        raise RuntimeError("request is not canonical JSON")
    return value


def file_sha(path):
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
    return hashlib.sha256(b"".join(chunks)).hexdigest()


def load_exact(path, name, expected_sha):
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
    if hashlib.sha256(payload).hexdigest() != expected_sha:
        raise RuntimeError("authority helper bytes differ from enrollment")
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    exec(compile(payload, str(path), "exec", dont_inherit=True), module.__dict__)
    return module


def validate_direct_root(path, prefix, must_exist=False):
    root = pathlib.Path(path)
    if not root.is_absolute() or root.parent != PARENT or not root.name.startswith(prefix) \
            or root != pathlib.Path(os.path.abspath(root)):
        raise RuntimeError("lab root is outside its disposable namespace")
    if root.exists() or must_exist:
        info = os.lstat(root)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) \
                or info.st_uid != 0:
            raise RuntimeError("lab root is not an exact root-owned directory")
    return root


def exclusive_json(root, name, value, mode=0o400):
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".{name}.tmp.{uuid.uuid4().hex}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=root_fd)
        try:
            payload = canonical(value)
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.link(temporary, name, src_dir_fd=root_fd, dst_dir_fd=root_fd,
                follow_symlinks=False)
        os.unlink(temporary, dir_fd=root_fd)
        os.fsync(root_fd)
    finally:
        os.close(root_fd)


def copy_snapshot(code_root, sources):
    root = validate_direct_root(code_root, CODE_PREFIX)
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    manifest = {}
    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        for name, source in sources.items():
            source = pathlib.Path(source).resolve(strict=True)
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                info = os.fstat(source_fd)
                if not stat.S_ISREG(info.st_mode):
                    raise RuntimeError("snapshot source is not a regular file")
                payload = b""
                while True:
                    chunk = os.read(source_fd, 1024 * 1024)
                    if not chunk:
                        break
                    payload += chunk
            finally:
                os.close(source_fd)
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         os.O_NOFOLLOW | os.O_CLOEXEC, 0o400, dir_fd=root_fd)
            try:
                offset = 0
                while offset < len(payload):
                    offset += os.write(fd, payload[offset:])
                os.fsync(fd)
            finally:
                os.close(fd)
            manifest[name] = hashlib.sha256(payload).hexdigest()
        os.fsync(root_fd)
    finally:
        os.close(root_fd)
    os.chmod(root, 0o500)
    return manifest


def validate_descriptor(value):
    fields = {"schema", "kind", "authority_id", "authority_node",
              "authority_boot_id", "enrollment_epoch", "ledger_root",
              "code_root", "manifest", "code_digest", "scope"}
    if not isinstance(value, dict) or set(value) != fields \
            or type(value.get("schema")) is not int or value["schema"] != 1 \
            or value.get("kind") != "PINNED_AUTHORITY_TWO_CLIENT_LAB" \
            or not HEX32.fullmatch(value.get("authority_id", "")) \
            or not BOOT_ID.fullmatch(value.get("authority_boot_id", "")) \
            or not HEX64.fullmatch(value.get("enrollment_epoch", "")) \
            or not HEX64.fullmatch(value.get("code_digest", "")):
        raise RuntimeError("enrollment descriptor schema is invalid")
    manifest = value["manifest"]
    names = {"authority.py", "ledger.py", "adapter.pl", "model.pm"}
    if not isinstance(manifest, dict) or set(manifest) != names \
            or any(not HEX64.fullmatch(item or "") for item in manifest.values()) \
            or digest(manifest) != value["code_digest"]:
        raise RuntimeError("enrollment code manifest is invalid")
    scope = value["scope"]
    if not isinstance(scope, dict) or set(scope) != {"vg_uuid", "volume", "object_key"}:
        raise RuntimeError("enrollment object scope is invalid")
    validate_direct_root(value["ledger_root"], AUTHORITY_PREFIX, must_exist=True)
    validate_direct_root(value["code_root"], CODE_PREFIX, must_exist=True)
    return value


def validate_request(value, descriptor, ledger_module):
    fields = {"schema", "kind", "request_id", "descriptor_sha256", "operation",
              "vg_uuid", "volume", "object_key", "transaction", "attempt",
              "unit", "code_digest", "policy_digest"}
    if not isinstance(value, dict) or set(value) != fields \
            or type(value.get("schema")) is not int or value["schema"] != 1 \
            or value.get("kind") != "PINNED_AUTHORITY_REQUEST" \
            or value.get("operation") != "RESERVE" \
            or not HEX32.fullmatch(value.get("request_id", "")) \
            or not HEX32.fullmatch(value.get("transaction", "")) \
            or not HEX32.fullmatch(value.get("attempt", "")) \
            or not HEX64.fullmatch(value.get("code_digest", "")) \
            or not HEX64.fullmatch(value.get("policy_digest", "")):
        raise RuntimeError("authority request schema is invalid")
    scope = descriptor["scope"]
    if any(value.get(key) != scope[key] for key in ("vg_uuid", "volume", "object_key")) \
            or value["object_key"] != ledger_module.volume_key(value["vg_uuid"], value["volume"]):
        raise RuntimeError("authority request is outside enrolled canonical scope")
    if value["code_digest"] != descriptor["code_digest"]:
        raise RuntimeError("authority request code digest differs from enrollment")
    return value


def record_from_request(request, descriptor):
    return {
        "schema": 1, "kind": "THICK_EXECUTOR_LAB",
        "enrollment_epoch": descriptor["enrollment_epoch"],
        "vg_uuid": request["vg_uuid"], "volume": request["volume"],
        "object_key": request["object_key"], "transaction": request["transaction"],
        "attempt": request["attempt"], "node": descriptor["authority_node"],
        "boot_id": descriptor["authority_boot_id"], "unit": request["unit"],
        "code_digest": request["code_digest"],
        "policy_digest": request["policy_digest"], "state": "RESERVED",
    }


def validate_response(value, request, descriptor, ledger_module):
    fields = {"schema", "kind", "descriptor_sha256", "request_id", "operation",
              "result", "allowed", "transaction_before_sha256", "ledger_revision",
              "ledger_sha256", "record"}
    if not isinstance(value, dict) or set(value) != fields \
            or type(value.get("schema")) is not int or value["schema"] != 1 \
            or value.get("kind") != "PINNED_AUTHORITY_RESPONSE" \
            or value.get("descriptor_sha256") != request["descriptor_sha256"] \
            or value.get("request_id") != request["request_id"] \
            or value.get("operation") != request["operation"] \
            or type(value.get("allowed")) is not int or value["allowed"] not in (0, 1) \
            or type(value.get("ledger_revision")) is not int \
            or value["ledger_revision"] < 1 \
            or not HEX64.fullmatch(value.get("transaction_before_sha256", "")) \
            or not HEX64.fullmatch(value.get("ledger_sha256", "")):
        raise RuntimeError("authority response schema or identity is invalid")
    if request["operation"] == "RESERVE":
        if value["record"] != record_from_request(request, descriptor) \
                or (value["allowed"] == 1 and value["result"] != "RESERVE_ATOMIC") \
                or (value["allowed"] == 0 and value["result"] != "BLOCKED"):
            raise RuntimeError("authority reserve response is not request-bound")
    return value


def validate_l1_fault_plan(plan, descriptor_sha, request_sha):
    fields = {"schema", "kind", "scenario", "boundary", "run_id",
              "descriptor_sha256", "request_sha256", "code_digest",
              "control_root", "checkpoint_name"}
    if not isinstance(plan, dict) or set(plan) != fields \
            or type(plan.get("schema")) is not int or plan["schema"] != 1 \
            or plan.get("kind") != "PINNED_AUTHORITY_FAULT_PLAN" \
            or plan.get("scenario") != "L1" \
            or plan.get("boundary") != "POST_DURABLE_RESERVE_PRE_RESPONSE" \
            or not HEX32.fullmatch(plan.get("run_id", "")) \
            or plan.get("descriptor_sha256") != descriptor_sha \
            or plan.get("request_sha256") != request_sha \
            or not HEX64.fullmatch(plan.get("code_digest", "")) \
            or plan.get("checkpoint_name") != "l1-checkpoint.json":
        raise RuntimeError("L1 fault plan schema or identity is invalid")
    validate_direct_root(plan["control_root"], CONTROL_PREFIX, must_exist=True)
    return plan


def inert_reserved_slot(slot, expected_record, schema):
    fields = {"record", "launch_issued", "startup_observed",
              "dispatch_issued", "recovery_hold"}
    if schema == 2:
        fields.add("finish_observed")
    return isinstance(slot, dict) and set(slot) == fields \
        and slot["record"] == expected_record \
        and all(slot[name] is None for name in fields - {"record"})


def publish_l1_checkpoint(plan, descriptor, request, response, ledger, ledger_module):
    if response.get("allowed") != 1 or response.get("result") != "RESERVE_ATOMIC" \
            or response.get("ledger_revision") != 2 or ledger.store_calls != 1 \
            or response.get("record") != record_from_request(request, descriptor):
        raise RuntimeError("L1 fault boundary requires one exact successful durable reserve")
    ledger._lock()
    try:
        observed, observed_sha = exact_ledger_bytes(ledger, ledger_module)
    finally:
        fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)
    slot = observed["slots"].get(request["object_key"])
    if observed["revision"] != 2 or observed_sha != response["ledger_sha256"] \
            or len(observed["slots"]) != 1 \
            or observed["consumed_attempts"] != [request["attempt"]] \
            or not inert_reserved_slot(slot, response["record"], observed["schema"]):
        raise RuntimeError("L1 durable reserve read-back is not exact")
    pid = os.getpid()
    checkpoint = {
        "schema": 1, "scenario": "L1", "boundary": plan["boundary"],
        "run_id": plan["run_id"], "descriptor_sha256": plan["descriptor_sha256"],
        "request_sha256": plan["request_sha256"], "code_digest": plan["code_digest"],
        "object_key": request["object_key"], "transaction": request["transaction"],
        "attempt": request["attempt"], "pid": pid,
        "start_ticks": proc_start_ticks(pid), "argv": proc_argv(pid),
        "pgid": os.getpgrp(),
        "boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "pre_revision": 1, "post_revision": 2,
        "ledger_sha256": observed_sha, "response_sha256": digest(response),
    }
    exclusive_json(pathlib.Path(plan["control_root"]), plan["checkpoint_name"], checkpoint)
    os.kill(pid, signal.SIGSTOP)
    raise RuntimeError("L1 helper resumed unexpectedly before response")


def validate_l1_checkpoint(checkpoint, expected, child_pid, command):
    fields = {"schema", "scenario", "boundary", "run_id",
              "descriptor_sha256", "request_sha256", "code_digest",
              "object_key", "transaction", "attempt", "pid",
              "start_ticks", "argv", "pgid", "boot_id",
              "pre_revision", "post_revision", "ledger_sha256",
              "response_sha256"}
    exact = {key: expected[key] for key in (
        "boundary", "run_id", "descriptor_sha256", "request_sha256",
        "code_digest", "object_key", "transaction", "attempt", "boot_id",
        "ledger_sha256", "response_sha256",
    )}
    if not isinstance(checkpoint, dict) or set(checkpoint) != fields \
            or type(checkpoint.get("schema")) is not int or checkpoint["schema"] != 1 \
            or checkpoint.get("scenario") != "L1" \
            or any(checkpoint.get(key) != value for key, value in exact.items()) \
            or any(type(checkpoint.get(key)) is not int or checkpoint[key] <= 0
                   for key in ("pid", "start_ticks", "pgid")) \
            or checkpoint["pid"] != child_pid \
            or checkpoint["start_ticks"] != proc_start_ticks(child_pid) \
            or checkpoint.get("argv") != command \
            or proc_argv(child_pid) != command \
            or checkpoint["pgid"] != os.getpgid(child_pid) \
            or checkpoint["pgid"] != child_pid \
            or type(checkpoint.get("pre_revision")) is not int \
            or checkpoint["pre_revision"] != expected["pre_revision"] \
            or type(checkpoint.get("post_revision")) is not int \
            or checkpoint["post_revision"] != expected["post_revision"] \
            or not HEX64.fullmatch(checkpoint.get("ledger_sha256", "")) \
            or not HEX64.fullmatch(checkpoint.get("response_sha256", "")):
        raise RuntimeError("L1 checkpoint identity is not exact")
    return checkpoint


def exact_ledger_bytes(ledger, ledger_module):
    fd = os.open("ledger.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                 dir_fd=ledger.root_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 \
                or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("authority ledger ownership/mode is invalid")
        raw = b""
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            raw += chunk
    finally:
        os.close(fd)
    value = json.loads(raw, object_pairs_hook=ledger_module.duplicate_keys)
    if raw != ledger_module.canonical(value):
        raise RuntimeError("authority ledger is not canonical JSON")
    return ledger._validate(value), hashlib.sha256(raw).hexdigest()


def enrolled_ledger_class(ledger_module, descriptor):
    expected_identity = {key: descriptor[key] for key in (
        "authority_id", "authority_node", "authority_boot_id", "enrollment_epoch"
    )}

    class EnrolledLedger(ledger_module.Ledger):
        def __init__(self, *values, **keywords):
            super().__init__(*values, **keywords)
            self.transaction_before_sha256 = None
            self.store_calls = 0

        def _load(self):
            value, exact_sha = exact_ledger_bytes(self, ledger_module)
            actual = {key: value[key] for key in expected_identity}
            current_boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            if actual != expected_identity or os.uname().nodename != descriptor["authority_node"] \
                    or current_boot != descriptor["authority_boot_id"]:
                raise RuntimeError("live authority continuity differs from enrollment")
            identity = actual
            if self.pinned_authority is None:
                self.pinned_authority = identity
            elif self.pinned_authority != identity:
                raise RuntimeError("authority changed within enrolled request")
            self.transaction_before_sha256 = exact_sha
            return value

        def _store(self, data):
            self.store_calls += 1
            return super()._store(data)

    return EnrolledLedger


def enrolled_runtime(args):
    descriptor, descriptor_sha = read_canonical(args.descriptor)
    validate_descriptor(descriptor)
    if descriptor_sha != args.descriptor_sha256:
        raise RuntimeError("enrollment descriptor digest mismatch")
    code_root = validate_direct_root(descriptor["code_root"], CODE_PREFIX, must_exist=True)
    paths = {
        "authority.py": code_root / "authority.py",
        "ledger.py": code_root / "ledger.py",
        "adapter.pl": code_root / "adapter.pl",
        "model.pm": code_root / "model.pm",
    }
    if {name: file_sha(path) for name, path in paths.items()} != descriptor["manifest"]:
        raise RuntimeError("enrollment code snapshot changed")
    if pathlib.Path(__file__) != paths["authority.py"] \
            or file_sha(pathlib.Path(__file__)) != descriptor["manifest"]["authority.py"]:
        raise RuntimeError("executing helper is not the enrolled authority helper")
    ledger_module = load_exact(paths["ledger.py"], "slt_pinned_authority_ledger",
                               descriptor["manifest"]["ledger.py"])
    model = ledger_module.Model(paths["adapter.pl"], paths["model.pm"])
    if model.module_sha256 != descriptor["manifest"]["model.pm"]:
        raise RuntimeError("enrollment model changed")
    return descriptor, descriptor_sha, ledger_module, model


def authority_request(args):
    descriptor, descriptor_sha, ledger_module, model = enrolled_runtime(args)
    request = read_pinned_canonical(args.request, args.request_sha256, "request")
    request = validate_request(request, descriptor, ledger_module)
    if request["descriptor_sha256"] != descriptor_sha:
        raise RuntimeError("request descriptor binding is invalid")
    fault_plan = None
    if args.fault_plan is not None or args.fault_plan_sha256 is not None:
        if args.fault_plan is None or not HEX64.fullmatch(args.fault_plan_sha256 or ""):
            raise RuntimeError("L1 fault plan requires pinned bytes")
        fault_plan = validate_l1_fault_plan(
            read_pinned_canonical(args.fault_plan, args.fault_plan_sha256, "fault plan"),
            descriptor_sha, args.request_sha256,
        )
        if fault_plan["code_digest"] != descriptor["code_digest"]:
            raise RuntimeError("L1 fault plan code identity differs from enrollment")
    if args.ready_fd is not None or args.start_fd is not None:
        if args.ready_fd is None or args.start_fd is None:
            raise RuntimeError("authority barrier is incomplete")
        os.write(args.ready_fd, b"R")
        os.close(args.ready_fd)
        if os.read(args.start_fd, 1) != b"G":
            raise RuntimeError("authority start barrier was not released exactly")
        os.close(args.start_fd)
    LedgerClass = enrolled_ledger_class(ledger_module, descriptor)
    ledger = LedgerClass(descriptor["ledger_root"], model)
    try:
        record = record_from_request(request, descriptor)
        decision, after = ledger_module.Protocol(ledger, model).reserve(record)
        transaction_before_sha = ledger.transaction_before_sha256
        ledger._lock()
        try:
            exact_after, after_sha = exact_ledger_bytes(ledger, ledger_module)
        finally:
            fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)
        if after != exact_after:
            raise RuntimeError("authority response state differs from exact ledger bytes")
        response = {"schema": 1, "kind": "PINNED_AUTHORITY_RESPONSE",
                    "descriptor_sha256": descriptor_sha,
                    "request_id": request["request_id"], "operation": "RESERVE",
                    "result": decision.get("action", "BLOCKED"),
                    "allowed": decision.get("allowed", 0),
                    "transaction_before_sha256": transaction_before_sha,
                    "ledger_revision": after["revision"],
                    "ledger_sha256": after_sha, "record": record}
        validate_response(response, request, descriptor, ledger_module)
        if fault_plan is not None:
            publish_l1_checkpoint(
                fault_plan, descriptor, request, response, ledger, ledger_module
            )
        sys.stdout.buffer.write(canonical(response))
        sys.stdout.buffer.flush()
    finally:
        ledger.close()


def helper_command(authority, authority_sha, descriptor_path, descriptor_sha,
                   request_path, request_sha, ready_fd=None, start_fd=None,
                   fault_plan_path=None, fault_plan_sha=None):
    command = ["/usr/bin/python3", "-I", "-B", "-c", PINNED_BOOTSTRAP,
               str(authority), authority_sha, "--ack", ACK,
               "--descriptor", str(descriptor_path),
               "--descriptor-sha256", descriptor_sha,
               "--request", str(request_path), "--request-sha256", request_sha]
    if ready_fd is not None and start_fd is not None:
        command.extend(["--ready-fd", str(ready_fd), "--start-fd", str(start_fd)])
    if fault_plan_path is not None and fault_plan_sha is not None:
        command.extend(["--fault-plan", str(fault_plan_path),
                        "--fault-plan-sha256", fault_plan_sha])
    command.append("authority-request")
    return command


def spawn_barrier_helper(authority, authority_sha, descriptor_path, descriptor_sha,
                         request_path, request_sha):
    ready_read, ready_write = os.pipe()
    start_read, start_write = os.pipe()
    command = helper_command(authority, authority_sha, descriptor_path, descriptor_sha,
                             request_path, request_sha, ready_write, start_read)
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             pass_fds=(ready_write, start_read), start_new_session=True)
    os.close(ready_write)
    os.close(start_read)
    return {"child": child, "command": command, "ready_read": ready_read,
            "start_write": start_write, "request_path": str(request_path),
            "request_sha256": request_sha, "released": False, "reaped": False}


def process_group_members(pgid, exclude=()):
    excluded = set(exclude)
    members = []
    for item in pathlib.Path("/proc").iterdir():
        if not item.name.isdigit() or int(item.name) in excluded:
            continue
        try:
            raw = item.joinpath("stat").read_text()
            remainder = raw[raw.rfind(")") + 2:].split()
            if remainder[0] != "Z" and int(remainder[2]) == pgid:
                members.append(int(item.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    return sorted(members)


def observe_leader_exit(pid):
    return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


def bounded_pipe_drain(stdout_stream, stderr_stream, timeout=2, limit=1024 * 1024):
    streams = {stdout_stream.fileno(): ("stdout", stdout_stream),
               stderr_stream.fileno(): ("stderr", stderr_stream)}
    chunks = {"stdout": [], "stderr": []}
    sizes = {"stdout": 0, "stderr": 0}
    deadline = time.monotonic() + timeout
    error = None
    try:
        for fd in streams:
            os.set_blocking(fd, False)
        while streams and time.monotonic() < deadline:
            readable, _, _ = select.select(list(streams), [], [],
                                           max(0, deadline - time.monotonic()))
            if not readable:
                continue
            for fd in readable:
                name, _ = streams[fd]
                try:
                    chunk = os.read(fd, min(65536, limit - sizes[name] + 1))
                except BlockingIOError:
                    continue
                if not chunk:
                    del streams[fd]
                    continue
                sizes[name] += len(chunk)
                if sizes[name] > limit:
                    error = f"{name.upper()}_CAPTURE_LIMIT_EXCEEDED"
                    streams.clear()
                    break
                chunks[name].append(chunk)
        if streams and error is None:
            error = "PIPE_DRAIN_TIMEOUT"
    finally:
        stdout_stream.close()
        stderr_stream.close()
    return b"".join(chunks["stdout"]), b"".join(chunks["stderr"]), error


def finalize_process_group(entry, timeout=15, force=False):
    """Keep the leader unreaped until its non-leader process group is terminal."""
    child = entry["child"]
    deadline = time.monotonic() + timeout
    try:
        observed = observe_leader_exit(child.pid)
    except ChildProcessError:
        return {"pid": child.pid, "leader_exit_observed": 0, "leader_reaped": 0,
                "group_terminal": 0, "signal_sent": None, "remaining_members": [],
                "result": "UNKNOWN_LEADER_ALREADY_REAPED"}
    signal_sent = None
    if force:
        try:
            os.killpg(child.pid, signal.SIGKILL)
            signal_sent = "SIGKILL"
        except ProcessLookupError:
            pass
    while observed is None and time.monotonic() < deadline:
        try:
            observed = observe_leader_exit(child.pid)
        except ChildProcessError:
            return {"pid": child.pid, "leader_exit_observed": 0, "leader_reaped": 0,
                    "group_terminal": 0, "signal_sent": signal_sent,
                    "remaining_members": [], "result": "UNKNOWN_LEADER_ALREADY_REAPED"}
        if observed is None:
            time.sleep(0.01)
    if observed is None:
        try:
            os.killpg(child.pid, signal.SIGKILL)
            signal_sent = "SIGKILL"
        except ProcessLookupError:
            pass
        kill_deadline = time.monotonic() + 5
        while observed is None and time.monotonic() < kill_deadline:
            try:
                observed = observe_leader_exit(child.pid)
            except ChildProcessError:
                return {"pid": child.pid, "leader_exit_observed": 0,
                        "leader_reaped": 0, "group_terminal": 0,
                        "signal_sent": signal_sent, "remaining_members": [],
                        "result": "UNKNOWN_LEADER_ALREADY_REAPED"}
            if observed is None:
                time.sleep(0.01)
    if observed is None:
        return {"pid": child.pid, "leader_exit_observed": 0, "leader_reaped": 0,
                "group_terminal": 0, "signal_sent": signal_sent,
                "remaining_members": process_group_members(child.pid, (child.pid,)),
                "result": "UNKNOWN_LEADER_UNOBSERVED"}
    members = process_group_members(child.pid, (child.pid,))
    if members:
        try:
            os.killpg(child.pid, signal.SIGKILL)
            signal_sent = "SIGKILL"
        except ProcessLookupError:
            pass
        group_deadline = time.monotonic() + 5
        while members and time.monotonic() < group_deadline:
            time.sleep(0.01)
            members = process_group_members(child.pid, (child.pid,))
    if members:
        return {"pid": child.pid, "leader_exit_observed": 1, "leader_reaped": 0,
                "group_terminal": 0, "signal_sent": signal_sent,
                "remaining_members": members, "result": "UNKNOWN_GROUP_NONTERMINAL"}
    waited, status = os.waitpid(child.pid, 0)
    if waited != child.pid:
        raise RuntimeError("exact authority leader was not reaped")
    child.returncode = os.waitstatus_to_exitcode(status)
    entry["reaped"] = True
    stdout, stderr, capture_error = bounded_pipe_drain(child.stdout, child.stderr)
    outcome = {"pid": child.pid, "leader_exit_observed": 1, "leader_reaped": 1,
               "group_terminal": 1, "signal_sent": signal_sent,
               "remaining_members": [], "returncode": child.returncode,
               "capture_error": capture_error,
               "result": "GROUP_TERMINAL" if capture_error is None
               else "GROUP_TERMINAL_CAPTURE_UNKNOWN"}
    return outcome, stdout, stderr


def run_single_helper(authority, authority_sha, descriptor_path, descriptor_sha,
                      request_path, request_sha):
    command = helper_command(authority, authority_sha, descriptor_path, descriptor_sha,
                             request_path, request_sha)
    entry = {"child": subprocess.Popen(command, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True),
             "command": command, "reaped": False}
    try:
        outcome, stdout, stderr = finalize_process_group(entry)
        if outcome["returncode"] != 0 or outcome["capture_error"] is not None:
            raise RuntimeError("authority helper failed: " + stderr.decode(errors="replace"))
        return stdout
    finally:
        if not entry["reaped"]:
            cleanup = finalize_process_group(entry, timeout=1, force=True)
            if isinstance(cleanup, tuple):
                cleanup = cleanup[0]
            if not cleanup["group_terminal"]:
                raise RuntimeError("authority helper cleanup is UNKNOWN")


def request_for(descriptor, descriptor_sha, transaction=None, attempt=None):
    transaction = transaction or uuid.uuid4().hex
    attempt = attempt or uuid.uuid4().hex
    scope = descriptor["scope"]
    return {
        "schema": 1, "kind": "PINNED_AUTHORITY_REQUEST",
        "request_id": uuid.uuid4().hex, "descriptor_sha256": descriptor_sha,
        "operation": "RESERVE", **scope, "transaction": transaction,
        "attempt": attempt, "unit": f"slt-thick-lab-exec-{attempt}.service",
        "code_digest": descriptor["code_digest"], "policy_digest": "3" * 64,
    }


def proc_argv(pid):
    return pathlib.Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")


def proc_start_ticks(pid):
    fields = pathlib.Path(f"/proc/{pid}/stat").read_text().split()
    return int(fields[21])


def wait_all_ready(entries, timeout=10):
    pending = {entry["ready_read"]: entry for entry in entries}
    deadline = time.monotonic() + timeout
    while pending and time.monotonic() < deadline:
        readable, _, _ = select.select(list(pending), [], [],
                                       max(0, deadline - time.monotonic()))
        for fd in readable:
            if os.read(fd, 1) != b"R":
                raise RuntimeError("authority helper readiness is malformed")
            entry = pending.pop(fd)
            os.close(fd)
            entry["ready_read"] = -1
            entry["ready"] = {"pid": entry["child"].pid,
                              "start_ticks": proc_start_ticks(entry["child"].pid),
                              "argv": proc_argv(entry["child"].pid)}
    if pending:
        raise RuntimeError("authority helpers did not reach the bounded ready barrier")
    for entry in entries:
        try:
            exited = observe_leader_exit(entry["child"].pid)
        except ChildProcessError as error:
            raise RuntimeError("authority helper leader was unexpectedly reaped") from error
        if exited is not None or entry["ready"]["argv"] != entry["command"]:
            raise RuntimeError("authority helper identity changed at ready barrier")


def cleanup_entries(entries, finalizer=finalize_process_group):
    outcomes = []
    for entry in entries:
        for name in ("ready_read", "start_write"):
            fd = entry.get(name, -1)
            if fd is not None and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
                entry[name] = -1
        if not entry["reaped"]:
            try:
                outcome = finalizer(entry, timeout=1, force=True)
                if isinstance(outcome, tuple):
                    outcome = outcome[0]
                outcomes.append(outcome)
            except Exception as error:
                outcomes.append({"pid": entry["child"].pid, "group_terminal": 0,
                                 "result": "UNKNOWN_CLEANUP_EXCEPTION",
                                 "error": str(error)})
    return outcomes


def qualify(args):
    if os.geteuid() != 0:
        raise RuntimeError("qualification requires root in the disposable lab")
    control_root = validate_direct_root(args.control_root, CONTROL_PREFIX)
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(control_root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    manifest = copy_snapshot(args.code_root, {
        "authority.py": pathlib.Path(__file__).resolve(),
        "ledger.py": args.ledger_helper, "adapter.pl": args.adapter, "model.pm": args.module,
    })
    code_root = pathlib.Path(args.code_root).resolve(strict=True)
    authority_root = validate_direct_root(args.authority_root, AUTHORITY_PREFIX)
    ledger_module = load_exact(code_root / "ledger.py", "slt_pinned_init_ledger",
                               manifest["ledger.py"])
    model = ledger_module.Model(code_root / "adapter.pl", code_root / "model.pm")
    authority = ledger_module.authority()
    ledger = ledger_module.Ledger.initialize(authority_root, authority, model)
    try:
        ledger._lock()
        try:
            initial, initial_sha = exact_ledger_bytes(ledger, ledger_module)
        finally:
            fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)
    finally:
        ledger.close()
    scope = {"vg_uuid": VG_UUID, "volume": VOLUME,
             "object_key": ledger_module.volume_key(VG_UUID, VOLUME)}
    descriptor = {"schema": 1, "kind": "PINNED_AUTHORITY_TWO_CLIENT_LAB",
                  **authority, "ledger_root": str(pathlib.Path(args.authority_root)),
                  "code_root": str(code_root), "manifest": manifest,
                  "code_digest": digest(manifest), "scope": scope}
    validate_descriptor(descriptor)
    exclusive_json(control_root, "enrollment.json", descriptor)
    descriptor_path = control_root / "enrollment.json"
    stored_descriptor, descriptor_sha = read_canonical(descriptor_path)
    if stored_descriptor != descriptor:
        raise RuntimeError("stored enrollment differs")
    transaction = uuid.uuid4().hex
    requests = [request_for(descriptor, descriptor_sha, transaction=transaction)
                for _ in range(2)]
    request_artifacts = []
    for index, request in enumerate(requests, 1):
        exclusive_json(control_root, f"request-{index}.json", request)
        path = control_root / f"request-{index}.json"
        stored, request_sha = read_canonical(path)
        if stored != request:
            raise RuntimeError("stored request differs before dispatch")
        request_artifacts.append((path, request_sha))
    authority_path = code_root / "authority.py"
    authority_sha = manifest["authority.py"]
    entries = []
    responses = []
    cleanup = []
    collection_errors = []
    try:
        for path, request_sha in request_artifacts:
            entries.append(spawn_barrier_helper(
                authority_path, authority_sha, descriptor_path, descriptor_sha,
                path, request_sha,
            ))
        wait_all_ready(entries)
        for entry in entries:
            os.write(entry["start_write"], b"G")
            os.close(entry["start_write"])
            entry["start_write"] = -1
            entry["released"] = True
        for entry, request in zip(entries, requests):
            try:
                outcome = finalize_process_group(entry)
                if not isinstance(outcome, tuple):
                    raise RuntimeError("authority process-group outcome is UNKNOWN")
                process_outcome, stdout, stderr = outcome
                entry["outcome"] = process_outcome
                if process_outcome["returncode"] != 0 \
                        or process_outcome["capture_error"] is not None:
                    raise RuntimeError(
                        "authority helper failed or capture is UNKNOWN: "
                        + stderr.decode(errors="replace")
                    )
                response = json.loads(stdout, object_pairs_hook=duplicate_keys)
                if stdout != canonical(response):
                    raise RuntimeError("authority response is not canonical")
                responses.append(validate_response(
                    response, request, descriptor, ledger_module
                ))
            except Exception as error:
                collection_errors.append({"pid": entry["child"].pid, "error": str(error)})
    finally:
        cleanup.extend(cleanup_entries(entries))
        if any(not item["group_terminal"] or item.get("capture_error") is not None
               for item in cleanup):
            raise RuntimeError("one or more authority process groups remain unreaped")
    if collection_errors:
        raise RuntimeError("authority request collection failed: " + canonical(
            collection_errors
        ).decode().strip())
    final_ledger = ledger_module.Ledger(authority_root, model)
    try:
        final_ledger._lock()
        try:
            final, final_sha = exact_ledger_bytes(final_ledger, ledger_module)
        finally:
            fcntl.flock(final_ledger.lock_fd, fcntl.LOCK_UN)
    finally:
        final_ledger.close()
    allowed = sorted(response["allowed"] for response in responses)
    winner_index = next((index for index, response in enumerate(responses)
                         if response["allowed"] == 1), None)
    loser_index = next((index for index, response in enumerate(responses)
                        if response["allowed"] == 0), None)
    winner_request = None if winner_index is None else requests[winner_index]
    final_slot = final["slots"].get(scope["object_key"])
    if allowed != [0, 1] or final["revision"] != initial["revision"] + 1 \
            or len(final["slots"]) != 1 or winner_request is None \
            or final_slot["record"] != record_from_request(winner_request, descriptor) \
            or final["consumed_attempts"] != [winner_request["attempt"]] \
            or loser_index is None \
            or responses[winner_index]["transaction_before_sha256"] != initial_sha \
            or responses[loser_index]["transaction_before_sha256"] != final_sha \
            or any(response["ledger_revision"] != final["revision"]
                   or response["ledger_sha256"] != final_sha for response in responses):
        raise RuntimeError("two independent clients did not produce exactly one winner")

    contender = request_for(descriptor, descriptor_sha, transaction=transaction)
    exclusive_json(control_root, "request-3-explicit-refusal.json", contender)
    contender_path = control_root / "request-3-explicit-refusal.json"
    stored_contender, contender_sha = read_canonical(contender_path)
    if stored_contender != contender:
        raise RuntimeError("stored explicit contender differs")
    contender_stdout = run_single_helper(
        authority_path, authority_sha, descriptor_path, descriptor_sha,
        contender_path, contender_sha,
    )
    contender_response = json.loads(contender_stdout, object_pairs_hook=duplicate_keys)
    if contender_stdout != canonical(contender_response):
        raise RuntimeError("explicit contender response is not canonical")
    validate_response(contender_response, contender, descriptor, ledger_module)
    post_ledger = ledger_module.Ledger(authority_root, model)
    try:
        post_ledger._lock()
        try:
            post, post_sha = exact_ledger_bytes(post_ledger, ledger_module)
        finally:
            fcntl.flock(post_ledger.lock_fd, fcntl.LOCK_UN)
    finally:
        post_ledger.close()
    if contender_response["allowed"] != 0 \
            or contender_response["transaction_before_sha256"] != final_sha \
            or contender_response["ledger_sha256"] != final_sha \
            or post_sha != final_sha or post != final:
        raise RuntimeError("explicit occupied-slot refusal changed exact ledger bytes")
    evidence = {"schema": 1, "classification": "PINNED_AUTHORITY_TWO_CLIENT_RESERVE_PASS",
                "descriptor_sha256": descriptor_sha, "authority": authority,
                "request_ids": [item["request_id"] for item in requests],
                "requests": [{"sha256": sha, "value": request}
                             for request, (_, sha) in zip(requests, request_artifacts)],
                "barrier": [entry["ready"] for entry in entries],
                "processes": [{"pid": entry["child"].pid,
                               "start_ticks": entry["ready"]["start_ticks"],
                               "argv": entry["command"], "ready": 1,
                               "released": 1 if entry["released"] else 0,
                               "reaped": 1 if entry["reaped"] else 0,
                               "returncode": entry["child"].returncode,
                               "group_terminal": entry["outcome"]["group_terminal"]}
                              for entry in entries],
                "responses": responses, "winner_request_id": winner_request["request_id"],
                "explicit_refusal": {"request_sha256": contender_sha,
                                     "response": contender_response},
                "cleanup": cleanup, "ledger_revision": final["revision"],
                "ledger_sha256": final_sha,
                "limitations": ["single authority current boot", "same-host clients only",
                                "ready-barrier reserve race and explicit refusal only",
                                "barrier does not prove simultaneous CPU execution",
                                "no remote executor, SSH or storage I/O",
                                "no retry, fallback, close, TTL or takeover"]}
    exclusive_json(control_root, "evidence.json", evidence)
    print(canonical({"classification": evidence["classification"],
                     "ledger_revision": final["revision"],
                     "ledger_sha256": final_sha,
                     "evidence_sha256": file_sha(control_root / "evidence.json")}).decode(), end="")


def proc_state(pid):
    for line in pathlib.Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("State:"):
            return line.split()[1]
    raise RuntimeError("process state is unavailable")


def wait_l1_checkpoint(path, child_pid, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value, sha = read_canonical(path)
            if proc_state(child_pid) in ("T", "t"):
                return value, sha
        try:
            if observe_leader_exit(child_pid) is not None:
                raise RuntimeError("L1 helper exited before its stopped checkpoint")
        except ChildProcessError as error:
            raise RuntimeError("L1 helper identity was reaped before checkpoint") from error
        time.sleep(0.01)
    raise RuntimeError("L1 checkpoint/stopped state deadline exceeded")


def observe_enrolled_authority(descriptor, ledger_module, model):
    LedgerClass = enrolled_ledger_class(ledger_module, descriptor)
    ledger = LedgerClass(descriptor["ledger_root"], model)
    try:
        value, observed = ledger.transaction(lambda data: (None, data))
        if value != observed or ledger.store_calls != 0:
            raise RuntimeError("authority observer was not read-only")
        return value, ledger.transaction_before_sha256
    finally:
        ledger.close()


def write_request_artifact(control_root, name, request):
    exclusive_json(control_root, name, request)
    path = control_root / name
    stored, sha = read_canonical(path)
    if stored != request:
        raise RuntimeError("stored request differs from intended request")
    return path, sha


def parse_helper_response(payload, request, descriptor, ledger_module):
    value = json.loads(payload, object_pairs_hook=duplicate_keys)
    if payload != canonical(value):
        raise RuntimeError("authority response is not canonical")
    return validate_response(value, request, descriptor, ledger_module)


def cleanup_l1_entry(entry, active_error, finalizer=finalize_process_group):
    try:
        fallback = finalizer(entry, timeout=1, force=True)
    except Exception as cleanup_error:
        message = "L1 failure cleanup raised before proving terminal state: " \
            + repr(cleanup_error)
        if active_error is not None:
            active_error.add_note(message)
            return None
        raise RuntimeError(message) from cleanup_error
    if isinstance(fallback, tuple):
        fallback = fallback[0]
    cleanup_exact = fallback.get("group_terminal") == 1 \
        and fallback.get("leader_reaped") == 1 \
        and fallback.get("capture_error") is None
    if not cleanup_exact:
        message = "L1 failure cleanup left A outcome/capture UNKNOWN: " \
            + json.dumps(fallback, sort_keys=True)
        if active_error is not None:
            active_error.add_note(message)
            return fallback
        raise RuntimeError(message)
    return fallback


def qualify_l1(args):
    if os.geteuid() != 0:
        raise RuntimeError("qualification requires root in the disposable lab")
    control_root = validate_direct_root(args.control_root, CONTROL_PREFIX)
    parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.mkdir(control_root.name, 0o700, dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    manifest = copy_snapshot(args.code_root, {
        "authority.py": pathlib.Path(__file__).resolve(),
        "ledger.py": args.ledger_helper, "adapter.pl": args.adapter, "model.pm": args.module,
    })
    code_root = pathlib.Path(args.code_root).resolve(strict=True)
    authority_root = validate_direct_root(args.authority_root, AUTHORITY_PREFIX)
    ledger_module = load_exact(code_root / "ledger.py", "slt_pinned_l1_ledger",
                               manifest["ledger.py"])
    model = ledger_module.Model(code_root / "adapter.pl", code_root / "model.pm")
    authority = ledger_module.authority()
    ledger = ledger_module.Ledger.initialize(authority_root, authority, model)
    try:
        ledger._lock()
        try:
            initial, initial_sha = exact_ledger_bytes(ledger, ledger_module)
        finally:
            fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)
    finally:
        ledger.close()
    scope = {"vg_uuid": VG_UUID, "volume": "vm-910002-disk-0",
             "object_key": ledger_module.volume_key(VG_UUID, "vm-910002-disk-0")}
    descriptor = {"schema": 1, "kind": "PINNED_AUTHORITY_TWO_CLIENT_LAB",
                  **authority, "ledger_root": str(authority_root),
                  "code_root": str(code_root), "manifest": manifest,
                  "code_digest": digest(manifest), "scope": scope}
    validate_descriptor(descriptor)
    exclusive_json(control_root, "enrollment.json", descriptor)
    descriptor_path = control_root / "enrollment.json"
    stored_descriptor, descriptor_sha = read_canonical(descriptor_path)
    if stored_descriptor != descriptor:
        raise RuntimeError("stored enrollment differs")
    transaction = uuid.uuid4().hex
    request_a = request_for(descriptor, descriptor_sha, transaction=transaction)
    request_a_path, request_a_sha = write_request_artifact(
        control_root, "l1-request-a.json", request_a
    )
    run_id = uuid.uuid4().hex
    fault_plan = {"schema": 1, "kind": "PINNED_AUTHORITY_FAULT_PLAN",
                  "scenario": "L1", "boundary": "POST_DURABLE_RESERVE_PRE_RESPONSE",
                  "run_id": run_id, "descriptor_sha256": descriptor_sha,
                  "request_sha256": request_a_sha, "code_digest": descriptor["code_digest"],
                  "control_root": str(control_root),
                  "checkpoint_name": "l1-checkpoint.json"}
    validate_l1_fault_plan(fault_plan, descriptor_sha, request_a_sha)
    exclusive_json(control_root, "l1-fault-plan.json", fault_plan)
    fault_path = control_root / "l1-fault-plan.json"
    stored_fault, fault_sha = read_canonical(fault_path)
    if stored_fault != fault_plan:
        raise RuntimeError("stored L1 fault plan differs")
    authority_path = code_root / "authority.py"
    authority_sha = manifest["authority.py"]
    command_a = helper_command(
        authority_path, authority_sha, descriptor_path, descriptor_sha,
        request_a_path, request_a_sha, fault_plan_path=fault_path,
        fault_plan_sha=fault_sha,
    )
    entry_a = {"child": subprocess.Popen(
        command_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    ), "command": command_a, "reaped": False}
    checkpoint = None
    cleanup_a = None
    try:
        checkpoint_path = control_root / "l1-checkpoint.json"
        checkpoint, checkpoint_sha = wait_l1_checkpoint(
            checkpoint_path, entry_a["child"].pid
        )
        reserved, reserved_sha = observe_enrolled_authority(
            descriptor, ledger_module, model
        )
        slot = reserved["slots"].get(scope["object_key"])
        expected_record_a = record_from_request(request_a, descriptor)
        expected_response_a = {
            "schema": 1, "kind": "PINNED_AUTHORITY_RESPONSE",
            "descriptor_sha256": descriptor_sha,
            "request_id": request_a["request_id"], "operation": "RESERVE",
            "result": "RESERVE_ATOMIC", "allowed": 1,
            "transaction_before_sha256": initial_sha,
            "ledger_revision": initial["revision"] + 1,
            "ledger_sha256": reserved_sha, "record": expected_record_a,
        }
        validate_response(expected_response_a, request_a, descriptor, ledger_module)
        validate_l1_checkpoint(checkpoint, {
            "boundary": fault_plan["boundary"], "run_id": run_id,
            "descriptor_sha256": descriptor_sha, "request_sha256": request_a_sha,
            "code_digest": descriptor["code_digest"],
            "object_key": request_a["object_key"], "transaction": transaction,
            "attempt": request_a["attempt"],
            "boot_id": authority["authority_boot_id"],
            "pre_revision": initial["revision"],
            "post_revision": initial["revision"] + 1,
            "ledger_sha256": reserved_sha,
            "response_sha256": digest(expected_response_a),
        }, entry_a["child"].pid, command_a)
        if reserved["revision"] != 2 or reserved_sha != checkpoint["ledger_sha256"] \
                or len(reserved["slots"]) != 1 \
                or reserved["consumed_attempts"] != [request_a["attempt"]] \
                or not inert_reserved_slot(slot, expected_record_a, reserved["schema"]):
            raise RuntimeError("L1 observer differs from durable A reservation")
        request_b = request_for(descriptor, descriptor_sha, transaction=transaction)
        request_b_path, request_b_sha = write_request_artifact(
            control_root, "l1-request-b.json", request_b
        )
        response_b = parse_helper_response(run_single_helper(
            authority_path, authority_sha, descriptor_path, descriptor_sha,
            request_b_path, request_b_sha,
        ), request_b, descriptor, ledger_module)
        if response_b["allowed"] != 0 \
                or response_b["ledger_revision"] != 2 \
                or response_b["transaction_before_sha256"] != reserved_sha \
                or response_b["ledger_sha256"] != reserved_sha \
                or proc_state(entry_a["child"].pid) not in ("T", "t") \
                or proc_start_ticks(entry_a["child"].pid) != checkpoint["start_ticks"]:
            raise RuntimeError("B did not prove refusal while exact A remained stopped")
        cleanup_result = finalize_process_group(entry_a, timeout=1, force=True)
        if not isinstance(cleanup_result, tuple):
            raise RuntimeError("A process-group cleanup is UNKNOWN")
        cleanup_a, stdout_a, stderr_a = cleanup_result
        if cleanup_a["returncode"] != -signal.SIGKILL \
                or cleanup_a["group_terminal"] != 1 \
                or cleanup_a["leader_reaped"] != 1 \
                or cleanup_a["capture_error"] is not None \
                or stdout_a != b"":
            raise RuntimeError("A did not terminate exactly without a delivered response")
        request_c = request_for(descriptor, descriptor_sha, transaction=transaction)
        request_c_path, request_c_sha = write_request_artifact(
            control_root, "l1-request-c.json", request_c
        )
        if len({request_a["attempt"], request_b["attempt"], request_c["attempt"]}) != 3 \
                or len({request_a["request_id"], request_b["request_id"],
                        request_c["request_id"]}) != 3:
            raise RuntimeError("L1 clients do not have three distinct request identities")
        response_c = parse_helper_response(run_single_helper(
            authority_path, authority_sha, descriptor_path, descriptor_sha,
            request_c_path, request_c_sha,
        ), request_c, descriptor, ledger_module)
        final, final_sha = observe_enrolled_authority(descriptor, ledger_module, model)
        if response_c["allowed"] != 0 \
                or response_c["ledger_revision"] != 2 \
                or response_c["transaction_before_sha256"] != reserved_sha \
                or response_c["ledger_sha256"] != reserved_sha \
                or final_sha != reserved_sha or final != reserved:
            raise RuntimeError("C changed durable reservation after A death")
        evidence = {
            "schema": 1,
            "classification": "PINNED_AUTHORITY_L1_PERSISTED_NO_RESPONSE_REFUSAL_PASS",
            "descriptor_sha256": descriptor_sha, "fault_plan_sha256": fault_sha,
            "checkpoint_sha256": checkpoint_sha, "checkpoint": checkpoint,
            "authority_outcome": "A_RESERVED_DURABLY",
            "caller_observation": "UNKNOWN_NO_RESPONSE",
            "request_a": {"sha256": request_a_sha, "value": request_a},
            "response_a_sha256": checkpoint["response_sha256"],
            "response_a_delivered": 0,
            "refusal_while_a_stopped": {"request_sha256": request_b_sha,
                                         "response": response_b},
            "a_process": cleanup_a,
            "a_stderr": stderr_a.decode("utf-8", "replace"),
            "refusal_after_a_death": {"request_sha256": request_c_sha,
                                       "response": response_c},
            "ledger_revision": final["revision"], "ledger_sha256": final_sha,
            "limitations": ["single authority current boot", "same-host handlers only",
                            "completed reserve store before process failure",
                            "no retry of A and no LOOKUP endpoint",
                            "no SSH, remote executor, dispatch, systemd or storage I/O",
                            "no power loss, rollback, reboot, close, TTL or takeover"],
        }
        exclusive_json(control_root, "l1-evidence.json", evidence)
        print(canonical({"classification": evidence["classification"],
                         "ledger_revision": final["revision"],
                         "ledger_sha256": final_sha,
                         "evidence_sha256": file_sha(
                             control_root / "l1-evidence.json"
                         )}).decode(), end="")
    finally:
        if not entry_a["reaped"]:
            cleanup_l1_entry(entry_a, sys.exc_info()[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ack", required=True)
    parser.add_argument("--descriptor")
    parser.add_argument("--descriptor-sha256")
    parser.add_argument("--request")
    parser.add_argument("--request-sha256")
    parser.add_argument("--fault-plan")
    parser.add_argument("--fault-plan-sha256")
    parser.add_argument("--ready-fd", type=int)
    parser.add_argument("--start-fd", type=int)
    parser.add_argument("--authority-root")
    parser.add_argument("--control-root")
    parser.add_argument("--code-root")
    parser.add_argument("--ledger-helper")
    parser.add_argument("--adapter")
    parser.add_argument("--module")
    parser.add_argument("action", choices=("authority-request", "qualify", "qualify-l1"))
    args = parser.parse_args()
    if args.ack != ACK:
        raise SystemExit("explicit disposable acknowledgement is required")
    if args.action == "authority-request":
        if not args.descriptor or not HEX64.fullmatch(args.descriptor_sha256 or "") \
                or not args.request or not HEX64.fullmatch(args.request_sha256 or ""):
            raise SystemExit("helper requires pinned enrollment and request bytes")
    if args.action == "authority-request":
        authority_request(args)
    else:
        required = (args.authority_root, args.control_root, args.code_root,
                    args.ledger_helper, args.adapter, args.module)
        if any(not value for value in required):
            raise SystemExit("qualification requires all fresh disposable paths")
        qualify_l1(args) if args.action == "qualify-l1" else qualify(args)


if __name__ == "__main__":
    main()
