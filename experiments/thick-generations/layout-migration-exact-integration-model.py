#!/usr/bin/env python3
"""UNSHIPPED executable integration MODEL, never a production coordinator.

Only closed in-process simulated endpoints exist. No SSH/systemctl process is
launched and no storage/package/config operation exists. A POSIX test may use
the real local journal in an explicit private disposable directory. Transport
and executor dependencies are private methods, not user-supplied callbacks.
Native entry/archive effects remain simulated: their server is not qualified.
"""
import builtins
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import types

HERE = Path(__file__).resolve().parent
FILES = (
    "layout-migration-exact-integration-model.py",
    "layout-migration-exact-grant-provider-model.py",
    "layout-migration-exact-restore-model.py", "layout-migration-exact-local-journal.py",
    "layout-migration-exact-transport.py", "layout-migration-exact-local-restore.py",
    "layout-migration-exact-release-validator.py", "layout-migration-exact-baseline-collector.py",
    "layout-migration-barrier-collector.py", "layout-migration-barrier-backend.py",
    "layout-migration-barrier-model.py", "layout-migration-barrier-file-journal.py",
    "prelive_exact_journal_file_backend.py", "layout-migration-node-evidence.py",
    "layout-migration-workload-gate.py", "layout-migration-all-certified-v2.py",
    "layout-migration-release-certificate-v2.py", "layout-migration-all-ready-v2.py",
    "layout-migration-all-configured-v2.py", "layout-migration-v2-context.py",
    "layout-migration-topology-v2.py", "layout-migration-plan.py",
    "layout-migration-release-certificate.py", "layout-migration-all-ready-plan.py",
    "layout-migration-finalize-plan.py",
)


class Refusal(ValueError): pass


def need(value, message):
    if not value: raise Refusal(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class Sources:
    """Fresh modules from a closed immutable source closure, never disk reload."""
    def __init__(self, sources, expected):
        need(type(sources) is dict and type(expected) is dict and set(sources) == set(expected) == set(FILES), "source closure differs")
        for name, raw in sources.items():
            need(type(raw) is bytes and 0 < len(raw) <= 2 * 1024 * 1024 and hashlib.sha256(raw).hexdigest() == expected[name], "source hash differs")
        self.raw = types.MappingProxyType(dict(sources))
        self.hashes = types.MappingProxyType(dict(expected))
        self.importlib = types.ModuleType("importlib")
        self.importlib.util = types.SimpleNamespace(spec_from_file_location=self.spec, module_from_spec=importlib.util.module_from_spec,
                                                   spec_from_loader=importlib.util.spec_from_loader)

    def importer(self, name, globals=None, locals=None, fromlist=(), level=0):
        if name == "importlib.util" and not level:
            return self.importlib.util if fromlist else self.importlib
        if os.name != "posix" and name in ("fcntl", "resource"):
            # No method exists: accidentally invoking a POSIX operation fails.
            return types.ModuleType(name)
        return builtins.__import__(name, globals, locals, fromlist, level)

    def spec(self, name, path):
        path = Path(path)
        need(path.parent == HERE and path.name in self.raw, "source import escaped closure")
        owner = self
        class Loader:
            def create_module(self, spec): return None
            def exec_module(self, module):
                need(hashlib.sha256(owner.raw[path.name]).hexdigest() == owner.hashes[path.name], "source pin drift")
                module.__file__ = str(path)
                module.__frozen_source_sha256__ = owner.hashes[path.name]
                module.__builtins__ = {**vars(builtins), "__import__": owner.importer}
                exec(compile(owner.raw[path.name], str(path), "exec"), module.__dict__)
        return importlib.util.spec_from_loader(name, Loader(), origin=str(path))

    def load(self, filename):
        spec = self.spec("integration_" + filename.replace("-", "_"), HERE / filename)
        value = importlib.util.module_from_spec(spec); spec.loader.exec_module(value)
        return value


class Cut(Exception): pass


class Prefixes:
    """Closed deterministic fault selector, not a callback/command surface."""
    def __init__(self, cut=None):
        need(cut is None or type(cut) is int and cut >= 0, "fault prefix must be an index")
        self.cut, self.trace, self.audit = cut, [], []

    def hit(self, name, payload=None):
        index = len(self.trace); self.trace.append(name)
        self.audit.append({"event": name, "payload": copy.deepcopy(payload)})
        if self.cut == index: raise Cut(name)


class MemoryJournal:
    """Portable fault MODEL only. Never claims actual filesystem durability."""
    def __init__(self, J, plan):
        self.J, self.plan, self.tx = J, copy.deepcopy(plan), plan["tx"]
        self.records = {}
        self.rules = object.__new__(J.Journal); self.rules.tx, self.rules.plan = self.tx, self.plan

    def read(self, tx, key):
        self.rules._name(tx, key)
        return copy.deepcopy(self.records.get(key))

    def create_once(self, tx, key, value):
        self.rules._name(tx, key)
        need(key not in self.records, "model create-only conflict")
        self.records[key] = self.J.decode(self.J.canonical(value) + b"\n")
        return {"durable": True, "sha256": digest(value)}

    def close(self): pass


class JournalPort:
    def __init__(self, journal, prefixes, label):
        self.journal, self.prefixes, self.label = journal, prefixes, label

    def read(self, tx, key): return self.journal.read(tx, key)

    def create_once(self, tx, key, value):
        name = self.label + ":" + key
        record = {"journal": self.label, "tx": tx, "key": key, "value": value}
        self.prefixes.hit("journal-before:" + name, record)
        ack = self.journal.create_once(tx, key, value)
        self.prefixes.hit("journal-stored:" + name, record)
        need(ack == {"durable": True, "sha256": digest(value)}, "journal ACK differs")
        self.prefixes.hit("journal-ack:" + name, record)
        return ack


class Pins:
    def __init__(self, owner, pins):
        self.owner, self.plan, self.pins = owner, copy.deepcopy(owner.plan), copy.deepcopy(pins)
        self.hash = digest(self.pins)

    def check(self):
        need(digest(self.pins) == self.hash, "transport source pin drift")
        self.owner.check()

    def load(self):
        self.check(); return copy.deepcopy((self.plan, self.pins))


class IntegrationModel:
    def __init__(self):
        raise Refusal("no production coordinator/server subsystem integration exists")

    @classmethod
    def for_simulation(cls, evidence, pins_before, pins_after, sources, expected, *, now, journal_root=None, cut=None):
        need(globals().get("__frozen_source_sha256__") == expected.get("layout-migration-exact-integration-model.py")
             and globals().get("__frozen_source_sha256__") is not None, "orchestrator must itself be fresh-loaded from pinned bytes")
        self = object.__new__(cls)
        self.code = Sources(sources, expected)
        self.M = self.code.load("layout-migration-exact-restore-model.py")
        self.T = self.code.load("layout-migration-exact-transport.py")
        self.E = self.code.load("layout-migration-exact-local-restore.py")
        self.V = self.code.load("layout-migration-exact-release-validator.py")
        self.G = self.code.load("layout-migration-exact-grant-provider-model.py")
        self.B = self.code.load("layout-migration-barrier-model.py")
        self.J = self.code.load("layout-migration-exact-local-journal.py")
        # Only this private MODEL module sees root/process identity simulation.
        self.E.os = types.SimpleNamespace(getuid=lambda: 0, geteuid=lambda: 0, getpid=os.getpid)
        self.evidence, self.plan = copy.deepcopy(evidence), copy.deepcopy(evidence["plan"])
        self.plan_hash, self.evidence_hash, self.code_hash = digest(self.plan), digest(self.evidence), digest(dict(self.code.hashes))
        self.time = now; self.last_time = now
        self.M.validate_plan(self.plan, now)
        names = [r["node"] for r in self.plan["participants"]]
        for value in (evidence["baseline"]["records"], evidence["baseline_sources"], evidence["baseline_captures"], evidence["current"]):
            need(type(value) is dict and set(value) == set(names), "partial baseline/current cohort")
        need(type(evidence["archives"]) is dict and set(evidence["archives"]) == set(names[:3]), "partial SAN archive cohort")
        self.T.validate_pins(self.plan, pins_before); self.T.validate_pins(self.plan, pins_after)
        self.pins_before, self.pins_after = copy.deepcopy(pins_before), copy.deepcopy(pins_after)
        for before, after, row in zip(pins_before["participants"], pins_after["participants"], self.plan["participants"]):
            need(before["source_package"]["artifact_sha256"] == row["before_package_sha256"]
                 and after["source_package"]["artifact_sha256"] == row["after_package_sha256"], "before/after transport package phase")
            need(all(before[k] == after[k] for k in before if k != "source_package"), "transport peer/helper changed across package phase")
            need(before["source_package"]["sources_sha256"] == after["source_package"]["sources_sha256"] == self.code_hash,
                 "transport source cohort does not bind the actual immutable closure")
            if row["role"] == "CONTROL_ONLY": need(before == after, "control-only package/source changed")
        self.records = copy.deepcopy(evidence["baseline"]["records"])
        self.namespaces = copy.deepcopy(evidence["baseline_sources"])
        self.baseline = copy.deepcopy(evidence["baseline"])
        self.trace = Prefixes(cut)
        self.effects, self.receipts, self.nonces, self.journals = [], {}, 0, {}
        self.used, self.poisoned, self.stage = False, False, "NEW"
        self.owner_pid = os.getpid()
        self.pending = None
        journal_class = self.J.Journal
        try:
            if journal_root is not None:
                root = Path(journal_root)
                need(os.name == "posix" and root.is_absolute() and root.is_dir() and not root.is_symlink()
                     and root.stat().st_mode & 0o777 == 0o700 and root.stat().st_uid == os.geteuid(), "private disposable POSIX root required")
                need(not any(root.iterdir()), "disposable journal root must be empty; no replay/adoption")
            for name in ("coordinator",) + self.T.NODES:
                journal = (MemoryJournal(self.J, self.plan) if journal_root is None else
                           journal_class.for_test(self.plan, root / name, create=True))
                self.journals[name] = JournalPort(journal, self.trace, name)
            self.transport = self.new_transport(self.pins_before)
        except BaseException:
            self.close(); raise
        return self

    def close(self):
        for port in getattr(self, "journals", {}).values(): port.journal.close()

    def check(self):
        need(not self.poisoned and self.owner_pid == os.getpid(), "poisoned/forked model; inspect only")
        need(digest(self.plan) == self.plan_hash and digest(self.evidence) == self.evidence_hash
             and digest(dict(self.code.hashes)) == self.code_hash, "immutable plan/evidence/source drift")
        control_node = self.plan["participants"][3]["node"]
        need(self.records[control_node]["package_sha256"] == self.plan["participants"][3]["before_package_sha256"],
             "control-only package changed")
        self.M.validate_plan(self.plan, self.time)
        need(self.time >= self.last_time, "model clock regressed")
        self.last_time = self.time

    def now(self): self.check(); return self.time

    def nonce(self):
        self.nonces += 1; return format(self.nonces, "064x")

    def new_transport(self, pins):
        return self.T.Transport(files=Pins(self, pins), runner=self.exchange, dispatch_guard=self.dispatch_guard,
                                receipt_journal=self.journals["coordinator"],
                                clock=self.now, nonce=self.nonce, require_root=self.check)

    def read(self, tx, key): return self.journals["coordinator"].read(tx, key)
    def create_once(self, tx, key, value): return self.journals["coordinator"].create_once(tx, key, value)
    def observe(self, node): return self.transport.observe(node)

    def exchange(self, argv, raw, timeout):
        """Closed in-memory server simulation. Never delegates argv to a process."""
        request = self.T.strict(raw)
        node = request["node"]
        pin = self.transport.pins["participants"][self.T.NODES.index(node)]
        need(argv == self.T.ssh_argv(node, pin["host"]), "unexpected transport argv")
        label = node + ":" + request["kind"]
        if request["kind"] == "RESTORE_EFFECT":
            label += ":" + request["payload"]["unit"] + ":" + request["payload"]["operation"]
        self.trace.hit("transport-before:" + label, request)
        self.check()
        if request["kind"] == "READ_OBSERVATION":
            payload = copy.deepcopy(self.records[node])
        else:
            need(request["kind"] == "RESTORE_EFFECT", "unknown server operation")
            payload = self.local_effect(request["payload"])
        self.trace.hit("transport-result:" + label, payload)
        response = {key: request[key] for key in ("authority", "kind", "tx", "plan_sha256", "node", "boot_id", "challenge")}
        response.update(schema="slt-exact-transport-response/v1", request_sha256=digest(request), helper_path=self.T.HELPER,
                        helper_sha256=pin["helper_sha256"], source_package=copy.deepcopy(pin["source_package"]), payload=payload)
        self.trace.hit("transport-ack:" + label, response)
        return 0, self.T.canonical(response), b""

    def source_identity(self, node):
        pin = self.pins_after["participants"][self.T.NODES.index(node)]
        return {"schema": "slt-local-restore-source/v1", "authority": "NONE", "sources": dict(self.code.hashes),
                "package": {"package-artifact-sha256": {"sha256": self.records[node]["package_sha256"]}},
                "transport_package": copy.deepcopy(pin["source_package"])}

    def unit(self, node, unit):
        state = self.records[node]["services"][unit]
        source = self.V.Validator.unit_source(self.namespaces[node], unit, state["masked"], self.M)
        return {"active": state["active"], "masked": state["masked"], "substate": state["substate"], "source": source}

    def current_evidence(self):
        evidence = copy.deepcopy(self.evidence)
        for node in self.T.NODES:
            current = evidence["current"][node]
            current["observation"] = copy.deepcopy(self.records[node])
            current["service_namespace"] = copy.deepcopy(self.namespaces[node])
            current["observed_at"] = self.time
            # Runtime facts are derived from the simulated service world only.
            for row in current["runtime"]["services"]:
                state = self.records[node]["services"][row["unit"]]
                row.update(active_state="active" if state["active"] else "inactive", sub_state=state["substate"],
                    main_pid=101 if state["active"] and row["unit"] != "pve-guests.service" else 0,
                    cgroup_empty=not state["active"] or row["unit"] == "pve-guests.service",
                    mask_target="/etc/systemd/system:/dev/null" if state["masked"] else None)
        return evidence

    def verifier(self):
        return self.V.Validator.for_offline_test({n: self.code.raw[n] for n in self.V.FILES},
                                                {n: self.code.hashes[n] for n in self.V.FILES})

    def release_proof(self):
        self.check()
        inputs = copy.deepcopy(self.evidence["certified_inputs"])
        for key in self.V.BYTE_FIELDS: inputs[key] = inputs[key].encode()
        certified = self.certifier.evaluate(**inputs, now=self.time)
        for row in inputs["records"]:
            need(self.records[row["node"]]["services"]["pve-sharedlvmthin-thin-guard.service"]["active"]
                 == (row["thinguard"]["service_active_state"] == "active"), "current/certified Thin guard lifecycle differs")
        archives = self.evidence["archives"]
        need(set(archives) == set(certified["release_nodes"]), "archive cohort incomplete")
        for node, value in archives.items(): self.V.Validator.archive(value, certified, node, self.time, self.certifier)
        return {"plan_sha256": self.plan_hash, "authority": "NONE", "all_certified_sha256": digest(certified),
                "archive_sha256": {n: digest(v["receipt"]) for n, v in archives.items()}}

    def verify_release(self, plan, observations):
        need(self.stage in ("RELEASED_MODEL", "RESTORING", "COMPLETE"), "release stage not reached")
        need(plan == self.plan and observations == self.records, "transport/coordinator observation split")
        self.check()
        # The full immutable release chain was validated before release. Its
        # certificate and archive proofs are freshly recomputed here; mutable
        # observations are checked against raw runtime/source evidence too.
        evidence = self.current_evidence()
        workload = copy.deepcopy(evidence["workload"]); workload.pop("evidence_sha256")
        for node, current in evidence["current"].items():
            self.V.Validator.runtime(current["runtime"], current["observation"], workload, list(self.T.NODES), node, self.workload)
            for unit in self.M.UNITS:
                state = current["observation"]["services"][unit]
                source = self.V.Validator.unit_source(current["service_namespace"], unit, state["masked"], self.M)
                for prefix in ("/usr/lib/systemd/system/", "/run/systemd/system/"):
                    need(source[prefix + unit] == self.evidence["baseline_sources"][node][prefix + unit], "mutable service source drift")
        return self.release_proof()

    def dispatch_guard(self, request, pins):
        self.check()
        key = request["node"] + ":" + request["unit"] + ":" + request["operation"]
        need(self.stage == "RESTORING" and pins == self.pins_after and self.pending == request
             and self.read(request["tx"], key + ":intent") == request
             and self.read(request["tx"], key + ":done") is None, "coordinator intent missing/foreign/completed")
        return True

    def dispatch_typed(self, request):
        need(self.pending is None, "nested dispatch")
        self.pending = copy.deepcopy(request)
        try:
            receipt = self.transport.dispatch_restore_once(request)
            return {"request_sha256": receipt["request_sha256"], "result": "CONFIRMED"}
        finally: self.pending = None

    def local_effect(self, request):
        node, unit = request["node"], request["unit"]
        evidence = self.current_evidence()
        raw = self.V.canonical(evidence)
        provider = self.G.GrantProvider.for_model(self.code, self.plan, raw, self.journals["coordinator"],
                                                   {node: self.journals[node] for node in self.T.NODES})
        def authorize(req, identity, before):
            self.check()
            need(self.V.canonical(self.current_evidence()) == raw, "cohort changed inside local bracket")
            result = provider.authorize(req, identity, before, now=self.time)
            recomputed = self.verifier().evaluate(raw, req, identity, before, now=self.time)
            need(result["server_grant"] == recomputed, "provider/server grant differs")
            return result["issued_grant"]
        executor = self.E.Executor(self.plan, self.journals[node], authorize=authorize,
            baseline_reader=lambda: copy.deepcopy(self.baseline), identity_reader=lambda: self.source_identity(node),
            unit_reader=lambda u: self.unit(node, u), command=lambda op, u: self.command(node, op, u),
            clock=self.now, node_reader=lambda: node, boot_reader=lambda: self.records[node]["boot_id"],
            process_reader=lambda: {"pid": 100 + self.T.NODES.index(node), "starttime": 1000})
        receipt = executor.execute(request)
        key = node + ":" + unit + ":" + request["operation"]
        need(receipt["state"] == "LOCAL_EFFECT_CONFIRMED" and receipt["after"] == self.unit(node, unit)
             and receipt["effect_retry_allowed"] is False, "local result unknown")
        self.receipts[key] = copy.deepcopy(receipt)
        return receipt

    def command(self, node, operation, unit):
        need(self.pending is not None and (node, operation, unit) ==
             (self.pending["node"], self.pending["operation"], self.pending["unit"]), "effect outside exact pending request")
        key = node + ":" + unit + ":" + operation
        self.trace.hit("effect-before:" + key, self.pending)
        self.check()
        need((node, unit, operation) not in self.effects, "no effect retry")
        state = self.records[node]["services"][unit]
        if operation == "UNMASK":
            state["masked"] = False
            self.namespaces[node]["/etc/systemd/system/" + unit] = None
            self.namespaces[node]["effective:" + unit] = copy.deepcopy(self.evidence["baseline_sources"][node]["effective:" + unit])
        else:
            need(operation == "START" and unit in self.M.TARGETS, "unknown or forbidden effect")
            state.update(active=True, substate="running")
        self.effects.append((node, unit, operation))
        self.trace.hit("effect-applied:" + key, {"request": self.pending, "after": self.unit(node, unit)})
        result = {"exit_code": 0, "stdout_sha256": hashlib.sha256(b"").hexdigest(), "stderr_sha256": hashlib.sha256(b"").hexdigest()}
        self.trace.hit("effect-ack:" + key, result)
        return result

    def barrier_observation(self, node):
        r = self.records[node]
        return {"node": node, "boot_id": r["boot_id"], "membership": r["membership"], "storage_cfg_sha256": r["storage_cfg_sha256"],
            "workload_snapshot_sha256": r["workload_sha256"], "quorate": r["quorate"], "running_guests": r["consumers"],
            "ha_state": "DRAINED", "tasks": r["tasks"], "workers": r["workers"], "pending_jobs": r["start_demands"],
            "persistent_masks": sorted(u for u, s in r["services"].items() if s["masked"]),
            "active_services": sorted(u for u, s in r["services"].items() if s["active"]),
            "stopped_services": sorted(u for u, s in r["services"].items() if not s["active"] and u in self.B.REQUIRED_STOPS)}

    def observe_node(self, node):
        self.trace.hit("barrier-observe-before:" + node); self.check()
        value = self.barrier_observation(node)
        self.trace.hit("barrier-observe-ack:" + node)
        return value

    def load_events(self, tx):
        events = []
        for index in range(12):
            for kind in ("INTENT_DURABLE", "ATTEMPTED", "OBSERVED_COMPLETE"):
                value = self.read(tx, f"barrier:{index:02d}:{kind}")
                if value is None: return events
                events.append(value)
        return events

    def persist_event(self, event):
        self.create_once(self.plan["tx"], f"barrier:{event['index']:02d}:{event['kind']}", event)

    def mask_persistent(self, node, units):
        need(tuple(units) == self.B.ENTRY_UNITS, "unknown entry operation")
        self.trace.hit("barrier-effect-before:ENTRY_BLOCK:" + node)
        for unit in units:
            self.records[node]["services"][unit]["masked"] = True
            if unit != "pve-guests.service": self.records[node]["services"][unit].update(active=False, substate="dead")
        self.trace.hit("barrier-effect-applied:ENTRY_BLOCK:" + node)
        return {"persistent_masks": sorted(units), "terminal_ingress": self.B.TERMINAL_INGRESS}

    def drain_services(self, node):
        self.trace.hit("barrier-effect-before:SERVICE_DRAIN:" + node)
        for unit in self.M.MASKED_BY_BARRIER:
            self.records[node]["services"][unit]["masked"] = True
            if unit != "pve-guests.service": self.records[node]["services"][unit].update(active=False, substate="dead")
        self.trace.hit("barrier-effect-applied:SERVICE_DRAIN:" + node)
        return self.barrier_observation(node)

    def run(self):
        need(not self.used, "one run; no retry/resume")
        self.used = True
        try:
            self.check()
            need(self.read(self.plan["tx"], "flow:started") is None, "existing run is inspection-only")
            self.create_once(self.plan["tx"], "flow:started", {"authority": "NONE", "plan_sha256": self.plan_hash, "sources_sha256": self.code_hash})
            coordinator = self.M.Coordinator(self.plan, self)
            need(coordinator.baseline() == self.baseline, "collected baseline differs from pinned captures")
            barrier = self.E.C.legacy_plan(self.plan)
            held = self.B.Controller(barrier, self, self.time).run()
            need(held["state"] == "MODEL_HELD", "barrier incomplete")
            self.create_once(self.plan["tx"], "flow:held", held)
            self.stage = "HELD_MODEL"
            for row in self.plan["participants"]:
                node = row["node"]
                current = self.evidence["current"][node]
                need(self.records[node]["services"] == current["observation"]["services"], "held state differs from release evidence")
                self.records[node] = copy.deepcopy(current["observation"])
                self.namespaces[node] = copy.deepcopy(current["service_namespace"])
            # Exact supplied post-package/certificate/archive transcript is a
            # MODEL state transition, never a package installation or rename.
            self.certifier = self.code.load("layout-migration-all-certified-v2.py")
            self.workload = self.code.load("layout-migration-workload-gate.py")
            self.proof = self.release_proof()
            first_node, first_unit = self.T.NODES[0], self.M.PHASES[0][0]
            first = {"tx": self.plan["tx"], "plan_sha256": self.plan_hash, "node": first_node,
                "boot_id": self.records[first_node]["boot_id"], "unit": first_unit, "operation": "UNMASK",
                "baseline_sha256": digest(self.baseline), "release_sha256": digest(self.proof)}
            self.verifier().evaluate(self.V.canonical(self.current_evidence()), first, self.source_identity(first_node),
                                      self.unit(first_node, first_unit), now=self.time)
            for node, archive in self.evidence["archives"].items():
                self.create_once(self.plan["tx"], "archive:" + node + ":intent", archive["authorization"])
                self.trace.hit("archive-model-before:" + node)
                self.trace.hit("archive-model-applied:" + node)
                self.create_once(self.plan["tx"], "archive:" + node + ":done", archive["receipt"])
            self.create_once(self.plan["tx"], "flow:release", self.proof)
            self.stage = "RELEASED_MODEL"
            self.transport = self.new_transport(self.pins_after)
            self.stage = "RESTORING"
            result = self.M.Coordinator(self.plan, self).restore()
            self.stage = "COMPLETE"
            self.create_once(self.plan["tx"], "flow:complete", result)
            return {**result, "state": "INTEGRATION_MODEL_EXACT_RESTORED", "server_qualified": False,
                    "source_closure_sha256": self.code_hash, "receipt_count": len(self.receipts)}
        except BaseException:
            self.poisoned = True
            raise

    def inspect(self):
        """No recovery dispatch even if remote completion exists after lost ACK."""
        return {"authority": "NONE", "state": "INSPECTION_ONLY", "stage": self.stage,
                "effect_retry_allowed": False, "effects": copy.deepcopy(self.effects),
                "receipts": copy.deepcopy(self.receipts), "trace_sha256": digest(self.trace.trace)}


class PrefixInspector:
    """Read-only finite-state checker for an ACTUAL integration trace prefix.

    No resume/execute method exists. This is a model oracle, not adoption of
    externally supplied durable records. Native fsync prefixes are covered by
    the local-journal filesystem fault suite, not inferred from this trace.
    """
    def __init__(self, plan):
        self.plan, self.records, self.pending_writes = copy.deepcopy(plan), {}, {}
        self.effect_intents, self.applied = {}, {}
        self.count = 0

    def accept(self, item):
        need(type(item) is dict and set(item) == {"event", "payload"} and type(item["event"]) is str, "trace schema")
        event, value = item["event"], item["payload"]
        prefix, _, label = event.partition(":")
        if prefix.startswith("journal-"):
            participant_nodes = tuple(row["node"] for row in self.plan["participants"])
            need(type(value) is dict and set(value) == {"journal", "tx", "key", "value"}
                 and value["tx"] == self.plan["tx"] and value["journal"] in ("coordinator", *participant_nodes)
                 and label == value["journal"] + ":" + value["key"], "trace journal identity")
            key = (value["journal"], value["key"])
            if prefix == "journal-before":
                need(key not in self.records and key not in self.pending_writes, "trace create-only/retry violation")
                self.pending_writes[key] = copy.deepcopy(value["value"])
            elif prefix == "journal-stored":
                need(key in self.pending_writes and self.pending_writes[key] == value["value"] and key not in self.records,
                     "trace write without intent/exact bytes")
                self.records[key] = copy.deepcopy(value["value"])
                self._validate_record(key, value["value"])
            else:
                need(prefix == "journal-ack" and key in self.pending_writes and self.records.get(key) == value["value"], "trace journal ACK without durable readback")
                del self.pending_writes[key]
        elif prefix == "effect-before":
            request = value; key = self._request(request)
            need(label == key and key not in self.effect_intents and key not in self.applied, "trace effect retry")
            local = self.records.get((request["node"], key + ":intent"))
            need(self.records.get(("coordinator", key + ":intent")) == request and type(local) is dict
                 and local.get("request") == request and local.get("authority") == "NONE", "effect lacks both durable intents")
            grant = local.get("grant", {})
            issued = self.records.get((request["node"], key + ":grant"))
            need(type(issued) is dict and issued.get("authority") == "NONE" and issued.get("grant") == grant
                 and issued.get("request") == request, "effect lacks exact durable grant issuance")
            need(grant.get("request_sha256") == digest(request) and grant.get("allowed_effect") == request["operation"]
                 and grant.get("before_sha256") == digest(local["before"]), "effect local grant differs")
            self.effect_intents[key] = copy.deepcopy(request)
        elif prefix == "effect-applied":
            need(type(value) is dict and set(value) == {"request", "after"}, "trace effect result")
            key = self._request(value["request"])
            need(label == key and self.effect_intents.get(key) == value["request"] and key not in self.applied, "trace mutation without exact one-shot intent")
            self.applied[key] = copy.deepcopy(value["after"])
        elif prefix == "effect-ack":
            need(label in self.applied and type(value) is dict and value.get("exit_code") == 0, "effect ACK without applied effect")
        else:
            need(prefix in ("transport-before", "transport-result", "transport-ack", "barrier-observe-before",
                "barrier-observe-ack", "barrier-effect-before", "barrier-effect-applied", "archive-model-before", "archive-model-applied"), "unknown trace event")
        self.count += 1
        return self.inspect()

    def _request(self, request):
        need(type(request) is dict and request.get("tx") == self.plan["tx"] and request.get("plan_sha256") == digest(self.plan)
             and request.get("node") in [n["node"] for n in self.plan["participants"]]
             and request.get("operation") in ("UNMASK", "START"), "trace request identity")
        return request["node"] + ":" + request["unit"] + ":" + request["operation"]

    def _validate_record(self, key, value):
        journal, name = key
        if journal != "coordinator" and name.endswith(":done"):
            target = name[:-5]
            intent = self.records.get((journal, target + ":intent"))
            need(type(intent) is dict and value.get("intent_sha256") == digest(intent)
                 and value.get("request_sha256") == digest(intent["request"])
                 and value.get("after") == self.applied.get(target) and value.get("state") == "LOCAL_EFFECT_CONFIRMED"
                 and value.get("effect_retry_allowed") is False, "local completion without exact applied intent")
        if journal == "coordinator" and name.startswith("receipt:"):
            target = name[len("receipt:"):]; node = target.split(":")[0]
            need(value == self.records.get((node, target + ":done")), "transport receipt differs from durable local receipt")
        if journal == "coordinator" and name.startswith("transport:") and name.endswith(":done"):
            target = name[len("transport:"):-5]
            attempt = self.records.get((journal, "transport:" + target + ":intent"))
            receipt = self.records.get((journal, "receipt:" + target))
            need(type(attempt) is dict and type(receipt) is dict and value == {"authority": "NONE",
                 "attempt_sha256": digest(attempt), "receipt_sha256": digest(receipt), "effect_retry_allowed": False},
                 "transport completion lacks exact attempt/receipt")
        if journal == "coordinator" and name.endswith(":done") and not name.startswith(("archive:", "transport:")):
            target = name[:-5]
            request = self.records.get((journal, target + ":intent"))
            need(type(request) is dict and value == {"request_sha256": digest(request), "result": "CONFIRMED"}
                 and (journal, "receipt:" + target) in self.records, "coordinator ACK without full preserved receipt")

    def inspect(self):
        unknown = sorted(key for key in self.effect_intents if (key.split(":")[0], key + ":done") not in self.records)
        return {"authority": "NONE", "state": "INSPECTION_ONLY", "effect_retry_allowed": False,
                "trace_events": self.count, "applied_count": len(self.applied), "unknown_effects": unknown}
