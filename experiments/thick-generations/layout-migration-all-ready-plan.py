#!/usr/bin/env python3
"""Evaluate the post-refresh ALL_READY barrier without releasing the hold.

The evaluator is deliberately offline and read-only.  It binds an exact
CONFIG_COMMITTED transaction, its ALL_CONFIGURED plan, one explicit refresh
authorization and one fresh post-refresh receipt per participant.  Its
strongest result is READY_FOR_RELEASE_COMMIT with authorization NONE.  It
never contacts a node, refreshes a service, writes a release certificate or
removes active.json.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import time
import uuid
from pathlib import Path


HERE = Path(__file__).resolve().parent
BASE_PATH = HERE / "layout-migration-finalize-plan.py"
SPEC = importlib.util.spec_from_file_location("slt_finalize_base", BASE_PATH)
BASE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BASE)

Refusal = BASE.Refusal
require = BASE.require
read_json = BASE.read_json
exact = BASE.exact
digest = BASE.digest
canonical = BASE.canonical
validate_manifest = BASE.validate_manifest

SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
ALLOWED_UNITS = {
    "pve-sharedlvmthin-thin-guard.service",
    "pvedaemon.service",
    "pvestatd.service",
    "pveproxy.service",
    "pve-ha-lrm.service",
}


def uuid_value(value, label):
    try:
        uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as error:
        raise Refusal(f"{label} is invalid") from error


def validate_all_configured(plan: dict, manifest: dict,
                            manifest_sha: str) -> None:
    exact(plan, {
        "schema", "phase", "verdict", "authorization",
        "mutation_performed", "tx", "generation", "cluster_name",
        "manifest_sha256", "candidate", "target_storage_cfg_sha256",
        "nodes", "node_evidence_sha256", "next_phase", "plan_sha256",
    }, "ALL_CONFIGURED plan")
    require(plan["schema"] == "slt-layout-finalize-plan/v1"
            and plan["phase"] == "ALL_CONFIGURED"
            and plan["verdict"] == "READY_FOR_REFRESH_PLAN"
            and plan["authorization"] == "NONE"
            and plan["mutation_performed"] is False
            and plan["next_phase"]
                == "EXPLICIT_REFRESH_AUTHORIZATION_REQUIRED",
            "ALL_CONFIGURED plan is not a non-authorizing ready plan")
    require(plan["tx"] == manifest["tx"]
            and type(plan["generation"]) is int
            and plan["generation"] == manifest["generation"]
            and plan["cluster_name"] == manifest["cluster_name"]
            and plan["manifest_sha256"] == manifest_sha
            and plan["candidate"] == manifest["candidate"]
            and plan["target_storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"],
            "ALL_CONFIGURED plan identity differs from the transaction")
    names = [row["name"] for row in manifest["nodes"]]
    require(plan["nodes"] == names
            and type(plan["node_evidence_sha256"]) is dict
            and sorted(plan["node_evidence_sha256"]) == names
            and all(SHA256.fullmatch(value or "")
                    for value in plan["node_evidence_sha256"].values()),
            "ALL_CONFIGURED participant evidence is incomplete")
    body = dict(plan)
    claimed = body.pop("plan_sha256")
    require(SHA256.fullmatch(claimed or "")
            and claimed == digest(canonical(body)),
            "ALL_CONFIGURED plan self-identity is invalid")


def validate_authorization(value: dict, *, manifest: dict,
                           manifest_sha: str, all_configured_sha: str,
                           now: int) -> dict:
    exact(value, {
        "schema", "tx", "generation", "manifest_sha256",
        "all_configured_plan_sha256", "candidate", "participant_boots",
        "target_storage_cfg_sha256", "corosync_conf_sha256",
        "authorization_id", "issued_at", "expires_at", "node_order",
        "service_plan", "challenges",
    }, "refresh authorization")
    require(value["schema"] == "slt-layout-refresh-authorization/v1"
            and value["tx"] == manifest["tx"]
            and type(value["generation"]) is int
            and value["generation"] == manifest["generation"]
            and value["manifest_sha256"] == manifest_sha
            and value["all_configured_plan_sha256"] == all_configured_sha
            and value["candidate"] == manifest["candidate"]
            and value["target_storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"]
            and value["corosync_conf_sha256"]
                == manifest["corosync_conf_sha256"],
            "refresh authorization identity differs from the transaction")
    require(type(value["authorization_id"]) is str
            and HEX32.fullmatch(value["authorization_id"]),
            "refresh authorization identity is invalid")
    require(type(manifest["issued_at"]) is int
            and type(manifest["expires_at"]) is int
            and manifest["issued_at"] <= now <= manifest["expires_at"]
            and 0 < manifest["expires_at"] - manifest["issued_at"] <= 1800,
            "CONFIG_COMMITTED transaction is not currently valid")
    require(type(value["issued_at"]) is int
            and type(value["expires_at"]) is int
            and value["issued_at"] <= now <= value["expires_at"]
            and 0 < value["expires_at"] - value["issued_at"] <= 1800,
            "refresh authorization is stale, future or overlong")
    require(value["issued_at"] >= manifest["issued_at"],
            "refresh authorization predates CONFIG_COMMITTED")
    names = [row["name"] for row in manifest["nodes"]]
    boots = {row["name"]: row["boot_id"] for row in manifest["nodes"]}
    require(value["node_order"] == names
            and value["participant_boots"] == boots,
            "refresh participant order or boot identities differ")
    challenges = value["challenges"]
    require(type(challenges) is dict and sorted(challenges) == names
            and len(set(challenges.values())) == len(names)
            and all(type(item) is str and HEX32.fullmatch(item)
                    for item in challenges.values()),
            "refresh challenges are incomplete, duplicated or invalid")
    plan = value["service_plan"]
    require(type(plan) is list and len(plan) == len(names),
            "service plan does not cover every participant")
    normalized = {}
    for row in plan:
        exact(row, {"node", "daemon_reload", "units"}, "node service plan")
        require(row["node"] in names and row["node"] not in normalized
                and row["daemon_reload"] is True
                and type(row["units"]) is list,
                "node service plan identity or daemon-reload intent is invalid")
        units = []
        for unit in row["units"]:
            exact(unit, {"unit", "operation", "was_active"}, "service intent")
            require(unit["unit"] in ALLOWED_UNITS
                    and unit["operation"] == "try-restart-active"
                    and type(unit["was_active"]) is bool,
                    "service intent is outside the fixed allowlist")
            units.append(unit["unit"])
        require(len(units) == len(set(units))
                and set(units) == ALLOWED_UNITS,
                "service plan has missing or duplicate fixed units")
        guard_intent = next(unit for unit in row["units"]
                            if unit["unit"]
                            == "pve-sharedlvmthin-thin-guard.service")
        require(guard_intent["was_active"] is True,
                "service plan contradicts the active ThinGuard barrier")
        normalized[row["node"]] = row
    require(list(normalized) == names,
            "service plan order differs from participant order")
    return normalized


def validate_lifecycle(value: dict, *, expected_active: bool,
                       label: str) -> None:
    exact(value, {"active", "pid", "starttime"}, label)
    require(type(value["active"]) is bool
            and type(value["pid"]) is int
            and type(value["starttime"]) is int
            and value["active"] is expected_active,
            f"{label} activity differs from the service plan")
    if expected_active:
        require(value["pid"] > 1 and value["starttime"] > 0,
                f"{label} has no positive lifecycle identity")
    else:
        require(value["pid"] == 0 and value["starttime"] == 0,
                f"{label} invents an inactive lifecycle identity")


def validate_ready(record: dict, *, manifest: dict, manifest_sha: str,
                   authorization_sha: str, expected_names: list[str],
                   service_plan: dict, challenges: dict, now: int,
                   max_age: int) -> None:
    exact(record, {
        "schema", "challenge", "authorization_sha256", "observed_at",
        "tx", "generation", "cluster_name", "node", "boot_id",
        "cluster_nodes", "quorate", "corosync_conf_sha256",
        "storage_cfg_sha256", "active_manifest_sha256", "installed",
        "payload", "workers", "thinguard", "hold", "refresh", "health",
    }, "ALL_READY node evidence")
    node = record["node"]
    require(record["schema"] == "slt-package-all-ready-node/v1"
            and node in expected_names
            and record["challenge"] == challenges[node]
            and record["authorization_sha256"] == authorization_sha
            and record["tx"] == manifest["tx"]
            and type(record["generation"]) is int
            and record["generation"] == manifest["generation"],
            "ALL_READY transaction, authorization or challenge differs")
    require(type(record["observed_at"]) is int
            and 0 <= now - record["observed_at"] <= max_age,
            "ALL_READY evidence is stale or from the future")
    participant = next(row for row in manifest["nodes"] if row["name"] == node)
    require(record["cluster_name"] == manifest["cluster_name"]
            and record["boot_id"] == participant["boot_id"]
            and record["cluster_nodes"] == expected_names
            and record["quorate"] is True
            and record["corosync_conf_sha256"]
                == manifest["corosync_conf_sha256"]
            and record["storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"]
            and record["active_manifest_sha256"] == manifest_sha,
            "ALL_READY cluster, boot, config or active hold identity differs")
    candidate = manifest["candidate"]
    require(record["installed"] == {
        "package": candidate["package"], "version": candidate["version"],
        "flavor": "dual", "artifact_sha256": candidate["artifact_sha256"],
        "dpkg_state": "installed",
    }, "ALL_READY installed package differs from the candidate")
    payload = record["payload"]
    exact(payload, {"dpkg_verify_complete", "dpkg_verify_clean",
                    "package_file_list_sha256"}, "ALL_READY payload")
    require(payload["dpkg_verify_complete"] is True
            and payload["dpkg_verify_clean"] is True
            and SHA256.fullmatch(payload["package_file_list_sha256"] or ""),
            "ALL_READY payload attestation failed")
    workers = record["workers"]
    exact(workers, {"inventory_complete", "storage_processes",
                    "transient_units", "pve_tasks",
                    "retired_service_lifecycles_absent"}, "ALL_READY workers")
    require(workers["inventory_complete"] is True
            and workers["storage_processes"] == []
            and workers["transient_units"] == []
            and workers["pve_tasks"] == []
            and type(workers["retired_service_lifecycles_absent"]) is list,
            "ALL_READY still has an ambiguous worker or task")
    for lifecycle in workers["retired_service_lifecycles_absent"]:
        exact(lifecycle, {"unit", "pid", "starttime"},
              "retired service lifecycle")
        require(type(lifecycle["unit"]) is str
                and lifecycle["unit"] in ALLOWED_UNITS
                and type(lifecycle["pid"]) is int and lifecycle["pid"] > 1
                and type(lifecycle["starttime"]) is int
                and lifecycle["starttime"] > 0,
                "retired service lifecycle identity is invalid")
    guard = record["thinguard"]
    exact(guard, {"service_active", "state", "watchdog", "pid",
                  "starttime", "socket_inode", "sample_sha256"},
          "ALL_READY ThinGuard")
    require(guard["service_active"] is True and guard["state"] == "IDLE"
            and guard["watchdog"] == "DISARMED"
            and type(guard["pid"]) is int and guard["pid"] > 1
            and type(guard["starttime"]) is int and guard["starttime"] > 0
            and type(guard["socket_inode"]) is int and guard["socket_inode"] > 0
            and SHA256.fullmatch(guard["sample_sha256"] or ""),
            "ALL_READY ThinGuard is not positively idle and disarmed")
    hold = record["hold"]
    exact(hold, {"active", "manifest_sha256"}, "ALL_READY hold")
    require(hold["active"] is True
            and hold["manifest_sha256"] == manifest_sha,
            "maintenance hold was released or changed before commit")
    refresh = record["refresh"]
    exact(refresh, {"state", "journal_sha256", "started_at", "finished_at",
                    "daemon_reload", "units"},
          "refresh receipt")
    daemon_reload = refresh["daemon_reload"]
    exact(daemon_reload, {"attempted", "terminal", "rc"},
          "daemon-reload result")
    require(refresh["state"] == "COMPLETE"
            and SHA256.fullmatch(refresh["journal_sha256"] or "")
            and type(refresh["started_at"]) is int
            and type(refresh["finished_at"]) is int
            and refresh["started_at"] <= refresh["finished_at"]
            and daemon_reload["attempted"] is True
            and daemon_reload["terminal"] is True
            and type(daemon_reload["rc"]) is int
            and daemon_reload["rc"] == 0,
            "refresh journal or daemon-reload result is incomplete")
    expected_units = service_plan[node]["units"]
    require(type(refresh["units"]) is list
            and len(refresh["units"]) == len(expected_units),
            "refresh results do not cover the exact service plan")
    retired = []
    guard_after = None
    for intent, result in zip(expected_units, refresh["units"]):
        exact(result, {"unit", "effect", "terminal", "rc", "before", "after"},
              "service refresh result")
        active = intent["was_active"]
        require(result["unit"] == intent["unit"]
                and result["terminal"] is True
                and type(result["rc"]) is int and result["rc"] == 0,
                "service refresh result differs from its intent")
        validate_lifecycle(result["before"], expected_active=active,
                           label="pre-refresh lifecycle")
        validate_lifecycle(result["after"], expected_active=active,
                           label="post-refresh lifecycle")
        if active:
            require(result["effect"] == "REFRESHED"
                    and (result["before"]["pid"], result["before"]["starttime"])
                    != (result["after"]["pid"], result["after"]["starttime"]),
                    "active service lacks a new proven lifecycle")
            retired.append({"unit": result["unit"],
                            "pid": result["before"]["pid"],
                            "starttime": result["before"]["starttime"]})
            if result["unit"] == "pve-sharedlvmthin-thin-guard.service":
                guard_after = result["after"]
        else:
            require(result["effect"] == "PRESERVED_INACTIVE",
                    "inactive service was unexpectedly started")
    require(workers["retired_service_lifecycles_absent"] == retired,
            "old service lifecycle absence is incomplete or differs")
    require(guard_after is not None
            and guard["pid"] == guard_after["pid"]
            and guard["starttime"] == guard_after["starttime"],
            "ThinGuard sample is not bound to the refreshed lifecycle")
    journal_body = {
        "started_at": refresh["started_at"],
        "finished_at": refresh["finished_at"],
        "daemon_reload": refresh["daemon_reload"],
        "units": refresh["units"],
    }
    require(refresh["journal_sha256"] == digest(canonical(journal_body)),
            "refresh journal digest does not bind its exact contents")
    health = record["health"]
    exact(health, {"observed_at", "storage_api", "doctor", "recovery",
                   "unexpected"},
          "ALL_READY health")
    require(type(health["observed_at"]) is int
            and health["storage_api"] == "PASS"
            and health["doctor"] == "PASS"
            and health["recovery"] == "PASS"
            and health["unexpected"] == [],
            "ALL_READY health contains a failure, unknown or exception")
    require(refresh["started_at"] >= 0
            and refresh["finished_at"] <= health["observed_at"]
            and health["observed_at"] <= record["observed_at"],
            "refresh, health and ALL_READY evidence are out of order")


def evaluate(args) -> dict:
    manifest, manifest_raw = read_json(Path(args.manifest),
                                       "CONFIG_COMMITTED manifest", 1024 * 1024)
    identity = validate_manifest(manifest)
    manifest_sha = digest(manifest_raw)
    all_configured, all_configured_raw = read_json(
        Path(args.all_configured), "ALL_CONFIGURED plan", 1024 * 1024)
    validate_all_configured(all_configured, manifest, manifest_sha)
    all_configured_sha = digest(all_configured_raw)
    authorization, authorization_raw = read_json(
        Path(args.authorization), "refresh authorization", 1024 * 1024)
    now = args.now if args.now is not None else int(time.time())
    service_plan = validate_authorization(
        authorization, manifest=manifest, manifest_sha=manifest_sha,
        all_configured_sha=all_configured_sha, now=now)
    authorization_sha = digest(authorization_raw)
    names = identity["names"]
    require(len(args.node_evidence) == len(names),
            "exactly one ALL_READY record per participant is required")
    evidence = {}
    observed = []
    intervals = {}
    payloads = set()
    journals = set()
    for path in args.node_evidence:
        record, raw = read_json(Path(path), "ALL_READY node evidence",
                                1024 * 1024)
        validate_ready(
            record, manifest=manifest, manifest_sha=manifest_sha,
            authorization_sha=authorization_sha, expected_names=names,
            service_plan=service_plan, challenges=authorization["challenges"],
            now=now, max_age=args.max_age_sec)
        require(record["node"] not in evidence,
                "ALL_READY node evidence is duplicated")
        evidence[record["node"]] = digest(raw)
        observed.append(record["observed_at"])
        payloads.add(record["payload"]["package_file_list_sha256"])
        journals.add(record["refresh"]["journal_sha256"])
        intervals[record["node"]] = (record["refresh"]["started_at"],
                                     record["refresh"]["finished_at"])
    require(sorted(evidence) == names,
            "ALL_READY evidence does not cover the participant set")
    require(max(observed) - min(observed) <= args.max_skew_sec,
            "ALL_READY evidence collection interval is too wide")
    require(len(payloads) == 1, "ALL_READY nodes attest different payloads")
    require(len(journals) == len(names),
            "refresh journals are duplicated across participants")
    previous_finish = None
    for node in names:
        started, finished = intervals[node]
        require(started >= authorization["issued_at"]
                and started >= manifest["issued_at"]
                and finished <= authorization["expires_at"]
                and finished <= manifest["expires_at"],
                "node refresh is outside the authorization interval")
        if previous_finish is not None:
            require(started >= previous_finish,
                    "node refresh intervals overlap or violate node order")
        previous_finish = finished
    body = {
        "schema": "slt-layout-all-ready-plan/v1", "phase": "ALL_READY",
        "verdict": "READY_FOR_RELEASE_COMMIT", "authorization": "NONE",
        "mutation_performed": False, "tx": manifest["tx"],
        "generation": manifest["generation"],
        "cluster_name": manifest["cluster_name"],
        "manifest_sha256": manifest_sha,
        "all_configured_plan_sha256": all_configured_sha,
        "refresh_authorization_sha256": authorization_sha,
        "candidate": identity["candidate"], "nodes": names,
        "node_evidence_sha256": dict(sorted(evidence.items())),
        "evidence_set_sha256": digest(canonical(dict(sorted(evidence.items())))),
        "ready_observed_at_max": max(observed),
        "next_phase": "EXPLICIT_RELEASE_COMMIT_REQUIRED",
    }
    body["plan_sha256"] = digest(canonical(body))
    return body


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", required=True)
    result.add_argument("--all-configured", required=True)
    result.add_argument("--authorization", required=True)
    result.add_argument("--node-evidence", action="append", required=True)
    result.add_argument("--max-age-sec", type=int, default=300)
    result.add_argument("--max-skew-sec", type=int, default=60)
    result.add_argument("--now", type=int, help=argparse.SUPPRESS)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        require(30 <= args.max_age_sec <= 900, "evidence age bound is unsafe")
        require(0 <= args.max_skew_sec <= 120, "evidence skew bound is unsafe")
        result = evaluate(args)
    except Refusal as error:
        print(json.dumps({"schema": 1, "verdict": "REFUSED",
                          "authorization": "NONE",
                          "mutation_performed": False,
                          "reason": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
