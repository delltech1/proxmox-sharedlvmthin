#!/usr/bin/env python3
"""Unshipped one-effect local restoration component. NO CLI/default authority.

The mandatory trusted authorizer is deliberately NOT implemented here. It
must validate transport, explicit operator authorization, the full release
chain, baseline, all participants and current zero-consumer evidence. Caller
JSON alone never enables effects. A partial intent is inspection-only forever;
observing the desired state is not proof that our prior command succeeded.
"""
import copy
import importlib.util
import os
from pathlib import Path
import socket
import subprocess
import time

HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
    return value


C = load("local_restore_collector", "layout-migration-exact-baseline-collector.py")
M, J = C.M, C.J
Refusal, need = M.Refusal, M.need
ROOT = Path("/var/lib/pve-sharedlvmthin/maintenance-restore")
INSTALLED = Path("/usr/libexec/pve-sharedlvmthin")
FILES = ("layout-migration-exact-local-restore.py", "layout-migration-exact-baseline-collector.py",
         "layout-migration-exact-local-journal.py", "layout-migration-exact-restore-model.py",
         "layout-migration-barrier-collector.py", "layout-migration-barrier-backend.py",
         "layout-migration-barrier-model.py", "layout-migration-barrier-file-journal.py",
         "layout-migration-node-evidence.py", "layout-migration-workload-gate.py",
         "prelive_exact_journal_file_backend.py")
# Only a future reviewed fresh-process loader may populate this in-process
# mapping after bracketing requires. No environment or caller JSON fallback.
LOADED_SOURCE_SHA256 = None


class LocalJournal(J.Journal):
    def __init__(self, plan, *, create=False, fault=None):
        need(os.getuid() == os.geteuid() == 0, "root required")
        self._open(plan, ROOT, create, fault, test_anchor=None)


def installed_identity():
    """Cannot succeed from an experiment tree; no candidate self-authorization."""
    need(Path(__file__).absolute() == INSTALLED / FILES[0], "executor is not installed at the fixed source path")
    need(type(LOADED_SOURCE_SHA256) is dict and set(LOADED_SOURCE_SHA256) == set(FILES)
         and all(M.sha(v) for v in LOADED_SOURCE_SHA256.values()), "fresh-process source boundary absent")
    pins = C.Pins()
    try:
        directory = pins.directory(str(INSTALLED))
        sources = {}
        for name in FILES:
            raw, identity = C.read_regular(directory, name)
            need(C.hashlib.sha256(raw).hexdigest() == LOADED_SOURCE_SHA256[name], "loaded/installed source drift")
            sources[name] = {"sha256": C.hashlib.sha256(raw).hexdigest(), "identity": identity}
        package = pins.directory("/usr/share/pve-sharedlvmthin")
        values = {}
        for name in ("runtime-build-id", "package-artifact-sha256"):
            raw, identity = C.read_regular(package, name, 65)
            need(len(raw) == 65 and raw.endswith(b"\n") and M.sha(raw[:-1].decode("ascii")), "package identity malformed")
            values[name] = {"sha256": raw[:-1].decode("ascii"), "identity": identity}
        flavor, flavor_identity = C.read_regular(package, "package-flavor", 32)
        need(flavor in (b"dual\n", b"thick-only\n"), "package profile malformed")
        pins.check()
        return {"schema": "slt-local-restore-source/v1", "authority": "NONE", "sources": sources,
                "package": values, "flavor": flavor.decode().strip(), "flavor_identity": flavor_identity}
    finally: pins.close()


def process_identity():
    raw = Path("/proc/self/stat").read_text()
    tail = raw.rsplit(") ", 1)[1].split()
    need(len(tail) > 19 and tail[19].isdigit(), "executor process identity unknown")
    return {"pid": os.getpid(), "starttime": int(tail[19])}


def observe_unit(unit):
    need(unit in M.MASKED_BY_BARRIER, "unit outside exact restoration set")
    pins = C.Pins()
    try:
        namespace = C.namespace(pins)
        rows = C.C.services()
        selected = [row for row in rows if row["unit"] == unit]
        need(len(selected) == 1, "unit lifecycle coverage")
        row = selected[0]
        need(row["job"] is None and row["control_pid"] == 0 and row["active_state"] in ("active", "inactive"),
             "unit pending/unknown")
        if row["active_state"] == "inactive":
            need(row["sub_state"] == "dead" and row["main_pid"] == 0 and row["cgroup_empty"] is True,
                 "inactive unit retains an executor")
        else:
            need(row["sub_state"] == ("exited" if unit == "pve-guests.service" else "running"), "active unit substate")
            need((row["main_pid"] == 0 and row["cgroup_empty"] is True) if unit == "pve-guests.service"
                 else (row["main_pid"] > 1 and row["cgroup_empty"] is False), "active executor identity unknown")
        need(C.namespace(pins) == namespace, "unit source namespace changed")
        return {"active": row["active_state"] == "active", "masked": row["mask_target"] is not None,
                "substate": row["sub_state"], "source": {key: namespace[key] for key in (
                    "/etc/systemd/system/" + unit, "/run/systemd/system/" + unit,
                    "/usr/lib/systemd/system/" + unit, "effective:" + unit)}}
    finally: pins.close()


def run_closed(operation, unit):
    need(operation in ("UNMASK", "START") and unit in M.MASKED_BY_BARRIER
         and (operation != "START" or unit in M.TARGETS), "effect outside closed vocabulary")
    argv = (["/usr/bin/systemctl", "unmask", "--", unit] if operation == "UNMASK" else
            ["/usr/bin/systemctl", "--job-mode=fail", "start", "--", unit])
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False, env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"})
    need(result.returncode == 0 and len(result.stdout) <= 65536 and len(result.stderr) <= 65536,
         "systemctl result is UNKNOWN")
    return {"exit_code": 0, "stdout_sha256": C.hashlib.sha256(result.stdout).hexdigest(),
            "stderr_sha256": C.hashlib.sha256(result.stderr).hexdigest()}


def confirmed_command(result):
    need(type(result) is dict and set(result) == {"exit_code", "stdout_sha256", "stderr_sha256"}
         and type(result["exit_code"]) is int and result["exit_code"] == 0
         and M.sha(result["stdout_sha256"]) and M.sha(result["stderr_sha256"]), "command outcome unknown")


class Executor:
    def __init__(self, plan, journal, *, authorize=None, baseline_reader=None, identity_reader=installed_identity,
                 unit_reader=observe_unit, command=run_closed, clock=time.time, node_reader=socket.gethostname,
                 boot_reader=lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                 process_reader=process_identity):
        self.plan, self.journal = copy.deepcopy(plan), journal
        self.authorize, self.baseline_reader = authorize, baseline_reader
        self.identity_reader, self.unit_reader, self.command = identity_reader, unit_reader, command
        self.clock, self.node_reader, self.boot_reader, self.process_reader = clock, node_reader, boot_reader, process_reader
        self.used = False
        self.owner_pid = os.getpid()

    def execute(self, request):
        need(os.getuid() == os.geteuid() == 0, "root required")
        need(not self.used and os.getpid() == self.owner_pid, "one request per executor; no retry/fork")
        self.used = True
        need(callable(self.authorize) and callable(self.baseline_reader), "production authorization/baseline integration absent")
        request = copy.deepcopy(request)
        M.exact(request, {"tx", "plan_sha256", "node", "boot_id", "unit", "operation", "baseline_sha256", "release_sha256"}, "request")
        M.validate_plan(self.plan, self.plan["issued_at"])
        need(request["tx"] == self.plan["tx"] and request["plan_sha256"] == M.digest(self.plan)
             and request["node"] == self.node_reader() and request["boot_id"] == self.boot_reader()
             and M.sha(request["baseline_sha256"]) and M.sha(request["release_sha256"])
             and request["unit"] in M.MASKED_BY_BARRIER and request["operation"] in ("UNMASK", "START")
             and (request["operation"] != "START" or request["unit"] in M.TARGETS), "request identity/effect invalid")
        rows = [row for row in self.plan["participants"] if row["node"] == request["node"]]
        need(len(rows) == 1 and rows[0]["boot_id"] == request["boot_id"], "participant identity differs")
        identity = self.identity_reader()
        need(type(identity) is dict and identity.get("authority") == "NONE"
             and identity.get("schema") == "slt-local-restore-source/v1"
             and identity.get("package", {}).get("package-artifact-sha256", {}).get("sha256")
                == rows[0]["after_package_sha256"], "source/package identity unavailable")
        key = request["node"] + ":" + request["unit"] + ":" + request["operation"]
        intent = self.journal.read(request["tx"], key + ":intent")
        outcome = self.journal.read(request["tx"], key + ":done")
        if intent is not None or outcome is not None:
            M.exact(intent, {"schema", "authority", "request", "executor_identity", "before", "grant", "process"}, "stored intent")
            need(type(intent) is dict and intent.get("request") == request and intent.get("executor_identity") == identity,
                 "prior attempt identity differs")
            need(intent["schema"] == "slt-local-restore-intent/v1" and intent["authority"] == "NONE", "stored intent schema")
            if outcome is None:
                return {"authority": "NONE", "state": "UNKNOWN_RETAIN", "request_sha256": M.digest(request),
                        "effect_retry_allowed": False, "intent_sha256": M.digest(intent)}
            M.exact(outcome, {"schema", "authority", "state", "request_sha256", "intent_sha256", "after", "command",
                             "started_at", "finished_at", "effect_retry_allowed"}, "stored completion")
            need(outcome["schema"] == "slt-local-restore-receipt/v1" and outcome["effect_retry_allowed"] is False
                 and type(outcome["started_at"]) is int and type(outcome["finished_at"]) is int
                 and intent["grant"]["issued_at"] <= outcome["started_at"] <= intent["grant"]["expires_at"]
                 and outcome["started_at"] <= outcome["finished_at"]
                 and outcome.get("intent_sha256") == M.digest(intent)
                 and outcome.get("request_sha256") == M.digest(request) and outcome.get("state") == "LOCAL_EFFECT_CONFIRMED"
                 and outcome.get("authority") == "NONE" and outcome.get("after") == self.unit_reader(request["unit"])
                 and M.digest(outcome["after"]["source"]) == intent["grant"]["after_source_sha256"],
                 "completed receipt/state differs")
            confirmed_command(outcome["command"])
            need(self.identity_reader() == identity and self.boot_reader() == request["boot_id"], "reconciliation identity drift")
            return copy.deepcopy(outcome)

        now = int(self.clock()); M.validate_plan(self.plan, now)
        baseline = self.baseline_reader()
        need(type(baseline) is dict and M.digest(baseline) == request["baseline_sha256"]
             and baseline.get("authority") == "NONE" and baseline.get("plan_sha256") == request["plan_sha256"], "baseline binding differs")
        validator = object.__new__(M.Coordinator); validator.plan = self.plan
        original = baseline["records"][request["node"]]
        validator.validate_record(original, request["node"], before=True)
        desired = original["services"][request["unit"]]
        need(not desired["masked"] and (request["operation"] != "START" or desired["active"]), "baseline forbids effect")
        before = self.unit_reader(request["unit"])
        need(type(before) is dict and type(before.get("active")) is bool and type(before.get("masked")) is bool
             and type(before.get("source")) is dict, "unit observation incomplete")
        need(before["masked"] if request["operation"] == "UNMASK" else not before["active"] and not before["masked"],
             "effect predecessor differs")
        need(before["active"] == desired["active"] if request["unit"] == "pve-guests.service" else not before["active"],
             "unit is not the exact drained/preserved predecessor")
        grant = self.authorize(copy.deepcopy(request), copy.deepcopy(identity), copy.deepcopy(before))
        M.exact(grant, {"schema", "request_sha256", "executor_sha256", "before_sha256", "after_source_sha256", "issued_at", "expires_at",
                        "allowed_effect", "authorization_sha256"}, "explicit grant")
        need(grant["schema"] == "slt-local-restore-explicit-grant/v1"
             and grant["request_sha256"] == M.digest(request) and grant["executor_sha256"] == M.digest(identity)
             and grant["before_sha256"] == M.digest(before) and grant["allowed_effect"] == request["operation"]
             and M.sha(grant["after_source_sha256"])
             and M.sha(grant["authorization_sha256"]) and type(grant["issued_at"]) is int
             and type(grant["expires_at"]) is int and grant["issued_at"] <= now <= grant["expires_at"]
             and 0 < grant["expires_at"] - grant["issued_at"] <= 300, "explicit grant invalid")
        process = self.process_reader()
        M.exact(process, {"pid", "starttime"}, "process")
        need(type(process["pid"]) is int and 1 < process["pid"] < 2 ** 31
             and type(process["starttime"]) is int and 0 < process["starttime"] < 2 ** 63, "process identity invalid")
        intent = {"schema": "slt-local-restore-intent/v1", "authority": "NONE", "request": request,
                  "executor_identity": identity, "before": before, "grant": grant, "process": process}
        ack = self.journal.create_once(request["tx"], key + ":intent", intent)
        need(ack == {"durable": True, "sha256": M.digest(intent)}, "intent not durably acknowledged")
        check = int(self.clock()); M.validate_plan(self.plan, check)
        need(now <= check <= grant["expires_at"] and self.identity_reader() == identity
             and self.node_reader() == request["node"] and self.boot_reader() == request["boot_id"]
             and self.baseline_reader() == baseline and self.unit_reader(request["unit"]) == before
             and self.authorize(copy.deepcopy(request), copy.deepcopy(identity), copy.deepcopy(before)) == grant
             and self.process_reader() == process and os.getpid() == self.owner_pid
             and self.journal.read(request["tx"], key + ":intent") == intent,
             "pre-effect identity/authorization/namespace drift")
        started = int(self.clock()); M.validate_plan(self.plan, started)
        need(check <= started <= grant["expires_at"], "authorization expired immediately before effect")
        result = self.command(request["operation"], request["unit"])
        confirmed_command(result)
        after = self.unit_reader(request["unit"])
        need(type(after) is dict and after.get("masked") is False
             and (after.get("active") is True if request["operation"] == "START" else after.get("active") == before["active"]),
             "effect target unknown")
        need(type(after.get("source")) is dict and M.digest(after["source"]) == grant["after_source_sha256"],
             "effect source target differs from authorization")
        for path in ("/usr/lib/systemd/system/", "/run/systemd/system/"):
            need(after["source"].get(path + request["unit"]) == before["source"].get(path + request["unit"]),
                 "vendor/runtime source changed during effect")
        if request["operation"] == "START":
            need(after["source"] == before["source"], "START changed unit source")
        need(self.identity_reader() == identity and self.boot_reader() == request["boot_id"]
             and self.process_reader() == process and self.journal.read(request["tx"], key + ":intent") == intent,
             "post-effect source/boot/process/intent drift")
        finished = int(self.clock()); need(finished >= started, "post-effect clock regressed")
        outcome = {"schema": "slt-local-restore-receipt/v1", "authority": "NONE", "state": "LOCAL_EFFECT_CONFIRMED",
                   "request_sha256": M.digest(request), "intent_sha256": M.digest(intent), "after": after,
                   "command": result, "started_at": started, "finished_at": finished, "effect_retry_allowed": False}
        ack = self.journal.create_once(request["tx"], key + ":done", outcome)
        need(ack == {"durable": True, "sha256": M.digest(outcome)}, "outcome not durably acknowledged")
        return outcome
