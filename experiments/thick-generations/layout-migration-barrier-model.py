#!/usr/bin/env python3
"""Fail-closed model of the four-node package-maintenance barrier.

This is deliberately not a live runner.  Its backend has three closed
operations and no generic command, release, start, reboot or storage method.
The strongest result is MODEL_HELD with every authority bit false.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re


HEX32 = re.compile(r"^[0-9a-f]{32}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
ENTRY_UNITS = ("pveproxy.service", "spiceproxy.service",
               "pvescheduler.service", "pve-guests.service")
FORBIDDEN_STOPS = {"pve-guests.service", "pve-cluster.service",
                   "corosync.service", "multipathd.service"}
REQUIRED_STOPS = {"pveproxy.service", "spiceproxy.service",
                  "pvescheduler.service", "pvedaemon.service",
                  "pvestatd.service", "pve-ha-lrm.service",
                  "pve-ha-crm.service", "qmeventd.service"}
ALLOWED_ACTIVE = {"pve-cluster.service", "corosync.service",
                   "multipathd.service", "pve-sharedlvmthin-thin-guard.service",
                   "pve-guests.service"}
RESTARTABLE_MASKS = REQUIRED_STOPS | set(ENTRY_UNITS)
TERMINAL_INGRESS = sorted(set(ENTRY_UNITS) - {"pve-guests.service"})
STEPS = ("ENTRY_BLOCK", "SERVICE_DRAIN", "FINAL_AUDIT")


class Refusal(Exception): pass


def require(value, message):
    if not value:
        raise Refusal(message)


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields),
            f"{label} fields invalid")


def frozen_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def validate_plan(plan, now):
    exact(plan, {"schema", "tx", "participants", "workload_snapshot_sha256",
                 "storage_cfg_sha256", "candidate_sha256", "issued_at",
                 "deadline", "actions"}, "plan")
    require(plan["schema"] == "slt-maintenance-barrier-model/v1"
            and type(plan["tx"]) is str and HEX32.fullmatch(plan["tx"]),
            "plan identity invalid")
    require(type(plan["issued_at"]) is int and type(plan["deadline"]) is int
            and plan["issued_at"] <= now <= plan["deadline"]
            and 0 < plan["deadline"] - plan["issued_at"] <= 1800,
            "plan timing invalid")
    for field in ("workload_snapshot_sha256", "storage_cfg_sha256",
                  "candidate_sha256"):
        require(type(plan[field]) is str and SHA256.fullmatch(plan[field]),
                f"{field} invalid")
    participants = plan["participants"]
    require(type(participants) is list and len(participants) == 4,
            "exactly four participants required")
    names = []
    for row in participants:
        exact(row, {"node", "boot_id", "san_role"}, "participant")
        require(type(row["node"]) is str and row["node"]
                and type(row["boot_id"]) is str and UUID.fullmatch(row["boot_id"])
                and type(row["san_role"]) is bool, "participant invalid")
        names.append(row["node"])
    require(names == sorted(set(names)), "participant set is duplicated or unordered")
    require(sum(not row["san_role"] for row in participants) == 1,
            "topology must explicitly bind one control-plane-only node")
    expected = ([{"step": "ENTRY_BLOCK", "node": node} for node in names]
                + [{"step": "SERVICE_DRAIN", "node": node} for node in names]
                + [{"step": "FINAL_AUDIT", "node": node} for node in names])
    require(type(plan["actions"]) is list and plan["actions"] == expected,
            "action plan is not the exact closed four-node order")
    return copy.deepcopy(plan)


def validate_observation(value, plan, node, *, final=False):
    # running_guests is the managed-consumer set. A live backend must derive
    # it from the bound workload/config inventory, never accept caller scope.
    # pve-guests may remain active/exited; the live backend proves its cgroup
    # and executors empty. It is deliberately never stopped.
    exact(value, {"node", "boot_id", "membership", "storage_cfg_sha256",
                  "workload_snapshot_sha256", "quorate", "running_guests",
                  "ha_state", "tasks", "workers", "pending_jobs",
                  "persistent_masks", "active_services", "stopped_services"},
          "observation")
    expected_boot = next(row["boot_id"] for row in plan["participants"]
                         if row["node"] == node)
    names = [row["node"] for row in plan["participants"]]
    require(value["node"] == node and value["boot_id"] == expected_boot
            and value["membership"] == names
            and value["storage_cfg_sha256"] == plan["storage_cfg_sha256"]
            and value["workload_snapshot_sha256"] == plan["workload_snapshot_sha256"]
            and value["quorate"] is True, "cluster identity drifted")
    for field in ("running_guests", "tasks", "workers", "pending_jobs"):
        require(type(value[field]) is list and not value[field],
                f"{field} is not empty")
    require(value["ha_state"] in ("EMPTY_IDLE", "DRAINED"), "HA is not empty and idle")
    for field in ("persistent_masks", "active_services", "stopped_services"):
        require(type(value[field]) is list
                and value[field] == sorted(set(value[field])), f"{field} invalid")
    if final:
        require(value["ha_state"] == "DRAINED", "HA daemons not drained")
        require(RESTARTABLE_MASKS <= set(value["persistent_masks"]),
                "persistent ingress masks are incomplete")
        require(not set(value["active_services"]) & set(value["stopped_services"]),
                "service lifecycle is contradictory")
        require(not set(FORBIDDEN_STOPS) & set(value["stopped_services"]),
                "forbidden service was stopped")
        require(set(value["stopped_services"]) == REQUIRED_STOPS,
                "required terminal service evidence is incomplete")
        require(set(value["active_services"]) <= ALLOWED_ACTIVE,
                "unexpected service remains active")
    return copy.deepcopy(value)


class Controller:
    """Single-consumer state machine over a durability-providing backend."""
    def __init__(self, plan, backend, now):
        self.plan = validate_plan(plan, now)
        self.plan_sha256 = frozen_hash(self.plan)
        self.backend = backend
        self.now = now
        self.poisoned = False
        self.running = False
        self.last_clock = now

    def _clock(self):
        previous = self.last_clock
        value = self.backend.now()
        self._authority()
        require(self.last_clock == previous,
                "clock watermark changed inside backend callback")
        require(type(value) is int and value >= previous,
                "clock is invalid or regressed")
        self.last_clock = value
        require(value <= self.plan["deadline"], "absolute deadline expired")
        return value

    def _authority(self):
        require(not self.poisoned and frozen_hash(self.plan) == self.plan_sha256,
                "plan changed after enrollment")

    def _persist(self, kind, index, action, receipt=None):
        event = {"tx": self.plan["tx"], "plan_sha256": self.plan_sha256,
                 "kind": kind, "index": index,
                 "step": action["step"], "node": action["node"]}
        if receipt is not None: event["receipt"] = copy.deepcopy(receipt)
        self.backend.persist_event(event)
        self._authority(); self._clock()

    def _receipt(self, action, value, final=False):
        if action["step"] == "ENTRY_BLOCK":
            require(type(value) is dict
                    and value == {"persistent_masks": sorted(ENTRY_UNITS),
                                  "terminal_ingress": TERMINAL_INGRESS},
                    "entry-block receipt invalid")
            return copy.deepcopy(value)
        return validate_observation(value, self.plan, action["node"], final=final)

    def _history(self, history):
        require(type(history) is list, "journal history invalid")
        position = 0
        for index, action in enumerate(self.plan["actions"]):
            kinds = ("INTENT_DURABLE", "ATTEMPTED", "OBSERVED_COMPLETE")
            for kind in kinds:
                if position >= len(history):
                    return index, kind
                row = history[position]
                fields = {"tx", "plan_sha256", "kind", "index", "step", "node"}
                if kind == "OBSERVED_COMPLETE": fields.add("receipt")
                exact(row, fields, "journal event")
                require(row["tx"] == self.plan["tx"]
                        and row["plan_sha256"] == self.plan_sha256
                        and row["kind"] == kind and row["index"] == index
                        and row["step"] == action["step"]
                        and row["node"] == action["node"],
                        "journal event identity or order invalid")
                if kind == "OBSERVED_COMPLETE":
                    self._receipt(action, row["receipt"],
                                  final=action["step"] == "FINAL_AUDIT")
                position += 1
        require(position == len(history), "journal has extra records")
        return len(self.plan["actions"]), None

    def run(self):
        if self.running:
            self.poisoned = True
            raise Refusal("controller is reentered and poisoned")
        require(not self.poisoned, "controller is poisoned")
        self.running = True
        try:
            self._authority(); self._clock()
            history = self.backend.load_events(self.plan["tx"])
            self._authority(); self._clock()
            completed, missing_kind = self._history(history)
            if missing_kind not in (None, "INTENT_DURABLE"):
                raise Refusal("pending action requires recovery")
            if completed == len(self.plan["actions"]):
                return {"state": "MODEL_COMPLETED_HISTORICAL",
                        "runtime_authorized": False, "rollout_authorized": False,
                        "release_authorized": False, "mutation_performed": False}
            require(completed == 0 and not history,
                    "partial completed history requires explicit recovery")
            # A complete fresh all-node preflight occurs before the first new effect.
            for participant in self.plan["participants"]:
                self._authority(); self._clock()
                observed = self.backend.observe_node(participant["node"])
                validate_observation(observed, self.plan, participant["node"])
                self._authority(); self._clock()
            for index in range(completed, len(self.plan["actions"])):
                action = self.plan["actions"][index]
                if index == 4:
                    for participant in self.plan["participants"]:
                        observed = self.backend.observe_node(participant["node"])
                        normalized = validate_observation(
                            observed, self.plan, participant["node"])
                        require(set(ENTRY_UNITS) <= set(normalized["persistent_masks"])
                                and not set(TERMINAL_INGRESS)
                                    & set(normalized["active_services"]),
                                "entry barrier is not terminal on every node")
                        self._authority(); self._clock()
                self._authority(); self._clock()
                self._persist("INTENT_DURABLE", index, action)
                self._persist("ATTEMPTED", index, action)
                self._authority(); self._clock()
                if action["step"] == "ENTRY_BLOCK":
                    receipt = self.backend.mask_persistent(action["node"], ENTRY_UNITS)
                elif action["step"] == "SERVICE_DRAIN":
                    receipt = self.backend.drain_services(action["node"])
                    normalized = self._receipt(action, receipt)
                    require(set(ENTRY_UNITS) <= set(normalized["persistent_masks"]),
                            "drain observation lost persistent masks")
                    require(not set(FORBIDDEN_STOPS) & set(normalized["stopped_services"]),
                            "drain stopped a forbidden service")
                else:
                    receipt = self.backend.observe_node(action["node"])
                normalized = self._receipt(action, receipt,
                                           final=action["step"] == "FINAL_AUDIT")
                self._authority(); self._clock()
                self._persist("OBSERVED_COMPLETE", index, action, normalized)
            self._authority(); self._clock()
            self._authority()
            return {"state": "MODEL_HELD", "runtime_authorized": False,
                    "rollout_authorized": False, "release_authorized": False,
                    "mutation_performed": False}
        except Exception:
            self.poisoned = True
            raise
        finally:
            self.running = False
