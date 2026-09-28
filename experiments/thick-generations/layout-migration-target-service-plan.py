#!/usr/bin/env python3
"""Pure desired-state model AFTER all three SAN holds have been archived.

This is neither a collector nor an executor. Input records are assertions from
independently qualified collectors. The result grants NO systemd authority.
No pre-barrier state is inferred: the caller supplies the explicit target set.
"""
import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("target_service_certified", HERE / "layout-migration-all-certified-v2.py")
CERTIFIED = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CERTIFIED)
Refusal = CERTIFIED.Refusal

PHASES = (
    ("BACKEND", ("qmeventd.service", "pvedaemon.service", "pvestatd.service")),
    ("API", ("pveproxy.service", "spiceproxy.service")),
    ("HA", ("pve-ha-crm.service", "pve-ha-lrm.service")),
    ("SCHEDULER", ("pvescheduler.service",)),
)
TARGET_UNITS = tuple(sorted(unit for _, units in PHASES for unit in units))
PRESERVE_UNITS = tuple(sorted(("pve-guests.service", "pve-cluster.service", "corosync.service",
    "multipathd.service", "pve-sharedlvmthin-thin-guard.service")))
WATCHED = tuple(sorted(TARGET_UNITS + PRESERVE_UNITS))
CERTIFIED_FIELDS = frozenset(("context", "topology_template", "baseline_raw", "target_raw", "configured",
    "refresh_authorization", "stored_ready", "release_authorization", "ready_records", "certificate_raw",
    "collection", "records"))
RECEIPT_FIELDS = frozenset(("schema", "tx", "generation", "commit_id", "node", "boot_id",
    "authorization_sha256", "all_certified_plan_sha256", "certificate_sha256", "manifest_sha256",
    "attempt_sha256", "source_identity", "archive_identity", "archive_name", "rename_outcome",
    "directory_sync_complete", "started_at", "finished_at", "classification", "cluster_released"))


def require(value, message):
    if not value:
        raise Refusal(message)


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def canonical(value):
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    except (TypeError, ValueError) as error:
        raise Refusal("value is not canonical JSON") from error


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def positive(value):
    return type(value) is int and 0 < value <= (1 << 63) - 1


def tree(value, depth=0, ancestors=None):
    require(depth <= 24 and type(value) in (dict, list, str, bytes, int, bool, type(None)), "input tree invalid")
    if type(value) in (str, bytes):
        require(len(value) <= 2 * 1024 * 1024, "input too large")
    if type(value) is int:
        require(0 <= value <= (1 << 63) - 1, "integer outside bound")
    if type(value) in (dict, list):
        require(len(value) <= 512, "container too large")
        ancestors = set() if ancestors is None else ancestors
        require(id(value) not in ancestors, "cyclic input")
        ancestors.add(id(value))
        if type(value) is dict:
            require(all(type(k) is str for k in value), "non-string key")
        for item in value.values() if type(value) is dict else value:
            tree(item, depth + 1, ancestors)
        ancestors.remove(id(value))


def validate_mask(mask, unit):
    if mask is None:
        return
    exact(mask, {"path", "target", "dev", "ino", "uid", "mtime_ns"}, "persistent mask")
    require(mask["path"] == "/etc/systemd/system/" + unit and mask["target"] == "/dev/null"
            and positive(mask["dev"]) and positive(mask["ino"]) and positive(mask["mtime_ns"])
            and type(mask["uid"]) is int and mask["uid"] == 0, "persistent mask identity invalid")


def validate_services(rows, guard_required, san):
    require(type(rows) is list and len(rows) == len(WATCHED), "service inventory incomplete")
    by_unit = {}
    for row in rows:
        exact(row, {"unit", "active_state", "sub_state", "main_pid", "starttime", "control_pid",
                    "job", "cgroup_empty", "persistent_mask", "runtime_override"}, "service before")
        unit = row["unit"]
        require(type(unit) is str and unit in WATCHED and unit not in by_unit, "service duplicated or foreign")
        require(row["active_state"] in ("active", "inactive") and type(row["sub_state"]) is str
                and type(row["main_pid"]) is int and type(row["starttime"]) is int
                and type(row["control_pid"]) is int and row["control_pid"] == 0
                and row["job"] is None and type(row["cgroup_empty"]) is bool
                and row["runtime_override"] is False, "service pending or unknown")
        validate_mask(row["persistent_mask"], unit)
        if row["active_state"] == "inactive":
            require(row["sub_state"] == "dead" and row["main_pid"] == row["starttime"] == 0
                    and row["cgroup_empty"] is True, "inactive service has executor")
        elif unit == "pve-guests.service":
            require(row["sub_state"] == "exited" and row["main_pid"] == row["starttime"] == 0
                    and row["cgroup_empty"] is True, "pve-guests executor present")
        else:
            require(row["sub_state"] == "running" and row["main_pid"] > 1 and row["starttime"] > 0
                    and row["cgroup_empty"] is False, "active daemon lifecycle invalid")
        if unit in TARGET_UNITS:
            require(row["active_state"] == "inactive" and row["persistent_mask"] is not None,
                    "target service is not the exact stopped masked predecessor")
        if unit in ("corosync.service", "pve-cluster.service") or (san and unit == "multipathd.service"):
            require(row["active_state"] == "active", "essential service inactive")
        if unit == "pve-sharedlvmthin-thin-guard.service":
            require((row["active_state"] == "active") is guard_required, "guard service role differs")
        by_unit[unit] = row
    require(list(by_unit) == list(WATCHED), "service inventory order differs")
    return by_unit


def evaluate(certified_inputs, archive_receipts, before_records, desired_targets, plan_id,
             workload_snapshot_sha256, now, max_age=120, max_skew=60):
    """Validate exact assertions and return a non-authorizing fixed-phase plan.

    certified_inputs are the mandatory ALL_CERTIFIED evaluate keyword inputs,
    excluding its clock/optional bounds. They are re-evaluated, not trusted as
    an already successful plan. Receipt hashes bind canonical bytes + newline.
    """
    tree([certified_inputs, archive_receipts, before_records, desired_targets, plan_id,
          workload_snapshot_sha256, now, max_age, max_skew])
    require(positive(now) and type(max_age) is int and 30 <= max_age <= 300
            and type(max_skew) is int and 0 <= max_skew <= 120, "clock bounds invalid")
    require(type(plan_id) is str and re.fullmatch(r"[0-9a-f]{32}", plan_id)
            and sha(workload_snapshot_sha256), "plan/workload identity invalid")
    exact(certified_inputs, CERTIFIED_FIELDS, "certified inputs")
    inputs = copy.deepcopy(certified_inputs)
    # Revalidate the immutable certificate chain at its own observed barrier
    # time. Short-lived refresh/collection authorizations are historical
    # admission evidence, not a lease that must remain live during the later
    # post-archive restore. Current safety is independently bounded below by
    # archive timing, fresh before records, `now`, and release_not_after.
    require(type(inputs["records"]) is list and len(inputs["records"]) == 4
            and all(type(r) is dict and positive(r.get("verification_finished_at"))
                    for r in inputs["records"]), "certification records invalid")
    certified_at = max(r["verification_finished_at"] for r in inputs["records"])
    certified = CERTIFIED.evaluate(**inputs, now=certified_at,
                                   ready_max_age=900, max_age=300, max_skew=60)
    context = inputs["context"]
    names, roles, boots = certified["nodes"], certified["node_roles"], certified["participant_boots"]
    require(type(archive_receipts) is list and len(archive_receipts) == 3, "three archive receipts required")
    archives = {}
    for r in archive_receipts:
        exact(r, RECEIPT_FIELDS, "archive receipt")
        node = r["node"]
        require(type(node) is str and node in certified["release_nodes"] and node not in archives,
                "archive receipt node duplicated or not SAN")
        hold = certified["hold_by_node"][node]
        require(r["schema"] == "slt-layout-local-hold-release-receipt/v1"
                and r["classification"] == "LOCAL_HOLD_ARCHIVED" and r["cluster_released"] is False
                and r["tx"] == context["tx"] and positive(r["generation"]) and r["generation"] == context["generation"]
                and r["boot_id"] == boots[node] and r["commit_id"] == certified["commit_id"]
                and r["all_certified_plan_sha256"] == certified["plan_sha256"]
                and r["certificate_sha256"] == certified["certificate_sha256"]
                and r["manifest_sha256"] == hold["manifest_sha256"]
                and canonical(r["source_identity"]) == canonical(hold["source_identity"])
                and canonical(r["archive_identity"]) == canonical(hold["source_identity"])
                and r["rename_outcome"] == "EXACT_ARCHIVE_CONFIRMED" and r["directory_sync_complete"] is True
                and sha(r["authorization_sha256"]) and sha(r["attempt_sha256"]), "archive identity/durability differs")
        require(r["archive_name"] == f"{context['tx']}-{context['generation']}-{certified['commit_id']}.json",
                "archive filename differs")
        require(positive(r["started_at"]) and positive(r["finished_at"])
                and certified["latest_verification_finished_at"] <= r["started_at"] <= r["finished_at"]
                <= certified["release_not_after"] and r["finished_at"] <= now,
                "archive timing invalid")
        archives[node] = copy.deepcopy(r)
    require(sorted(archives) == certified["release_nodes"], "archive coverage differs")
    latest_archive = max(r["finished_at"] for r in archives.values())
    certified_by_node = {r["node"]: r for r in inputs["records"]}
    expected_targets = [{"node": node, "units": [{"unit": unit, "target_active": True,
        "target_persistent_mask": False} for unit in TARGET_UNITS], "preserve_units": list(PRESERVE_UNITS)} for node in names]
    require(canonical(desired_targets) == canonical(expected_targets), "explicit desired target differs from fixed contract")
    require(type(before_records) is list and len(before_records) == 4, "four fresh before records required")
    records, finished, payload_hashes = {}, [], set()
    for record in before_records:
        exact(record, {"schema", "plan_id", "challenge", "collector_sha256", "node", "role", "boot_id_start", "boot_id_end",
            "tx", "generation", "context_sha256", "observed_start", "observed_end", "control_plane", "installed",
            "payload", "workers", "hold", "archive_receipt_sha256", "services", "workload", "thinguard", "vg_identities"}, "before record")
        node = record["node"]
        require(type(node) is str and node in names and node not in records, "before node duplicated/foreign")
        san = roles[node] == "SAN_PARTICIPANT"
        require(record["schema"] == "slt-target-service-before/v1" and record["plan_id"] == plan_id
                and type(record["challenge"]) is str and re.fullmatch(r"[0-9a-f]{32}", record["challenge"])
                and sha(record["collector_sha256"]) and record["role"] == roles[node]
                and record["boot_id_start"] == record["boot_id_end"] == boots[node]
                and record["tx"] == context["tx"] and positive(record["generation"])
                and record["generation"] == context["generation"] and record["context_sha256"] == context["context_sha256"],
                "before identity differs")
        start, end = record["observed_start"], record["observed_end"]
        require(positive(start) and positive(end) and latest_archive <= start <= end <= now
                and now - start <= max_age, "before stale or precedes archive completion")
        require(canonical(record["control_plane"]) == canonical({"cluster_name": context["cluster_name"], "cluster_nodes": names,
            "quorate": True, "target_storage_cfg_sha256": context["target_storage_cfg_sha256"]}), "control plane differs")
        candidate = context["candidate"]
        installed = {k: candidate[k] for k in ("package", "version", "flavor", "artifact_sha256")}
        installed["dpkg_state"] = "installed"
        require(canonical(record["installed"]) == canonical(installed), "installed candidate differs")
        payload = record["payload"]
        exact(payload, {"dpkg_verify_complete", "dpkg_verify_clean", "package_file_list_sha256"}, "payload")
        require(payload["dpkg_verify_complete"] is True and payload["dpkg_verify_clean"] is True
                and sha(payload["package_file_list_sha256"])
                and payload["package_file_list_sha256"] == certified_by_node[node]["payload"]["package_file_list_sha256"],
                "payload proof incomplete or differs from certification")
        payload_hashes.add(payload["package_file_list_sha256"])
        CERTIFIED.CERT.READY.AC.validate_workers(record["workers"])
        require(canonical(record["hold"]) == canonical({"active": False, "namespace_verified": True}), "hold absence unproven")
        receipt_sha = hashlib.sha256(canonical(archives[node]) + b"\n").hexdigest() if san else None
        require(record["archive_receipt_sha256"] == receipt_sha, "archive receipt live reread differs")
        services = validate_services(record["services"], context["thinguard_required_by_node"][node], san)
        workload = record["workload"]
        exact(workload, {"snapshot_sha256", "inventory_complete", "managed_running_guests", "managed_mappers", "managed_open_lvs",
                         "ha_start_demands", "scheduled_start_demands"}, "workload")
        require(workload["snapshot_sha256"] == workload_snapshot_sha256 and workload["inventory_complete"] is True
                and all(type(workload[k]) is list and workload[k] == [] for k in
                        ("managed_running_guests", "managed_mappers", "managed_open_lvs", "ha_start_demands", "scheduled_start_demands")),
                "workload inventory incomplete or start demand present")
        guard = {"node": node, "observed_at": end, "thinguard": record["thinguard"], "vg_identities": record["vg_identities"]}
        CERTIFIED.CERT.READY.validate_post_guard(guard, context, now, max_age, start)
        if context["thinguard_required_by_node"][node]:
            service = services["pve-sharedlvmthin-thin-guard.service"]
            require(record["thinguard"]["daemon_pid"] == service["main_pid"]
                    and record["thinguard"]["daemon_starttime"] == service["starttime"], "guard lifecycle differs")
        records[node] = copy.deepcopy(record)
        finished.append(end)
    require(sorted(records) == names and max(finished) - min(finished) <= max_skew
            and len({r["challenge"] for r in records.values()}) == 4
            and len({r["collector_sha256"] for r in records.values()}) == 1 and len(payload_hashes) == 1,
            "before cohort coverage/skew/challenge/payload differs")
    body = {"schema": "slt-target-service-plan/v1", "tx": context["tx"], "generation": context["generation"],
        "context_sha256": context["context_sha256"], "plan_id": plan_id, "candidate": copy.deepcopy(context["candidate"]),
        "target_storage_cfg_sha256": context["target_storage_cfg_sha256"], "participant_boots": boots, "node_roles": roles,
        "all_certified_plan_sha256": certified["plan_sha256"], "certificate_bytes_sha256": certified["certificate_sha256"],
        "commit_id": certified["commit_id"], "archive_receipt_sha256": {n: hashlib.sha256(canonical(r) + b"\n").hexdigest() for n, r in sorted(archives.items())},
        "workload_snapshot_sha256": workload_snapshot_sha256, "observed_before_sha256": {n: digest(records[n]) for n in names},
        "targets": copy.deepcopy(expected_targets), "phase_order": [p for p, _ in PHASES],
        "phases": [{"phase": p, "units": list(units), "require_all_nodes_previous_phase": True} for p, units in PHASES],
        "preserved_states": {n: [r for r in records[n]["services"] if r["unit"] in PRESERVE_UNITS] for n in names},
        "observed_at": now, "evidence_valid_until": min(r["observed_start"] for r in records.values()) + max_age,
        "authorization": "NONE", "mutation_performed": False, "execution_authorized": False,
        "runtime_qualified": False, "verdict": "TARGET_PLAN_ONLY_EXPLICIT_EXECUTOR_AUTHORIZATION_REQUIRED"}
    body["plan_sha256"] = digest(body)
    return copy.deepcopy(body)
