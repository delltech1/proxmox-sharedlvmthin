#!/usr/bin/env python3
"""Pure projection of current node receipts into the existing v2 barrier.

Inputs must arrive over the coordinator's authenticated node transport. JSON
hashes bind evidence, not its origin. Historical v1 runner output alone cannot
prove a current hold. Every node must supply a fresh v2 OBSERVE receipt with
both completed create-only latches and a later current v2 node preflight for
thin-guard proof. Nothing here writes PREPARE, installs, settles or releases.
"""
from __future__ import annotations

import copy
import importlib.util
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


A = load("barrier_receipt_v1_adapter", "layout-migration-v2-v1-adapter.py")
B = load("barrier_receipt_backend", "layout-migration-barrier-backend.py")
L = load("barrier_receipt_latch", "layout-migration-barrier-node-latch.py")
C = load("barrier_receipt_composer", "layout-migration-offline-composer.py")
Refusal, require, exact = A.Refusal, A.require, A.exact


def response(value, request, plan, *, final=False):
    exact(value, {"request", "evidence"}, "node response")
    require(value["request"] == request, "node response request differs")
    try:
        observed = B.scoped_observation(value["evidence"], plan, request["node"])
        return B.MODEL.validate_observation(observed, plan, request["node"], final=final)
    except (B.Refusal, B.WORKLOAD.Refusal) as error:
        raise Refusal(str(error)) from error


def completed_latch(value, request, plan, boot):
    exact(value, {"state", "records", "retry_authorized", "release_authorized"}, "latch inspection")
    require(value["state"] == "COMPLETED_HISTORICAL"
            and value["retry_authorized"] is False and value["release_authorized"] is False
            and type(value["records"]) is list and len(value["records"]) == 2,
            "completed non-authorizing latch required")
    intent, result = value["records"]
    exact(intent, {"schema", "request", "boot_id", "state"}, "latch intent")
    require(intent == {"schema": "slt-barrier-node-attempt/v1", "request": request,
                       "boot_id": boot, "state": "ATTEMPT_RESERVED"}, "latch intent binding differs")
    exact(result, {"schema", "request_sha256", "state", "response"}, "latch result")
    require(result["schema"] == "slt-barrier-node-result/v1"
            and result["request_sha256"] == B.digest(L.canonical(request))
            and result["state"] == "OBSERVED_COMPLETE", "latch result binding differs")
    observed = response(result["response"], request, plan, final=request["operation"] == "SERVICE_DRAIN")
    require(set(B.MODEL.ENTRY_UNITS) <= set(observed["persistent_masks"])
            and not set(B.MODEL.TERMINAL_INGRESS) & set(observed["active_services"]),
            "historical entry barrier absent")


def project(context, topology_template, baseline_raw, target_raw, plan, receipts,
            preflights, authorization, now, max_age=300):
    """Return exactly slt-live-maintenance-barrier/v2, or fail closed.

    The caller must subsequently publish the projected PREPARE under the
    existing lock and revalidate live evidence at each effect boundary.
    Supplying an evaluator clock never refreshes a receipt's observation time.
    """
    try:
        C.tree([context, topology_template, plan, receipts, preflights, authorization, now, max_age])
    except C.Refusal as error:
        raise Refusal(str(error)) from error
    require(type(now) is int and now > 0 and type(max_age) is int and 0 < max_age <= 900,
            "clock/bound invalid")
    context = A.CTX.validate_context(context, topology_template, baseline_raw, target_raw)
    try:
        plan = B.MODEL.validate_plan(plan, now)
    except B.Refusal as error:
        raise Refusal(str(error)) from error
    require(plan["tx"] == context["tx"]
            and plan["candidate_sha256"] == context["candidate"]["deb_sha256"]
            and plan["storage_cfg_sha256"] == context["baseline_storage_cfg_sha256"]
            and plan["participants"] == [{"node": row["name"], "boot_id": row["boot_id"],
                "san_role": row["san_role"] == "SAN_PARTICIPANT"} for row in context["nodes"]],
            "barrier plan and migration context differ")
    require(type(receipts) is list and len(receipts) == 4
            and type(preflights) is list and len(preflights) == 4, "four-node coverage required")
    names = [row["name"] for row in context["nodes"]]
    by_preflight = {}
    for row in preflights:
        require(type(row) is dict and type(row.get("node")) is str
                and row["node"] in names and row["node"] not in by_preflight,
                "preflight coverage differs")
        by_preflight[row["node"]] = row
    rows, starts, seen = [], [], set()
    for receipt in receipts:
        exact(receipt, {"schema", "verdict", "started_at", "observed_at", "attempts", "response"}, "live receipt")
        require(receipt["schema"] == "slt-barrier-node-runner/v2" and receipt["verdict"] == "COMPLETED",
                "fresh v2 OBSERVE receipt required")
        start, end = receipt["started_at"], receipt["observed_at"]
        require(type(start) is int and type(end) is int
                and plan["issued_at"] <= start <= end <= now <= plan["deadline"]
                and now - start <= max_age, "receipt is stale, future, or outside plan")
        exact(receipt["response"], {"request", "evidence"}, "live response")
        request = receipt["response"]["request"]
        exact(request, {"tx", "plan_sha256", "node", "operation"}, "live request")
        node = request["node"]
        require(type(node) is str and node in names and node not in seen, "receipt node duplicated or foreign")
        require(request == {"tx": plan["tx"], "plan_sha256": B.MODEL.frozen_hash(plan),
                            "node": node, "operation": "OBSERVE"}, "final request binding differs")
        boot = next(row["boot_id"] for row in plan["participants"] if row["node"] == node)
        exact(receipt["attempts"], {"ENTRY_BLOCK", "SERVICE_DRAIN"}, "completed attempts")
        for operation in ("ENTRY_BLOCK", "SERVICE_DRAIN"):
            completed_latch(receipt["attempts"][operation], {**request, "operation": operation}, plan, boot)
        response(receipt["response"], request, plan, final=True)
        preflight = by_preflight[node]
        require(type(preflight.get("observed_at")) is int and end <= preflight["observed_at"] <= now,
                "guard preflight must follow final observation")
        # Services and guard protocol must agree on the actual daemon PID.
        require(type(preflight.get("role_evidence")) is dict, "guard role evidence invalid")
        guard = preflight["role_evidence"].get("thinguard")
        A.validate_guard(guard, context["thinguard_required_by_node"][node], preflight["observed_at"], now, max_age)
        service = next(row for row in receipt["response"]["evidence"]["services"]
                       if row["unit"] == "pve-sharedlvmthin-thin-guard.service")
        require(service["active_state"] == guard["service_active_state"]
                and service["main_pid"] == guard.get("daemon_pid", guard.get("main_pid")),
                "guard lifecycle changed between observations")
        if context["thinguard_required_by_node"][node]:
            require(all(sample["observed_at"] >= end for sample in guard["samples"]),
                    "guard samples predate final barrier")
        rows.append({"node": node, "boot_id": boot,
                     "evidence_sha256": A.digest({"receipt": receipt, "preflight": preflight}),
                     "barrier": True, "workers_clear": True,
                     "guard_quiescent": True, "old_consumers_absent": True})
        starts.append(start); seen.add(node)
    barrier = {"schema": "slt-live-maintenance-barrier/v2", "tx": context["tx"],
               "generation": context["generation"], "context_sha256": context["context_sha256"],
               "observed_at": min(starts), "nodes": sorted(rows, key=lambda row: row["node"]),
               "authorization": "HOLD_ACTIVE", "mutation_performed": True}
    # Reuse all current preflight/candidate/role/guard and authorization gates.
    A.project(context, topology_template, baseline_raw, target_raw,
              preflights, barrier, authorization, now, max_age)
    return copy.deepcopy(barrier)
