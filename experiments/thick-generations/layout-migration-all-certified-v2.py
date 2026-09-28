#!/usr/bin/env python3
"""Read-only role-aware ALL_CERTIFIED evaluator, not a collector or releaser.

Inputs are already collected values/bytes. The exact v2 certificate is freshly
recomputed through the current v2 modules. Three SAN records attest persisted
certificate bytes and unchanged active holds. CONTROL_ONLY instead supplies a
distinct VERIFY_CURRENT_ONLY observation and may claim no local mutation.
"""

import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("slt_certified_certificate_v2", HERE / "layout-migration-release-certificate-v2.py")
CERT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CERT)
Refusal = CERT.Refusal

SHA = re.compile(r"[0-9a-f]{64}\Z")
HEX32 = re.compile(r"[0-9a-f]{32}\Z")
CERTIFICATE_DIRECTORY = "/var/lib/pve-sharedlvmthin/maintenance/release-certificates"
ACTIVE_MANIFEST = "/var/lib/pve-sharedlvmthin/maintenance/active.json"
COMPONENT_PATHS = ("/", "/var", "/var/lib", "/var/lib/pve-sharedlvmthin",
                   "/var/lib/pve-sharedlvmthin/maintenance", CERTIFICATE_DIRECTORY)


def require(value, message):
    if not value:
        raise Refusal(message)


def builtin(value, depth=0, ancestors=None):
    kind = type(value)
    require(depth <= 24 and any(kind is t for t in (dict, list, str, int, bool, type(None))),
            "non-builtin input or excessive nesting")
    if kind is str or kind is bytes:
        require(len(value) <= 2 * 1024 * 1024, "input scalar exceeds bound")
    elif kind is int:
        require(0 <= value <= (1 << 63) - 1, "input integer exceeds bound")
    elif kind is dict or kind is list:
        require(len(value) <= 512, "input container exceeds bound")
        ancestors = set() if ancestors is None else ancestors
        require(id(value) not in ancestors, "cyclic input")
        ancestors.add(id(value))
        if kind is dict:
            require(all(type(key) is str and len(key) <= 256 for key in value), "custom namespace key")
            items = value.values()
        else:
            items = value
        for item in items:
            builtin(item, depth + 1, ancestors)
        ancestors.remove(id(value))


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def posint(value, label):
    require(type(value) is int and value > 0, label + " must be an exact positive integer")


def sha(value, label):
    require(type(value) is str and SHA.fullmatch(value) is not None, label + " invalid")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def hold_identity(value, expected, context_sha):
    exact(value, {"active", "context_sha256", "manifest_sha256", "device", "inode"}, "active hold")
    require(type(value["active"]) is bool and type(value["device"]) is int
            and type(value["inode"]) is int and value["context_sha256"] == context_sha
            and canonical(value) == canonical(expected), "exact active hold differs")


def inode(value, kind, size=None, expected_sha=None):
    fields = {"type", "dev", "ino", "uid", "mode", "nlink"}
    if kind == "file":
        fields |= {"size", "sha256"}
    exact(value, fields, "inode")
    posint(value["dev"], "device"); posint(value["ino"], "inode")
    require(type(value["uid"]) is int and value["uid"] == 0 and type(value["mode"]) is int
            and type(value["nlink"]) is int and value["nlink"] > 0, "inode owner/types invalid")
    if kind == "file":
        require(value["type"] == "regular" and value["mode"] == 0o600 and value["nlink"] == 1,
                "certificate inode unsafe")
        posint(value["size"], "certificate size")
        require(value["size"] == size and value["sha256"] == expected_sha, "persisted certificate bytes differ")
    else:
        require(value["type"] == "directory" and value["mode"] <= 0o777
                and value["mode"] & 0o022 == 0, "certificate directory unsafe")


def namespace(value, decision_name, certificate_size, certificate_sha):
    exact(value, {"directory", "decision_name", "components", "certificate"}, "certificate namespace")
    require(value["directory"] == CERTIFICATE_DIRECTORY and value["decision_name"] == decision_name,
            "certificate namespace differs")
    components = value["components"]
    require(type(components) is list and len(components) == len(COMPONENT_PATHS), "namespace proof incomplete")
    for component, path in zip(components, COMPONENT_PATHS):
        exact(component, {"path", "identity"}, "namespace component")
        require(component["path"] == path, "namespace component path differs")
        inode(component["identity"], "directory")
    inode(value["certificate"], "file", certificate_size, certificate_sha)


def evaluate(context, topology_template, baseline_raw, target_raw, configured,
             refresh_authorization, stored_ready, release_authorization, ready_records,
             certificate_raw, collection, records, now, ready_max_age=300,
             max_age=120, max_skew=60):
    """Return a non-authorizing plan from a complete fresh role-aware barrier.

    certificate_raw is the persisted canonical certificate INCLUDING its final
    newline. certificate_bytes_sha256 is deliberately distinct from the
    certificate's embedded certificate_sha256 (the pre-self-field body hash).
    No function here writes certificates, holds, receipts, services or storage.
    """
    inputs = [context, topology_template, baseline_raw, target_raw, configured,
              refresh_authorization, stored_ready, release_authorization, ready_records,
              certificate_raw, collection, records, now, ready_max_age, max_age, max_skew]
    for index, value in enumerate(inputs):
        if index in (2, 3, 9):
            require(type(value) is bytes and 0 < len(value) <= 2 * 1024 * 1024,
                    "exact bounded config/certificate bytes required")
        else:
            builtin(value)
    require(type(now) is int and now > 0 and type(ready_max_age) is int and 30 <= ready_max_age <= 900
            and type(max_age) is int and 30 <= max_age <= 300
            and type(max_skew) is int and 0 <= max_skew <= 120, "evidence timing bounds invalid")
    require(type(baseline_raw) is bytes and type(target_raw) is bytes and type(certificate_raw) is bytes,
            "exact config/certificate bytes required")
    (context, topology_template, baseline_raw, target_raw, configured, refresh_authorization,
     stored_ready, release_authorization, ready_records, certificate_raw, collection, records,
     now, ready_max_age, max_age, max_skew) = copy.deepcopy(inputs)
    certificate, computed_raw = CERT.evaluate(context, topology_template, baseline_raw, target_raw,
        configured, refresh_authorization, stored_ready, release_authorization, ready_records, now, ready_max_age)
    require(certificate_raw == computed_raw, "persisted certificate bytes differ from fresh v2 evaluation")
    certificate_sha = hashlib.sha256(certificate_raw).hexdigest()
    certificate_size = len(certificate_raw)
    names = [row["name"] for row in context["nodes"]]
    boots = {row["name"]: row["boot_id"] for row in context["nodes"]}
    roles = context["node_roles"]
    san = certificate["release_nodes"]
    control = certificate["verify_only_nodes"]
    require(len(san) == 3 and len(control) == 1 and sorted(san + control) == names, "role partition invalid")
    expected_participants = {row["node"]: row for row in certificate["participants"]}
    ready_by_node = {row["node"]: row for row in ready_records}
    decision_name = f"{context['tx']}-{context['generation']}.json"

    exact(collection, {"schema", "collection_id", "tx", "generation", "context_sha256", "commit_id",
                       "certificate_bytes_sha256", "candidate", "participant_boots", "node_roles",
                       "collector_sha256", "issued_at", "expires_at", "challenges", "allowed_effects"}, "collection")
    posint(collection["generation"], "collection generation")
    require(collection["schema"] == "slt-layout-certificate-collection/v2"
            and type(collection["collection_id"]) is str and HEX32.fullmatch(collection["collection_id"])
            and collection["tx"] == context["tx"] and collection["generation"] == context["generation"]
            and collection["context_sha256"] == context["context_sha256"]
            and collection["commit_id"] == certificate["commit_id"]
            and collection["certificate_bytes_sha256"] == certificate_sha
            and canonical(collection["candidate"]) == canonical(context["candidate"])
            and collection["participant_boots"] == boots and collection["node_roles"] == roles
            and type(collection["allowed_effects"]) is list and collection["allowed_effects"] == [],
            "collection identity/roles/effects differ")
    sha(collection["collector_sha256"], "collector SHA")
    posint(collection["issued_at"], "collection issued time")
    posint(collection["expires_at"], "collection expiry")
    require(certificate["committed_at"] <= collection["issued_at"] <= now <= collection["expires_at"]
            <= certificate["release_not_after"] and 0 < collection["expires_at"] - collection["issued_at"] <= 300,
            "collection interval invalid")
    challenges = collection["challenges"]
    exact(challenges, names, "collection challenges")
    require(all(type(v) is str and HEX32.fullmatch(v) for v in challenges.values())
            and len(set(challenges.values())) == len(names), "collection challenges invalid")
    collection_sha = digest(collection)
    require(type(records) is list and len(records) == 4, "three SAN ACKs and one control observation required")
    common = {"schema", "action", "collection_id", "collection_plan_sha256", "challenge", "collector_sha256",
              "node", "role", "boot_id_start", "boot_id_end", "tx", "generation", "context_sha256", "commit_id",
              "certificate_bytes_sha256", "certificate_size", "verification_started_at", "verification_finished_at",
              "control_plane", "installed", "payload", "workers", "thinguard", "vg_identities"}
    hashes, finished, payload_hashes, holds = {}, [], set(), {}
    for record in records:
        require(type(record) is dict and type(record.get("node")) is str
                and record["node"] in names and record["node"] not in hashes, "record node missing/duplicated/foreign")
        node = record["node"]
        is_san = node in san
        exact(record, common | ({"durability", "namespace_before", "namespace_after", "hold"} if is_san else
                               {"local_effects", "local_certificate_present", "hold"}), "role record")
        posint(record["generation"], "record generation")
        posint(record["certificate_size"], "record certificate size")
        require(record["schema"] == ("slt-layout-certificate-ack/v2" if is_san else "slt-layout-certificate-current-observation/v2")
                and record["action"] == ("VERIFY_DURABLE_CERTIFICATE_AND_HOLD" if is_san else "VERIFY_CURRENT_ONLY")
                and record["role"] == roles[node] and record["collection_id"] == collection["collection_id"]
                and record["collection_plan_sha256"] == collection_sha and record["challenge"] == challenges[node]
                and record["collector_sha256"] == collection["collector_sha256"]
                and record["boot_id_start"] == boots[node] and record["boot_id_end"] == boots[node]
                and record["tx"] == context["tx"] and record["generation"] == context["generation"]
                and record["context_sha256"] == context["context_sha256"] and record["commit_id"] == certificate["commit_id"]
                and record["certificate_bytes_sha256"] == certificate_sha and record["certificate_size"] == certificate_size,
                "record identity/challenge/role/certificate differs")
        start, finish = record["verification_started_at"], record["verification_finished_at"]
        posint(start, "verification start"); posint(finish, "verification finish")
        require(collection["issued_at"] <= start <= finish <= now <= collection["expires_at"]
                and 0 <= now - start <= max_age, "record freshness/order invalid")
        plane = record["control_plane"]
        exact(plane, {"cluster_name", "cluster_nodes", "quorate", "target_storage_cfg_sha256"}, "control plane")
        require(plane["cluster_name"] == context["cluster_name"] and plane["cluster_nodes"] == names
                and plane["quorate"] is True and plane["target_storage_cfg_sha256"] == context["target_storage_cfg_sha256"],
                "record control plane differs")
        candidate = context["candidate"]
        expected_installed = {key: candidate[key] for key in ("package", "version", "flavor", "artifact_sha256")}
        expected_installed["dpkg_state"] = "installed"
        require(canonical(record["installed"]) == canonical(expected_installed), "installed payload differs")
        payload = record["payload"]
        exact(payload, {"dpkg_verify_complete", "dpkg_verify_clean", "package_file_list_sha256"}, "payload proof")
        require(payload["dpkg_verify_complete"] is True and payload["dpkg_verify_clean"] is True, "payload not positively verified")
        sha(payload["package_file_list_sha256"], "payload file list SHA")
        payload_hashes.add(payload["package_file_list_sha256"])
        CERT.READY.AC.validate_workers(record["workers"])
        guard_record = {"node": node, "observed_at": finish, "thinguard": record["thinguard"],
                        "vg_identities": record["vg_identities"]}
        CERT.READY.validate_post_guard(guard_record, context, now, max_age, start)
        if context["thinguard_required_by_node"][node]:
            require(all(type(record["thinguard"][key]) is int and record["thinguard"][key] ==
                        ready_by_node[node]["thinguard"][key] for key in ("daemon_pid", "daemon_starttime", "socket_inode")),
                    "certified guard lifecycle changed")
        expected_hold = expected_participants[node]["active_hold"]
        if is_san:
            durability = record["durability"]
            exact(durability, {"result", "file_fsync", "directory_fsync", "parent_fsync", "exact_reread"}, "durability")
            require(type(durability["result"]) is str and durability["result"] in
                    ("CERTIFICATE_DURABLE_LOCAL", "VERIFIED_REPLAY")
                    and all(durability[k] is True for k in ("file_fsync", "directory_fsync", "parent_fsync", "exact_reread")),
                    "certificate durability incomplete")
            for key in ("namespace_before", "namespace_after"):
                namespace(record[key], decision_name, certificate_size, certificate_sha)
            require(canonical(record["namespace_before"]) == canonical(record["namespace_after"]), "certificate namespace changed")
            hold = record["hold"]
            exact(hold, {"path", "before", "after", "identity_before", "identity_after", "exact_reread"}, "SAN hold proof")
            require(hold["path"] == ACTIVE_MANIFEST and hold["exact_reread"] is True, "active hold not revalidated")
            hold_identity(hold["before"], expected_hold, context["context_sha256"])
            hold_identity(hold["after"], expected_hold, context["context_sha256"])
            require(hold["after"]["active"] is True, "SAN hold no longer active")
            for key in ("identity_before", "identity_after"):
                identity = hold[key]
                require(type(identity) is dict and type(identity.get("size")) is int
                        and 0 < identity["size"] <= 1024 * 1024, "hold file size invalid")
                inode(identity, "file", identity["size"], expected_hold["manifest_sha256"])
                require(identity["dev"] == expected_hold["device"] and identity["ino"] == expected_hold["inode"],
                        "hold file identity differs from certificate")
            require(canonical(hold["identity_before"]) == canonical(hold["identity_after"]), "hold file metadata changed")
            holds[node] = {"manifest_sha256": expected_hold["manifest_sha256"],
                           "source_identity": {key: value for key, value in hold["identity_after"].items() if key != "type"},
                           "certificate_identity": {key: value for key, value in record["namespace_after"]["certificate"].items() if key != "type"}}
        else:
            effects = record["local_effects"]
            exact(effects, {"certificate_written", "hold_created", "hold_changed", "hold_released",
                            "package_changed", "services_changed"}, "control-only effects")
            require(all(value is False for value in effects.values()) and record["local_certificate_present"] is False,
                    "control-only record claims local mutation/certificate")
            hold_identity(record["hold"], expected_hold, context["context_sha256"])
            require(record["hold"]["active"] is False and record["hold"]["device"] == 0
                    and record["hold"]["inode"] == 0 and record["hold"]["manifest_sha256"] == "0" * 64,
                    "control-only record fabricated a hold")
        hashes[node] = digest(record)
        finished.append(finish)
    require(sorted(hashes) == names and max(finished) - min(finished) <= max_skew
            and len(payload_hashes) == 1, "record coverage/skew/payload cohort differs")
    body = {"schema": "slt-layout-all-certified-plan/v2", "phase": "ALL_CERTIFIED",
            "verdict": "READY_FOR_SAN_HOLD_RELEASE_AUTHORIZATION", "authorization": "NONE",
            "hold_released": False, "mutation_performed": False, "tx": context["tx"],
            "generation": context["generation"], "context_sha256": context["context_sha256"],
            "commit_id": certificate["commit_id"], "certificate_sha256": certificate_sha,
            "candidate": copy.deepcopy(candidate),
            "nodes": names, "node_roles": copy.deepcopy(roles), "release_nodes": list(san),
            "verify_only_nodes": list(control), "participant_boots": boots,
            "hold_by_node": dict(sorted(holds.items())), "node_ack_sha256": dict(sorted(hashes.items())),
            "latest_verification_finished_at": max(finished), "release_not_after": certificate["release_not_after"]}
    body["plan_sha256"] = digest(body)
    return body
