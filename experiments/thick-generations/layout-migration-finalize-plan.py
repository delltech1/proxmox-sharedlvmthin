#!/usr/bin/env python3
"""Evaluate the ALL_CONFIGURED barrier without refreshing or releasing anything.

This is an offline, read-only evaluator.  It consumes one exact
CONFIG_COMMITTED manifest and one freshly collected node record for every
participant.  Its strongest result is READY_FOR_REFRESH_PLAN with
authorization NONE.  It never contacts a node, controls a service, changes a
package, writes pmxcfs or removes the runtime maintenance hold.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
import time
import uuid
from pathlib import Path


SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9.+:~_-]+$")


class Refusal(Exception):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate JSON key: {key}")
        result[key] = value
    return result


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path, label: str, maximum: int) -> tuple[dict, bytes]:
    try:
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode) and not path.is_symlink(),
                f"{label} is not a regular non-symlink file")
        require(0 < before.st_size <= maximum,
                f"{label} size is outside the bound")
        raw = path.read_bytes()
        after = path.lstat()
    except OSError as error:
        raise Refusal(f"{label} is unavailable: {error}") from error
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            and len(raw) == before.st_size,
            f"{label} changed while being read")
    try:
        value = json.loads(raw, object_pairs_hook=no_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(f"{label} is invalid JSON: {error}") from error
    require(type(value) is dict, f"{label} is not an object")
    return value, raw


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields),
            f"{label} fields do not match schema")


def validate_manifest(value: dict) -> dict:
    exact(value, {
        "schema", "tx", "phase", "generation", "issued_at", "expires_at",
        "cluster_name", "corosync_conf_sha256", "nodes", "candidate",
        "baseline_storage_cfg_sha256", "target_storage_cfg_sha256",
        "allowed_effects", "plan_sha256", "node_evidence",
    }, "maintenance manifest")
    require(value["schema"] == "slt-package-maintenance/v1"
            and value["phase"] == "CONFIG_COMMITTED",
            "manifest is not the CONFIG_COMMITTED transaction")
    require(type(value["tx"]) is str and HEX32.fullmatch(value["tx"]),
            "transaction identity is invalid")
    require(type(value["generation"]) is int and value["generation"] > 0,
            "transaction generation is invalid")
    require(type(value["cluster_name"]) is str
            and SAFE_NAME.fullmatch(value["cluster_name"]),
            "cluster identity is invalid")
    require(SHA256.fullmatch(value["corosync_conf_sha256"] or ""),
            "corosync identity is invalid")
    require(SHA256.fullmatch(value["baseline_storage_cfg_sha256"] or "")
            and SHA256.fullmatch(value["target_storage_cfg_sha256"] or "")
            and value["baseline_storage_cfg_sha256"]
                != value["target_storage_cfg_sha256"],
            "storage configuration identities are invalid")
    require(value["allowed_effects"]
            == ["package-unpack", "package-configure-deferred"],
            "configured transaction has an unexpected effect allowlist")
    require(SHA256.fullmatch(value["plan_sha256"] or ""),
            "source plan identity is invalid")
    candidate = value["candidate"]
    exact(candidate, {"package", "version", "flavor", "artifact_sha256",
                      "deb_sha256"}, "candidate")
    require(candidate["package"] == "pve-sharedlvmthin"
            and candidate["flavor"] == "dual"
            and type(candidate["version"]) is str
            and SAFE_VERSION.fullmatch(candidate["version"])
            and SHA256.fullmatch(candidate["artifact_sha256"] or "")
            and SHA256.fullmatch(candidate["deb_sha256"] or ""),
            "candidate identity is invalid")
    nodes = value["nodes"]
    require(type(nodes) is list and len(nodes) >= 3,
            "participant set is incomplete")
    names = []
    for node in nodes:
        exact(node, {"name", "boot_id"}, "participant")
        require(type(node["name"]) is str and SAFE_NAME.fullmatch(node["name"]),
                "participant name is invalid")
        try:
            uuid.UUID(node["boot_id"])
        except (ValueError, TypeError, AttributeError) as error:
            raise Refusal("participant boot identity is invalid") from error
        names.append(node["name"])
    require(names == sorted(names) and len(names) == len(set(names)),
            "participant set is unordered or duplicated")
    return {"nodes": nodes, "names": names, "candidate": candidate}


def validate_receipt(receipt: dict, *, manifest: dict, manifest_sha: str,
                     node: str, boot_id: str) -> None:
    exact(receipt, {
        "schema", "tx", "generation", "phase", "node", "boot_id",
        "package", "version", "flavor", "artifact_sha256",
        "manifest_sha256", "storage_cfg_sha256", "recorded_at",
    }, "configured receipt")
    candidate = manifest["candidate"]
    require(receipt["schema"] == "slt-package-maintenance-receipt/v1"
            and receipt["phase"] == "PACKAGE_CONFIGURED_DEFERRED"
            and receipt["tx"] == manifest["tx"]
            and receipt["generation"] == manifest["generation"]
            and receipt["node"] == node and receipt["boot_id"] == boot_id,
            "configured receipt transaction or node identity differs")
    require(receipt["package"] == candidate["package"]
            and receipt["version"] == candidate["version"]
            and receipt["flavor"] == candidate["flavor"]
            and receipt["artifact_sha256"] == candidate["artifact_sha256"],
            "configured receipt package identity differs")
    require(receipt["manifest_sha256"] == manifest_sha
            and receipt["storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"],
            "configured receipt is not bound to the committed state")
    require(type(receipt["recorded_at"]) is int and receipt["recorded_at"] > 0,
            "configured receipt timestamp is invalid")


def validate_node(record: dict, *, manifest: dict, manifest_sha: str,
                  expected_names: list[str], now: int, max_age: int) -> dict:
    exact(record, {
        "schema", "challenge", "observed_at", "cluster_name", "node",
        "boot_id", "cluster_nodes", "quorate", "corosync_conf_sha256",
        "storage_cfg_sha256", "active_manifest_sha256", "receipt",
        "installed", "payload", "workers", "thinguard",
        "old_consumers_absent",
    }, "ALL_CONFIGURED node evidence")
    require(record["schema"] == "slt-package-finalize-node/v1",
            "node evidence schema is unsupported")
    require(type(record["challenge"]) is str
            and HEX32.fullmatch(record["challenge"]),
            "node evidence challenge is invalid")
    require(type(record["observed_at"]) is int
            and 0 <= now - record["observed_at"] <= max_age,
            "node evidence is stale or from the future")
    require(record["cluster_name"] == manifest["cluster_name"]
            and record["node"] in expected_names
            and record["cluster_nodes"] == expected_names
            and record["quorate"] is True,
            "node cluster identity, membership or quorum differs")
    participant = next(node for node in manifest["nodes"]
                       if node["name"] == record["node"])
    require(record["boot_id"] == participant["boot_id"],
            "node rebooted after the transaction barrier")
    require(record["corosync_conf_sha256"]
            == manifest["corosync_conf_sha256"]
            and record["storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"]
            and record["active_manifest_sha256"] == manifest_sha,
            "node control-plane identity differs from committed state")
    validate_receipt(record["receipt"], manifest=manifest,
                     manifest_sha=manifest_sha, node=record["node"],
                     boot_id=record["boot_id"])
    require(record["receipt"]["recorded_at"] <= record["observed_at"],
            "configured receipt is newer than node evidence")
    installed = record["installed"]
    exact(installed, {"package", "version", "flavor", "artifact_sha256",
                      "dpkg_state"}, "installed package")
    candidate = manifest["candidate"]
    require(installed == {
        "package": candidate["package"], "version": candidate["version"],
        "flavor": "dual", "artifact_sha256": candidate["artifact_sha256"],
        "dpkg_state": "installed",
    }, "installed package does not exactly match the candidate")
    payload = record["payload"]
    exact(payload, {"dpkg_verify_complete", "dpkg_verify_clean",
                    "package_file_list_sha256"}, "payload attestation")
    require(payload["dpkg_verify_complete"] is True
            and payload["dpkg_verify_clean"] is True
            and SHA256.fullmatch(payload["package_file_list_sha256"] or ""),
            "installed payload attestation is incomplete or failed")
    workers = record["workers"]
    exact(workers, {"inventory_complete", "storage_processes",
                    "transient_units", "pve_tasks"}, "worker inventory")
    require(workers["inventory_complete"] is True
            and workers["storage_processes"] == []
            and workers["transient_units"] == []
            and workers["pve_tasks"] == [],
            "node still has a worker or task")
    guard = record["thinguard"]
    exact(guard, {"service_active", "state", "watchdog", "pid",
                  "starttime", "socket_inode", "sample_sha256"},
          "ThinGuard evidence")
    require(guard["service_active"] is True and guard["state"] == "IDLE"
            and guard["watchdog"] == "DISARMED"
            and type(guard["pid"]) is int and guard["pid"] > 1
            and type(guard["starttime"]) is int and guard["starttime"] > 0
            and type(guard["socket_inode"]) is int and guard["socket_inode"] > 0
            and SHA256.fullmatch(guard["sample_sha256"] or ""),
            "ThinGuard is not positively idle and disarmed")
    require(record["old_consumers_absent"] is True,
            "an old PVE consumer may still have the previous plugin loaded")
    return record


def evaluate(args) -> dict:
    manifest, manifest_raw = read_json(Path(args.manifest),
                                       "CONFIG_COMMITTED manifest", 1024 * 1024)
    identity = validate_manifest(manifest)
    require(len(args.node_evidence) == len(identity["names"]),
            "exactly one node evidence file per participant is required")
    now = args.now if args.now is not None else int(time.time())
    manifest_sha = digest(manifest_raw)
    records = []
    evidence_digests = {}
    for path in args.node_evidence:
        record, raw = read_json(Path(path), "ALL_CONFIGURED node evidence",
                                1024 * 1024)
        validate_node(record, manifest=manifest, manifest_sha=manifest_sha,
                      expected_names=identity["names"], now=now,
                      max_age=args.max_age_sec)
        require(record["node"] not in evidence_digests,
                "node evidence is duplicated")
        evidence_digests[record["node"]] = digest(raw)
        records.append(record)
    require(sorted(evidence_digests) == identity["names"],
            "node evidence does not cover the participant set")
    observed = [record["observed_at"] for record in records]
    require(max(observed) - min(observed) <= args.max_skew_sec,
            "node evidence collection interval is too wide")
    require(len({record["challenge"] for record in records}) == len(records),
            "node challenges are duplicated")
    payload_identities = {
        record["payload"]["package_file_list_sha256"] for record in records
    }
    require(len(payload_identities) == 1,
            "nodes attest different installed payload file lists")
    body = {
        "schema": "slt-layout-finalize-plan/v1",
        "phase": "ALL_CONFIGURED",
        "verdict": "READY_FOR_REFRESH_PLAN",
        "authorization": "NONE",
        "mutation_performed": False,
        "tx": manifest["tx"], "generation": manifest["generation"],
        "cluster_name": manifest["cluster_name"],
        "manifest_sha256": manifest_sha,
        "candidate": identity["candidate"],
        "target_storage_cfg_sha256": manifest["target_storage_cfg_sha256"],
        "nodes": identity["names"],
        "node_evidence_sha256": dict(sorted(evidence_digests.items())),
        "next_phase": "EXPLICIT_REFRESH_AUTHORIZATION_REQUIRED",
    }
    body["plan_sha256"] = digest(canonical(body))
    return body


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", required=True)
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
