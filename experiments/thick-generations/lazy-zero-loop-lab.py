#!/usr/bin/python3
"""PRE-LIVE scaffolding, not a kernel-mutating harness.

CLI only emits a plan or refuses execute. Direct-I/O functions accept caller-owned
FDs, never discover/open a device pathname. Controller/backend behavior is tested
with mocks only. No subprocess, loop, dmsetup, mount, modprobe or cleanup executor.
"""
import argparse
import copy
import errno
import fcntl
import hashlib
import importlib.util
import json
import mmap
import os
from pathlib import Path
import re
import secrets
import stat
import struct
import sys
import threading

_GUARD_SPEC = importlib.util.spec_from_file_location(
    "slt_lazy_zero_guard_model", Path(__file__).with_name("lazy_zero_guard_model.py"))
_GUARD_MODULE = importlib.util.module_from_spec(_GUARD_SPEC)
_GUARD_SPEC.loader.exec_module(_GUARD_MODULE)
GuardEpochModel = _GUARD_MODULE.GuardEpochModel

ACK = "EXECUTE_DISPOSABLE_LAZY_LOOP_L1_L4"
CASES = ("L1", "L2", "L3", "L4a", "L4b", "L4c", "L4d", "L4e")
LIMITS = {"production_authorized": False, "online_reload_qualified": False,
          "shared_owner_qualified": False, "power_loss_qualified": False,
          "live_backend_implemented": False}
BLKDISCARD = 0x1277
BLKROGET = 0x125E
BLKSSZGET = 0x1268
BLKGETSIZE64 = 0x80081272
BLKGETDISKSEQ = 0x80081280


class Refusal(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def canonical(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False) + "\n").encode("ascii")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def plan(nonce):
    require(type(nonce) is str and re.fullmatch(r"[0-9a-f]{32}", nonce), "nonce")
    return {
        "schema": 1, "kind": "LAZY_LOOP_PRELIVE_PLAN", "nonce": nonce,
        "classification": "PLAN_ONLY_NO_MUTATION", **LIMITS,
        "data_bytes": 134217728, "metadata_bytes": 33554432,
        "region_sectors": 2048, "source_table": "0 262144 zero",
        "clone_template": "0 262144 clone META DEST ZERO 2048 2 no_hydration no_discard_passdown",
        "root_template": "/var/tmp/slt-lazy-loop-lab-" + nonce,
        "fixtures": ["guarded-" + nonce, "negative-" + nonce],
        "cases": {case: "NOT_RUN" for case in CASES},
        "sequence": ["L0_EXACT_BOOT_AND_TARGET_RECHECK", "FULL_NONZERO_CANARY",
                     "OWNED_LOOP_IDENTITIES", "READONLY_ZERO_SOURCE", "CLONE_UNPUBLISHED",
                     "GUARD_BEFORE_PROBES", "L1_DIRECT_ZERO_AND_RAW_CANARY",
                     "L2_SEPARATE_UNGUARDED_HAZARD", "L3_EXACT_DISCARD_ERRNO",
                     "L4a_STOPPED_SUSPEND_RESUME", "L4b_STOPPED_EXACT_RELOAD",
                     "L4c_SAME_METADATA_REOPEN", "IDENTITY_BOUND_CLEANUP_REVIEW"],
        "model_components": ["one-shot exact child/terminal receipts", "two-snapshot exact device identity"],
        "not_implemented": ["live kernel command supervision and live identity collector",
                            "L0/provenance admission", "live sysfs guard writer",
                            "live L1-L4 orchestration and cleanup executor",
                            "L4e independently owned controller-crash supervisor"],
    }


class Journal:
    """Append-only events in a caller-created, fresh local lab directory.

    File and directory fsync are mechanics, not power-loss qualification.
    Never reopen/resume an old journal or truncate an existing event.
    """
    def __init__(self, directory, nonce):
        require(re.fullmatch(r"[0-9a-f]{32}", nonce) is not None, "nonce")
        self.directory = Path(directory)
        self.fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o700,
                    "private journal directory")
            require(not os.listdir(self.fd), "journal must be fresh and empty")
            # Empty-directory observation is not an atomic ownership claim.
            # Two initializers may both see empty; exactly one O_EXCL may win.
            claim = os.open("journal-owner.json",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                            0o600, dir_fd=self.fd)
            try:
                payload = canonical({"schema": 1, "nonce": nonce,
                                     "owner_pid": os.getpid(),
                                     "scope": "PRELIVE_JOURNAL_MECHANICS_ONLY"})
                require(os.write(claim, payload) == len(payload), "short owner claim")
                os.fsync(claim)
            finally:
                os.close(claim)
            os.fsync(self.fd)
        except BaseException:
            os.close(self.fd)
            raise
        self.owner_pid = os.getpid()
        self.owner_thread = threading.get_ident()
        self.nonce = nonce
        self.sequence = 0

    def append(self, event):
        require(os.getpid() == self.owner_pid, "inherited journal")
        require(threading.get_ident() == self.owner_thread, "cross-thread journal")
        require(self.fd is not None, "closed journal")
        self.sequence += 1
        payload = canonical({"schema": 1, "nonce": self.nonce,
                             "sequence": self.sequence, "event": event})
        fd = os.open(f"event-{self.sequence:06d}.json",
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=self.fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                require(written > 0, "journal short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(self.fd)
        return sha(payload)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def validate_identity(identity):
    common = {"kind", "role", "devno", "diskseq", "size_bytes", "readonly", "holders"}
    require(type(identity) is dict, "identity object")
    kind = identity.get("kind")
    extra = {"name", "uuid", "table", "suspended", "open_count", "dependencies"} if kind == "dm" else {
        "backing_dev", "backing_inode", "offset", "sizelimit"}
    require(kind in ("dm", "loop") and set(identity) == common | extra, "identity schema")
    require(identity["role"] in ("clone", "zero", "data", "metadata"), "identity role")
    devno_pattern = r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)"
    require(type(identity["devno"]) is str and re.fullmatch(devno_pattern, identity["devno"]), "devno")
    require(integer(identity["diskseq"], 1) and integer(identity["size_bytes"], 1), "incarnation/size")
    require(type(identity["readonly"]) is bool and type(identity["holders"]) is list, "identity types")
    require(all(type(x) is str and re.fullmatch(devno_pattern, x) for x in identity["holders"]) and
            len(identity["holders"]) == len(set(identity["holders"])), "holders")
    if kind == "dm":
        require(identity["role"] in ("clone", "zero"), "dm role")
        require(all(type(identity[k]) is str and identity[k] for k in ("name", "uuid", "table")), "dm strings")
        require(type(identity["suspended"]) is bool and integer(identity["open_count"]), "dm state")
        require(type(identity["dependencies"]) is list and
                all(type(x) is str and re.fullmatch(devno_pattern, x) for x in identity["dependencies"]) and
                len(identity["dependencies"]) == len(set(identity["dependencies"])), "dependencies")
    else:
        require(identity["role"] in ("data", "metadata"), "loop role")
        require(all(integer(identity[k]) for k in ("backing_dev", "backing_inode", "offset", "sizelimit")), "loop integers")
        require(identity["backing_inode"] > 0 and identity["offset"] == 0, "loop backing")
    return identity


def validate_boot_id(value):
    require(type(value) is str and re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", value), "boot identity")


def collect_exact_identity_model(expected, backend, boot_id):
    """Two closed, exact synthetic snapshots, queried by devno + incarnation.

    The injected backend supplies already-parsed evidence. No /proc, sysfs, DM
    or loop collector exists here. Two equal snapshots do NOT exclude an ABA
    between samples and do not authorize future I/O or cleanup.
    """
    require(getattr(backend, "model_only", None) is True, "model backend required")
    validate_boot_id(boot_id)
    expected = copy.deepcopy(validate_identity(expected))
    selector = {"kind": expected["kind"], "devno": expected["devno"],
                "diskseq": expected["diskseq"]}
    snapshots = []
    for unused in range(2):
        snapshot = copy.deepcopy(backend.device_snapshot(copy.deepcopy(selector)))
        require(type(snapshot) is dict and set(snapshot) == {
            "boot_id", "kernel", "node_devno", "node_diskseq"}, "snapshot schema")
        validate_boot_id(snapshot["boot_id"])
        require(snapshot["boot_id"] == boot_id, "snapshot boot changed")
        validate_identity(snapshot["kernel"])
        require(snapshot["kernel"] == expected, "kernel identity mismatch")
        require(type(snapshot["node_devno"]) is str and
                snapshot["node_devno"] == expected["devno"] and
                integer(snapshot["node_diskseq"], 1) and
                snapshot["node_diskseq"] == expected["diskseq"], "stale block node")
        snapshots.append(snapshot)
    return {"classification": "MODEL_EXACT_IDENTITY_SNAPSHOTS_MATCH",
            "identity": expected, "snapshot_sha256": sha(canonical(snapshots)),
            "runtime_authorized": False, "continuous_identity_proven": False}


def validate_child(child, request, owner):
    require(type(child) is dict and set(child) == {
        "request_id", "boot_id", "pid", "start_ticks", "argv", "owner_pid", "owner_start_ticks"}, "child schema")
    require(all(integer(child[key], 1) for key in
                ("pid", "start_ticks", "owner_pid", "owner_start_ticks")), "child numeric identity")
    require(child["pid"] != child["owner_pid"], "child cannot be controller")
    require(type(child["argv"]) is list and child["argv"] == request["argv"] and
            child["request_id"] == request["request_id"] and
            child["boot_id"] == request["boot_id"] and
            child["owner_pid"] == owner["pid"] and
            child["owner_start_ticks"] == owner["start_ticks"], "child binding")


def validate_capture(capture):
    require(type(capture) is dict and set(capture) == {
        "eof", "truncated", "bytes", "sha256"}, "capture schema")
    require(capture["eof"] is True and capture["truncated"] is False,
            "held pipe or truncated capture")
    require(integer(capture["bytes"]) and capture["bytes"] <= 1048576 and
            type(capture["sha256"]) is str and re.fullmatch(r"[0-9a-f]{64}", capture["sha256"]), "capture bounds")
    require(capture["bytes"] != 0 or capture["sha256"] == sha(b""), "empty capture digest")


class KernelExecutorModel:
    """One-shot model of a single command, not an executable subprocess runner.

    Backend protocol: capabilities(), spawn_model(request, owner),
    observe_spawn(child), open_pidfd_model(child), wait_exact_model(child,
    pidfd_token, timeout_ms), reap_exact_model(child, pidfd_token, timeout_ms).
    Every method must be bounded by a future live implementation; the model
    cannot interrupt a blocking Python callback. No automatic signal or retry is
    issued on UNKNOWN, and no failure cleanup is inferred. An independent
    admission authority is still needed to prevent a different model instance
    from replaying an attempt. This is NOT duplicate-executor P0 closure.
    """
    def __init__(self, journal, backend, owner, boot_id):
        require(getattr(backend, "model_only", None) is True, "model backend required")
        require(type(owner) is dict and set(owner) == {"pid", "start_ticks"} and
                all(integer(value, 1) for value in owner.values()), "controller identity")
        validate_boot_id(boot_id)
        self.journal, self.backend = journal, backend
        self.owner, self.boot_id = copy.deepcopy(owner), boot_id
        self.state = "NEW"
        self.child = None

    def run(self, request_id, argv, timeout_ms=30000):
        require(self.state == "NEW", "one-shot executor forbids redispatch")
        require(type(request_id) is str and re.fullmatch(r"[0-9a-f]{32}", request_id), "request identity")
        require(type(argv) is list and 0 < len(argv) <= 64 and
                all(type(item) is str and 0 < len(item) <= 4096 and "\x00" not in item for item in argv)
                and argv[0].startswith("/"), "literal absolute command argv")
        require(integer(timeout_ms, 1) and timeout_ms <= 60000, "bounded timeout")
        request = {"schema": 1, "request_id": request_id, "boot_id": self.boot_id,
                   "argv": copy.deepcopy(argv), "timeout_ms": timeout_ms,
                   "command_sha256": sha(canonical(argv)), "runtime_authorized": False}
        try:
            capabilities = copy.deepcopy(self.backend.capabilities())
            require(type(capabilities) is dict and set(capabilities) == {"pidfd", "owned_child_wnowait"} and
                    all(type(value) is bool for value in capabilities.values()) and
                    capabilities["owned_child_wnowait"], "wait capabilities")
            # Any persistence exception poisons the model before spawn.
            self.journal.append({"kind": "EXECUTOR_INTENT", "request": request, "owner": self.owner})
            self.state = "INTENT_PERSISTED"
            # Mark dispatch ambiguous BEFORE entering the callback.
            self.state = "DISPATCH_ISSUED"
            child = copy.deepcopy(self.backend.spawn_model(copy.deepcopy(request), copy.deepcopy(self.owner)))
            validate_child(child, request, self.owner)
            self.child = child
            observed = copy.deepcopy(self.backend.observe_spawn(copy.deepcopy(child)))
            validate_child(observed, request, self.owner)
            require(observed == child, "spawn PID/start/argv identity changed")
            pidfd = None
            method = "OWNED_CHILD_WNOWAIT"
            if capabilities["pidfd"]:
                binding = copy.deepcopy(self.backend.open_pidfd_model(copy.deepcopy(child)))
                require(type(binding) is dict and set(binding) == {"identity", "token"}, "pidfd binding schema")
                validate_child(binding["identity"], request, self.owner)
                require(binding["identity"] == child and integer(binding["token"]), "pidfd binding")
                pidfd = binding["token"]
                method = "PIDFD_WNOWAIT"
            observed = copy.deepcopy(self.backend.observe_spawn(copy.deepcopy(child)))
            validate_child(observed, request, self.owner)
            require(observed == child, "child changed while pinning wait handle")
            self.journal.append({"kind": "EXECUTOR_BOUND", "request_id": request_id,
                                 "child": child, "wait_method": method, "pidfd_token": pidfd})
            self.state = "BOUND"
            waited = copy.deepcopy(self.backend.wait_exact_model(copy.deepcopy(child), pidfd, timeout_ms))
            require(type(waited) is dict and set(waited) == {
                "identity", "wait_method", "pidfd_token", "elapsed_ms", "timed_out",
                "process_state", "unreaped", "returncode", "descendants", "descendants_complete",
                "stdout", "stderr"}, "wait schema")
            validate_child(waited["identity"], request, self.owner)
            require(waited["identity"] == child and waited["wait_method"] == method and
                    type(waited["pidfd_token"]) is type(pidfd) and waited["pidfd_token"] == pidfd,
                    "wait identity/method binding")
            require(integer(waited["elapsed_ms"]) and waited["elapsed_ms"] < timeout_ms and
                    waited["timed_out"] is False, "command timeout")
            require(waited["process_state"] == "Z" and waited["unreaped"] is True,
                    "nonterminal/D-state or already reaped child")
            require(type(waited["returncode"]) is int and -127 <= waited["returncode"] <= 255,
                    "wait returncode")
            require(waited["descendants_complete"] is True and
                    type(waited["descendants"]) is list and waited["descendants"] == [],
                    "live/unknown descendants")
            validate_capture(waited["stdout"])
            validate_capture(waited["stderr"])
            # No reap until exact owned-child terminal + pipe/descendant proof.
            remaining_ms = timeout_ms - waited["elapsed_ms"]
            reaped = copy.deepcopy(self.backend.reap_exact_model(copy.deepcopy(child), pidfd, remaining_ms))
            require(type(reaped) is dict and set(reaped) == {
                "identity", "wait_method", "pidfd_token", "reaped", "returncode", "elapsed_ms"}, "reap schema")
            validate_child(reaped["identity"], request, self.owner)
            require(reaped["identity"] == child and reaped["wait_method"] == method and
                    type(reaped["pidfd_token"]) is type(pidfd) and reaped["pidfd_token"] == pidfd and
                    reaped["reaped"] is True and type(reaped["returncode"]) is int and
                    reaped["returncode"] == waited["returncode"] and
                    integer(reaped["elapsed_ms"]) and reaped["elapsed_ms"] <= remaining_ms,
                    "exact bounded reap")
            require(waited["returncode"] == 0, "command failed; outcome needs separate reconciliation")
            result = {"classification": "MODEL_EXECUTOR_TERMINAL_EXIT0", "child": child,
                      "wait_method": method, "wait": copy.deepcopy(waited), "reap": copy.deepcopy(reaped),
                      "runtime_authorized": False, "postcondition_verified": False}
            self.journal.append({"kind": "EXECUTOR_TERMINAL", "result": result})
            self.state = "TERMINAL"
            return result
        except BaseException as exc:
            self.state = "UNKNOWN"
            try:
                self.journal.append({"kind": "EXECUTOR_UNKNOWN_PRESERVE", "request": request,
                                     "child": self.child, "error": str(exc),
                                     "retry_dispatched": False, "cleanup_dispatched": False})
            except BaseException as journal_exc:
                exc.add_note("Failure evidence unavailable: " + str(journal_exc))
            raise


def cleanup_next(owned, observed, role, removed=None, terminal_proven=False):
    """Return ONE proposed action, not execute it. Reobserve after every action.

    Only safe after independent command/probe terminal proof. No inference of
    disappearance or quiescence from an absent process name or timeout.
    """
    require(terminal_proven is True, "terminal executors not proven")
    require(set(owned) == {"clone", "zero", "data", "metadata"}, "owned graph roles")
    for key, identity in owned.items():
        validate_identity(identity)
        require(identity["role"] == key, "owned role")
    require(len({identity["devno"] for identity in owned.values()}) == 4, "aliased roles")
    require(sorted(owned["clone"]["dependencies"]) ==
            sorted(owned[key]["devno"] for key in ("zero", "data", "metadata")), "clone graph")
    require(owned["zero"]["dependencies"] == [] and owned["zero"]["readonly"] is True and
            all(owned[key]["readonly"] is False for key in ("clone", "data", "metadata")), "graph access modes")
    removed = {} if removed is None else removed
    require(set(removed) <= set(owned) and not set(removed) & set(observed), "removal evidence roles")
    for key, proof_identity in removed.items():
        require(proof_identity == owned[key], "removal evidence identity")
    require(role in owned and set(observed) <= set(owned), "observed roles")
    for key, current in observed.items():
        expected = validate_identity(owned[key])
        validate_identity(current)
        require(expected["role"] == current["role"] == key, "graph role binding")
        # Open count/holders are expected to decrease during teardown, never silently ignored below.
        stable = set(expected) - {"open_count", "holders"}
        require(all(current[k] == expected[k] for k in stable), "foreign or recycled identity")
    require(role in observed, "absence needs separate exact removal evidence")
    current = observed[role]
    require(current["holders"] == [], "held resource")
    if role == "clone":
        require(current["open_count"] == 0 and not current["suspended"], "clone not quiescent")
    else:
        require("clone" not in observed, "clone still present")
        require("clone" in removed, "clone absence unproven")
        if role == "zero":
            require(current["open_count"] == 0, "zero source open")
        else:
            require("zero" not in observed, "zero source still present")
            require("zero" in removed, "source absence unproven")
    return {"operation": "REMOVE_EXACT_DM" if current["kind"] == "dm" else "DETACH_EXACT_LOOP",
            "identity": copy.deepcopy(current), "executable": False,
            "requires": "terminal executors plus proven predecessor removal; then reobserve"}


class Controller:
    """Mockable ordering model; never interpreted as live authorization."""
    def __init__(self, journal, guard_model=None):
        self.journal = journal
        require(guard_model is None or type(guard_model) is GuardEpochModel,
                "guard model type")
        self.guard_model = guard_model
        self.state = "UNPUBLISHED"
        self.identity = None
        self.results = {case: "NOT_RUN" for case in CASES}

    def _unknown(self):
        self.state = "UNKNOWN"
        if self.guard_model is not None:
            self.guard_model.invalidate()

    def persist(self, event):
        try:
            return self.journal.append(event)
        except BaseException:
            self._unknown()
            raise

    def action(self, operation, callback):
        require(self.state != "UNKNOWN", "UNKNOWN forbids further dispatch")
        require(type(operation) is str and operation, "operation")
        try:
            self.persist({"kind": "INTENT", "operation": operation})
            result = callback()
            self.persist({"kind": "OBSERVATION", "operation": operation, "result": result})
            return result
        except BaseException as exc:
            self._unknown()
            try:
                self.journal.append({"kind": "UNKNOWN_PRESERVE", "operation": operation,
                                     "error": str(exc), "cleanup_dispatched": False})
            except BaseException as journal_exc:
                exc.add_note("Failure evidence unavailable: " + str(journal_exc))
            raise

    def publish_model(self, expected, observed, guard_receipt):
        if self.state in ("PUBLISHING", "WITHDRAWING"):
            self._unknown()
            raise Refusal("reentrant publication operation")
        require(self.state == "UNPUBLISHED", "publication state")
        require(type(self.guard_model) is GuardEpochModel,
                "guard epoch authority required")
        validate_identity(expected)
        validate_identity(observed)
        require(expected == observed and expected["role"] == "clone" and
                not expected["readonly"] and not expected["suspended"] and
                expected["open_count"] == 0, "exact active writable unopened clone")
        require(expected == self.guard_model.fixture["roles"]["clone"],
                "controller and guard graph differ")
        expected_snapshot = copy.deepcopy(expected)
        observed_snapshot = copy.deepcopy(observed)
        guard_snapshot = copy.deepcopy(guard_receipt)
        fixture_snapshot = copy.deepcopy(self.guard_model.fixture)
        epoch_snapshot = self.guard_model.epoch
        digest_snapshot = self.guard_model.digest
        self.state = "PUBLISHING"
        try:
            self.guard_model.accept_guard(guard_snapshot)
            self.persist({"kind": "MODEL_GUARD_EVIDENCE",
                          "identity": copy.deepcopy(observed_snapshot),
                          "guard": copy.deepcopy(guard_snapshot),
                          "runtime_authorized": False})
            require(self.state == "PUBLISHING"
                    and self.guard_model.state == "GUARD_VERIFIED"
                    and self.guard_model.epoch == epoch_snapshot
                    and self.guard_model.digest == digest_snapshot
                    and self.guard_model.fixture == fixture_snapshot
                    and self.guard_model.guard == guard_snapshot
                    and expected == expected_snapshot
                    and observed == observed_snapshot,
                    "publication state changed during persistence")
            result = self.guard_model.publish()
            require(result["classification"] ==
                    "MODEL_GUARD_PUBLICATION_ORDER_VALID"
                    and all(result[key] is False for key in _GUARD_MODULE.LIMITS),
                    "guard publication result")
            self.identity = expected_snapshot
            self.state = "MODEL_PUBLISHED"
        except _GUARD_MODULE.Refusal as exc:
            self._unknown()
            raise Refusal("guard epoch refused publication") from exc
        except BaseException:
            self._unknown()
            raise

    def withdraw_model(self, terminal, open_count):
        if self.state in ("PUBLISHING", "WITHDRAWING"):
            self._unknown()
            raise Refusal("reentrant publication operation")
        require(self.state == "MODEL_PUBLISHED" and terminal is True and
                type(open_count) is int and open_count == 0, "nonterminal probes")
        identity_snapshot = copy.deepcopy(self.identity)
        fixture_snapshot = copy.deepcopy(self.guard_model.fixture)
        guard_snapshot = copy.deepcopy(self.guard_model.guard)
        epoch_snapshot = self.guard_model.epoch
        digest_snapshot = self.guard_model.digest
        self.state = "WITHDRAWING"
        try:
            self.persist({"kind": "MODEL_WITHDRAW", "runtime_authorized": False})
            require(self.state == "WITHDRAWING"
                    and self.guard_model.state == "MODEL_PUBLISHED"
                    and self.identity == identity_snapshot
                    and self.guard_model.epoch == epoch_snapshot
                    and self.guard_model.digest == digest_snapshot
                    and self.guard_model.fixture == fixture_snapshot
                    and self.guard_model.guard == guard_snapshot,
                    "withdrawal state changed during persistence")
            self.guard_model.withdraw(terminal, open_count)
            self.state = "UNPUBLISHED"
        except _GUARD_MODULE.Refusal as exc:
            self._unknown()
            raise Refusal("guard epoch refused withdrawal") from exc
        except BaseException:
            self._unknown()
            raise

    def record_case(self, case, result):
        require(case in CASES and result in ("MODEL_PASS", "FAIL", "UNKNOWN", "NOT_RUN"), "case result")
        require(self.state != "UNKNOWN" or result in ("FAIL", "UNKNOWN"), "unknown case")
        self.persist({"kind": "CASE", "case": case, "result": result})
        self.results[case] = result


def ioctl_number(fd, request, fmt):
    buf = bytearray(struct.calcsize(fmt))
    fcntl.ioctl(fd, request, buf, True)
    return struct.unpack(fmt, buf)[0]


def fd_identity(fd, expected, writable=False, direct=False):
    require(type(expected) is dict and set(expected) == {"major", "minor", "diskseq", "size_bytes", "logical_sector"}, "FD schema")
    require(all(integer(v) for v in expected.values()) and expected["diskseq"] > 0 and
            expected["size_bytes"] > 0 and expected["logical_sector"] in (512, 4096), "FD values")
    info = os.fstat(fd)
    require(stat.S_ISBLK(info.st_mode), "not a block FD")
    require((os.major(info.st_rdev), os.minor(info.st_rdev)) == (expected["major"], expected["minor"]), "FD devno mismatch")
    for field, request, fmt in (("diskseq", BLKGETDISKSEQ, "=Q"), ("size_bytes", BLKGETSIZE64, "=Q"), ("logical_sector", BLKSSZGET, "=I")):
        require(ioctl_number(fd, request, fmt) == expected[field], "FD " + field + " mismatch")
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if writable:
        require(flags & os.O_ACCMODE == os.O_RDWR, "discard needs O_RDWR")
        require(ioctl_number(fd, BLKROGET, "=I") == 0, "read-only block device")
    if direct:
        require(flags & os.O_DIRECT and flags & os.O_ACCMODE != os.O_WRONLY, "direct readable FD required")


def discard_exact(fd, identity, offset, length):
    fd_identity(fd, identity, writable=True)
    sector = identity["logical_sector"]
    require(integer(offset) and integer(length, 1) and offset % sector == 0 and
            length % sector == 0 and offset + length <= identity["size_bytes"], "discard range")
    error = 0
    try:
        fcntl.ioctl(fd, BLKDISCARD, struct.pack("=QQ", offset, length))
    except OSError as exc:
        error = exc.errno
        require(type(error) is int and error > 0, "unknown discard errno")
    fd_identity(fd, identity, writable=True)
    return {"errno": error, "offset": offset, "length": length,
            "guard_rejection": error == errno.EOPNOTSUPP}


def direct_oracle(fd, identity, fill_byte, overlays=()):
    """Full aligned direct read, compared to independently specified bytes.

    overlays is a sorted non-overlapping sequence of (offset, bytes), e.g. the
    specified ordinary write, never an image read back from the tested device.
    """
    fd_identity(fd, identity, direct=True)
    require(type(fill_byte) is int and 0 <= fill_byte <= 255, "oracle byte")
    require(type(overlays) in (tuple, list), "oracle overlays must be replayable")
    overlays = tuple(overlays)
    size = identity["size_bytes"]
    require(size <= 134217728 and size % 4096 == 0, "bounded oracle geometry")
    end = 0
    for offset, data in overlays:
        require(integer(offset) and type(data) is bytes and len(data) > 0 and
                offset >= end and offset + len(data) <= size, "oracle overlay")
        end = offset + len(data)
    digest = hashlib.sha256()
    chunk = min(1048576, size)
    with mmap.mmap(-1, chunk) as buffer:
        for position in range(0, size, chunk):
            count = min(chunk, size - position)
            view = memoryview(buffer)[:count]
            try:
                actual = os.preadv(fd, [view], position)
                require(actual == count, "short direct read")
                expected = bytearray([fill_byte]) * count
                for offset, data in overlays:
                    left, right = max(position, offset), min(position + count, offset + len(data))
                    if left < right:
                        expected[left - position:right - position] = data[left - offset:right - offset]
                require(view == expected, "direct oracle mismatch")
                digest.update(view)
            finally:
                view.release()
    fd_identity(fd, identity, direct=True)
    return {"bytes": size, "sha256": digest.hexdigest(), "direct": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute-token")
    args = parser.parse_args(argv)
    result = plan(secrets.token_hex(16))
    if args.execute_token is not None:
        result["classification"] = "REFUSED_EXECUTE_BACKEND_NOT_QUALIFIED"
        result["token_valid"] = args.execute_token == ACK
        print(canonical(result).decode("ascii"), end="")
        return 2
    print(canonical(result).decode("ascii"), end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
