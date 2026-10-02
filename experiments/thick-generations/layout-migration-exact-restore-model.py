#!/usr/bin/env python3
"""Unshipped typed coordinator model; NO live transport or mutation authority.

Backend methods are trusted model dependencies, not callable CLI commands.
create_once must fsync/create-only and return exact durable acknowledgements.
verify_release must independently re-evaluate the complete composed release
chain AND all three local archive receipts, not trust an ALL_CERTIFIED label.
No implementation of either trust boundary is supplied by this model.
"""
import copy
import hashlib
import json
import re

PHASES = (
    ("qmeventd.service", "pvedaemon.service", "pvestatd.service"),
    ("pveproxy.service", "spiceproxy.service"),
    ("pve-ha-crm.service", "pve-ha-lrm.service"),
    ("pvescheduler.service",),
)
TARGETS = tuple(sorted(unit for phase in PHASES for unit in phase))
PRESERVED = ("corosync.service", "multipathd.service", "pve-cluster.service",
             "pve-guests.service", "pve-sharedlvmthin-thin-guard.service")
UNITS = tuple(sorted(TARGETS + PRESERVED))
MASKED_BY_BARRIER = set(TARGETS) | {"pve-guests.service"}


class Refusal(ValueError):
    pass


def need(value, message):
    if not value:
        raise Refusal(message)


def exact(value, keys, label):
    need(type(value) is dict and set(value) == set(keys), label + " fields differ")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    allow_nan=False).encode()).hexdigest()


def sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def validate_plan(plan, now):
    exact(plan, {"schema", "tx", "participants", "candidate_sha256", "storage_cfg_sha256",
                 "target_storage_cfg_sha256", "workload_sha256", "issued_at", "expires_at"}, "plan")
    need(plan["schema"] == "slt-exact-restore-model/v1" and type(plan["tx"]) is str
         and re.fullmatch(r"[0-9a-f]{32}", plan["tx"]), "plan identity")
    need(type(now) is int and type(plan["issued_at"]) is int and type(plan["expires_at"]) is int
         and plan["issued_at"] <= now <= plan["expires_at"]
         and 0 < plan["expires_at"] - plan["issued_at"] <= 1800, "plan deadline")
    for field in ("candidate_sha256", "storage_cfg_sha256", "target_storage_cfg_sha256", "workload_sha256"):
        need(sha(plan[field]), "plan hash")
    rows = plan["participants"]
    need(type(rows) is list and len(rows) == 4, "four participants required")
    for row in rows:
        exact(row, {"node", "boot_id", "role", "before_package_sha256", "after_package_sha256"}, "participant")
        need(type(row["node"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,62}", row["node"])
             and type(row["boot_id"]) is str and re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", row["boot_id"])
             and row["role"] in ("SAN", "CONTROL_ONLY")
             and sha(row["before_package_sha256"]) and sha(row["after_package_sha256"]), "participant identity")
        need(row["after_package_sha256"] == (plan["candidate_sha256"] if row["role"] == "SAN"
             else row["before_package_sha256"]), "control-only package must remain unchanged")
    names = [row["node"] for row in rows]
    need(names == sorted(set(names)) and sum(row["role"] == "CONTROL_ONLY" for row in rows) == 1,
         "participant roles/order")


class Coordinator:
    """Baseline -> verified archive cohort -> exact service restoration model.

    An uncertain dispatch poisons this instance. A new instance may inspect
    durable COMPLETE effects, but any INTENT lacking COMPLETE is refused.
    There is intentionally no automatic retry, resume-unknown or latch removal.
    """
    def __init__(self, plan, backend):
        self.plan = copy.deepcopy(plan)
        self.backend = backend
        self.hash = digest(self.plan)
        self.poisoned = False
        self.last_clock = plan.get("issued_at")
        self.clock()

    def clock(self):
        need(not self.poisoned and digest(self.plan) == self.hash, "poisoned or changed coordinator")
        now = self.backend.now()
        validate_plan(self.plan, now)
        need(type(self.last_clock) is int and now >= self.last_clock, "clock regressed")
        self.last_clock = now
        return now

    def observe(self, node, *, before=False):
        self.clock()
        value = self.backend.observe(node)
        self.validate_record(value, node, before=before)
        self.clock()
        return copy.deepcopy(value)

    def validate_record(self, value, node, *, before=False):
        row = next(row for row in self.plan["participants"] if row["node"] == node)
        exact(value, {"node", "boot_id", "membership", "quorate", "storage_cfg_sha256",
                      "workload_sha256", "package_sha256", "consumers", "workers", "tasks",
                      "start_demands", "services"}, "observation")
        need(value["node"] == node and value["boot_id"] == row["boot_id"]
             and value["membership"] == [p["node"] for p in self.plan["participants"]]
             and value["quorate"] is True
             and value["storage_cfg_sha256"] == self.plan["storage_cfg_sha256" if before else "target_storage_cfg_sha256"]
             and value["package_sha256"] == row["before_package_sha256" if before else "after_package_sha256"]
             and value["workload_sha256"] == self.plan["workload_sha256"], "identity/quorum drift")
        for key in ("consumers", "workers", "tasks", "start_demands"):
            need(type(value[key]) is list and value[key] == [], "nonzero/unknown " + key)
        services = value["services"]
        need(type(services) is dict and set(services) == set(UNITS), "service coverage")
        for unit, state in services.items():
            exact(state, {"active", "substate", "masked", "job", "control_pid"}, "service")
            need(type(state["active"]) is bool and type(state["masked"]) is bool
                 and state["job"] is None and type(state["control_pid"]) is int and state["control_pid"] == 0
                 and state["substate"] == (("exited" if unit == "pve-guests.service" else "running")
                                          if state["active"] else "dead"),
                 "unknown service lifecycle")
            if before and unit in TARGETS:
                need(not (state["active"] and state["masked"]), "active+masked restoration unsupported")
        for unit in ("corosync.service", "pve-cluster.service") + (("multipathd.service",) if row["role"] == "SAN" else ()):
            need(services[unit]["active"], "essential service inactive")

    def put(self, key, value):
        result = self.backend.create_once(self.plan["tx"], key, copy.deepcopy(value))
        need(result == {"durable": True, "sha256": digest(value)}, "durable create ACK missing")

    def baseline(self):
        need(self.backend.read(self.plan["tx"], "baseline") is None, "baseline already exists; no adoption")
        records = {row["node"]: self.observe(row["node"], before=True) for row in self.plan["participants"]}
        # A second full cohort pass precedes baseline publication and any mask.
        for node, record in records.items():
            need(self.observe(node, before=True) == record, "baseline changed during capture")
        value = {"plan_sha256": self.hash, "authority": "NONE", "records": records}
        self.put("baseline", value)
        return copy.deepcopy(value)

    def release(self, baseline):
        """Read-only composed-chain gate, never local hold/archive authority."""
        observations = {row["node"]: self.observe(row["node"]) for row in self.plan["participants"]}
        for node, observed in observations.items():
            desired = baseline["records"][node]["services"]
            for unit in PRESERVED:
                current = copy.deepcopy(observed["services"][unit])
                # pve-guests is masked by ENTRY_BLOCK, but never stopped or
                # started. Only that one temporary mask difference is allowed.
                if unit == "pve-guests.service" and current["masked"]:
                    current["masked"] = desired[unit]["masked"]
                need(current == desired[unit], "preserved service drift")
            for unit in TARGETS:
                current = observed["services"][unit]
                need(not current["active"] or desired[unit]["active"], "previously inactive service started")
                need(not desired[unit]["masked"] or current["masked"], "preexisting mask removed")
        proof = self.backend.verify_release(copy.deepcopy(self.plan), observations)
        exact(proof, {"plan_sha256", "authority", "all_certified_sha256", "archive_sha256"}, "release proof")
        san = {row["node"] for row in self.plan["participants"] if row["role"] == "SAN"}
        need(proof["plan_sha256"] == self.hash and proof["authority"] == "NONE"
             and sha(proof["all_certified_sha256"]) and type(proof["archive_sha256"]) is dict
             and set(proof["archive_sha256"]) == san and all(sha(v) for v in proof["archive_sha256"].values()),
             "incomplete archive cohort")
        self.clock()
        return copy.deepcopy(proof)

    def restore(self):
        try:
            return self._restore()
        except BaseException:
            self.poisoned = True
            raise

    def _restore(self):
        self.clock()
        baseline = self.backend.read(self.plan["tx"], "baseline")
        exact(baseline, {"plan_sha256", "authority", "records"}, "baseline")
        need(baseline["plan_sha256"] == self.hash and baseline["authority"] == "NONE"
             and set(baseline["records"]) == {p["node"] for p in self.plan["participants"]}, "baseline binding")
        for node, record in baseline["records"].items():
            self.validate_record(record, node, before=True)
        release = self.release(baseline)
        release_sha = digest(release)
        baseline_sha = digest(baseline)
        for phase in PHASES + (("pve-guests.service",),):
            for row in self.plan["participants"]:
                node = row["node"]
                for unit in phase:
                    desired = baseline["records"][node]["services"][unit]
                    for operation in ("UNMASK", "START"):
                        wanted = (not desired["masked"] if operation == "UNMASK"
                                  else desired["active"] and unit != "pve-guests.service")
                        if not wanted:
                            continue
                        need(digest(self.release(baseline)) == release_sha, "release cohort changed")
                        need(self.backend.read(self.plan["tx"], "baseline") == baseline, "durable baseline changed")
                        current = self.observe(node)["services"][unit]
                        key = node + ":" + unit + ":" + operation
                        request = {"tx": self.plan["tx"], "plan_sha256": self.hash, "node": node,
                                   "boot_id": row["boot_id"], "unit": unit, "operation": operation,
                                   "baseline_sha256": baseline_sha, "release_sha256": release_sha}
                        intent = self.backend.read(self.plan["tx"], key + ":intent")
                        done = self.backend.read(self.plan["tx"], key + ":done")
                        if intent is not None or done is not None:
                            need(intent == request and done == {"request_sha256": digest(request), "result": "CONFIRMED"},
                                 "uncertain prior attempt: inspect only, never redispatch")
                        else:
                            need(current["masked"] if operation == "UNMASK" else
                                 not current["active"] and not current["masked"], "effect predecessor drift")
                            self.put(key + ":intent", request)
                            self.clock()
                            need(self.observe(node)["services"][unit] == current
                                 and digest(self.release(baseline)) == release_sha, "pre-dispatch drift")
                            ack = self.backend.dispatch_typed(copy.deepcopy(request))
                            need(ack == {"request_sha256": digest(request), "result": "CONFIRMED"}, "effect ACK unknown")
                            self.clock()
                            after = self.observe(node)["services"][unit]
                            need((not after["masked"] if operation == "UNMASK" else after["active"] and not after["masked"]),
                                 "effect postcondition missing")
                            self.put(key + ":done", ack)
                        after = self.observe(node)["services"][unit]
                        need(not after["masked"] and (operation != "START" or after["active"]), "completed effect drift")
        for row in self.plan["participants"]:
            node = row["node"]
            need(self.observe(node)["services"] == baseline["records"][node]["services"], "exact service restoration differs")
        need(digest(self.release(baseline)) == release_sha, "final release cohort changed")
        return {"state": "MODEL_EXACT_RESTORED", "authority": "NONE", "runtime_authorized": False,
                "rollout_authorized": False, "release_authorized": False, "mutation_performed": False}
