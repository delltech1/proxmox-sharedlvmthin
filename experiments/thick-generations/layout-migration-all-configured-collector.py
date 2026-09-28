#!/usr/bin/env python3
"""Collect one fresh, read-only ALL_CONFIGURED-v2 node record."""
import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


V1 = load("slt_configured_collector_v1", "layout-migration-node-evidence.py")
ADAPT = load("slt_configured_receipt_adapter", "layout-migration-configured-receipt-adapter.py")
Refusal = V1.Refusal
require = V1.require
HEX32 = re.compile(r"^[0-9a-f]{32}$")
ROOT = Path("/var/lib/pve-sharedlvmthin/maintenance")


def read_json(path, label, maximum=2 * 1024 * 1024):
    raw = V1.regular_bytes(Path(path), label, maximum)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(label + " is invalid") from error
    require(type(value) is dict, label + " root invalid")
    return value, raw


def candidate_installed(context):
    installed = V1.installed_identity()
    candidate = context["candidate"]
    require(installed["dpkg_state"] == "installed"
            and all(installed[key] == candidate[key]
                    for key in ("package", "version", "flavor", "artifact_sha256")),
            "installed candidate differs")
    verified = V1.command(["dpkg", "--verify", candidate["package"]])
    require(verified == b"", "dpkg payload verification differs")
    return installed


def collect(args):
    context_doc, _ = read_json(args.context, "v2 context document")
    context = context_doc.get("context", context_doc)
    require(type(context) is dict, "v2 context invalid")
    manifest, manifest_raw = read_json(args.manifest, "CONFIG_COMMITTED manifest")
    require(type(args.challenge) is str and HEX32.fullmatch(args.challenge), "challenge invalid")
    nodes = [row["name"] for row in context["nodes"]]
    local, _, quorate, observed = V1.membership(nodes)
    require(quorate and observed == nodes, "membership/quorum differs")
    node = next((row for row in context["nodes"] if row["name"] == local), None)
    require(node is not None, "local node absent from context")
    boot = V1.pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"), "boot id", 128).decode().strip()
    require(boot == node["boot_id"], "boot identity differs")
    storage = V1.regular_bytes(Path("/etc/pve/storage.cfg"), "storage config", 16 * 1024 * 1024)
    require(hashlib.sha256(storage).hexdigest() == context["target_storage_cfg_sha256"],
            "target storage config differs")
    installed = candidate_installed(context)
    role = context["node_roles"][local]
    if role == "SAN_PARTICIPANT":
        active = V1.regular_bytes(ROOT / "active.json", "active hold", 1024 * 1024)
        require(active == manifest_raw, "active hold differs from supplied committed manifest")
        receipt_path = ROOT / "receipts" / (context["tx"] + ".json")
        receipt_raw = V1.regular_bytes(receipt_path, "configured receipt", 1024 * 1024)
        adapter_context = {key: context[key] for key in
                           ("tx", "generation", "context_sha256", "candidate",
                            "target_storage_cfg_sha256")}
        receipt = ADAPT.adapt(receipt_raw, manifest_raw, adapter_context, local, boot)
        vg_identities = V1.actual_lvm_identities(V1.expected_mixed_vgs(storage))
    else:
        require(role == "CONTROL_ONLY" and not os.path.lexists(ROOT / "active.json"),
                "control-only node has a maintenance hold")
        vg_identities = []
        receipt = {"schema": "slt-current-payload-verified/v2",
                   "phase": "CURRENT_PAYLOAD_VERIFIED", "tx": context["tx"],
                   "generation": context["generation"], "node": local, "boot_id": boot,
                   "context_sha256": context["context_sha256"],
                   "candidate": context["candidate"],
                   "target_storage_cfg_sha256": context["target_storage_cfg_sha256"],
                   "recorded_at": int(time.time()), "dpkg_state": "installed",
                   "payload_verified": True}
    guard = load("slt_configured_collector_guard", "layout-migration-node-evidence-v2.py").inactive_guard_evidence()
    _, _, quorate2, observed2 = V1.membership(nodes)
    workers0 = V1.worker_evidence(observed2)
    workers = {"inventory_complete": True,
               "storage_processes": workers0["storage_processes"],
               "transient_units": workers0["blocking_transient_units"],
               "pve_tasks": workers0["active_pve_tasks"]}
    require(quorate2 and observed2 == nodes and not any(workers[k] for k in
            ("storage_processes", "transient_units", "pve_tasks")), "workers/control plane busy")
    require(V1.regular_bytes(Path("/etc/pve/storage.cfg"), "storage recheck", 16 * 1024 * 1024) == storage,
            "storage config changed during collection")
    return {"schema": "slt-layout-all-configured-node/v2", "challenge": args.challenge,
            "observed_at": int(time.time()), "tx": context["tx"],
            "generation": context["generation"], "cluster_name": context["cluster_name"],
            "node": local, "boot_id": boot, "cluster_nodes": nodes, "quorate": True,
            "target_storage_cfg_sha256": context["target_storage_cfg_sha256"],
            "context_sha256": context["context_sha256"], "candidate": context["candidate"],
            "receipt": receipt, "workers": workers, "thinguard": guard,
            "vg_identities": vg_identities}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True); parser.add_argument("--manifest", required=True)
    parser.add_argument("--challenge", required=True)
    args = parser.parse_args()
    try:
        result = collect(args)
    except (Refusal, ADAPT.Refusal, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"schema": "slt-layout-all-configured-node/v2", "verdict": "BLOCKED",
                          "authorization": "NONE", "mutation_performed": False,
                          "reason": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2)); return 0


if __name__ == "__main__":
    sys.exit(main())
