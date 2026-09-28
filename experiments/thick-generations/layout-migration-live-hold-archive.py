#!/usr/bin/env python3
"""Narrow SAN-only archive adapter; no CLI, cluster release or service actions.

The ALL_CERTIFIED evaluator is called afresh, never supplied as a plan by the
caller.  The filesystem primitive owns locking, rename and crash reconciliation.
One invocation makes one call to it; errors are never retried automatically.
The root override is solely for disposable file-only qualification.
"""
import copy
import hashlib
import importlib.util
import json
import os
import re
import socket
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXED_ROOT = Path("/var/lib/pve-sharedlvmthin/maintenance")
SHA = re.compile(r"^[0-9a-f]{64}$")
HEX32 = re.compile(r"^[0-9a-f]{32}$")


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


FILES = load("slt_archive_file_primitive", "local-hold-release-file-lab.py")
Refusal = FILES.Refusal
require = FILES.require


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def builtin_tree(value):
    if type(value) is dict:
        require(all(type(k) is str for k in value), "non-builtin mapping key")
        for item in value.values():
            builtin_tree(item)
    elif type(value) is list:
        for item in value:
            builtin_tree(item)
    else:
        require(type(value) in (str, int, bool, type(None)), "non-builtin value")


def exact(value, fields, label):
    builtin_tree(value)
    require(type(value) is dict and set(value) == set(fields), label + " schema invalid")


def positive(value):
    return type(value) is int and value > 0


def sha(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def decode(raw):
    require(type(raw) is bytes and 0 < len(raw) <= 1024 * 1024, "record bytes invalid")
    def pairs(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    builtin_tree(value)
    return value


def _evaluate_certified(inputs):
    evaluator = load("slt_archive_all_certified_v2", "layout-migration-all-certified-v2.py")
    return evaluator.evaluate(**inputs)


def _local_identity():
    return socket.gethostname().split(".", 1)[0], Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def validate_inode(value, expected_sha, expected_size, expected_uid):
    exact(value, {"dev", "ino", "uid", "mode", "nlink", "size", "sha256"}, "file identity")
    require(all(type(value[k]) is int for k in ("dev", "ino", "uid", "mode", "nlink", "size"))
            and value["dev"] >= 0 and value["ino"] > 0
            and value["uid"] == expected_uid and value["mode"] == 0o600
            and value["nlink"] == 1 and value["size"] == expected_size
            and sha(value["sha256"]) and value["sha256"] == expected_sha,
            "file identity differs")


def derive_request(plan, authorization, manifest_bytes, certificate_bytes, node, boot_id, now, expected_uid):
    exact(plan, {"schema", "phase", "verdict", "authorization", "hold_released", "mutation_performed",
                 "tx", "generation", "context_sha256", "commit_id", "certificate_sha256", "candidate",
                 "nodes", "node_roles", "participant_boots", "release_nodes", "verify_only_nodes",
                 "node_ack_sha256", "latest_verification_finished_at", "release_not_after", "hold_by_node",
                 "plan_sha256"}, "ALL_CERTIFIED plan")
    require(plan["schema"] == "slt-layout-all-certified-plan/v2" and plan["phase"] == "ALL_CERTIFIED"
            and plan["verdict"] == "READY_FOR_SAN_HOLD_RELEASE_AUTHORIZATION"
            and plan["authorization"] == "NONE" and plan["hold_released"] is False
            and plan["mutation_performed"] is False, "ALL_CERTIFIED ceiling invalid")
    body = dict(plan); body.pop("plan_sha256")
    require(sha(plan["plan_sha256"]) and digest(canonical(body)) == plan["plan_sha256"], "plan digest differs")
    names = plan["nodes"]
    require(type(names) is list and len(names) == 4 and all(type(n) is str and FILES.SAFE_NAME.fullmatch(n) for n in names)
            and names == sorted(set(names)), "participant set invalid")
    for field in ("node_roles", "participant_boots", "node_ack_sha256"):
        require(type(plan[field]) is dict and sorted(plan[field]) == names, "participant mapping incomplete")
    san = [n for n in names if plan["node_roles"][n] == "SAN_PARTICIPANT"]
    control = [n for n in names if plan["node_roles"][n] == "CONTROL_ONLY"]
    require(len(san) == 3 and len(control) == 1 and plan["release_nodes"] == san
            and plan["verify_only_nodes"] == control and type(plan["hold_by_node"]) is dict
            and sorted(plan["hold_by_node"]) == san, "role/release sets invalid")
    require(type(node) is str and node in san, "CONTROL_ONLY or foreign node cannot archive")
    for name in names:
        boot = plan["participant_boots"][name]
        require(type(boot) is str and str(uuid.UUID(boot)) == boot and sha(plan["node_ack_sha256"][name]),
                "participant boot/ACK invalid")
    require(type(boot_id) is str and boot_id == plan["participant_boots"][node], "local boot differs")
    require(type(plan["tx"]) is str and HEX32.fullmatch(plan["tx"])
            and type(plan["commit_id"]) is str and HEX32.fullmatch(plan["commit_id"])
            and positive(plan["generation"]) and sha(plan["context_sha256"])
            and positive(now) and positive(plan["latest_verification_finished_at"])
            and positive(plan["release_not_after"]), "plan identity/time invalid")
    manifest = decode(manifest_bytes); certificate = decode(certificate_bytes)
    manifest_sha = digest(manifest_bytes); certificate_sha = digest(certificate_bytes)
    require(certificate_sha == plan["certificate_sha256"] and sha(plan["certificate_sha256"]), "certificate bytes differ")
    require(type(manifest) is dict and manifest.get("schema") == "slt-package-maintenance/v1"
            and manifest.get("phase") == "CONFIG_COMMITTED" and manifest.get("tx") == plan["tx"]
            and positive(manifest.get("generation")) and manifest["generation"] == plan["generation"], "active manifest differs")
    require(type(certificate) is dict and certificate.get("schema") == "slt-layout-release-commit/v2"
            and certificate.get("phase") == "RELEASE_COMMITTED" and certificate.get("tx") == plan["tx"]
            and positive(certificate.get("generation")) and certificate["generation"] == plan["generation"]
            and certificate.get("context_sha256") == plan["context_sha256"]
            and certificate.get("commit_id") == plan["commit_id"]
            and type(certificate.get("release_not_after")) is int
            and certificate["release_not_after"] == plan["release_not_after"], "certificate identity differs")
    hold = plan["hold_by_node"][node]
    exact(hold, {"manifest_sha256", "source_identity", "certificate_identity"}, "local hold")
    require(hold["manifest_sha256"] == manifest_sha, "active bytes differ from ACK")
    validate_inode(hold["source_identity"], manifest_sha, len(manifest_bytes), expected_uid)
    validate_inode(hold["certificate_identity"], certificate_sha, len(certificate_bytes), expected_uid)
    exact(authorization, {"schema", "tx", "generation", "context_sha256", "commit_id", "node", "boot_id",
                          "all_certified_plan_sha256", "certificate_sha256", "manifest_sha256",
                          "source_identity_sha256", "certificate_identity_sha256", "operation_id",
                          "issued_at", "expires_at", "effect"}, "archive authorization")
    require(authorization["schema"] == "slt-live-hold-archive-authorization/v2"
            and authorization["effect"] == "archive-exact-active-once"
            and positive(authorization["generation"])
            and all(authorization[k] == plan[k] for k in ("tx", "generation", "context_sha256", "commit_id", "certificate_sha256"))
            and authorization["node"] == node and authorization["boot_id"] == boot_id
            and authorization["all_certified_plan_sha256"] == plan["plan_sha256"]
            and authorization["manifest_sha256"] == manifest_sha
            and authorization["source_identity_sha256"] == digest(canonical(hold["source_identity"]))
            and authorization["certificate_identity_sha256"] == digest(canonical(hold["certificate_identity"]))
            and type(authorization["operation_id"]) is str and HEX32.fullmatch(authorization["operation_id"])
            and positive(authorization["issued_at"]) and positive(authorization["expires_at"])
            and plan["latest_verification_finished_at"] <= authorization["issued_at"] <= now <= authorization["expires_at"] <= plan["release_not_after"]
            and 0 < authorization["expires_at"] - authorization["issued_at"] <= 900, "archive authorization invalid")
    return {"tx": plan["tx"], "generation": plan["generation"], "commit_id": plan["commit_id"],
            "node": node, "boot_id": boot_id, "authorization_sha256": digest(canonical(authorization)),
            "all_certified_plan_sha256": plan["plan_sha256"], "certificate_sha256": certificate_sha,
            "manifest_sha256": manifest_sha, "valid_until": authorization["expires_at"],
            "source_identity": copy.deepcopy(hold["source_identity"]),
            "certificate_identity": copy.deepcopy(hold["certificate_identity"])}


class ArchiveAdapter:
    def __init__(self, root=FIXED_ROOT, *, expected_uid=0):
        self.root = Path(root)
        self.expected_uid = expected_uid
        self.used = False

    def archive_once(self, *, certified_inputs, authorization, manifest_bytes, certificate_bytes, node, boot_id):
        require(self.used is False, "adapter already attempted; explicit recovery required")
        self.used = True
        builtin_tree(authorization)
        authorization = copy.deepcopy(authorization)
        # Evaluator inputs include raw configuration/certificate bytes; their own
        # closed validators, not JSON coercion, own that input contract.
        now = int(time.time())
        require(type(certified_inputs) is dict and all(type(k) is str for k in certified_inputs),
                "evaluator inputs invalid")
        inputs = dict(certified_inputs)
        inputs["now"] = now
        plan = _evaluate_certified(inputs)
        request = derive_request(plan, authorization, manifest_bytes, certificate_bytes,
                                 node, boot_id, now, self.expected_uid)
        require(_local_identity() == (node, boot_id), "live local node/boot differs")
        # The package creates the fixed maintenance root, while the release
        # protocol owns its two one-shot child namespaces. Create only those
        # exact directories after authorization and local identity validation;
        # then pin and fsync the parent before the first archive effect.
        root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
        try:
            root_info = os.fstat(root_fd)
            require(root_info.st_uid == self.expected_uid
                    and (root_info.st_mode & 0o022) == 0,
                    "maintenance root is unsafe")
            for name in ("archive", "release-attempts"):
                try:
                    os.mkdir(name, 0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
                info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                require((info.st_mode & 0o170000) == 0o040000
                        and info.st_uid == self.expected_uid
                        and (info.st_mode & 0o777) == 0o700
                        and info.st_dev == root_info.st_dev,
                        "release child directory is unsafe")
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        outcome = FILES.LocalHoldReleaseLab(self.root, expected_uid=self.expected_uid).release(
            request, manifest_bytes, certificate_bytes, now=now)
        require(outcome["classification"] == "LOCAL_HOLD_ARCHIVED" and outcome["cluster_released"] is False,
                "filesystem archive outcome differs")
        return {"classification": "LOCAL_HOLD_ARCHIVED", "authorization": "NONE",
                "cluster_released": False, "runtime_authorized": False,
                "receipt": outcome}
