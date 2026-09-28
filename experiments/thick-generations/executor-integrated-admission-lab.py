#!/usr/bin/python3
"""Disposable model-backed executor ledger; never touches guest storage.

This qualifies persistence and model composition only.  It deliberately does
not recover, retry, publish a systemd grant, or mutate LVM/device-mapper state.
"""

import argparse
import fcntl
import hashlib
import json
import os
import pathlib
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
import uuid


ACK = "DISPOSABLE-INTEGRATED-EXECUTOR-ADMISSION-LAB"
PARENT = pathlib.Path("/var/tmp")
PREFIX = "slt-integrated-executor-lab-"
HEX24 = re.compile(r"^[a-f0-9]{24}$")
HEX32 = re.compile(r"^[a-f0-9]{32}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
BOOT_ID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def volume_key(vg_uuid, volume):
    return hashlib.sha256((vg_uuid + "\0" + volume).encode()).hexdigest()[:24]


def validate_grant(grant):
    fields = {"schema", "authority_id", "enrollment_epoch", "attempt", "transaction",
              "unit", "invocation_id", "boot_id", "run_nonce", "pid", "start_ticks",
              "code_digest", "policy_digest", "runner_sha256", "command_sha256",
              "control_group", "inert_operation_digest"}
    if not isinstance(grant, dict) or set(grant) != fields:
        raise RuntimeError("dispatch grant schema is malformed")
    if type(grant["schema"]) is not int or grant["schema"] != 1:
        raise RuntimeError("dispatch grant schema is unsupported")
    for field in ("authority_id", "attempt", "transaction", "invocation_id", "run_nonce"):
        if not HEX32.fullmatch(grant[field] or ""):
            raise RuntimeError(f"dispatch grant {field} is invalid")
    for field in ("enrollment_epoch", "code_digest", "policy_digest", "runner_sha256",
                  "command_sha256", "inert_operation_digest"):
        if not HEX64.fullmatch(grant[field] or ""):
            raise RuntimeError(f"dispatch grant {field} is invalid")
    if not BOOT_ID.fullmatch(grant["boot_id"] or ""):
        raise RuntimeError("dispatch grant boot ID is invalid")
    if not re.fullmatch(r"slt-thick-lab-exec-[a-f0-9]{32}\.service", grant["unit"] or ""):
        raise RuntimeError("dispatch grant unit is invalid")
    if grant["unit"] != f"slt-thick-lab-exec-{grant['attempt']}.service":
        raise RuntimeError("dispatch grant unit does not match its attempt")
    for field in ("pid", "start_ticks"):
        if type(grant[field]) is not int or grant[field] < 1:
            raise RuntimeError(f"dispatch grant {field} is invalid")
    if grant["control_group"] != f"/system.slice/{grant['unit']}":
        raise RuntimeError("dispatch grant control group is invalid")
    return grant


def validate_startup(startup):
    fields = {"attempt", "unit", "invocation_id", "boot_id", "run_nonce", "pid",
              "start_ticks", "runner_sha256", "command_sha256", "control_group",
              "inert_operation_digest"}
    if not isinstance(startup, dict) or set(startup) != fields:
        raise RuntimeError("startup observation schema is malformed")
    probe = {
        "schema": 1, "authority_id": "0" * 32, "enrollment_epoch": "0" * 64,
        "transaction": "0" * 32, "code_digest": "0" * 64,
        "policy_digest": "0" * 64, **startup,
    }
    validate_grant(probe)
    return startup


def validate_finish_evidence(evidence):
    fields = {"identity", "cgroup_terminal", "pending_jobs_absent", "io_terminal",
              "storage_postcondition_proven", "executor_result"}
    if not isinstance(evidence, dict) or set(evidence) != fields:
        raise RuntimeError("terminal evidence schema is malformed")
    identity = evidence["identity"]
    if not isinstance(identity, dict) or identity.get("state") != "BOUND":
        raise RuntimeError("terminal evidence identity is not BOUND")
    for field in ("cgroup_terminal", "pending_jobs_absent", "io_terminal",
                  "storage_postcondition_proven"):
        if type(evidence[field]) is not int or evidence[field] not in (0, 1):
            raise RuntimeError(f"terminal evidence {field} is not an exact bit")
    if evidence["executor_result"] not in ("SUCCESS", "FAILED", "UNKNOWN"):
        raise RuntimeError("terminal evidence result is invalid")
    return evidence


def validate_terminal_proof(proof):
    fields = {"schema", "scope", "origin", "identity", "dispatch_grant_sha256",
              "operation_sha256", "marker", "controller", "terminal",
              "cleanup", "verifier"}
    if not isinstance(proof, dict) or set(proof) != fields \
            or type(proof["schema"]) is not int or proof["schema"] != 1 \
            or proof["scope"] != "INERT_MARKER_ONLY":
        raise RuntimeError("terminal proof schema/scope is invalid")
    if proof["origin"] not in ("RUNTIME_COLLECTED", "MODEL_SYNTHETIC"):
        raise RuntimeError("terminal proof origin is invalid")
    identity_fields = {"authority_id", "enrollment_epoch", "object_key", "transaction",
                       "attempt", "unit", "invocation_id", "boot_id"}
    identity = proof["identity"]
    if not isinstance(identity, dict) or set(identity) != identity_fields:
        raise RuntimeError("terminal proof identity is malformed")
    marker = proof["marker"]
    if not isinstance(marker, dict) or set(marker) != {"sha256", "value"} \
            or not isinstance(marker["value"], dict) \
            or set(marker["value"]) != {"schema", "attempt", "transaction",
                                         "invocation_id", "grant_sha256",
                                         "inert_operation_digest"} \
            or type(marker["value"]["schema"]) is not int \
            or marker["value"]["schema"] != 1 \
            or marker["sha256"] != digest(marker["value"]):
        raise RuntimeError("terminal proof marker is malformed")
    controller = proof["controller"]
    if not isinstance(controller, dict) or set(controller) != {
            "pid", "start_ticks", "wait_status", "reaped", "checkpoint_boundary",
            "command_sha256"} \
            or type(controller["pid"]) is not int or controller["pid"] < 1 \
            or type(controller["start_ticks"]) is not int or controller["start_ticks"] < 1 \
            or type(controller["wait_status"]) is not int or controller["wait_status"] != -9 \
            or type(controller["reaped"]) is not int or controller["reaped"] != 1 \
            or controller["checkpoint_boundary"] != "C6_GRANT_PUBLISHED" \
            or not HEX64.fullmatch(controller["command_sha256"] or ""):
        raise RuntimeError("terminal proof controller evidence is incomplete")
    terminal = proof["terminal"]
    if not isinstance(terminal, dict) or set(terminal) != {
            "invocation_id", "pid", "start_ticks", "result", "exec_main_status",
            "main_pid", "job", "pid_absent", "cgroup_terminal"} \
            or not HEX32.fullmatch(terminal.get("invocation_id", "")) \
            or type(terminal["pid"]) is not int or terminal["pid"] < 1 \
            or type(terminal["start_ticks"]) is not int or terminal["start_ticks"] < 1 \
            or terminal["result"] != "success" or terminal["exec_main_status"] != "0" \
            or terminal["main_pid"] not in ("", "0") or terminal["job"] not in ("", "0") \
            or type(terminal["pid_absent"]) is not int or terminal["pid_absent"] != 1 \
            or terminal["cgroup_terminal"] not in (
                "DIRECT_EMPTY", "PRUNED_AFTER_SAME_LIFECYCLE_TERMINAL"):
        raise RuntimeError("terminal proof lifecycle evidence is incomplete")
    cleanup = proof["cleanup"]
    if not isinstance(cleanup, dict) or set(cleanup) != {"unit_absent", "job_absent"} \
            or any(type(cleanup[field]) is not int or cleanup[field] != 1
                   for field in cleanup):
        raise RuntimeError("terminal proof cleanup evidence is incomplete")
    verifier = proof["verifier"]
    observations = {key: value for key, value in proof.items() if key != "verifier"}
    expected_kind = "C6_POST_CRASH_TERMINAL_V1" \
        if proof["origin"] == "RUNTIME_COLLECTED" else "MODEL_SYNTHETIC_TERMINAL_V1"
    if not isinstance(verifier, dict) or set(verifier) != {
            "kind", "version", "observations_sha256"} \
            or verifier["kind"] != expected_kind \
            or type(verifier["version"]) is not int or verifier["version"] != 1 \
            or verifier["observations_sha256"] != digest(observations):
        raise RuntimeError("terminal proof verifier binding is invalid")
    for value in (proof["dispatch_grant_sha256"], proof["operation_sha256"],
                  identity.get("enrollment_epoch"), marker.get("sha256")):
        if not HEX64.fullmatch(value or ""):
            raise RuntimeError("terminal proof digest/epoch is invalid")
    for value in (identity.get("authority_id"), identity.get("transaction"),
                  identity.get("attempt"), identity.get("invocation_id")):
        if not HEX32.fullmatch(value or ""):
            raise RuntimeError("terminal proof identity token is invalid")
    if not HEX24.fullmatch(identity.get("object_key", "")) \
            or not BOOT_ID.fullmatch(identity.get("boot_id", "")):
        raise RuntimeError("terminal proof object/boot identity is invalid")
    return proof


def evidence_from_terminal_proof(expected_bound, proof, authority_id, dispatch):
    validate_terminal_proof(proof)
    identity = proof["identity"]
    grant = dispatch["grant"]
    expected_identity = {
        "authority_id": authority_id,
        "enrollment_epoch": expected_bound["enrollment_epoch"],
        "object_key": expected_bound["object_key"],
        "transaction": expected_bound["transaction"],
        "attempt": expected_bound["attempt"], "unit": expected_bound["unit"],
        "invocation_id": expected_bound["invocation_id"],
        "boot_id": expected_bound["boot_id"],
    }
    expected_marker = {
        "schema": 1, "attempt": expected_bound["attempt"],
        "transaction": expected_bound["transaction"],
        "invocation_id": expected_bound["invocation_id"],
        "grant_sha256": dispatch["grant_sha256"],
        "inert_operation_digest": grant["inert_operation_digest"],
    }
    terminal = proof["terminal"]
    if identity != expected_identity \
            or proof["dispatch_grant_sha256"] != dispatch["grant_sha256"] \
            or proof["operation_sha256"] != grant["inert_operation_digest"] \
            or proof["marker"]["value"] != expected_marker \
            or proof["marker"]["sha256"] != digest(expected_marker) \
            or terminal["invocation_id"] != grant["invocation_id"] \
            or terminal["pid"] != grant["pid"] \
            or terminal["start_ticks"] != grant["start_ticks"]:
        raise RuntimeError("terminal proof is not bound to dispatch/runner identity")
    return {
        "identity": expected_bound, "cgroup_terminal": 1,
        "pending_jobs_absent": 1, "io_terminal": 1,
        "storage_postcondition_proven": 1, "executor_result": "SUCCESS",
    }


def model_only_terminal_proof(expected_bound, authority_id, dispatch, startup):
    marker_value = {
        "schema": 1, "attempt": expected_bound["attempt"],
        "transaction": expected_bound["transaction"],
        "invocation_id": expected_bound["invocation_id"],
        "grant_sha256": dispatch["grant_sha256"],
        "inert_operation_digest": dispatch["grant"]["inert_operation_digest"],
    }
    proof = {
        "schema": 1, "scope": "INERT_MARKER_ONLY", "origin": "MODEL_SYNTHETIC",
        "identity": {
            "authority_id": authority_id,
            "enrollment_epoch": expected_bound["enrollment_epoch"],
            "object_key": expected_bound["object_key"],
            "transaction": expected_bound["transaction"],
            "attempt": expected_bound["attempt"], "unit": expected_bound["unit"],
            "invocation_id": expected_bound["invocation_id"],
            "boot_id": expected_bound["boot_id"],
        },
        "dispatch_grant_sha256": dispatch["grant_sha256"],
        "operation_sha256": dispatch["grant"]["inert_operation_digest"],
        "marker": {"sha256": digest(marker_value), "value": marker_value},
        "controller": {
            "pid": os.getpid(), "start_ticks": 1, "wait_status": -9, "reaped": 1,
            "checkpoint_boundary": "C6_GRANT_PUBLISHED",
            "command_sha256": startup["command_sha256"],
        },
        "terminal": {
            "invocation_id": startup["invocation_id"], "pid": startup["pid"],
            "start_ticks": startup["start_ticks"], "result": "success",
            "exec_main_status": "0", "main_pid": "0", "job": "",
            "pid_absent": 1,
            "cgroup_terminal": "PRUNED_AFTER_SAME_LIFECYCLE_TERMINAL",
        },
        "cleanup": {"unit_absent": 1, "job_absent": 1},
    }
    observations = {key: value for key, value in proof.items() if key != "verifier"}
    proof["verifier"] = {
        "kind": "MODEL_SYNTHETIC_TERMINAL_V1", "version": 1,
        "observations_sha256": digest(observations),
    }
    return proof


class Model:
    def __init__(self, adapter, module):
        self.adapter = pathlib.Path(adapter).resolve(strict=True)
        self.module = pathlib.Path(module).resolve(strict=True)
        self.module_sha256 = hashlib.sha256(self.module.read_bytes()).hexdigest()

    def evaluate(self, action, payload):
        environment = os.environ.copy()
        for name in ("PERL5OPT", "PERL5LIB", "PERLLIB", "PERL_USE_UNSAFE_INC"):
            environment.pop(name, None)
        result = subprocess.run(
            ["/usr/bin/perl", "-T", str(self.adapter), "--module", str(self.module),
             "--module-sha256", self.module_sha256, "--action", action],
            input=canonical(payload), capture_output=True, timeout=10, env=environment,
        )
        if result.returncode != 0:
            raise RuntimeError("admission adapter failed: " + result.stderr.decode(errors="replace"))
        try:
            decision = json.loads(result.stdout, object_pairs_hook=duplicate_keys)
        except Exception as error:
            raise RuntimeError(f"admission adapter returned invalid JSON: {error}") from error
        if not isinstance(decision, dict) or type(decision.get("allowed")) is not int:
            raise RuntimeError("admission adapter returned an invalid decision")
        return decision


class Ledger:
    @staticmethod
    def validate_root(root):
        root = pathlib.Path(root)
        if not root.is_absolute() or root.parent != PARENT or not root.name.startswith(PREFIX):
            raise RuntimeError("root must be a direct /var/tmp/slt-integrated-executor-lab-* path")
        return root

    @classmethod
    def initialize(cls, root, authority, model):
        root = cls.validate_root(root)
        parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            os.mkdir(root.name, 0o700, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
        ledger = cls(root, model, allow_initialize=True)
        try:
            ledger._lock()
            initial = {
                "schema": 2, "revision": 0, **authority,
                "consumed_attempts": [], "slots": {},
            }
            ledger._validate(initial, initializing=True)
            ledger._store(initial)
            ledger.allow_initialize = False
        finally:
            fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)
        return ledger

    def __init__(self, root, model, allow_initialize=False):
        self.root = self.validate_root(root)
        self.model = model
        self.allow_initialize = allow_initialize
        self.owner_pid = os.getpid()
        self.owner_thread = threading.get_ident()
        parent_fd = os.open(PARENT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            self.root_fd = os.open(self.root.name, os.O_RDONLY | os.O_DIRECTORY |
                                   os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        root_stat = os.fstat(self.root_fd)
        if root_stat.st_uid != 0 or stat.S_IMODE(root_stat.st_mode) != 0o700:
            raise RuntimeError("ledger root must be root-owned mode 0700")
        flags = os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
        if allow_initialize:
            flags |= os.O_CREAT | os.O_EXCL
        try:
            self.lock_fd = os.open("ledger.lock", flags, 0o600, dir_fd=self.root_fd)
        except FileNotFoundError as error:
            raise RuntimeError("stable lock is missing; authority is UNKNOWN") from error
        lock_stat = os.fstat(self.lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_uid != 0 \
                or stat.S_IMODE(lock_stat.st_mode) != 0o600:
            raise RuntimeError("ledger lock must be root-owned mode 0600")
        self.pinned_authority = None

    def close(self):
        os.close(self.lock_fd)
        os.close(self.root_fd)

    def _lock(self, timeout=10):
        if os.getpid() != self.owner_pid:
            raise RuntimeError("inherited ledger instance is forbidden; reopen after fork")
        if threading.get_ident() != self.owner_thread:
            raise RuntimeError("ledger instance is restricted to its creating thread")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("authority lock timeout; no takeover")
                time.sleep(0.01)

    def _validate(self, data, initializing=False):
        expected = {"schema", "revision", "authority_id", "authority_node",
                    "authority_boot_id", "enrollment_epoch", "consumed_attempts", "slots"}
        if not isinstance(data, dict) or set(data) != expected:
            raise RuntimeError("ledger schema is malformed")
        if type(data["schema"]) is not int or data["schema"] not in (1, 2):
            raise RuntimeError("ledger schema is unsupported")
        minimum = 0 if initializing else 1
        if type(data["revision"]) is not int or data["revision"] < minimum:
            raise RuntimeError("ledger revision is invalid")
        if not HEX32.fullmatch(data["authority_id"] or ""):
            raise RuntimeError("authority ID is invalid")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", data["authority_node"] or ""):
            raise RuntimeError("authority node is invalid")
        if not BOOT_ID.fullmatch(data["authority_boot_id"] or ""):
            raise RuntimeError("authority boot ID is invalid")
        if not HEX64.fullmatch(data["enrollment_epoch"] or ""):
            raise RuntimeError("enrollment epoch is invalid")
        attempts = data["consumed_attempts"]
        if not isinstance(attempts, list) or len(attempts) != len(set(attempts)) or \
                any(not isinstance(item, str) or not HEX32.fullmatch(item) for item in attempts):
            raise RuntimeError("attempt history is invalid")
        if not isinstance(data["slots"], dict):
            raise RuntimeError("slots are invalid")
        for key, slot in data["slots"].items():
            envelope = {"record", "launch_issued", "startup_observed",
                        "dispatch_issued", "recovery_hold"}
            if data["schema"] == 2:
                envelope.add("finish_observed")
            if not HEX24.fullmatch(key) or not isinstance(slot, dict) \
                    or set(slot) != envelope:
                raise RuntimeError("slot envelope is malformed")
            record = slot["record"]
            if not isinstance(record, dict):
                raise RuntimeError("persisted executor record is not an object")
            decision = self.model.evaluate("validate", {"record": record})
            if decision.get("allowed") != 1 or decision.get("action") != "VALID_LAB_RECORD":
                raise RuntimeError("persisted executor record failed model validation")
            if record.get("object_key") != key \
                    or record.get("attempt") not in attempts:
                raise RuntimeError("slot record is inconsistent with authority history")
            if key != volume_key(record.get("vg_uuid", ""), record.get("volume", "")):
                raise RuntimeError("slot key is not the canonical volume identity")
            if record.get("enrollment_epoch") != data["enrollment_epoch"] \
                    or record.get("node") != data["authority_node"] \
                    or record.get("boot_id") != data["authority_boot_id"]:
                raise RuntimeError("slot record is not bound to ledger authority")
            if slot["recovery_hold"] is not None:
                hold = slot["recovery_hold"]
                if not isinstance(hold, dict) or set(hold) != {"reason"} \
                        or not isinstance(hold["reason"], str) or not hold["reason"]:
                    raise RuntimeError("recovery hold is malformed")
            launch = slot["launch_issued"]
            if launch is not None:
                if not isinstance(launch, dict) or set(launch) != {
                    "attempt", "unit", "boot_id", "command_sha256"
                } or launch["attempt"] != record.get("attempt") \
                        or launch["unit"] != record.get("unit") \
                        or launch["boot_id"] != record.get("boot_id") \
                        or not HEX64.fullmatch(launch.get("command_sha256", "")):
                    raise RuntimeError("launch intent is malformed")
            startup = slot["startup_observed"]
            if startup is not None:
                validate_startup(startup)
                if launch is None or startup["attempt"] != record.get("attempt") \
                        or startup["unit"] != record.get("unit") \
                        or startup["boot_id"] != record.get("boot_id") \
                        or startup["command_sha256"] != launch["command_sha256"]:
                    raise RuntimeError("startup observation is not bound to launch/record")
                if record.get("state") in ("BOUND", "TERMINAL", "UNKNOWN") \
                        and startup["invocation_id"] != record.get("invocation_id"):
                    raise RuntimeError("startup invocation is not bound to record")
            dispatch = slot["dispatch_issued"]
            finish = slot.get("finish_observed")
            state = record.get("state")
            if state == "RESERVED" and dispatch is not None:
                raise RuntimeError("reserved record cannot contain dispatch history")
            if state in ("BOUND", "TERMINAL", "UNKNOWN") and launch is None:
                raise RuntimeError("non-reserved record lacks launch history")
            if state in ("BOUND", "TERMINAL", "UNKNOWN") and startup is None:
                raise RuntimeError("non-reserved record lacks startup observation")
            if state == "RESERVED" and startup is not None:
                raise RuntimeError("reserved record cannot contain startup observation")
            if dispatch is not None:
                if not isinstance(dispatch, dict) or set(dispatch) != {
                    "grant", "grant_sha256", "issued_revision"
                } or dispatch["grant_sha256"] != digest(validate_grant(dispatch["grant"])) \
                        or type(dispatch["issued_revision"]) is not int \
                        or dispatch["issued_revision"] < 1 \
                        or dispatch["issued_revision"] > data["revision"]:
                    raise RuntimeError("dispatch intent is malformed")
                grant = dispatch["grant"]
                for field in ("attempt", "transaction", "unit", "invocation_id", "boot_id",
                              "code_digest", "policy_digest"):
                    if grant[field] != record.get(field):
                        raise RuntimeError("dispatch grant is not bound to slot record")
                if grant["authority_id"] != data["authority_id"] \
                        or grant["enrollment_epoch"] != data["enrollment_epoch"]:
                    raise RuntimeError("dispatch grant is not bound to ledger authority")
                for field in ("attempt", "unit", "invocation_id", "boot_id", "run_nonce",
                              "pid", "start_ticks", "runner_sha256", "command_sha256",
                              "control_group", "inert_operation_digest"):
                    if grant[field] != startup.get(field):
                        raise RuntimeError("dispatch grant is not bound to startup observation")
            if state in ("RESERVED", "BOUND") and finish is not None:
                raise RuntimeError("non-terminal record carries finish evidence")
            if state in ("TERMINAL", "UNKNOWN"):
                if data["schema"] != 2 or dispatch is None or not isinstance(finish, dict) \
                        or set(finish) != {"proof", "proof_sha256", "evidence",
                                           "evidence_sha256", "observed_revision"}:
                    raise RuntimeError("terminal record lacks exact finish evidence")
                proof = validate_terminal_proof(finish["proof"])
                evidence = validate_finish_evidence(finish["evidence"])
                if finish["proof_sha256"] != digest(proof) \
                        or finish["evidence_sha256"] != digest(evidence) \
                        or type(finish["observed_revision"]) is not int \
                        or finish["observed_revision"] <= dispatch["issued_revision"] \
                        or finish["observed_revision"] > data["revision"]:
                    raise RuntimeError("finish evidence envelope is malformed")
                bound = json.loads(json.dumps(record))
                bound["state"] = "BOUND"
                bound.pop("executor_result", None)
                if evidence_from_terminal_proof(
                        bound, proof, data["authority_id"], dispatch) != evidence:
                    raise RuntimeError("persisted terminal proof/evidence binding changed")
                decision = self.model.evaluate("finish", {
                    "record": bound, "evidence": evidence,
                })
                wanted = "MARK_TERMINAL" if state == "TERMINAL" else "MARK_UNKNOWN"
                if decision.get("allowed") != 1 or decision.get("action") != wanted \
                        or decision.get("record") != record:
                    raise RuntimeError("persisted finish evidence does not reproduce record")
        active_attempts = [slot["record"].get("attempt") for slot in data["slots"].values()]
        if len(active_attempts) != len(set(active_attempts)):
            raise RuntimeError("one execution attempt appears in multiple active slots")
        return data

    def _load(self):
        try:
            fd = os.open("ledger.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
                         dir_fd=self.root_fd)
        except FileNotFoundError as error:
            raise RuntimeError("ledger is missing; authority is UNKNOWN") from error
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 \
                    or stat.S_IMODE(info.st_mode) != 0o600:
                raise RuntimeError("ledger must be root-owned mode 0600")
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                data = json.load(stream, object_pairs_hook=duplicate_keys)
        finally:
            if fd >= 0:
                os.close(fd)
        validated = self._validate(data)
        identity = {field: validated[field] for field in (
            "authority_id", "authority_node", "authority_boot_id", "enrollment_epoch")}
        if self.pinned_authority is None:
            self.pinned_authority = identity
        elif identity != self.pinned_authority:
            raise RuntimeError("ledger authority changed within one open instance")
        return validated

    def _store(self, data):
        updated = json.loads(json.dumps(data))
        updated["revision"] += 1
        self._validate(updated)
        temporary = ".ledger.tmp." + uuid.uuid4().hex
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=self.root_fd)
        try:
            payload = canonical(updated)
            offset = 0
            while offset < len(payload):
                offset += os.write(fd, payload[offset:])
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, "ledger.json", src_dir_fd=self.root_fd, dst_dir_fd=self.root_fd)
        os.fsync(self.root_fd)
        return updated

    def transaction(self, callback):
        self._lock()
        try:
            before = self._load()
            after, result = callback(before)
            return result, self._store(after) if after is not None else before
        finally:
            fcntl.flock(self.lock_fd, fcntl.LOCK_UN)


class Protocol:
    def __init__(self, ledger, model):
        self.ledger = ledger
        self.model = model

    @staticmethod
    def _continuity(data):
        current_boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return data["authority_node"] == os.uname().nodename \
            and data["authority_boot_id"] == current_boot

    def reserve(self, requested):
        def operation(data):
            key = requested.get("object_key", "")
            if key != volume_key(requested.get("vg_uuid", ""), requested.get("volume", "")):
                raise RuntimeError("reservation object key is not canonical")
            existing_slot = data["slots"].get(key)
            attempt = requested.get("attempt", "")
            if not self._continuity(data) \
                    or requested.get("node") != data["authority_node"] \
                    or requested.get("boot_id") != data["authority_boot_id"] \
                    or requested.get("enrollment_epoch") != data["enrollment_epoch"]:
                return None, {"allowed": 0, "action": "BLOCKED",
                              "reason": "authority continuity is not proven"}
            decision = self.model.evaluate("reserve", {
                "requested": requested,
                "existing": None if existing_slot is None else existing_slot["record"],
                "continuity_proven": 1,
                "attempt_fresh_proven": 1 if attempt not in data["consumed_attempts"] else 0,
            })
            if decision.get("allowed") != 1 or decision.get("action") != "RESERVE_ATOMIC":
                return None, decision
            updated = json.loads(json.dumps(data))
            updated["consumed_attempts"].append(attempt)
            updated["slots"][key] = {"record": requested, "launch_issued": None,
                                      "startup_observed": None, "dispatch_issued": None,
                                      "recovery_hold": None}
            if data["schema"] == 2:
                updated["slots"][key]["finish_observed"] = None
            return updated, decision
        return self.ledger.transaction(operation)

    def issue_launch(self, expected, command_sha256):
        def operation(data):
            slot = data["slots"].get(expected["object_key"])
            if slot is None or slot["record"] != expected or slot["launch_issued"] is not None \
                    or slot["dispatch_issued"] is not None or slot["recovery_hold"] is not None:
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "launch CAS lost"}
            if not self._continuity(data) or expected.get("state") != "RESERVED":
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "launch continuity/state mismatch"}
            updated = json.loads(json.dumps(data))
            updated["slots"][expected["object_key"]]["launch_issued"] = {
                "attempt": expected["attempt"], "unit": expected["unit"],
                "boot_id": expected["boot_id"], "command_sha256": command_sha256,
            }
            return updated, {"allowed": 1, "action": "LAUNCH_ISSUED_ONCE"}
        return self.ledger.transaction(operation)

    def bind(self, expected, runner):
        def operation(data):
            slot = data["slots"].get(expected["object_key"])
            if slot is None or slot["record"] != expected or slot["launch_issued"] is None \
                    or slot["dispatch_issued"] is not None or slot["recovery_hold"] is not None:
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "bind CAS lost"}
            if not self._continuity(data) or expected.get("state") != "RESERVED":
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "bind continuity/state mismatch"}
            launch = slot["launch_issued"]
            validate_startup(runner)
            if any(runner.get(field) != launch.get(field) for field in ("attempt", "unit", "boot_id")):
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "runner/launch mismatch"}
            if runner["command_sha256"] != launch["command_sha256"]:
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "runner command mismatch"}
            decision = self.model.evaluate("bind", {"record": expected, "claim": {
                "identity": expected, "invocation_id": runner.get("invocation_id"),
                "startup_proven": 1,
            }})
            if decision.get("allowed") != 1 or decision.get("action") != "BIND_ATOMIC":
                return None, decision
            updated = json.loads(json.dumps(data))
            updated["slots"][expected["object_key"]]["record"] = decision["record"]
            updated["slots"][expected["object_key"]]["startup_observed"] = runner
            return updated, decision
        return self.ledger.transaction(operation)

    def issue_dispatch(self, expected_bound, grant):
        def operation(data):
            slot = data["slots"].get(expected_bound["object_key"])
            if slot is None or slot["record"] != expected_bound or slot["launch_issued"] is None \
                    or slot["dispatch_issued"] is not None or slot["recovery_hold"] is not None:
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "dispatch CAS lost"}
            if not self._continuity(data) or expected_bound.get("state") != "BOUND":
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "dispatch continuity/state mismatch"}
            identity = ("attempt", "transaction", "unit", "invocation_id", "boot_id",
                        "code_digest", "policy_digest")
            validate_grant(grant)
            if any(grant.get(field) != expected_bound.get(field) for field in identity):
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "grant identity mismatch"}
            if grant["authority_id"] != data["authority_id"] \
                    or grant["enrollment_epoch"] != data["enrollment_epoch"] \
                    or grant["enrollment_epoch"] != expected_bound.get("enrollment_epoch"):
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "grant authority mismatch"}
            startup = slot["startup_observed"]
            if startup is None or any(grant.get(field) != startup.get(field) for field in (
                    "attempt", "unit", "invocation_id", "boot_id", "run_nonce", "pid",
                    "start_ticks", "runner_sha256", "command_sha256", "control_group",
                    "inert_operation_digest")):
                return None, {"allowed": 0, "action": "BLOCKED", "reason": "grant startup mismatch"}
            decision = self.model.evaluate("dispatch", {
                "persisted": expected_bound, "claim": {"identity": expected_bound},
                "binding_persisted": 1,
            })
            if decision.get("allowed") != 1 or decision.get("dispatch_allowed") != 1:
                return None, decision
            updated = json.loads(json.dumps(data))
            updated["slots"][expected_bound["object_key"]]["dispatch_issued"] = {
                "grant": grant, "grant_sha256": digest(grant),
                "issued_revision": data["revision"] + 1,
            }
            return updated, {"allowed": 1, "action": "DISPATCH_ISSUED_ONCE",
                             "grant_sha256": digest(grant)}
        return self.ledger.transaction(operation)

    def finish_exact(self, expected_bound, proof, allow_model_fixture=False):
        validate_terminal_proof(proof)

        def operation(data):
            if data["schema"] != 2:
                return None, {"allowed": 0, "action": "BLOCKED",
                              "reason": "finish requires schema-2 evidence persistence"}
            if proof["origin"] != "RUNTIME_COLLECTED" and not allow_model_fixture:
                return None, {"allowed": 0, "action": "BLOCKED",
                              "reason": "synthetic terminal proof cannot authorize runtime finish"}
            slot = data["slots"].get(expected_bound["object_key"])
            if slot is None or slot["record"] != expected_bound \
                    or slot["dispatch_issued"] is None \
                    or slot["finish_observed"] is not None \
                    or slot["recovery_hold"] is not None:
                return None, {"allowed": 0, "action": "BLOCKED",
                              "reason": "finish CAS lost or dispatch is absent"}
            if not self._continuity(data) or expected_bound.get("state") != "BOUND":
                return None, {"allowed": 0, "action": "BLOCKED",
                              "reason": "finish continuity/state mismatch"}
            try:
                evidence = evidence_from_terminal_proof(
                    expected_bound, proof, data["authority_id"], slot["dispatch_issued"]
                )
            except RuntimeError as error:
                return None, {"allowed": 0, "action": "BLOCKED", "reason": str(error)}
            decision = self.model.evaluate("finish", {
                "record": expected_bound, "evidence": evidence,
            })
            if decision.get("allowed") != 1 \
                    or decision.get("action") not in ("MARK_TERMINAL", "MARK_UNKNOWN"):
                return None, decision
            updated = json.loads(json.dumps(data))
            target = updated["slots"][expected_bound["object_key"]]
            target["record"] = decision["record"]
            target["finish_observed"] = {
                "proof": proof, "proof_sha256": digest(proof),
                "evidence": evidence, "evidence_sha256": digest(evidence),
                "observed_revision": data["revision"] + 1,
            }
            return updated, decision

        return self.ledger.transaction(operation)


def authority():
    return {
        "authority_id": uuid.uuid4().hex,
        "authority_node": os.uname().nodename,
        "authority_boot_id": pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "enrollment_epoch": uuid.uuid4().hex + uuid.uuid4().hex,
    }


def bounded_reap_after_signal(children, timeout=2):
    remaining = set(children)
    statuses = {}
    deadline = time.monotonic() + timeout
    while remaining and time.monotonic() < deadline:
        for child in list(remaining):
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited == child:
                statuses[child] = status
                remaining.remove(child)
        if remaining:
            time.sleep(0.01)
    return statuses, sorted(remaining)


def qualify_inherited_instance_refusal(ledger):
    child = os.fork()
    if child == 0:
        try:
            ledger.transaction(lambda data: (None, {"revision": data["revision"]}))
        except RuntimeError as error:
            os._exit(0 if "inherited ledger instance is forbidden" in str(error) else 92)
        os._exit(91)
    deadline = time.monotonic() + 10
    status = None
    while time.monotonic() < deadline:
        waited, candidate = os.waitpid(child, os.WNOHANG)
        if waited == child:
            status = candidate
            break
        time.sleep(0.01)
    if status is None:
        os.kill(child, signal.SIGKILL)
        _, unreaped = bounded_reap_after_signal([child])
        raise RuntimeError(
            f"inherited-instance child outcome is UNKNOWN after deadline; unreaped={unreaped}"
        )
    if not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0:
        raise RuntimeError("inherited ledger instance did not fail closed")


def qualify_launch_contenders(ledger, model, record):
    start_read, start_write = os.pipe()
    result_read, result_write = os.pipe()
    children = []
    for _ in range(2):
        child = os.fork()
        if child == 0:
            os.close(start_write)
            os.close(result_read)
            try:
                ledger.close()
                if os.read(start_read, 1) != b"x":
                    os._exit(93)
                contender = Ledger(ledger.root, model)
                try:
                    result, _ = Protocol(contender, model).issue_launch(record, "4" * 64)
                    os.write(result_write, b"1" if result.get("allowed") == 1 else b"0")
                finally:
                    contender.close()
                os._exit(0)
            except Exception:
                os._exit(94)
        children.append(child)
    os.close(start_read)
    os.close(result_write)
    os.write(start_write, b"xx")
    os.close(start_write)
    os.set_blocking(result_read, False)
    poller = selectors.DefaultSelector()
    poller.register(result_read, selectors.EVENT_READ)
    outcomes = b""
    deadline = time.monotonic() + 10
    while len(outcomes) < 2 and time.monotonic() < deadline:
        for _, _ in poller.select(max(0, deadline - time.monotonic())):
            chunk = os.read(result_read, 2 - len(outcomes))
            if chunk:
                outcomes += chunk
    poller.close()
    os.close(result_read)
    statuses = {}
    while len(statuses) < len(children) and time.monotonic() < deadline:
        for child in children:
            if child in statuses:
                continue
            waited, status = os.waitpid(child, os.WNOHANG)
            if waited == child:
                statuses[child] = status
        if len(statuses) < len(children):
            time.sleep(0.01)
    unreaped = [child for child in children if child not in statuses]
    if unreaped or len(outcomes) != 2:
        for child in unreaped:
            os.kill(child, signal.SIGKILL)
        _, still_unreaped = bounded_reap_after_signal(unreaped)
        raise RuntimeError(
            f"launch contender outcome is UNKNOWN after deadline; unreaped={still_unreaped}"
        )
    if sorted(outcomes) != [48, 49] or any(
            not os.WIFEXITED(status) or os.WEXITSTATUS(status) != 0
            for status in statuses.values()):
        raise RuntimeError("independently reopened launch contenders did not produce one winner")
    return outcomes.decode()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ack", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--adapter", required=True)
    parser.add_argument("--module", required=True)
    parser.add_argument("action", choices=("initialize", "show", "qualify"))
    args = parser.parse_args()
    if args.ack != ACK:
        raise SystemExit("explicit disposable-lab acknowledgement is required")
    if args.action in ("initialize", "qualify"):
        model = Model(args.adapter, args.module)
        ledger = Ledger.initialize(args.root, authority(), model)
    else:
        model = Model(args.adapter, args.module)
        ledger = Ledger(args.root, model)
    try:
        if args.action == "show":
            ledger._lock()
            try:
                print(canonical(ledger._load()).decode(), end="")
            finally:
                fcntl.flock(ledger.lock_fd, fcntl.LOCK_UN)
        elif args.action == "initialize":
            print("INITIALIZED")
        else:
            protocol = Protocol(ledger, model)
            current = ledger._load()
            attempt = uuid.uuid4().hex
            transaction = uuid.uuid4().hex
            vg_uuid = "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF"
            volume = "vm-900001-disk-0"
            record = {
                "schema": 1, "kind": "THICK_EXECUTOR_LAB",
                "enrollment_epoch": current["enrollment_epoch"], "vg_uuid": vg_uuid,
                "volume": volume, "object_key": volume_key(vg_uuid, volume),
                "transaction": transaction, "attempt": attempt,
                "node": current["authority_node"], "boot_id": current["authority_boot_id"],
                "unit": f"slt-thick-lab-exec-{attempt}.service",
                "code_digest": model.module_sha256, "policy_digest": "3" * 64,
                "state": "RESERVED",
            }
            foreign = {**record, "enrollment_epoch": "f" * 64}
            foreign_reserve, before_reserve = protocol.reserve(foreign)
            if foreign_reserve.get("allowed") != 0 or before_reserve["revision"] != 1:
                raise RuntimeError("foreign authority reservation changed the ledger")
            reserve, _ = protocol.reserve(record)
            qualify_inherited_instance_refusal(ledger)
            contender_outcomes = qualify_launch_contenders(ledger, model, record)
            invocation = uuid.uuid4().hex
            runner = {"attempt": attempt, "unit": record["unit"],
                      "boot_id": record["boot_id"], "invocation_id": invocation,
                      "run_nonce": uuid.uuid4().hex, "pid": os.getpid(), "start_ticks": 1,
                      "runner_sha256": "6" * 64, "command_sha256": "4" * 64,
                      "control_group": f"/system.slice/{record['unit']}",
                      "inert_operation_digest": "5" * 64}
            bind, after_bind = protocol.bind(record, runner)
            bound = after_bind["slots"][record["object_key"]]["record"]
            grant = {
                "schema": 1, "authority_id": after_bind["authority_id"],
                "enrollment_epoch": after_bind["enrollment_epoch"],
                "attempt": attempt, "transaction": transaction, "unit": record["unit"],
                "invocation_id": invocation, "boot_id": record["boot_id"],
                "run_nonce": runner["run_nonce"], "pid": runner["pid"],
                "start_ticks": runner["start_ticks"],
                "code_digest": record["code_digest"],
                "policy_digest": record["policy_digest"],
                "runner_sha256": runner["runner_sha256"],
                "command_sha256": runner["command_sha256"],
                "control_group": runner["control_group"],
                "inert_operation_digest": runner["inert_operation_digest"],
            }
            foreign_grant = {**grant, "enrollment_epoch": "f" * 64}
            foreign_dispatch, before_dispatch = protocol.issue_dispatch(bound, foreign_grant)
            if foreign_dispatch.get("allowed") != 0 or before_dispatch["revision"] != 4:
                raise RuntimeError("foreign authority dispatch changed the ledger")
            dispatch, after_dispatch = protocol.issue_dispatch(bound, grant)
            duplicate_dispatch, final = protocol.issue_dispatch(bound, grant)
            terminal_proof = model_only_terminal_proof(
                bound, after_dispatch["authority_id"],
                after_dispatch["slots"][record["object_key"]]["dispatch_issued"],
                runner,
            )
            changed_identity = {**bound, "invocation_id": "f" * 32}
            wrong_proof = json.loads(json.dumps(terminal_proof))
            wrong_proof["identity"]["invocation_id"] = changed_identity["invocation_id"]
            observations = {key: value for key, value in wrong_proof.items() if key != "verifier"}
            wrong_proof["verifier"]["observations_sha256"] = digest(observations)
            wrong_finish, before_finish = protocol.finish_exact(
                bound, wrong_proof, allow_model_fixture=True
            )
            if wrong_finish.get("allowed") != 0 or before_finish["revision"] != 5:
                raise RuntimeError("foreign finish identity changed the ledger")
            finish, after_finish = protocol.finish_exact(
                bound, terminal_proof, allow_model_fixture=True
            )
            duplicate_finish, after_duplicate_finish = protocol.finish_exact(
                bound, terminal_proof, allow_model_fixture=True
            )
            second = {**record, "attempt": uuid.uuid4().hex}
            second["unit"] = f"slt-thick-lab-exec-{second['attempt']}.service"
            second_reserve, final = protocol.reserve(second)
            if [reserve.get("allowed"), bind.get("allowed"),
                    dispatch.get("allowed"), duplicate_dispatch.get("allowed"),
                    wrong_finish.get("allowed"), finish.get("allowed"),
                    duplicate_finish.get("allowed"), second_reserve.get("allowed")] \
                    != [1, 1, 1, 0, 0, 1, 0, 0]:
                raise RuntimeError("integrated admission qualification did not fail closed")
            terminal_slot = final["slots"][record["object_key"]]
            if final != after_duplicate_finish or final["revision"] != 6 \
                    or after_finish["revision"] != 6 \
                    or terminal_slot["record"].get("state") != "TERMINAL" \
                    or terminal_slot["finish_observed"]["evidence_sha256"] \
                    != digest(evidence_from_terminal_proof(
                        bound, terminal_proof, final["authority_id"],
                        terminal_slot["dispatch_issued"])):
                raise RuntimeError("terminal finish persistence or refusal is not exact")
            evidence = {
                "classification": "INTEGRATED_MODEL_LEDGER_FINISH_PASS",
                "ledger_revision": final["revision"],
                "ledger_sha256": digest(final), "attempt": attempt,
                "grant_sha256": digest(grant),
                "foreign_reserve": foreign_reserve,
                "foreign_dispatch": foreign_dispatch,
                "launch_contenders": contender_outcomes,
                "inherited_instance": "REFUSED",
                "duplicate_dispatch": duplicate_dispatch,
                "wrong_finish": wrong_finish, "finish": finish,
                "duplicate_finish": duplicate_finish,
                "finish_proof_sha256": digest(terminal_proof),
                "second_reserve": second_reserve,
            }
            print(canonical(evidence).decode(), end="")
    finally:
        ledger.close()


if __name__ == "__main__":
    main()
