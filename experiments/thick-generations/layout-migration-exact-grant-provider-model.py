#!/usr/bin/env python3
"""Unshipped durable grant-provider MODEL; no production constructor or CLI.

The only inputs with effect-shaped meaning are a model request and independently
recomputed release evidence. Caller labels/signatures are never authority. The
typed journal dependencies below are simulation-owned objects, not network or
caller-supplied reader callbacks. Real authenticated evidence delivery and the
entry/archive server adapters remain separate, unqualified trust boundaries.
"""
import copy
import hashlib
import json

FILE = "layout-migration-exact-grant-provider-model.py"


class Refusal(ValueError): pass


def need(value, message):
    if not value: raise Refusal(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def key(request):
    return request["node"] + ":" + request["unit"] + ":" + request["operation"]


class GrantProvider:
    def __init__(self):
        raise Refusal("no live grant provider until authenticated evidence wire and entry/archive adapters qualify")

    @classmethod
    def for_model(cls, code, plan, raw_evidence, coordinator_journal, local_journals):
        need(globals().get("__frozen_source_sha256__") == code.hashes.get(FILE)
             and globals().get("__frozen_source_sha256__") is not None, "grant provider must be fresh source-pinned")
        self = object.__new__(cls)
        self.code = code
        self.V = code.load("layout-migration-exact-release-validator.py")
        self.M = code.load("layout-migration-exact-restore-model.py")
        self.B = code.load("layout-migration-barrier-model.py")
        self.evidence = self.V.strict_json(raw_evidence)
        self.raw, self.plan = bytes(raw_evidence), copy.deepcopy(plan)
        need(self.evidence["plan"] == self.plan and type(local_journals) is dict
             and set(local_journals) == {r["node"] for r in plan["participants"]}, "provider plan/journal cohort differs")
        self.coordinator, self.locals = coordinator_journal, dict(local_journals)
        self.source_hash = digest(dict(code.hashes))
        self.evidence_hash = hashlib.sha256(self.raw).hexdigest()
        self.plan_hash = digest(self.plan)
        self.bound, self.last_time, self.poisoned = None, None, False
        return self

    def _check(self, now):
        need(not self.poisoned and type(now) is int and (self.last_time is None or now >= self.last_time), "poisoned/regressed provider")
        self.last_time = now
        self.M.validate_plan(self.plan, now)
        need(digest(self.plan) == self.plan_hash and self.V.canonical(self.evidence) == self.raw
             and digest(dict(self.code.hashes)) == self.source_hash, "immutable provider context/source drift")

    def _requests(self, request):
        tx = self.plan["tx"]
        need(self.coordinator.read(tx, "flow:started") == {"authority": "NONE", "plan_sha256": self.plan_hash,
             "sources_sha256": self.source_hash} and self.coordinator.read(tx, "flow:complete") is None,
             "coordinator source admission absent or already completed")
        barrier_plan = {"schema": "slt-maintenance-barrier-model/v1", "tx": tx,
            "participants": [{"node": r["node"], "boot_id": r["boot_id"], "san_role": r["role"] == "SAN"} for r in self.plan["participants"]],
            "workload_snapshot_sha256": self.plan["workload_sha256"], "storage_cfg_sha256": self.plan["storage_cfg_sha256"],
            "candidate_sha256": self.plan["candidate_sha256"], "issued_at": self.plan["issued_at"], "deadline": self.plan["expires_at"],
            "actions": [{"step": step, "node": r["node"]} for step in ("ENTRY_BLOCK", "SERVICE_DRAIN", "FINAL_AUDIT") for r in self.plan["participants"]]}
        history = [self.coordinator.read(tx, f"barrier:{i:02d}:{kind}") for i in range(12)
                   for kind in ("INTENT_DURABLE", "ATTEMPTED", "OBSERVED_COMPLETE")]
        checker = object.__new__(self.B.Controller)
        checker.plan, checker.plan_sha256 = barrier_plan, digest(barrier_plan)
        need(checker._history(history) == (12, None), "durable barrier trace incomplete")
        need(self.coordinator.read(tx, "flow:held") == {"state": "MODEL_HELD", "runtime_authorized": False,
            "rollout_authorized": False, "release_authorized": False, "mutation_performed": False}, "held admission differs")
        for node, archive in self.evidence["archives"].items():
            need(self.coordinator.read(tx, "archive:" + node + ":intent") == archive["authorization"]
                 and self.coordinator.read(tx, "archive:" + node + ":done") == archive["receipt"], "durable SAN archive trace differs")
        baseline = self.coordinator.read(self.plan["tx"], "baseline")
        need(baseline == self.evidence["baseline"] and digest(baseline) == request["baseline_sha256"], "durable baseline differs")
        need(self.coordinator.read(self.plan["tx"], "flow:release") is not None, "durable release admission absent")
        release = self.coordinator.read(self.plan["tx"], "flow:release")
        need(digest(release) == request["release_sha256"] and release.get("authority") == "NONE"
             and release.get("plan_sha256") == self.plan_hash, "durable release differs")
        result = []
        for phase in self.M.PHASES + (("pve-guests.service",),):
            for row in self.plan["participants"]:
                for unit in phase:
                    desired = baseline["records"][row["node"]]["services"][unit]
                    for operation in ("UNMASK", "START"):
                        wanted = not desired["masked"] if operation == "UNMASK" else desired["active"] and unit != "pve-guests.service"
                        if wanted:
                            result.append({"tx": self.plan["tx"], "plan_sha256": self.plan_hash, "node": row["node"],
                                "boot_id": row["boot_id"], "unit": unit, "operation": operation,
                                "baseline_sha256": digest(baseline), "release_sha256": digest(release)})
        need(request in result, "request is not an exact baseline-derived restoration step")
        return result

    def _record(self, value):
        self.M.exact(value, {"schema", "authority", "request", "executor_sha256", "before_sha256", "evidence_sha256",
            "source_closure_sha256", "phase_index", "phase_prefix_sha256", "server_grant", "grant", "record_sha256"}, "grant issuance")
        body = copy.deepcopy(value); claimed = body.pop("record_sha256")
        need(claimed == digest(body) and value["schema"] == "slt-exact-grant-issuance-model/v1" and value["authority"] == "NONE",
             "grant issuance is not an exact NONE record")
        self.M.exact(value["grant"], {"schema", "request_sha256", "executor_sha256", "before_sha256", "after_source_sha256",
            "issued_at", "expires_at", "allowed_effect", "authorization_sha256"}, "stored grant")
        self.M.exact(value["server_grant"], set(value["grant"]), "server-recomputed grant")
        need(type(value["phase_index"]) is int and value["phase_index"] >= 0
             and value["grant"]["schema"] == "slt-local-restore-explicit-grant/v1"
             and type(value["grant"]["issued_at"]) is int and type(value["grant"]["expires_at"]) is int
             and 0 < value["grant"]["expires_at"] - value["grant"]["issued_at"] <= 30
             and all(self.M.sha(value[k]) for k in ("executor_sha256", "before_sha256", "evidence_sha256", "source_closure_sha256", "phase_prefix_sha256"))
             and self.M.sha(value["grant"]["after_source_sha256"])
             and value["grant"]["request_sha256"] == digest(value["request"])
             and value["grant"]["executor_sha256"] == value["executor_sha256"]
             and value["grant"]["before_sha256"] == value["before_sha256"]
             and value["grant"]["allowed_effect"] == value["request"]["operation"], "stored grant bindings differ")
        server_grant = copy.deepcopy(value["grant"])
        server_grant["authorization_sha256"] = value["server_grant"]["authorization_sha256"]
        need(server_grant == value["server_grant"], "provider changed server grant except phase binding")
        binding = {k: value[k] for k in ("request", "executor_sha256", "before_sha256", "evidence_sha256",
                                        "source_closure_sha256", "phase_index", "phase_prefix_sha256", "server_grant")}
        need(value["grant"]["authorization_sha256"] == digest(binding), "phase-bound authorization differs")
        return copy.deepcopy(value)

    def _phase(self, request):
        requests = self._requests(request)
        position = requests.index(request)
        allowed = {key(r) for r in requests}
        # A skipped baseline operation is not an ignorable journal namespace.
        # Its presence means a foreign/older writer attempted a forbidden step.
        for participant in self.plan["participants"]:
            node = participant["node"]
            for unit in self.M.MASKED_BY_BARRIER:
                for operation in ("UNMASK", "START"):
                    name = node + ":" + unit + ":" + operation
                    if name in allowed: continue
                    need(all(self.locals[node].read(self.plan["tx"], name + suffix) is None
                             for suffix in (":grant", ":intent", ":done"))
                         and all(self.coordinator.read(self.plan["tx"], name + suffix) is None for suffix in (":intent", ":done"))
                         and self.coordinator.read(self.plan["tx"], "receipt:" + name) is None,
                         "journal contains a baseline-forbidden operation")
        prefix = []
        for index, expected in enumerate(requests):
            name, node = key(expected), expected["node"]
            c_intent = self.coordinator.read(self.plan["tx"], name + ":intent")
            c_done = self.coordinator.read(self.plan["tx"], name + ":done")
            c_receipt = self.coordinator.read(self.plan["tx"], "receipt:" + name)
            local = self.locals[node]
            grant = local.read(self.plan["tx"], name + ":grant")
            intent = local.read(self.plan["tx"], name + ":intent")
            done = local.read(self.plan["tx"], name + ":done")
            if index > position:
                need(all(v is None for v in (c_intent, c_done, c_receipt, grant, intent, done)), "future phase already attempted")
                continue
            need(c_intent == expected, "missing/swapped predecessor coordinator intent")
            if index == position:
                need(c_done is None and c_receipt is None and done is None, "current step already completed; inspection only")
                if intent is not None:
                    need(grant is not None and intent.get("request") == expected and intent.get("grant") == self._record(grant)["grant"],
                         "current local intent is not bound to the issued grant")
                return_value = {"index": position, "prefix_sha256": digest(prefix), "current_grant": grant}
                continue
            grant = self._record(grant)
            need(grant["request"] == expected and type(grant["phase_index"]) is int and grant["phase_index"] == index
                 and grant["phase_prefix_sha256"] == digest(prefix) and grant["source_closure_sha256"] == self.source_hash,
                 "predecessor grant order/source differs")
            self.M.exact(intent, {"schema", "authority", "request", "executor_identity", "before", "grant", "process"}, "predecessor intent")
            self.M.exact(done, {"schema", "authority", "state", "request_sha256", "intent_sha256", "after", "command", "started_at", "finished_at", "effect_retry_allowed"}, "predecessor completion")
            self.M.exact(done["command"], {"exit_code", "stdout_sha256", "stderr_sha256"}, "predecessor command")
            need(type(done["command"]["exit_code"]) is int and done["command"]["exit_code"] == 0
                 and all(self.M.sha(done["command"][k]) for k in ("stdout_sha256", "stderr_sha256")), "predecessor command outcome unknown")
            need(type(intent) is dict and intent.get("schema") == "slt-local-restore-intent/v1" and intent.get("authority") == "NONE"
                 and intent.get("request") == expected and intent.get("grant") == grant["grant"]
                 and digest(intent.get("executor_identity")) == grant["executor_sha256"]
                 and digest(intent.get("before")) == grant["before_sha256"], "predecessor local intent differs")
            need(type(done) is dict and done == c_receipt and done.get("schema") == "slt-local-restore-receipt/v1"
                 and done.get("authority") == "NONE" and done.get("state") == "LOCAL_EFFECT_CONFIRMED"
                 and done.get("request_sha256") == digest(expected) and done.get("intent_sha256") == digest(intent)
                 and done.get("effect_retry_allowed") is False and c_done == {"request_sha256": digest(expected), "result": "CONFIRMED"},
                 "predecessor lacks exact durable local/coordinator completion")
            need(type(done.get("started_at")) is int and type(done.get("finished_at")) is int
                 and grant["grant"]["issued_at"] <= done["started_at"] <= grant["grant"]["expires_at"]
                 and done["started_at"] <= done["finished_at"] <= self.last_time
                 and type(done.get("after")) is dict and done["after"].get("masked") is False
                 and (expected["operation"] != "START" or done["after"].get("active") is True)
                 and digest(done["after"].get("source")) == grant["grant"]["after_source_sha256"], "predecessor postcondition/time differs")
            prefix.append({"request_sha256": digest(expected), "grant_sha256": digest(grant), "intent_sha256": digest(intent), "receipt_sha256": digest(done)})
        return return_value

    def authorize(self, request, executor, before, *, now):
        """Exactly one request; repeat bracket can only read identical issuance."""
        try:
            self._check(now)
            request, executor, before = copy.deepcopy((request, executor, before))
            self.M.exact(request, {"tx", "plan_sha256", "node", "boot_id", "unit", "operation", "baseline_sha256", "release_sha256"}, "grant request")
            bound = digest({"request": request, "executor": executor, "before": before, "evidence_sha256": self.evidence_hash})
            need(self.bound is None or self.bound == bound, "provider is already bound to another request/source")
            self.bound = bound
            # Even on replay, recompute the complete server evidence. Never
            # replace this with a caller-provided ALL_CERTIFIED or GRANTED label.
            verifier = self.V.Validator.for_offline_test({n: self.code.raw[n] for n in self.V.FILES},
                                                        {n: self.code.hashes[n] for n in self.V.FILES})
            server_grant = verifier.evaluate(self.raw, request, executor, before, now=now)
            phase = self._phase(request)
            body = {"schema": "slt-exact-grant-issuance-model/v1", "authority": "NONE", "request": request,
                "executor_sha256": digest(executor), "before_sha256": digest(before), "evidence_sha256": self.evidence_hash,
                "source_closure_sha256": self.source_hash, "phase_index": phase["index"], "phase_prefix_sha256": phase["prefix_sha256"],
                "server_grant": server_grant}
            binding = {k: body[k] for k in ("request", "executor_sha256", "before_sha256", "evidence_sha256",
                                           "source_closure_sha256", "phase_index", "phase_prefix_sha256", "server_grant")}
            body["grant"] = {**server_grant, "authorization_sha256": digest(binding)}
            body["record_sha256"] = digest(body)
            journal = self.locals[request["node"]]; name = key(request) + ":grant"
            if phase["current_grant"] is None:
                need(journal.create_once(self.plan["tx"], name, body) == {"durable": True, "sha256": digest(body)}, "grant issuance ACK unknown")
            else:
                need(self._record(phase["current_grant"]) == body, "stored grant differs; no renewed or substituted grant")
            self._check(now)
            need(journal.read(self.plan["tx"], name) == body, "durable grant readback differs")
            after = self._phase(request)
            need(after["index"] == phase["index"] and after["prefix_sha256"] == phase["prefix_sha256"]
                 and after["current_grant"] == body, "phase changed during grant publication")
            return {"schema": "slt-exact-grant-provider-result/v1", "authority": "NONE",
                    "server_grant": copy.deepcopy(body["server_grant"]),
                    "issued_grant": copy.deepcopy(body["grant"]), "issuance": copy.deepcopy(body)}
        except BaseException:
            self.poisoned = True
            raise

    def inspect_issued(self, request):
        """Lost-ACK observation only, including after expiry; not an effect grant."""
        self.M.exact(request, {"tx", "plan_sha256", "node", "boot_id", "unit", "operation", "baseline_sha256", "release_sha256"}, "inspection request")
        need(request["tx"] == self.plan["tx"] and request["plan_sha256"] == self.plan_hash
             and request["node"] in self.locals, "inspection identity differs")
        record = self.locals[request["node"]].read(self.plan["tx"], key(request) + ":grant")
        if record is None:
            return {"authority": "NONE", "state": "NO_ISSUANCE_OBSERVED", "effect_retry_allowed": False}
        record = self._record(record)
        need(record["request"] == request and record["source_closure_sha256"] == self.source_hash
             and record["evidence_sha256"] == self.evidence_hash, "inspection grant context differs")
        return {"authority": "NONE", "state": "EXACT_ISSUANCE_OBSERVED", "record_sha256": digest(record), "effect_retry_allowed": False}
