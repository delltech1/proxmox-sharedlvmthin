#!/usr/bin/env python3
"""Normalize an exact packaged v1 configured receipt into v2 evidence.

This is an offline, non-authorizing adapter.  It accepts no filesystem path and
performs no mutation; callers must supply the exact durable receipt and active
CONFIG_COMMITTED manifest bytes they collected independently.
"""
import copy
import hashlib
import json
import re

SHA = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")


class Refusal(ValueError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def decode(raw, label):
    require(type(raw) is bytes and 0 < len(raw) <= 1024 * 1024, label + " bytes invalid")
    def pairs(items):
        out = {}
        for key, value in items:
            require(key not in out, label + " has duplicate JSON key")
            out[key] = value
        return out
    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(label + " is not canonical JSON") from error


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def adapt(receipt_raw, manifest_raw, context, node, boot_id):
    receipt = decode(receipt_raw, "configured receipt")
    manifest = decode(manifest_raw, "committed manifest")
    exact(context, {"tx", "generation", "context_sha256", "candidate",
                    "target_storage_cfg_sha256"}, "adapter context")
    exact(receipt, {"schema", "tx", "generation", "phase", "node", "boot_id",
                    "package", "version", "flavor", "artifact_sha256",
                    "manifest_sha256", "storage_cfg_sha256", "recorded_at"},
          "configured receipt")
    exact(manifest, {"schema", "tx", "phase", "generation", "issued_at", "expires_at",
                     "cluster_name", "corosync_conf_sha256", "nodes", "candidate",
                     "baseline_storage_cfg_sha256", "target_storage_cfg_sha256",
                     "allowed_effects", "plan_sha256", "node_evidence"}, "committed manifest")
    require(type(node) is str and type(boot_id) is str, "node identity invalid")
    require(receipt["schema"] == "slt-package-maintenance-receipt/v1"
            and receipt["phase"] == "PACKAGE_CONFIGURED_DEFERRED"
            and manifest["schema"] == "slt-package-maintenance/v1"
            and manifest["phase"] == "CONFIG_COMMITTED", "receipt/manifest phase invalid")
    require(type(context["tx"]) is str and HEX32.fullmatch(context["tx"])
            and type(context["generation"]) is int and not isinstance(context["generation"], bool)
            and context["generation"] > 0
            and type(context["context_sha256"]) is str and SHA.fullmatch(context["context_sha256"])
            and type(context["target_storage_cfg_sha256"]) is str
            and SHA.fullmatch(context["target_storage_cfg_sha256"]), "context identity invalid")
    candidate = context["candidate"]
    exact(candidate, {"package", "version", "architecture", "flavor", "artifact_sha256", "deb_sha256"},
          "candidate")
    require(all(type(candidate[k]) is str for k in candidate)
            and SHA.fullmatch(candidate["artifact_sha256"])
            and SHA.fullmatch(candidate["deb_sha256"]), "candidate identity invalid")
    require(receipt["tx"] == manifest["tx"] == context["tx"]
            and receipt["generation"] == manifest["generation"] == context["generation"]
            and receipt["node"] == node and receipt["boot_id"] == boot_id
            and {row.get("name"): row.get("boot_id") for row in manifest["nodes"]}.get(node) == boot_id
            and receipt["manifest_sha256"] == digest(manifest_raw)
            and receipt["storage_cfg_sha256"] == manifest["target_storage_cfg_sha256"]
            == context["target_storage_cfg_sha256"], "transaction/config identity differs")
    manifest_candidate = {key: candidate[key] for key in
                          ("package", "version", "flavor", "artifact_sha256", "deb_sha256")}
    require(candidate["architecture"] == "all" and manifest["candidate"] == manifest_candidate
            and all(receipt[key] == candidate[key]
                    for key in ("package", "version", "flavor", "artifact_sha256")),
            "candidate identity differs")
    require(manifest["allowed_effects"] == ["package-unpack", "package-configure-deferred"]
            and type(receipt["recorded_at"]) is int and not isinstance(receipt["recorded_at"], bool)
            and manifest["issued_at"] <= receipt["recorded_at"] <= manifest["expires_at"],
            "receipt time/effects invalid")
    return {"schema": "slt-package-maintenance-receipt/v2",
            "phase": "PACKAGE_CONFIGURED_DEFERRED", "tx": context["tx"],
            "generation": context["generation"], "node": node, "boot_id": boot_id,
            "context_sha256": context["context_sha256"],
            "candidate": copy.deepcopy(candidate),
            "target_storage_cfg_sha256": context["target_storage_cfg_sha256"],
            "recorded_at": receipt["recorded_at"]}
