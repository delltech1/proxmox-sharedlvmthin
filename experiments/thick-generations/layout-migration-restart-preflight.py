#!/usr/bin/env python3
"""Explicit archived-PREPARE restart evidence; never relabel unpacked as installed.

Pure validation is importable off-node. The separate read-only collector uses
fixed live paths and an exact pinned archive request; it never creates a hold,
changes package state, writes configuration, or authorizes a mutation.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import time

HERE = Path(__file__).resolve().parent
SCHEMA = "slt-layout-node-maintenance-restart-evidence/v2"
RECOVERY = "slt-layout-archived-prepare-restart/v1"
RECOVERY_REPLACEMENT = "slt-layout-archived-prepare-replacement/v2"
SHA = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")
VERSION = re.compile(r"^[0-9A-Za-z.+:~_-]+$")
ROOT = PurePosixPath("/var/lib/pve-sharedlvmthin")


class Refusal(Exception): pass


def require(value, message):
    if not value: raise Refusal(message)


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    return json.loads(raw, object_pairs_hook=pairs)


def hex_document(value):
    require(type(value) is str and 0 < len(value) <= 2 * 1024 * 1024
            and len(value) % 2 == 0 and re.fullmatch("[0-9a-f]+", value), "exact document hex invalid")
    raw = bytes.fromhex(value)
    return raw, decode(raw)


def validate_tree(tree, *, cas=False):
    require(type(tree) is dict and 0 < len(tree) <= 1024 and "" in tree, "evidence tree absent or unbounded")
    for path, item in tree.items():
        require(type(path) is str and type(item) is dict
                and (path == "" or (not path.startswith("/") and all(
                    re.fullmatch(r"[A-Za-z0-9_.-]{1,240}", part) and part not in (".", "..") for part in path.split("/")))),
                "evidence tree path invalid")
        if item.get("kind") == "directory":
            exact(item, {"kind", "dev", "ino", "uid", "mode"}, "evidence directory")
            require(type(item["dev"]) is int and item["dev"] >= 0 and type(item["ino"]) is int and item["ino"] > 0
                    and type(item["uid"]) is int and item["uid"] == 0 and item["mode"] == 0o700
                    and path in (("",) if cas else ("", "attempts", "sidecars", "receipts")), "evidence directory unsafe")
        else:
            exact(item, {"kind", "identity", "sha256"}, "evidence file")
            identity = item["identity"]
            require(item["kind"] == "file" and type(identity) is list and len(identity) == 8
                    and all(type(value) is int for value in identity) and identity[0] >= 0 and identity[1] > 0
                    and identity[2] == 0 and identity[3] == 0o100600 and identity[4] == 1
                    and 0 <= identity[5] <= 1024 * 1024 and type(item["sha256"]) is str and SHA.fullmatch(item["sha256"]),
                    "evidence file unsafe")
        if cas:
            require(not any(word in path.upper() for word in ("INTENT", "OUTCOME"))
                    and "/" not in path, "CAS reservation or unknown object present")
    require(tree[""].get("kind") == "directory", "evidence root invalid")


def validate(row, *, tx, generation, baseline_sha256, target_sha256, now, max_age=300):
    require(type(now) is int and now > 0 and type(max_age) is int and 0 < max_age <= 900, "restart clock invalid")
    require(row.get("schema") == SCHEMA and row.get("role") == "SAN_PARTICIPANT"
            and row.get("action") == "UNPACK_CONFIGURE", "restart evidence is SAN-only")
    installed = row["installed"]
    exact(installed, {"package", "version", "config_version", "flavor", "artifact_sha256", "dpkg_state"}, "restart package")
    require(installed["dpkg_state"] == "unpacked" and installed["package"] == "pve-sharedlvmthin"
            and installed["flavor"] == "dual" and type(installed["config_version"]) is str
            and VERSION.fullmatch(installed["config_version"])
            and installed["config_version"] != installed["version"],
            "restart package must be an exact unpacked candidate with old Config-Version")
    recovery = row["recovery"]
    recovery_fields = {"schema", "tx", "generation", "observed_at", "archive_path", "archive_tree",
        "abort_request", "predecessor_manifest_hex", "preinst_receipt_hex", "maintenance_absent",
        "cas_tree", "blocking_processes", "authorization", "mutation_performed"}
    if recovery.get("schema") == RECOVERY_REPLACEMENT:
        recovery_fields.add("successor_version_order")
    exact(recovery, recovery_fields, "restart provenance")
    require(recovery["schema"] in {RECOVERY, RECOVERY_REPLACEMENT} and recovery["tx"] == tx
            and type(generation) is int and generation > 0
            and type(recovery["generation"]) is int and recovery["generation"] == generation
            and type(recovery["observed_at"]) is int and type(row.get("observed_at")) is int
            and 0 <= now - recovery["observed_at"] <= max_age
            and recovery["observed_at"] <= row["observed_at"] <= now
            and recovery["maintenance_absent"] is True and recovery["blocking_processes"] == []
            and recovery["authorization"] == "NONE" and recovery["mutation_performed"] is False,
            "restart provenance time/identity/ceiling invalid")
    request = recovery["abort_request"]
    exact(request, {"schema", "operation_id", "node", "boot_id", "manifest_sha256", "evidence_tree_sha256",
                    "storage_cfg_sha256", "expected_package", "receipt_sha256"}, "predecessor abort request")
    require(request["schema"] == "slt-expired-prepare-archive/v1"
            and type(request["operation_id"]) is str and HEX32.fullmatch(request["operation_id"])
            and request["node"] == row["node"] and request["boot_id"] == row["role_evidence"]["boot_id"]
            and request["expected_package"] == installed and request["storage_cfg_sha256"] == baseline_sha256,
            "predecessor abort binding differs")
    raw, manifest = hex_document(recovery["predecessor_manifest_hex"])
    receipt_raw, receipt = hex_document(recovery["preinst_receipt_hex"])
    exact(manifest, {"schema", "tx", "phase", "generation", "issued_at", "expires_at", "cluster_name",
        "corosync_conf_sha256", "nodes", "candidate", "baseline_storage_cfg_sha256", "target_storage_cfg_sha256",
        "allowed_effects", "plan_sha256", "node_evidence"}, "archived predecessor manifest")
    if recovery["schema"] == RECOVERY:
        require(all(installed[key] == row["candidate"][key]
                    for key in ("package", "version", "flavor", "artifact_sha256")),
                "restart package differs from candidate")
        expected_predecessor_candidate = {
            key: row["candidate"][key]
            for key in ("package", "version", "flavor", "artifact_sha256", "deb_sha256")}
    else:
        require(recovery["successor_version_order"] == "LT"
                and row["candidate"]["package"] == installed["package"]
                and row["candidate"]["flavor"] == installed["flavor"]
                and row["candidate"]["version"] != installed["version"]
                and row["candidate"]["artifact_sha256"] != installed["artifact_sha256"],
                "replacement successor identity/order invalid")
        expected_predecessor_candidate = None
    require(type(manifest) is dict and manifest.get("schema") == "slt-package-maintenance/v1"
            and manifest.get("phase") == "PREPARE_READY" and type(manifest.get("tx")) is str
            and HEX32.fullmatch(manifest["tx"]) and manifest["tx"] != tx
            and type(manifest.get("generation")) is int and manifest["generation"] > 0
            and type(manifest.get("issued_at")) is int and type(manifest.get("expires_at")) is int
            and 0 < manifest["issued_at"] < manifest["expires_at"] < recovery["observed_at"]
            and manifest["expires_at"] - manifest["issued_at"] <= 1800
            and digest(raw) == request["manifest_sha256"]
            and manifest["baseline_storage_cfg_sha256"] == baseline_sha256
            and type(manifest["target_storage_cfg_sha256"]) is str
            and SHA.fullmatch(manifest["target_storage_cfg_sha256"])
            and (manifest["target_storage_cfg_sha256"] == target_sha256
                 if recovery["schema"] == RECOVERY else True)
            and manifest["cluster_name"] == row["cluster_name"]
            and manifest["corosync_conf_sha256"] == row["corosync_conf_sha256"]
            and manifest["allowed_effects"] == ["package-unpack", "package-configure-deferred"]
            and type(manifest["plan_sha256"]) is str and SHA.fullmatch(manifest["plan_sha256"])
            and (manifest["candidate"] == expected_predecessor_candidate if expected_predecessor_candidate is not None else
                 all(manifest["candidate"].get(key) == installed[key]
                     for key in ("package", "version", "flavor", "artifact_sha256")))
            and {"name": row["node"], "boot_id": row["role_evidence"]["boot_id"]} in manifest["nodes"],
            "archived predecessor manifest differs or transaction is reused")
    expected_path = str(ROOT / ("maintenance.expired-" + manifest["tx"] + "-" + request["operation_id"]))
    require(recovery["archive_path"] == expected_path, "archive path is not the exact fixed-root sibling")
    receipt_name = manifest["tx"] + ".json"
    exact(receipt, {"schema", "tx", "generation", "phase", "node", "boot_id", "package", "version", "flavor",
                    "artifact_sha256", "manifest_sha256", "storage_cfg_sha256", "recorded_at"}, "archived PREINST")
    require(receipt["schema"] == "slt-package-maintenance-receipt/v1" and receipt["phase"] == "PREINST_ACCEPTED"
            and receipt["tx"] == manifest["tx"] and type(receipt["generation"]) is int
            and receipt["generation"] == manifest["generation"]
            and all(receipt[key] == request[key] for key in ("node", "boot_id", "manifest_sha256", "storage_cfg_sha256"))
            and all(receipt[key] == installed[key] for key in ("package", "version", "flavor", "artifact_sha256"))
            and type(receipt["recorded_at"]) is int and manifest["issued_at"] <= receipt["recorded_at"] <= manifest["expires_at"]
            and request["receipt_sha256"] == {receipt_name: digest(receipt_raw)}, "archived PREINST receipt differs")
    tree = recovery["archive_tree"]
    validate_tree(tree)
    require(type(tree) is dict and digest(canonical(tree)) == request["evidence_tree_sha256"], "archived tree digest differs")
    require(tree.get("active.json", {}).get("sha256") == digest(raw)
            and tree.get("receipts/" + receipt_name, {}).get("sha256") == digest(receipt_raw)
            and {key for key, value in tree.items() if key.startswith("receipts/") and value.get("kind") == "file"}
                == {"receipts/" + receipt_name}, "archive does not contain exact manifest and receipt")
    cas = recovery["cas_tree"]
    validate_tree(cas, cas=True)
    return {"predecessor_tx": manifest["tx"], "predecessor_generation": manifest["generation"],
            "archive_path": expected_path, "archive_tree_sha256": request["evidence_tree_sha256"],
            "predecessor_manifest_sha256": digest(raw), "preinst_receipt_sha256": digest(receipt_raw),
            "config_version": installed["config_version"], "provenance_sha256": digest(canonical(recovery))}


def collect_provenance(inputs, candidate):
    """Read-only fixed-path collector; inputs pin the original abort request."""
    exact(inputs, {"tx", "generation", "abort_request"}, "restart collection request")
    require(type(inputs["tx"]) is str and HEX32.fullmatch(inputs["tx"])
            and type(inputs["generation"]) is int and inputs["generation"] > 0, "new transaction invalid")
    spec = importlib.util.spec_from_file_location("restart_archive_reader", HERE / "layout-migration-expired-prepare-archive.py")
    archive = importlib.util.module_from_spec(spec); spec.loader.exec_module(archive)
    request = inputs["abort_request"]
    # Read no caller-selected path: old tx comes only from its pinned receipt name.
    receipts = request.get("receipt_sha256", {})
    require(type(receipts) is dict and len(receipts) == 1, "one exact PREINST receipt required")
    receipt_name = next(iter(receipts))
    require(re.fullmatch(r"[0-9a-f]{32}\.json", receipt_name) and HEX32.fullmatch(request.get("operation_id", "")), "archive identity invalid")
    path = Path(str(ROOT)) / ("maintenance.expired-" + receipt_name[:-5] + "-" + request["operation_id"])
    descriptors, ids = archive.FILES.BASE._open_pinned_directory(path, 0)
    try:
        tree, payloads = archive._scan(descriptors[-1], 0)
        facts = archive.current_facts()
        now = int(time.time())
        manifest_raw = payloads["active.json"]
        manifest = archive.validate(request, manifest_raw, facts, now)
        archive.validate_payloads(request, manifest_raw, manifest, payloads)
        require(digest(canonical(tree)) == request["evidence_tree_sha256"], "archive changed")
        require(not os.path.lexists(archive.FIXED_ROOT), "new maintenance hold already exists")
        cas_before = archive.snapshot(archive.CAS_ROOT, 0)["tree"]
        require(archive.current_facts() == facts, "current facts changed during restart collection")
        require(archive._scan(descriptors[-1], 0) == (tree, payloads), "archive changed during collection")
        archive.FILES.BASE._require_directory_chain(descriptors, ids, path, 0)
        require(archive.snapshot(archive.CAS_ROOT, 0)["tree"] == cas_before
                and not os.path.lexists(archive.FIXED_ROOT), "CAS/maintenance namespace changed")
        require(facts["package"]["dpkg_state"] == "unpacked", "restart requires an unpacked SAN package")
        same = all(facts["package"][key] == candidate[key]
                   for key in ("package", "version", "flavor", "artifact_sha256"))
        if same:
            recovery_schema = RECOVERY
        else:
            require(all(facts["package"][key] == manifest["candidate"][key]
                        for key in ("package", "version", "flavor", "artifact_sha256"))
                    and candidate["package"] == facts["package"]["package"]
                    and candidate["flavor"] == facts["package"]["flavor"],
                    "replacement predecessor/candidate identity differs")
            result = __import__("subprocess").run(
                ["/usr/bin/dpkg", "--compare-versions", facts["package"]["version"], "lt", candidate["version"]],
                stdin=__import__("subprocess").DEVNULL, stdout=__import__("subprocess").PIPE,
                stderr=__import__("subprocess").PIPE, timeout=15, check=False,
                env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
            require(result.returncode == 0 and result.stdout == b"" and result.stderr == b"",
                    "replacement candidate is not strictly newer")
            recovery_schema = RECOVERY_REPLACEMENT
        recovery = {"schema": recovery_schema, "tx": inputs["tx"], "generation": inputs["generation"],
            "observed_at": now, "archive_path": str(path), "archive_tree": tree, "abort_request": copy.deepcopy(request),
            "predecessor_manifest_hex": manifest_raw.hex(), "preinst_receipt_hex": payloads["receipts/" + receipt_name].hex(),
            "maintenance_absent": True, "cas_tree": cas_before, "blocking_processes": facts["blocking_processes"],
            "authorization": "NONE", "mutation_performed": False}
        if recovery_schema == RECOVERY_REPLACEMENT:
            recovery["successor_version_order"] = "LT"
        return facts["package"], recovery
    except archive.Refusal as error:
        raise Refusal(str(error)) from error
    finally:
        for fd in reversed(descriptors): os.close(fd)
