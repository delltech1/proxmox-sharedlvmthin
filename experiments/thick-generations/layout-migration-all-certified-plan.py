#!/usr/bin/env python3
"""Evaluate the all-node durable certificate barrier without releasing hold."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path, PurePosixPath


HERE = Path(__file__).resolve().parent
CERT_PATH = HERE / "layout-migration-release-certificate.py"
SPEC = importlib.util.spec_from_file_location("slt_release_certificate", CERT_PATH)
CERT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CERT)

Refusal = CERT.Refusal
require = CERT.require
read_json = CERT.read_json
exact = CERT.exact
digest = CERT.digest
canonical = CERT.canonical

SHA256 = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
CERTIFICATE_DIRECTORY = "/var/lib/pve-sharedlvmthin/maintenance/release-certificates"
ACTIVE_MANIFEST = "/var/lib/pve-sharedlvmthin/maintenance/active.json"


def positive_integer(value, label):
    require(type(value) is int and value > 0, f"{label} is invalid")


def validate_collection(value: dict, *, manifest: dict, manifest_sha: str,
                        certificate: dict, certificate_sha: str,
                        now: int) -> None:
    exact(value, {
        "schema", "collection_id", "tx", "generation", "commit_id",
        "manifest_sha256", "certificate_sha256", "candidate",
        "participant_boots", "collector_sha256", "issued_at", "expires_at",
        "challenges", "allowed_effects",
    }, "certificate collection plan")
    require(value["schema"] == "slt-layout-certificate-collection/v1"
            and type(value["collection_id"]) is str
            and HEX32.fullmatch(value["collection_id"] or "")
            and value["tx"] == manifest["tx"]
            and type(value["generation"]) is int
            and value["generation"] == manifest["generation"]
            and value["commit_id"] == certificate["commit_id"]
            and value["manifest_sha256"] == manifest_sha
            and value["certificate_sha256"] == certificate_sha
            and value["candidate"] == manifest["candidate"]
            and SHA256.fullmatch(value["collector_sha256"] or "")
            and value["allowed_effects"] == [],
            "certificate collection identity or effects differ")
    boots = {row["name"]: row["boot_id"] for row in manifest["nodes"]}
    require(value["participant_boots"] == boots,
            "certificate collection boot identities differ")
    require(type(value["issued_at"]) is int
            and type(value["expires_at"]) is int
            and certificate["committed_at"] <= value["issued_at"] <= now
            and now <= value["expires_at"]
            and value["expires_at"] <= certificate["release_not_after"]
            and 0 < value["expires_at"] - value["issued_at"] <= 300,
            "certificate collection interval is stale, future or overlong")
    challenges = value["challenges"]
    names = list(boots)
    require(type(challenges) is dict and sorted(challenges) == sorted(names)
            and len(set(challenges.values())) == len(names)
            and all(type(item) is str and HEX32.fullmatch(item or "")
                    for item in challenges.values()),
            "certificate collection challenges are incomplete or duplicated")


def validate_inode(value: dict, *, kind: str, expected_size=None,
                   expected_sha=None) -> None:
    fields = {"type", "dev", "ino", "uid", "mode", "nlink"}
    if kind == "file":
        fields |= {"size", "sha256"}
    exact(value, fields, f"{kind} inode identity")
    require(value["type"] == ("regular" if kind == "file" else "directory"),
            f"{kind} inode type is invalid")
    positive_integer(value["dev"], f"{kind} device")
    positive_integer(value["ino"], f"{kind} inode")
    require(type(value["uid"]) is int and value["uid"] == 0
            and type(value["mode"]) is int
            and type(value["nlink"]) is int and value["nlink"] >= 1,
            f"{kind} inode ownership or mode is invalid")
    if kind == "directory":
        require(value["mode"] & 0o022 == 0,
                "directory inode is group/world writable")
    else:
        require(value["mode"] == 0o600 and value["nlink"] == 1
                and type(value["size"]) is int and value["size"] > 0
                and value["size"] == expected_size
                and value["sha256"] == expected_sha,
                "file inode identity differs from exact bytes")


def validate_namespace(value: dict, *, manifest_sha: str, certificate_sha: str,
                       certificate_size: int, decision_name: str) -> None:
    exact(value, {"directory", "decision_name", "components", "certificate"},
          "certificate namespace")
    require(value["directory"] == CERTIFICATE_DIRECTORY
            and value["decision_name"] == decision_name,
            "certificate decision namespace differs")
    expected_paths = ["/"]
    current = PurePosixPath("/")
    for part in PurePosixPath(CERTIFICATE_DIRECTORY).parts[1:]:
        current /= part
        expected_paths.append(str(current))
    components = value["components"]
    require(type(components) is list and len(components) == len(expected_paths),
            "certificate namespace component proof is incomplete")
    seen = set()
    for expected_path, component in zip(expected_paths, components):
        exact(component, {"path", "identity"}, "namespace component")
        require(component["path"] == expected_path
                and component["path"] not in seen,
                "certificate namespace component path differs")
        seen.add(component["path"])
        validate_inode(component["identity"], kind="directory")
    validate_inode(value["certificate"], kind="file",
                   expected_size=certificate_size,
                   expected_sha=certificate_sha)


def validate_ack(record: dict, *, manifest: dict, manifest_sha: str,
                 manifest_size: int,
                 certificate: dict, certificate_sha: str,
                 certificate_size: int, collection: dict,
                 collection_sha: str, names: list[str], now: int,
                 max_age: int, expected_payload_sha: str) -> None:
    exact(record, {
        "schema", "collection_id", "collection_plan_sha256", "challenge",
        "collector_sha256",
        "node", "boot_id_start", "boot_id_end", "tx", "generation",
        "commit_id", "certificate_sha256", "certificate_size",
        "verification_started_at", "verification_finished_at", "durability",
        "namespace", "control_plane", "installed", "payload", "hold",
    }, "certificate ACK")
    node = record["node"]
    boots = collection["participant_boots"]
    require(record["schema"] == "slt-layout-certificate-ack/v1"
            and node in names
            and record["collection_id"] == collection["collection_id"]
            and record["collection_plan_sha256"] == collection_sha
            and record["challenge"] == collection["challenges"][node]
            and record["collector_sha256"] == collection["collector_sha256"]
            and record["boot_id_start"] == boots[node]
            and record["boot_id_end"] == boots[node]
            and record["tx"] == manifest["tx"]
            and type(record["generation"]) is int
            and record["generation"] == manifest["generation"]
            and record["commit_id"] == certificate["commit_id"]
            and record["certificate_sha256"] == certificate_sha
            and type(record["certificate_size"]) is int
            and record["certificate_size"] == certificate_size,
            "certificate ACK identity, challenge or boot differs")
    start = record["verification_started_at"]
    finish = record["verification_finished_at"]
    require(type(start) is int and type(finish) is int
            and certificate["committed_at"] <= start <= finish <= now
            and 0 <= now - finish <= max_age
            and collection["issued_at"] <= start
            and finish <= collection["expires_at"]
            and finish <= certificate["release_not_after"],
            "certificate ACK time is stale, future or out of order")
    durability = record["durability"]
    exact(durability, {"result", "file_fsync", "directory_fsync",
                       "parent_fsync", "exact_reread"}, "durability proof")
    require(durability["result"] in
            ("CERTIFICATE_DURABLE_LOCAL", "VERIFIED_REPLAY")
            and durability["file_fsync"] is True
            and durability["directory_fsync"] is True
            and durability["parent_fsync"] is True
            and durability["exact_reread"] is True,
            "certificate durability proof is incomplete")
    decision_name = f"{manifest['tx']}-{manifest['generation']}.json"
    validate_namespace(record["namespace"], manifest_sha=manifest_sha,
                       certificate_sha=certificate_sha,
                       certificate_size=certificate_size,
                       decision_name=decision_name)
    control = record["control_plane"]
    exact(control, {"cluster_name", "cluster_nodes", "quorate",
                    "corosync_conf_sha256", "storage_cfg_sha256"},
          "certificate ACK control plane")
    require(control["cluster_name"] == manifest["cluster_name"]
            and control["cluster_nodes"] == names
            and control["quorate"] is True
            and control["corosync_conf_sha256"]
                == manifest["corosync_conf_sha256"]
            and control["storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"],
            "certificate ACK control-plane identity differs")
    candidate = manifest["candidate"]
    require(record["installed"] == {
        "package": candidate["package"], "version": candidate["version"],
        "flavor": candidate["flavor"],
        "artifact_sha256": candidate["artifact_sha256"],
        "dpkg_state": "installed",
    }, "certificate ACK installed package differs")
    payload = record["payload"]
    exact(payload, {"dpkg_verify_complete", "dpkg_verify_clean",
                    "package_file_list_sha256"}, "certificate ACK payload")
    require(payload["dpkg_verify_complete"] is True
            and payload["dpkg_verify_clean"] is True
            and payload["package_file_list_sha256"] == expected_payload_sha
            and SHA256.fullmatch(payload["package_file_list_sha256"] or ""),
            "certificate ACK payload proof failed")
    hold = record["hold"]
    exact(hold, {"active", "path", "manifest_sha256", "identity"},
          "certificate ACK hold")
    require(hold["active"] is True and hold["path"] == ACTIVE_MANIFEST
            and hold["manifest_sha256"] == manifest_sha,
            "certificate ACK hold is absent or differs")
    validate_inode(hold["identity"], kind="file",
                   expected_size=manifest_size,
                   expected_sha=manifest_sha)


def evaluate(args) -> dict:
    require(30 <= args.ready_max_age_sec <= 900,
            "READY evidence age bound is unsafe")
    require(0 <= args.ready_max_skew_sec <= 120,
            "READY evidence skew bound is unsafe")
    require(30 <= args.max_age_sec <= 300, "ACK age bound is unsafe")
    require(0 <= args.max_skew_sec <= 120, "ACK skew bound is unsafe")
    manifest, manifest_raw = read_json(Path(args.manifest),
                                       "CONFIG_COMMITTED manifest", 1024 * 1024)
    CERT.BASE.validate_manifest(manifest)
    manifest_sha = digest(manifest_raw)
    expected_certificate, expected_bytes = CERT.evaluate(argparse.Namespace(
        manifest=args.manifest, all_configured=args.all_configured,
        refresh_authorization=args.refresh_authorization,
        all_ready=args.all_ready,
        release_authorization=args.release_authorization,
        node_evidence=args.ready_evidence, max_age_sec=args.ready_max_age_sec,
        max_skew_sec=args.ready_max_skew_sec, now=args.now))
    participant_identity = [
        {"node": row["name"], "boot_id": row["boot_id"],
         "all_ready_evidence_sha256": expected_certificate["participants"][index]
             ["all_ready_evidence_sha256"]}
        for index, row in enumerate(manifest["nodes"])
    ]
    require(expected_certificate["manifest_sha256"] == manifest_sha
            and expected_certificate["tx"] == manifest["tx"]
            and expected_certificate["generation"] == manifest["generation"]
            and expected_certificate["candidate"] == manifest["candidate"]
            and expected_certificate["corosync_conf_sha256"]
                == manifest["corosync_conf_sha256"]
            and expected_certificate["target_storage_cfg_sha256"]
                == manifest["target_storage_cfg_sha256"]
            and expected_certificate["participants"] == participant_identity,
            "fresh certificate evaluation used a different manifest")
    supplied, supplied_raw = read_json(Path(args.certificate),
                                       "release certificate", 1024 * 1024)
    require(supplied_raw == expected_bytes and supplied == expected_certificate,
            "supplied release certificate bytes differ from fresh evaluation")
    certificate_sha = digest(supplied_raw)
    collection, collection_raw = read_json(
        Path(args.collection), "certificate collection plan", 1024 * 1024)
    now = args.now if args.now is not None else int(time.time())
    validate_collection(collection, manifest=manifest,
                        manifest_sha=manifest_sha, certificate=supplied,
                        certificate_sha=certificate_sha, now=now)
    collection_sha = digest(collection_raw)
    names = [row["name"] for row in manifest["nodes"]]
    expected_ready = {row["node"]: row for row in supplied["participants"]}
    ready_payloads = {}
    for path in args.ready_evidence:
        ready, ready_raw = read_json(Path(path), "ALL_READY node evidence",
                                     1024 * 1024)
        node = ready.get("node")
        require(node in expected_ready and node not in ready_payloads
                and digest(ready_raw)
                    == expected_ready[node]["all_ready_evidence_sha256"],
                "ALL_READY evidence identity differs from certificate")
        ready_payloads[node] = ready["payload"]["package_file_list_sha256"]
    require(sorted(ready_payloads) == sorted(names),
            "ALL_READY payload identities are incomplete")
    require(len(args.node_ack) == len(names),
            "exactly one certificate ACK per participant is required")
    acknowledgements = {}
    finished = []
    payloads = set()
    for path in args.node_ack:
        record, raw = read_json(Path(path), "certificate ACK", 1024 * 1024)
        validate_ack(record, manifest=manifest, manifest_sha=manifest_sha,
                     manifest_size=len(manifest_raw),
                     certificate=supplied, certificate_sha=certificate_sha,
                     certificate_size=len(supplied_raw), collection=collection,
                     collection_sha=collection_sha, names=names, now=now,
                     max_age=args.max_age_sec,
                     expected_payload_sha=ready_payloads[record["node"]])
        require(record["node"] not in acknowledgements,
                "certificate ACK participant is duplicated")
        acknowledgements[record["node"]] = digest(raw)
        finished.append(record["verification_finished_at"])
        payloads.add(record["payload"]["package_file_list_sha256"])
    require(sorted(acknowledgements) == sorted(names),
            "certificate ACK set does not cover every participant")
    require(max(finished) - min(finished) <= args.max_skew_sec,
            "certificate ACK collection interval is too wide")
    require(len(payloads) == 1,
            "certificate ACK nodes attest different package payloads")
    body = {
        "schema": "slt-layout-all-certified-plan/v1",
        "phase": "ALL_CERTIFIED",
        "verdict": "READY_FOR_HOLD_RELEASE_AUTHORIZATION",
        "authorization": "NONE", "hold_released": False,
        "mutation_performed": False, "tx": manifest["tx"],
        "generation": manifest["generation"],
        "commit_id": supplied["commit_id"],
        "manifest_sha256": manifest_sha,
        "certificate_sha256": certificate_sha,
        "collection_plan_sha256": collection_sha,
        "candidate": manifest["candidate"], "nodes": names,
        "node_ack_sha256": dict(sorted(acknowledgements.items())),
        "latest_verification_finished_at": max(finished),
        "next_phase": "EXPLICIT_HOLD_RELEASE_AUTHORIZATION_REQUIRED",
    }
    body["plan_sha256"] = digest(canonical(body))
    return body


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--manifest", required=True)
    result.add_argument("--all-configured", required=True)
    result.add_argument("--refresh-authorization", required=True)
    result.add_argument("--all-ready", required=True)
    result.add_argument("--release-authorization", required=True)
    result.add_argument("--ready-evidence", action="append", required=True)
    result.add_argument("--certificate", required=True)
    result.add_argument("--collection", required=True)
    result.add_argument("--node-ack", action="append", required=True)
    result.add_argument("--ready-max-age-sec", type=int, default=300)
    result.add_argument("--ready-max-skew-sec", type=int, default=60)
    result.add_argument("--max-age-sec", type=int, default=120)
    result.add_argument("--max-skew-sec", type=int, default=60)
    result.add_argument("--now", type=int, help=argparse.SUPPRESS)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        require(30 <= args.max_age_sec <= 300, "ACK age bound is unsafe")
        require(0 <= args.max_skew_sec <= 120, "ACK skew bound is unsafe")
        result = evaluate(args)
    except (OSError, Refusal) as error:
        print(json.dumps({"schema": 1, "verdict": "REFUSED",
                          "authorization": "NONE", "hold_released": False,
                          "mutation_performed": False, "reason": str(error)},
                         sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
