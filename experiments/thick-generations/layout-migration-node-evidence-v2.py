#!/usr/bin/env python3
"""Collect role-aware, read-only package-maintenance evidence.

The record never authorizes a mutation.  SAN participants may be assigned the
UNPACK_CONFIGURE action; the single control-only member is always
VERIFY_CURRENT_ONLY and must already contain the exact candidate payload.
"""

from __future__ import annotations

import argparse
import io
import hashlib
import importlib.util
import json
import re
import sys
import tarfile
import time
from pathlib import Path


HERE = Path(__file__).resolve().parent


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


V1 = load("slt_layout_node_v1_for_v2", "layout-migration-node-evidence.py")
TOPO = load("slt_layout_topology_for_node_v2", "layout-migration-topology-v2.py")
RESTART = load("slt_layout_restart_for_node_v2", "layout-migration-restart-preflight.py")
Refusal = V1.Refusal
require = V1.require
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def inactive_guard_evidence() -> dict:
    state = V1.systemd_property(
        "pve-sharedlvmthin-thin-guard.service", "ActiveState")
    pid = V1.systemd_property(
        "pve-sharedlvmthin-thin-guard.service", "MainPID")
    require(state == "inactive", "ThinGuard must be inactive for this topology")
    require(pid == "0", "inactive ThinGuard has a live MainPID")
    jobs = V1.command(["systemctl", "list-jobs", "--no-legend", "--plain",
                       "pve-sharedlvmthin-thin-guard.service"])
    require(not jobs.strip(), "ThinGuard has a pending systemd job")
    daemons = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = V1.pseudo_bytes(entry / "cmdline", "process command line",
                                      1024 * 1024, allow_empty=True)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except Refusal as error:
            # pseudo_bytes wraps open/read failures.  A process disappearing
            # between /proc enumeration and cmdline open is benign only for
            # the exact ENOENT/ESRCH race; every other incomplete observation
            # remains fail-closed.
            if isinstance(error.__cause__, (FileNotFoundError, ProcessLookupError)):
                continue
            raise
        if b"sharedlvmthin-thin-guardd" in cmdline:
            daemons.append({"pid": int(entry.name),
                            "starttime": V1.proc_starttime(int(entry.name)),
                            "command_sha256": hashlib.sha256(cmdline).hexdigest()})
    require(not daemons, "orphan ThinGuard daemon process is present")
    # The only supported registration owner is thin-guardd. Its watchdog-mux
    # descriptor is process-owned and closes on exit; absence of every daemon
    # process is therefore the observable proof that no plugin registration
    # survives. Unknown external watchdog clients are outside this plugin gate.
    return {"service_active_state": "inactive", "main_pid": 0, "jobs": [],
            "daemon_processes": daemons, "watchdog_registrations": []}


def tar_member(raw: bytes, name: str, label: str, maximum=4096) -> str:
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            member = archive.getmember(name)
            require(member.isfile() and 0 < member.size <= maximum,
                    f"{label} size is outside the bound")
            stream = archive.extractfile(member)
            require(stream is not None, f"{label} cannot be read")
            data = stream.read(maximum + 1)
    except (tarfile.TarError, KeyError, OSError) as error:
        raise Refusal(f"{label} is unavailable: {error}") from error
    require(len(data) == member.size, f"{label} read is incomplete")
    try:
        value = data.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise Refusal(f"{label} is not ASCII") from error
    return value


def candidate_identity(path: Path) -> dict:
    data = V1.regular_bytes(path, "candidate package", 512 * 1024 * 1024)
    package = V1.command(["dpkg-deb", "-f", str(path), "Package"]).decode("ascii").strip()
    version = V1.command(["dpkg-deb", "-f", str(path), "Version"]).decode("ascii").strip()
    architecture = V1.command(["dpkg-deb", "-f", str(path), "Architecture"]).decode("ascii").strip()
    require(package == "pve-sharedlvmthin" and V1.SAFE_VERSION.fullmatch(version),
            "candidate package name or version is invalid")
    require(architecture == "all", "candidate package architecture is unsupported")
    control = V1.command(["dpkg-deb", "--ctrl-tarfile", str(path)])
    payload = V1.command(["dpkg-deb", "--fsys-tarfile", str(path)])
    control_artifact = tar_member(
        control, "./sharedlvmthin-candidate-artifact-sha256",
        "candidate control artifact identity")
    data_artifact = tar_member(
        payload, "./usr/share/pve-sharedlvmthin/package-artifact-sha256",
        "candidate payload artifact identity")
    flavor = tar_member(payload, "./usr/share/pve-sharedlvmthin/package-flavor",
                        "candidate package flavor", 64)
    require(SHA256.fullmatch(control_artifact or "")
            and control_artifact == data_artifact and flavor == "dual",
            "candidate embedded identity or flavor is invalid")
    return {"package": package, "version": version, "architecture": architecture,
            "flavor": flavor, "deb_sha256": hashlib.sha256(data).hexdigest(),
            "artifact_sha256": control_artifact}


def collect(args) -> dict:
    topology_raw = V1.regular_bytes(Path(args.topology_evidence),
                                    "topology evidence", 16 * 1024 * 1024)
    try:
        topology = TOPO.validate_topology_evidence(json.loads(topology_raw))
    except (UnicodeDecodeError, json.JSONDecodeError, TOPO.Refusal) as error:
        raise Refusal(f"topology evidence is invalid: {error}") from error

    storage = V1.regular_bytes(Path("/etc/pve/storage.cfg"),
                               "storage configuration", 16 * 1024 * 1024)
    corosync = V1.regular_bytes(Path("/etc/pve/corosync.conf"),
                                "corosync configuration", 16 * 1024 * 1024)
    require(hashlib.sha256(storage).hexdigest() == topology["storage_cfg_sha256"],
            "storage configuration differs from topology evidence")
    topology_template = {
        "schema": "slt-layout-topology/v2",
        "cluster_name": topology["cluster_name"], "nodes": topology["nodes"],
        "storage_cfg_sha256": topology["storage_cfg_sha256"],
    }
    require(TOPO.validate(topology_template, storage) == topology,
            "topology policy is not reproducible from current storage configuration")
    expected_nodes = sorted(row["name"] for row in topology["nodes"])
    local, nodeid, quorate, observed = V1.membership(expected_nodes)
    require(quorate, "cluster is not quorate")
    node = next((row for row in topology["nodes"] if row["name"] == local), None)
    require(node is not None, "local node is absent from topology")
    boot_id = V1.pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"),
                              "boot identity", 128).decode("ascii").strip()
    require(boot_id == node["boot_id"], "local boot identity differs from topology")

    candidate = candidate_identity(Path(args.candidate_deb))
    role = node["san_role"]
    restart_inputs = None
    if getattr(args, "restart_inputs", None):
        require(role == "SAN_PARTICIPANT", "restart collection is SAN-only")
        restart_inputs = RESTART.decode(V1.regular_bytes(Path(args.restart_inputs), "restart inputs", 1024 * 1024))
        installed, recovery = RESTART.collect_provenance(restart_inputs, candidate)
    else:
        installed = V1.installed_identity()
    action = "UNPACK_CONFIGURE" if role == "SAN_PARTICIPANT" else "VERIFY_CURRENT_ONLY"
    guard = inactive_guard_evidence()
    if role == "SAN_PARTICIPANT":
        expected_vgs = V1.expected_mixed_vgs(storage)
        vg_identities = V1.actual_lvm_identities(expected_vgs)
    else:
        vg_identities = []
        require(installed["version"] == candidate["version"]
                and installed["artifact_sha256"] == candidate["artifact_sha256"],
                "control-only node does not contain the exact candidate payload")

    workers = V1.worker_evidence(observed)
    require(not workers["storage_processes"]
            and not workers["blocking_transient_units"]
            and not workers["active_pve_tasks"],
            "storage/package activity is present")
    storage_after = V1.regular_bytes(Path("/etc/pve/storage.cfg"),
                                     "storage configuration", 16 * 1024 * 1024)
    require(storage_after == storage, "storage configuration changed during collection")
    local_after, nodeid_after, quorate_after, observed_after = V1.membership(expected_nodes)
    require((local_after, nodeid_after, quorate_after, observed_after)
            == (local, nodeid, True, observed),
            "cluster membership or quorum changed during collection")
    boot_after = V1.pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"),
                                 "boot identity", 128).decode("ascii").strip()
    require(boot_after == boot_id, "boot identity changed during collection")
    if restart_inputs is not None:
        installed_after, recovery_after = RESTART.collect_provenance(restart_inputs, candidate)
        require({key: value for key, value in recovery.items() if key != "observed_at"}
                == {key: value for key, value in recovery_after.items() if key != "observed_at"},
                "restart provenance changed during collection")
        recovery = recovery_after
    else:
        installed_after = V1.installed_identity()
    require(installed_after == installed, "installed package changed during collection")
    role_evidence = {
        "schema": "slt-layout-node-role-evidence/v2", "node": local,
        "boot_id": boot_id, "san_role": role,
        "topology_sha256": topology["topology_sha256"],
        "vg_identities": vg_identities, "thinguard": guard,
    }
    TOPO.bind_node_evidence(role_evidence, topology)
    result = {
        "schema": RESTART.SCHEMA if restart_inputs is not None else "slt-layout-node-maintenance-evidence/v2",
        "challenge": args.challenge, "node": local, "nodeid": nodeid,
        "cluster_name": topology["cluster_name"],
        "cluster_nodes": observed, "observed_at": int(time.time()),
        "corosync_conf_sha256": hashlib.sha256(corosync).hexdigest(),
        "role": role, "action": action, "candidate": candidate,
        "installed": installed, "role_evidence": role_evidence,
        "workers": workers, "verdict": "PREFLIGHT_ONLY",
        "authorization": "NONE", "authorizes_mutation": False,
        "mutation_performed": False,
    }
    if restart_inputs is not None:
        result["recovery"] = recovery
        _, predecessor = RESTART.hex_document(recovery["predecessor_manifest_hex"])
        RESTART.validate(result, tx=restart_inputs["tx"], generation=restart_inputs["generation"],
            baseline_sha256=topology["storage_cfg_sha256"], target_sha256=predecessor["target_storage_cfg_sha256"],
            now=result["observed_at"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--topology-evidence", required=True)
    parser.add_argument("--candidate-deb", required=True)
    parser.add_argument("--challenge", required=True)
    parser.add_argument("--restart-inputs", help="SAN only: exact archived PREPARE abort request and new transaction")
    args = parser.parse_args()
    try:
        require(re.fullmatch(r"[0-9a-f]{32}", args.challenge),
                "challenge is invalid")
        result = collect(args)
    except (Refusal, RESTART.Refusal, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"schema": "slt-layout-node-maintenance-evidence/v2",
                          "verdict": "BLOCKED", "authorization": "NONE",
                          "authorizes_mutation": False,
                          "mutation_performed": False, "reason": str(error)},
                         sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
