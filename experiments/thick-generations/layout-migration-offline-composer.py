#!/usr/bin/env python3
"""Deterministic offline migration bundle composer; stdout only, never a runner.

Saved preflight is historical evidence, NOT an active barrier. Native-rendered
target bytes must be supplied; this program never impersonates the PVE writer.
Null draft fields deliberately fail the consuming schemas. A PREPARE projection
exists only with a supplied positive barrier and explicit bounded authorization.
Even a complete bundle has authorization NONE and performs no live operation.
"""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path


HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ADAPTER = load("slt_composer_adapter", "layout-migration-v2-v1-adapter.py")
CAS = load("slt_composer_cas", "layout-migration-native-config-cas-controller.py")
CTX = ADAPTER.CTX
Refusal = ADAPTER.Refusal


def require(value, message):
    if not value:
        raise Refusal(message)


def tree(value):
    try:
        CAS.tree(value)
    except CAS.Refusal as error:
        raise Refusal(str(error)) from error


def exact(value, fields, label):
    require(type(value) is dict and set(value) == set(fields), label + " fields invalid")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def raw_sha(value):
    return hashlib.sha256(value).hexdigest()


def preflight_inputs(preflights, topology, topology_evidence, now, max_age, *, transaction=None, target_sha256=None):
    """Validate historical identity without upgrading it to a current barrier."""
    tree([preflights, topology, topology_evidence])
    CTX.TOPO.validate_topology_evidence(topology_evidence)
    exact(topology, {"schema", "cluster_name", "nodes", "storage_cfg_sha256"}, "saved topology")
    require(canonical(topology) == canonical({"schema": "slt-layout-topology/v2",
                **{key: topology_evidence[key] for key in ("cluster_name", "nodes", "storage_cfg_sha256")}}),
            "saved topology and evidence differ")
    names = [node["name"] for node in topology["nodes"]]
    roles = {node["name"]: node["san_role"] for node in topology["nodes"]}
    require(type(preflights) is list and len(preflights) == 4, "four preflights required")
    fields = {"schema", "challenge", "node", "nodeid", "cluster_name", "cluster_nodes", "observed_at",
              "corosync_conf_sha256", "role", "action", "candidate", "installed", "role_evidence", "workers",
              "verdict", "authorization", "authorizes_mutation", "mutation_performed"}
    seen, candidates, corosync, challenges, nodeids = {}, set(), set(), set(), set()
    recovery_by_node = {}
    for row in preflights:
        restart = type(row) is dict and row.get("schema") == ADAPTER.RESTART.SCHEMA
        exact(row, fields | ({"recovery"} if restart else set()), "preflight")
        node = row["node"]
        require(type(node) is str and node in names and node not in seen, "preflight node duplicated or foreign")
        role = roles[node]
        require(row["schema"] in ("slt-layout-node-maintenance-evidence/v2", ADAPTER.RESTART.SCHEMA) and row["role"] == role
                and row["action"] == ("VERIFY_CURRENT_ONLY" if role == "CONTROL_ONLY" else "UNPACK_CONFIGURE")
                and row["cluster_name"] == topology["cluster_name"] and row["cluster_nodes"] == names
                and type(row["observed_at"]) is int and 0 < row["observed_at"] <= now
                and type(row["nodeid"]) is int and row["nodeid"] > 0
                and row["verdict"] == "PREFLIGHT_ONLY" and row["authorization"] == "NONE"
                and row["authorizes_mutation"] is False and row["mutation_performed"] is False,
                "preflight identity/ceiling differs")
        require(type(row["challenge"]) is str and ADAPTER.HEX32.fullmatch(row["challenge"])
                and type(row["corosync_conf_sha256"]) is str and ADAPTER.SHA.fullmatch(row["corosync_conf_sha256"]),
                "preflight challenge/config digest invalid")
        CTX.validate_candidate(row["candidate"])
        CTX.TOPO.bind_node_evidence(row["role_evidence"], topology_evidence)
        require(row["role_evidence"]["node"] == node, "inner preflight node differs")
        ADAPTER.validate_guard(row["role_evidence"]["thinguard"], topology_evidence["thinguard_required_by_node"][node],
                               row["observed_at"], row["observed_at"], max_age)
        workers = row["workers"]
        exact(workers, {"proc_inventory_complete", "systemd_inventory_complete", "storage_processes",
                        "blocking_transient_units", "active_pve_tasks"}, "workers")
        require(workers["proc_inventory_complete"] is True and workers["systemd_inventory_complete"] is True
                and all(type(workers[key]) is list and workers[key] == [] for key in
                        ("storage_processes", "blocking_transient_units", "active_pve_tasks")), "preflight incomplete or busy")
        installed = row["installed"]
        if restart:
            require(type(transaction) is dict and target_sha256 is not None, "restart requires explicit new transaction and target")
            try:
                recovery_by_node[node] = ADAPTER.RESTART.validate(row, tx=transaction["tx"], generation=transaction["generation"],
                    baseline_sha256=topology["storage_cfg_sha256"], target_sha256=target_sha256, now=now, max_age=max_age)
            except ADAPTER.RESTART.Refusal as error:
                raise Refusal(str(error)) from error
        exact(installed, {"package", "version", "flavor", "artifact_sha256", "dpkg_state"} | ({"config_version"} if restart else set()), "installed")
        require(installed["package"] == "pve-sharedlvmthin" and installed["flavor"] == "dual"
                and installed["dpkg_state"] == ("unpacked" if restart else "installed") and type(installed["version"]) is str
                and CTX.VERSION.fullmatch(installed["version"]) and type(installed["artifact_sha256"]) is str
                and ADAPTER.SHA.fullmatch(installed["artifact_sha256"]), "installed package invalid")
        if role == "CONTROL_ONLY":
            require(all(installed[key] == row["candidate"][key] for key in ("package", "version", "flavor", "artifact_sha256")),
                    "control-only candidate is not currently installed")
        seen[node] = row
        candidates.add(canonical(row["candidate"])); corosync.add(row["corosync_conf_sha256"])
        challenges.add(row["challenge"]); nodeids.add(row["nodeid"])
    require(sorted(seen) == names and len(candidates) == len(corosync) == 1
            and len(challenges) == len(nodeids) == 4, "preflight cohort inconsistent")
    if recovery_by_node:
        require(sorted(recovery_by_node) == [name for name in names if roles[name] == "SAN_PARTICIPANT"]
                and len({(row["predecessor_tx"], row["predecessor_generation"]) for row in recovery_by_node.values()}) == 1,
                "restart requires the complete SAN cohort and one predecessor transaction")
    return copy.deepcopy([seen[node] for node in names])


def compose(transaction, topology, topology_evidence, preflights, baseline_raw, target_raw,
            executor_node, now, *, barrier=None, adapter_auth_fields=None, cas_fill=None, max_age=300):
    """Compose drafts or exact existing-schema inputs; never synthesize proof.

    adapter_auth_fields = {authorization_id, issued_at, expires_at}.
    cas_fill = {attempt_id, baseline_native_digest, serializer, preinst_sha256,
                payload_sha256, action, issued_wall_ns, expires_wall_ns}.
    CAS filling requires a projected PREPARE. Supplied receipt/module digests
    remain collector assertions; the native executor must validate them afresh.
    """
    tree([transaction, topology, topology_evidence, preflights, executor_node, now, barrier,
          adapter_auth_fields, cas_fill, max_age])
    require(type(now) is int and now > 0 and type(max_age) is int and 0 < max_age <= 900, "clock/bound invalid")
    require(type(baseline_raw) is bytes and type(target_raw) is bytes
            and 0 < len(baseline_raw) <= 1024 * 1024 and 0 < len(target_raw) <= 1024 * 1024,
            "exact bounded baseline and supplied native-rendered target bytes required")
    rows = preflight_inputs(preflights, topology, topology_evidence, now, max_age,
                            transaction=transaction, target_sha256=raw_sha(target_raw))
    require(topology["storage_cfg_sha256"] == raw_sha(baseline_raw), "baseline differs from saved topology")
    computed_topology = CTX.TOPO.validate(topology, baseline_raw)
    require(canonical(computed_topology) == canonical(topology_evidence), "saved topology evidence differs from exact baseline")
    template = {key: copy.deepcopy(topology[key]) for key in ("schema", "cluster_name", "nodes")}
    context = CTX.build_context(transaction, rows[0]["candidate"], template, baseline_raw, target_raw)
    names = [node["name"] for node in context["nodes"]]
    roles = context["node_roles"]
    san = [name for name in names if roles[name] == "SAN_PARTICIPANT"]
    control = [name for name in names if roles[name] == "CONTROL_ONLY"]
    boots = {node["name"]: node["boot_id"] for node in context["nodes"]}
    require(type(executor_node) is str and executor_node in san, "executor must be an exact SAN participant")
    for row in rows:
        require(canonical(row["role_evidence"]["vg_identities"]) == canonical(context["expected_vg_identities_by_node"][row["node"]]),
                "preflight physical identities differ from context")
    changes = [{"storage_id": change["storage_id"], "property": "slt-vg-layout", "old_value": None, "new_value": "mixed"}
               for change in sorted(context["changes"], key=lambda value: value["storage_id"])]
    barrier_template = {"schema": "slt-live-maintenance-barrier/v2", "tx": context["tx"],
        "generation": context["generation"], "context_sha256": context["context_sha256"], "observed_at": None,
        "nodes": [{"node": node, "boot_id": boots[node], "evidence_sha256": None, "barrier": None,
                   "workers_clear": None, "guard_quiescent": None, "old_consumers_absent": None} for node in names],
        "authorization": "NONE", "mutation_performed": False}
    auth = {"schema": "slt-v1-package-adapter-authorization/v2", "tx": context["tx"], "generation": context["generation"],
            "context_sha256": context["context_sha256"], "authorization_id": None, "issued_at": None, "expires_at": None,
            "effect": "project-v1-prepare-manifest"}
    if adapter_auth_fields is not None:
        exact(adapter_auth_fields, {"authorization_id", "issued_at", "expires_at"}, "operator adapter authorization fields")
        require(type(adapter_auth_fields["authorization_id"]) is str and ADAPTER.HEX32.fullmatch(adapter_auth_fields["authorization_id"])
                and type(adapter_auth_fields["issued_at"]) is int and type(adapter_auth_fields["expires_at"]) is int
                and 0 < adapter_auth_fields["issued_at"] <= now <= adapter_auth_fields["expires_at"]
                and 0 < adapter_auth_fields["expires_at"] - adapter_auth_fields["issued_at"] <= 1800,
                "operator adapter authorization interval invalid")
        auth.update(copy.deepcopy(adapter_auth_fields))
    projection = None
    if barrier is not None:
        require(adapter_auth_fields is not None, "supplied positive barrier requires explicit bounded projection authorization")
        projection = ADAPTER.project(context, template, baseline_raw, target_raw, rows, barrier, auth, now, max_age)
    cas = {"schema": "slt-native-config-cas-controller/v1", "tx": context["tx"], "generation": str(context["generation"]),
           "attempt_id": None, "executor": {"node": executor_node, "boot_id": boots[executor_node]},
           "context_sha256": context["context_sha256"],
           "candidate": {key: context["candidate"][key] for key in ("package", "version", "flavor", "deb_sha256", "artifact_sha256")},
           "config": {"baseline_hex": baseline_raw.hex(), "target_hex": target_raw.hex(), "baseline_native_digest": None},
           "changes": changes, "participants": [{"node": node, "boot_id": boots[node], "role": roles[node]} for node in names],
           "evidence": {"barrier_sha256": digest(barrier) if projection is not None else None,
                        "preinst_sha256": {node: None for node in san}, "payload_sha256": {node: None for node in names},
                        "prepare_manifest_sha256": raw_sha(ADAPTER.canonical(projection["manifest"])) if projection is not None else None},
           "serializer": {"helper_sha256": None, "perl_sha256": None, "perl_hash_seed": "0", "perl_perturb_keys": "0",
                          "modules": None, "registered_plugins": None},
           "authorization": {"action": "NONE", "request_body_sha256": None, "issued_wall_ns": None, "expires_wall_ns": None}}
    if cas_fill is not None:
        require(projection is not None, "CAS filling requires exact projected PREPARE")
        exact(cas_fill, {"attempt_id", "baseline_native_digest", "serializer", "preinst_sha256", "payload_sha256",
                         "action", "issued_wall_ns", "expires_wall_ns"}, "CAS collector/operator fields")
        require(cas_fill["action"] in ("NATIVE_PVE_CONFIG_CAS_ONCE", "CONTROLLER_MODEL_ONE_CAS_ONLY"), "CAS action must be explicit")
        cas["attempt_id"] = cas_fill["attempt_id"]
        cas["config"]["baseline_native_digest"] = cas_fill["baseline_native_digest"]
        cas["serializer"] = copy.deepcopy(cas_fill["serializer"])
        for key in ("preinst_sha256", "payload_sha256"):
            cas["evidence"][key] = copy.deepcopy(cas_fill[key])
        cas["authorization"] = {"action": cas_fill["action"], "request_body_sha256": CAS.request_body_sha256(cas),
                                "issued_wall_ns": cas_fill["issued_wall_ns"], "expires_wall_ns": cas_fill["expires_wall_ns"]}
        # Reuse the current closed structural schema without promoting a model
        # token into live authority. The caller explicitly selected native/model.
        checked = copy.deepcopy(cas); checked["authorization"]["action"] = "CONTROLLER_MODEL_ONE_CAS_ONLY"
        try:
            CAS.validate_request(checked)
            require(int(cas_fill["issued_wall_ns"]) <= now * 10**9 <= int(cas_fill["expires_wall_ns"]), "CAS authorization not current")
        except CAS.Refusal as error:
            raise Refusal(str(error)) from error
    prepare = None if projection is None else {
        **projection, "manifest_bytes_hex": ADAPTER.canonical(projection["manifest"]).hex(),
        "manifest_bytes_sha256": raw_sha(ADAPTER.canonical(projection["manifest"])),
        "sidecar_bytes_hex": ADAPTER.canonical(projection["sidecar"]).hex(),
        "sidecar_bytes_sha256": raw_sha(ADAPTER.canonical(projection["sidecar"]))}
    missing = []
    if barrier is None:
        missing.append("fresh_positive_live_barrier")
    if adapter_auth_fields is None:
        missing.append("explicit_bounded_adapter_authorization")
    if cas_fill is None:
        missing.extend(["post_unpack_PREINST_and_payload_evidence", "pinned_native_digest_and_serializer", "explicit_CAS_attempt_and_authorization"])
    body = {"schema": "slt-layout-offline-composer/v1", "authorization": "NONE", "mutation_performed": False,
            "execution_authorized": False, "verdict": "INPUTS_COMPOSED_NOT_EXECUTED" if not missing else "DRAFT_INPUTS_ONLY",
            "context": context, "topology_template": template, "candidate": copy.deepcopy(context["candidate"]),
            "preflight_sha256": {row["node"]: digest(row) for row in rows},
            "preflight_fresh_by_node": {row["node"]: now - row["observed_at"] <= max_age for row in rows},
            "release_role_partition": {"san_nodes": san, "verify_only_nodes": control},
            "native_render_inputs": {"baseline_hex": baseline_raw.hex(), "baseline_sha256": raw_sha(baseline_raw),
                "changes": changes, "perl_hash_seed": "0", "perl_perturb_keys": "0"},
            "supplied_target": {"bytes_hex": target_raw.hex(), "sha256": raw_sha(target_raw),
                "provenance": "SUPPLIED_BYTES_NATIVE_RENDER_MUST_BE_REVERIFIED"},
            "live_barrier_template": barrier_template, "live_barrier": copy.deepcopy(barrier),
            "adapter_authorization": auth, "prepare": prepare, "native_cas_request": cas,
            "native_cas_schema_complete": cas_fill is not None, "missing_inputs": missing}
    recovery = {row["node"]: copy.deepcopy(row["recovery"]) for row in rows if row["schema"] == ADAPTER.RESTART.SCHEMA}
    if recovery:
        body["recovery_by_node"] = recovery
    body["bundle_sha256"] = digest(body)
    return body


def read_raw(path):
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        before = os.fstat(fd)
        require(stat.S_ISREG(before.st_mode) and 0 < before.st_size <= 4 * 1024 * 1024, "input is not a bounded regular file")
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, min(65536, 4 * 1024 * 1024 + 1 - total))
            if not chunk: break
            total += len(chunk); require(total <= 4 * 1024 * 1024, "input too large"); chunks.append(chunk)
        after = os.fstat(fd); named = os.stat(path, follow_symlinks=False)
        identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        require(identity(before) == identity(after) == identity(named) and stat.S_ISREG(named.st_mode), "input changed")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_json(path):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value, "duplicate JSON key")
            value[key] = item
        return value
    value = json.loads(read_raw(path), object_pairs_hook=pairs)
    tree(value)
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("topology", "topology-evidence", "baseline", "target-rendered", "tx", "executor"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--preflight", action="append", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--now", type=int, required=True)
    parser.add_argument("--barrier")
    parser.add_argument("--adapter-auth-fields")
    parser.add_argument("--cas-fill")
    args = parser.parse_args(argv)
    try:
        result = compose({"tx": args.tx, "generation": args.generation}, read_json(args.topology), read_json(args.topology_evidence),
            [read_json(path) for path in args.preflight], read_raw(args.baseline), read_raw(args.target_rendered), args.executor, args.now,
            barrier=read_json(args.barrier) if args.barrier else None,
            adapter_auth_fields=read_json(args.adapter_auth_fields) if args.adapter_auth_fields else None,
            cas_fill=read_json(args.cas_fill) if args.cas_fill else None)
    except (OSError, ValueError, Refusal) as error:
        print(json.dumps({"verdict": "REFUSED", "authorization": "NONE", "mutation_performed": False, "reason": str(error)}, sort_keys=True))
        return 2
    print(canonical(result).decode("ascii"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
