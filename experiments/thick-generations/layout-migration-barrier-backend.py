#!/usr/bin/env python3
"""Closed maintenance barrier backend; no production transport or live CLI.

Transport must deliver each request at most once to a pinned node. There is
deliberately no shell string, arbitrary argv, retry, resume or release method.
The node collector is an injected trust boundary: its local guest inventory
must cover configurations AND actual QEMU/LXC processes; unknown is refusal.
This module validates evidence, it does not manufacture that inventory.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODEL = load("barrier_backend_model", "layout-migration-barrier-model.py")
JOURNAL = load("barrier_backend_journal", "layout-migration-barrier-file-journal.py")
WORKLOAD = load("barrier_backend_workload", "layout-migration-workload-gate.py")
Refusal = MODEL.Refusal
require = MODEL.require
exact = MODEL.exact
OPS = ("OBSERVE", "ENTRY_BLOCK", "SERVICE_DRAIN")
WATCHED = tuple(sorted(MODEL.RESTARTABLE_MASKS | MODEL.ALLOWED_ACTIVE))


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def scoped_observation(raw, plan, node):
    """Derive managed running consumers; unrelated guests are never stopped."""
    exact(raw, {"observation", "storage_config", "workload", "guests",
                "inventory_complete", "managed_mappers", "managed_open_lvs",
                "services"}, "node evidence")
    require(raw["inventory_complete"] is True,
            "configuration/runtime guest inventory is incomplete")
    require(type(raw["storage_config"]) is str, "storage config invalid")
    cfg = raw["storage_config"].encode()
    require(digest(cfg) == plan["storage_cfg_sha256"], "storage config drifted")
    managed = set(WORKLOAD.storage_ids(cfg))
    snapshot = copy.deepcopy(raw["workload"])
    require(type(snapshot) is dict, "workload snapshot invalid")
    claimed = snapshot.pop("evidence_sha256", None)
    require(claimed == plan["workload_snapshot_sha256"]
            and digest(WORKLOAD.canonical(snapshot)) == claimed
            and snapshot.get("schema") == "slt-workload-prepare/v1"
            and snapshot.get("verdict") == "SNAPSHOT_READY"
            and snapshot.get("storage_cfg_sha256") == plan["storage_cfg_sha256"]
            and snapshot.get("managed_storages") == sorted(managed)
            and snapshot.get("running_consumers") == [],
            "workload scope is not the bound ready snapshot")
    require(type(snapshot.get("consumers")) is list
            and type(snapshot.get("config_sha256")) is dict,
            "workload consumer inventory invalid")
    expected = {}
    names = {p["node"] for p in plan["participants"]}
    for item in snapshot["consumers"]:
        require(type(item) is dict and type(item.get("vmid")) is int
                and item["vmid"] > 0 and item["vmid"] not in expected
                and item.get("node") in names and item.get("status") == "stopped"
                and item.get("type") in ("qemu", "lxc"),
                "workload consumer identity invalid")
        expected[item["vmid"]] = item
    require(type(raw["guests"]) is list, "guest inventory invalid")
    seen = set()
    found = set()
    running = []
    for guest in raw["guests"]:
        exact(guest, {"vmid", "type", "node", "status", "config"}, "guest")
        vmid = guest["vmid"]
        require(type(vmid) is int and vmid > 0 and vmid not in seen
                and guest["type"] in ("qemu", "lxc") and guest["node"] == node
                and guest["status"] in ("running", "stopped")
                and type(guest["config"]) is str, "guest identity invalid")
        seen.add(vmid)
        disks = WORKLOAD.current_disks(guest["config"].encode(), managed, guest["type"])
        if not disks:
            require(vmid not in expected, "managed guest lost its disk scope")
            continue
        require(vmid in expected and expected[vmid]["node"] == node
                and expected[vmid]["type"] == guest["type"]
                and expected[vmid].get("disks") == disks
                and snapshot["config_sha256"].get(str(vmid))
                    == digest(guest["config"].encode()),
                "managed guest or config differs from snapshot")
        found.add(vmid)
        if guest["status"] == "running":
            running.append(vmid)
    require(found == {v for v, g in expected.items() if g["node"] == node},
            "managed guest coverage is incomplete")
    for field in ("managed_mappers", "managed_open_lvs"):
        require(type(raw[field]) is list and not raw[field],
                f"{field} is not empty")
    observation = copy.deepcopy(raw["observation"])
    require(type(observation) is dict and "running_guests" not in observation,
            "caller may not supply derived running guest scope")
    observation["running_guests"] = sorted(running)
    services = raw["services"]
    require(type(services) is list and len(services) == len(WATCHED),
            "service coverage incomplete")
    masks, active, stopped, unit_names = [], [], [], []
    for row in services:
        exact(row, {"unit", "active_state", "sub_state", "main_pid",
                    "control_pid", "cgroup_empty", "job", "mask_target"}, "service")
        unit = row["unit"]
        require(unit in WATCHED and unit not in unit_names
                and row["active_state"] in ("active", "inactive")
                and type(row["sub_state"]) is str
                and type(row["main_pid"]) is int and row["main_pid"] >= 0
                and type(row["control_pid"]) is int and row["control_pid"] >= 0
                and type(row["cgroup_empty"]) is bool and row["job"] is None
                and row["mask_target"] in (None, "/etc/systemd/system:/dev/null"),
                "service state unknown, pending, or mask is not persistent")
        unit_names.append(unit)
        if row["mask_target"] is not None:
            masks.append(unit)
        if row["active_state"] == "active":
            active.append(unit)
        else:
            require(row["sub_state"] == "dead" and row["main_pid"] == 0
                    and row["control_pid"] == 0 and row["cgroup_empty"],
                    "inactive service has surviving executor or cgroup")
            if unit in MODEL.REQUIRED_STOPS:
                stopped.append(unit)
        # These cluster/storage services must survive every phase.
        if unit in ("corosync.service", "pve-cluster.service") or (
                unit == "multipathd.service" and next(
                    p["san_role"] for p in plan["participants"] if p["node"] == node)):
            require(row["active_state"] == "active", "essential service is not active")
        if unit == "pve-guests.service":
            require(row["main_pid"] == 0 and row["control_pid"] == 0
                    and row["cgroup_empty"] and (row["active_state"], row["sub_state"])
                        in (("inactive", "dead"), ("active", "exited")),
                    "pve-guests has an active executor")
    for key, value in (("persistent_masks", masks), ("active_services", active),
                       ("stopped_services", stopped)):
        require(key not in observation, "caller may not supply derived service state")
        observation[key] = sorted(value)
    return MODEL.validate_observation(observation, plan, node)


class NodeAgent:
    """Injected collector and executor; only fixed systemctl commands exist.

    A production wrapper must hold a create-only local request latch before
    dispatch, verify the coordinator request and authenticate its transport.
    This class is not an independently authorized live entry point.
    """
    def __init__(self, plan, node, collect, execute, clock):
        MODEL.validate_plan(plan, plan["issued_at"])
        require(node in [p["node"] for p in plan["participants"]], "unknown node")
        self.plan, self.node = copy.deepcopy(plan), node
        self.collect, self.execute = collect, execute
        self.clock = clock
        self.plan_sha = MODEL.frozen_hash(plan)
        self.last_clock = plan["issued_at"]
        self.attempts = set()
        self.poisoned = False
        self.running = False

    def _check(self):
        require(not self.poisoned and MODEL.frozen_hash(self.plan) == self.plan_sha,
                "node agent state or plan changed")
        value = self.clock()
        require(type(value) is int and self.last_clock <= value <= self.plan["deadline"],
                "node clock regressed or deadline expired")
        self.last_clock = value

    def request(self, request):
        require(not self.poisoned, "node agent poisoned")
        if self.running:
            self.poisoned = True
            raise Refusal("node agent reentered")
        self.running = True
        try:
            self._check()
            request = copy.deepcopy(request)
            exact(request, {"tx", "plan_sha256", "node", "operation"}, "request")
            require(request["tx"] == self.plan["tx"] and request["node"] == self.node
                    and request["plan_sha256"] == MODEL.frozen_hash(self.plan)
                    and request["operation"] in OPS, "request binding invalid")
            operation = request["operation"]
            raw = self.collect()
            self._check()
            scoped_observation(raw, self.plan, self.node)
            if operation != "OBSERVE":
                require(operation not in self.attempts, "operation already attempted")
                self.attempts.add(operation)
                if operation == "ENTRY_BLOCK":
                    commands = [("/usr/bin/systemctl", "mask", "--", *sorted(MODEL.ENTRY_UNITS)),
                                ("/usr/bin/systemctl", "stop", "--", *MODEL.TERMINAL_INGRESS)]
                else:
                    observation = scoped_observation(raw, self.plan, self.node)
                    require(set(MODEL.ENTRY_UNITS) <= set(observation["persistent_masks"])
                            and not set(MODEL.TERMINAL_INGRESS) & set(observation["active_services"]),
                            "entry barrier not established")
                    commands = [("/usr/bin/systemctl", "mask", "--", *sorted(MODEL.REQUIRED_STOPS)),
                                ("/usr/bin/systemctl", "stop", "--", *sorted(MODEL.REQUIRED_STOPS))]
                for argv in commands:
                    self._check()
                    require(self.execute(argv) is True, "command result ambiguous")
                    self._check()
                raw = self.collect()
                self._check()
                observed = scoped_observation(raw, self.plan, self.node)
                if operation == "ENTRY_BLOCK":
                    require(set(MODEL.ENTRY_UNITS) <= set(observed["persistent_masks"])
                            and not set(MODEL.TERMINAL_INGRESS) & set(observed["active_services"]),
                            "entry terminal postcondition absent")
                else:
                    MODEL.validate_observation(observed, self.plan, self.node, final=True)
            return {"request": copy.deepcopy(request), "evidence": raw}
        except BaseException:
            self.poisoned = True
            raise
        finally:
            self.running = False


class Backend:
    """Compose the existing durable coordinator journal with closed transport."""
    def __init__(self, plan, journal, transport, clock):
        self.plan = copy.deepcopy(plan)
        require(journal.plan == plan, "journal plan differs")
        self.journal, self.transport, self.clock = journal, transport, clock
        self.poisoned = False
        self.plan_sha = MODEL.frozen_hash(self.plan)
        self.dispatch_token = None

    def now(self):
        return self.clock()

    def load_events(self, tx):
        return self.journal.load_events(tx)

    def persist_event(self, event):
        result = self.journal.persist_event(event)
        if event["kind"] == "ATTEMPTED":
            self.dispatch_token = (event["step"], event["node"])
        return result

    def _call(self, node, operation):
        require(not self.poisoned, "backend poisoned")
        try:
            require(MODEL.frozen_hash(self.plan) == self.plan_sha, "backend plan changed")
            request = {"tx": self.plan["tx"], "plan_sha256": MODEL.frozen_hash(self.plan),
                       "node": node, "operation": operation}
            if operation != "OBSERVE":
                history = self.journal.load_events(self.plan["tx"])
                step = "ENTRY_BLOCK" if operation == "ENTRY_BLOCK" else "SERVICE_DRAIN"
                require(len(history) >= 2 and history[-1]["kind"] == "ATTEMPTED"
                        and history[-1]["step"] == step and history[-1]["node"] == node,
                        "durable matching attempt missing")
                require(self.dispatch_token == (step, node), "fresh single-use dispatch absent")
                self.dispatch_token = None
            response = self.transport.request(node, copy.deepcopy(request))
            exact(response, {"request", "evidence"}, "transport response")
            require(response["request"] == request, "transport receipt belongs to another request")
            return scoped_observation(response["evidence"], self.plan, node)
        except BaseException:
            self.poisoned = True
            raise

    def observe_node(self, node):
        return self._call(node, "OBSERVE")

    def mask_persistent(self, node, units):
        require(tuple(units) == MODEL.ENTRY_UNITS, "entry unit set differs")
        observation = self._call(node, "ENTRY_BLOCK")
        require(set(MODEL.ENTRY_UNITS) <= set(observation["persistent_masks"])
                and not set(MODEL.TERMINAL_INGRESS) & set(observation["active_services"]),
                "entry barrier postcondition absent")
        return {"persistent_masks": sorted(MODEL.ENTRY_UNITS),
                "terminal_ingress": MODEL.TERMINAL_INGRESS}

    def drain_services(self, node):
        return MODEL.validate_observation(self._call(node, "SERVICE_DRAIN"),
                                          self.plan, node, final=True)
