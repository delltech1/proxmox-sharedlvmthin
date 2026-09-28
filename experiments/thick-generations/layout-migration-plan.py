#!/usr/bin/env python3
"""Read-only planner for the legacy same-VG layout schema transition.

The planner never contacts a node, changes storage.cfg, invokes systemctl or
runs dpkg in a mutating mode.  It binds already-collected per-node evidence to
one exact candidate and computes, in memory, the only permitted configuration
change: adding ``slt-vg-layout mixed`` to every legacy same-VG Thin/Thick
alias.  Its strongest verdict is READY_FOR_MAINTENANCE_PREPARE; it is not an
upgrade authorization or a commit token.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import time
import uuid
from pathlib import Path


SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9.+:~_-]+$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
THICK_MODES = {"thick-generations", "thick-generations-lazy"}


class Refusal(Exception):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refusal(message)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_regular(path: Path, label: str, maximum: int) -> bytes:
    try:
        info = path.lstat()
    except OSError as error:
        raise Refusal(f"{label} is unavailable: {error}") from error
    require(stat.S_ISREG(info.st_mode), f"{label} must be a regular file")
    require(not path.is_symlink(), f"{label} must not be a symlink")
    require(0 < info.st_size <= maximum, f"{label} size is outside the bound")
    data = path.read_bytes()
    require(len(data) == info.st_size, f"{label} changed while being read")
    return data


def candidate_identity(path: Path) -> dict[str, str]:
    data = read_regular(path, "candidate package", 512 * 1024 * 1024)
    try:
        result = subprocess.run(
            ["dpkg-deb", "-f", str(path), "Package", "Version", "Architecture"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"candidate control inspection failed: {error}") from error
    require(result.returncode == 0, "candidate control inspection was refused")
    values = result.stdout.splitlines()
    require(len(values) == 3, "candidate control identity is ambiguous")
    package, version, architecture = values
    require(package == "pve-sharedlvmthin", "layout migration requires the DUAL package")
    require(SAFE_VERSION.fullmatch(version) is not None, "candidate version is invalid")
    require(architecture == "all", "candidate architecture is unsupported")
    try:
        control = subprocess.run(
            ["dpkg-deb", "--ctrl-tarfile", str(path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=15, check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"candidate artifact identity inspection failed: {error}") from error
    require(control.returncode == 0 and len(control.stdout) <= 16 * 1024 * 1024,
            "candidate control archive is invalid or oversized")
    artifact = None
    try:
        with tarfile.open(fileobj=io.BytesIO(control.stdout), mode="r:") as archive:
            for member in archive.getmembers():
                normalized = member.name.lstrip("./")
                if normalized != "sharedlvmthin-candidate-artifact-sha256":
                    continue
                require(member.isfile() and not member.issym() and member.size == 65,
                        "candidate artifact marker has unsafe metadata")
                stream = archive.extractfile(member)
                require(stream is not None, "candidate artifact marker is unreadable")
                marker = stream.read()
                require(marker.endswith(b"\n"), "candidate artifact marker is malformed")
                artifact = marker[:-1].decode("ascii")
    except (tarfile.TarError, UnicodeDecodeError) as error:
        raise Refusal(f"candidate artifact marker inspection failed: {error}") from error
    require(artifact is not None and SHA256.fullmatch(artifact) is not None,
            "candidate artifact identity is missing or invalid")
    return {
        "package": package,
        "version": version,
        "architecture": architecture,
        "deb_sha256": sha256_bytes(data),
        "artifact_sha256": artifact,
    }


def debian_version_is_newer(candidate: str, installed: str) -> bool:
    require(SAFE_VERSION.fullmatch(installed) is not None,
            "installed package version is invalid")
    try:
        result = subprocess.run(
            ["dpkg", "--compare-versions", candidate, "gt", installed],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"Debian version comparison failed: {error}") from error
    return result.returncode == 0


def parse_storage_config(data: bytes) -> list[dict]:
    require(b"\x00" not in data, "storage configuration contains NUL")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refusal("storage configuration is not UTF-8") from error
    require(all(character in "\t\n" or
                (ord(character) >= 32 and ord(character) != 127 and
                 not 128 <= ord(character) <= 159 and
                 character not in "\u2028\u2029")
                for character in text),
            "storage configuration contains unsupported control or line separator")
    require(text.endswith("\n"), "storage configuration lacks a final newline")
    lines = text.splitlines(keepends=True)
    blocks: list[dict] = []
    current = None
    section_ids: set[str] = set()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line and not line[0].isspace():
            require(":" in line,
                    f"storage configuration has unparsed top-level line {index + 1}")
            if current is not None:
                current["end"] = index
                blocks.append(current)
            head = line.rstrip("\r\n")
            kind, remainder = head.split(":", 1)
            sid = remainder.strip()
            require(SAFE_NAME.fullmatch(kind) is not None and
                    SAFE_NAME.fullmatch(sid) is not None,
                    f"storage configuration has invalid section header at line {index + 1}")
            require(sid not in section_ids,
                    f"storage configuration repeats storage identifier '{sid}'")
            section_ids.add(sid)
            current = {"kind": kind, "sid": sid, "start": index,
                       "end": len(lines), "properties": {}, "prop_lines": {}}
            continue
        require(current is not None,
                f"storage configuration has a property before any section at line {index + 1}")
        fields = stripped.split(None, 1)
        key = fields[0]
        value = fields[1].strip() if len(fields) == 2 else ""
        require(key not in current["properties"],
                f"storage '{current['sid']}' repeats property '{key}'")
        current["properties"][key] = value
        current["prop_lines"][key] = index
    if current is not None:
        blocks.append(current)
    return [{**block, "lines": lines} for block in blocks]


def target_layout(data: bytes) -> tuple[bytes, list[dict]]:
    blocks = parse_storage_config(data)
    aliases = []
    for block in blocks:
        if block["kind"] != "sharedlvmthin":
            continue
        sid = block["sid"]
        require(SAFE_NAME.fullmatch(sid) is not None, "invalid storage identifier")
        props = block["properties"]
        vg = props.get("slt-vgname", props.get("vgname", ""))
        require(SAFE_NAME.fullmatch(vg) is not None,
                f"storage '{sid}' has no safe VG identity")
        mode = props.get("slt-allocation-mode", "thin")
        require(mode == "thin" or mode in THICK_MODES,
                f"storage '{sid}' has unsupported allocation mode")
        layout = props.get("slt-vg-layout")
        require(layout in (None, "isolated", "mixed"),
                f"storage '{sid}' has unsupported VG layout")
        aliases.append({"block": block, "sid": sid, "vg": vg,
                        "mode": mode, "layout": layout})

    by_vg: dict[str, list[dict]] = {}
    for alias in aliases:
        by_vg.setdefault(alias["vg"], []).append(alias)

    changes = []
    insertions: dict[int, list[str]] = {}
    for vg, entries in sorted(by_vg.items()):
        has_thin = any(item["mode"] == "thin" for item in entries)
        has_thick = any(item["mode"] in THICK_MODES for item in entries)
        if not (has_thin and has_thick):
            continue
        for item in entries:
            require(item["layout"] != "isolated",
                    f"mixed VG '{vg}' has an explicit isolated alias")
            if item["layout"] == "mixed":
                continue
            block = item["block"]
            properties = block["properties"]
            preferred = block["prop_lines"].get("slt-allocation-mode")
            if preferred is None:
                preferred = block["prop_lines"].get("slt-vgname",
                                                     block["start"])
            indentation = "\t"
            probe = block["lines"][preferred] if preferred > block["start"] else ""
            match = re.match(r"^(\s+)", probe)
            if match:
                indentation = match.group(1)
            insertion_at = preferred + 1
            insertions.setdefault(insertion_at, []).append(
                f"{indentation}slt-vg-layout mixed\n")
            changes.append({"storage_id": item["sid"], "vg": vg,
                            "from": "missing", "to": "mixed"})

    require(changes, "no legacy mixed-VG aliases require migration")
    original_lines = data.decode("utf-8").splitlines(keepends=True)
    output = []
    for index in range(len(original_lines) + 1):
        if index in insertions:
            output.extend(insertions[index])
        if index < len(original_lines):
            output.append(original_lines[index])
    target = "".join(output).encode("utf-8")

    reparsed = parse_storage_config(target)
    require(len(reparsed) == len(blocks), "target configuration changed block count")
    return target, changes


def renderer_properties(properties: dict[str, str]) -> dict:
    """Normalize only PVE's two known comma-separated set properties.

    Never collapse duplicate/empty tokens, change case, or normalize arbitrary
    values. Exact rendered bytes remain the caller's digest/CAS authority.
    """
    result = dict(properties)
    content_types = {"images", "rootdir", "vztmpl", "backup", "iso", "snippets", "import"}
    for key in ("content", "nodes"):
        if key not in properties:
            continue
        values = properties[key].split(",")
        require(all(values) and len(values) == len(set(values)),
                f"rendered set property '{key}' has empty or duplicate items")
        require(all(SAFE_NAME.fullmatch(value) is not None for value in values),
                f"rendered set property '{key}' has invalid items")
        if key == "content":
            require(set(values) <= content_types,
                    "rendered content property has unsupported items")
        result[key] = frozenset(values)
    return result


def validate_target_layout(baseline: bytes, target: bytes) -> list[dict]:
    """Prove a renderer-produced target has only the permitted semantic delta.

    PVE's SectionConfig writer canonicalizes the whole file, so byte equality
    with the line-preserving preview is neither expected nor required.  Exact
    target bytes are still bound by the caller; this function permits only the
    same section order and property maps as the preview, except ordering of
    validated content/nodes set items.
    """
    preview, changes = target_layout(baseline)
    baseline_comments = [line.strip() for line in baseline.decode("utf-8").splitlines()
                         if line.lstrip().startswith("#")]
    target_comments = [line.strip() for line in target.decode("utf-8").splitlines()
                       if line.lstrip().startswith("#")]
    require(target_comments == baseline_comments,
            "rendered target changed configuration comments")
    expected = parse_storage_config(preview)
    rendered = parse_storage_config(target)
    require(len(rendered) == len(expected),
            "rendered target changed storage section count")
    for before, after in zip(expected, rendered):
        require((after["kind"], after["sid"]) ==
                (before["kind"], before["sid"]),
                "rendered target changed storage section order or identity")
        require(renderer_properties(after["properties"]) ==
                renderer_properties(before["properties"]),
                f"rendered target changed properties for storage '{before['sid']}'")
    return changes


def node_evidence(path: Path, *, expected_cluster: str, expected_nodes: list[str],
                  config_sha: str, now: int, max_age: int) -> dict:
    raw = read_regular(path, "node evidence", 1024 * 1024)
    try:
        record = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(f"node evidence is invalid JSON: {error}") from error
    require(type(record) is dict, "node evidence must be an object")
    required = {
        "schema", "challenge", "cluster_name", "node", "boot_id", "cluster_nodes",
        "nodeid", "quorate", "observed_at", "corosync_conf_sha256",
        "storage_config_sha256_before", "storage_config_sha256_after",
        "candidate_deb_sha256", "installed", "thinguard", "workers",
        "consumer_processes", "vg_identities",
    }
    require(set(record) == required, "node evidence fields do not match schema")
    require(record["schema"] == 1, "node evidence schema is unsupported")
    require(type(record["challenge"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", record["challenge"]),
            "node evidence challenge is invalid")
    require(record["cluster_name"] == expected_cluster, "cluster identity mismatch")
    require(record["node"] in expected_nodes, "unexpected node evidence")
    require(type(record["nodeid"]) is int and record["nodeid"] > 0,
            "node ID is invalid")
    require(record["cluster_nodes"] == expected_nodes, "cluster membership mismatch")
    try:
        uuid.UUID(record["boot_id"])
    except (ValueError, AttributeError, TypeError) as error:
        raise Refusal("node boot identity is invalid") from error
    require(record["quorate"] is True, "cluster is not positively quorate")
    require(type(record["observed_at"]) is int, "evidence timestamp is invalid")
    age = now - record["observed_at"]
    require(0 <= age <= max_age, "node evidence is stale or from the future")
    require(SHA256.fullmatch(record["corosync_conf_sha256"]) is not None,
            "corosync configuration identity is invalid")
    require(record["storage_config_sha256_before"] == config_sha
            and record["storage_config_sha256_after"] == config_sha,
            "node observed a different or changing storage configuration")
    guard = record["thinguard"]
    require(type(guard) is dict and set(guard) == {
        "service_active_state", "daemon_pid", "daemon_starttime",
        "socket_inode", "samples"},
        "ThinGuard evidence fields do not match schema")
    require(guard["service_active_state"] == "active",
            "ThinGuard service is not active")
    require(type(guard["daemon_pid"]) is int and guard["daemon_pid"] > 1,
            "ThinGuard daemon PID is invalid")
    require(type(guard["daemon_starttime"]) is int
            and guard["daemon_starttime"] > 0,
            "ThinGuard daemon starttime is invalid")
    require(type(guard["socket_inode"]) is int and guard["socket_inode"] > 0,
            "ThinGuard socket identity is invalid")
    samples = guard["samples"]
    require(type(samples) is list and len(samples) == 2,
            "exactly two ThinGuard samples are required")
    request_ids = set()
    previous_sample_time = None
    for sample in samples:
        require(type(sample) is dict and set(sample) == {
            "request_id", "observed_at", "state", "action", "watchdog",
            "response_sha256"},
            "ThinGuard sample fields do not match schema")
        try:
            request_id = str(uuid.UUID(sample["request_id"]))
        except (ValueError, AttributeError, TypeError) as error:
            raise Refusal("ThinGuard request identity is invalid") from error
        require(request_id not in request_ids,
                "ThinGuard samples reused a cached request identity")
        request_ids.add(request_id)
        require(type(sample["observed_at"]) is int,
                "ThinGuard sample timestamp is invalid")
        require(record["observed_at"] - max_age <= sample["observed_at"]
                <= record["observed_at"], "ThinGuard sample is stale or future")
        if previous_sample_time is not None:
            require(sample["observed_at"] >= previous_sample_time,
                    "ThinGuard samples are out of order")
        previous_sample_time = sample["observed_at"]
        require(sample["state"] == "IDLE", "ThinGuard is not IDLE")
        require(sample["action"] == "NONE", "ThinGuard action is not NONE")
        require(sample["watchdog"] == "DISARMED",
                "ThinGuard watchdog is not DISARMED")
        require(SHA256.fullmatch(sample["response_sha256"]) is not None,
                "ThinGuard response identity is invalid")
    installed = record["installed"]
    require(type(installed) is dict and set(installed) == {
        "package", "version", "flavor", "artifact_sha256", "dpkg_state"},
        "installed package evidence fields do not match schema")
    require(installed["package"] == "pve-sharedlvmthin"
            and installed["flavor"] == "dual"
            and installed["dpkg_state"] == "installed",
            "installed package is not a configured DUAL profile")
    require(SAFE_VERSION.fullmatch(installed["version"]) is not None,
            "installed package version is invalid")
    require(SHA256.fullmatch(installed["artifact_sha256"]) is not None,
            "installed artifact identity is invalid")
    require(SHA256.fullmatch(record["candidate_deb_sha256"]) is not None,
            "staged candidate identity is invalid")
    workers = record["workers"]
    require(type(workers) is dict and set(workers) == {
        "proc_inventory_complete", "systemd_inventory_complete",
        "storage_processes", "blocking_transient_units", "active_pve_tasks"},
        "worker evidence fields do not match schema")
    require(workers["proc_inventory_complete"] is True
            and workers["systemd_inventory_complete"] is True,
            "worker inventory is incomplete")
    require(workers["storage_processes"] == [], "storage worker processes exist")
    require(workers["blocking_transient_units"] == [],
            "blocking transient storage units exist")
    require(workers["active_pve_tasks"] == [], "active storage tasks exist")
    require(type(record["consumer_processes"]) is list,
            "consumer process inventory is invalid")
    for consumer in record["consumer_processes"]:
        require(type(consumer) is dict and set(consumer) == {
            "unit", "pid", "process_starttime"},
            "consumer process evidence is invalid")
        require(type(consumer["pid"]) is int and consumer["pid"] > 1
                and type(consumer["process_starttime"]) is int
                and consumer["process_starttime"] > 0,
                "consumer process identity is invalid")
    require(type(record["vg_identities"]) is list and record["vg_identities"],
            "VG identity evidence is missing")
    return record


def plan(arguments: argparse.Namespace) -> dict:
    require(SAFE_NAME.fullmatch(arguments.cluster_name) is not None,
            "cluster name is invalid")
    expected_nodes = sorted(arguments.expected_node)
    require(len(expected_nodes) >= 3, "at least three expected nodes are required")
    require(len(set(expected_nodes)) == len(expected_nodes), "expected nodes repeat")
    require(all(SAFE_NAME.fullmatch(node) for node in expected_nodes),
            "an expected node name is invalid")
    require(len(arguments.node_evidence) == len(expected_nodes),
            "exactly one evidence file per expected node is required")

    config = read_regular(Path(arguments.storage_config),
                          "storage configuration", 16 * 1024 * 1024)
    config_sha = sha256_bytes(config)
    target, changes = target_layout(config)
    candidate = candidate_identity(Path(arguments.candidate_deb))
    now = arguments.now if arguments.now is not None else int(time.time())
    records = [node_evidence(Path(path), expected_cluster=arguments.cluster_name,
                             expected_nodes=expected_nodes, config_sha=config_sha,
                             now=now, max_age=arguments.max_age_sec)
               for path in arguments.node_evidence]
    nodes = [record["node"] for record in records]
    require(sorted(nodes) == expected_nodes and len(set(nodes)) == len(nodes),
            "node evidence coverage is incomplete or duplicated")
    observed = [record["observed_at"] for record in records]
    require(max(observed) - min(observed) <= arguments.max_skew_sec,
            "node evidence collection interval is too wide")
    challenges = [record["challenge"] for record in records]
    require(len(set(challenges)) == len(challenges),
            "node evidence challenges are duplicated")
    nodeids = [record["nodeid"] for record in records]
    require(len(set(nodeids)) == len(nodeids), "node IDs are duplicated")
    corosync = {record["corosync_conf_sha256"] for record in records}
    require(len(corosync) == 1, "nodes observed different corosync configurations")
    require(all(record["candidate_deb_sha256"] == candidate["deb_sha256"]
                for record in records), "a node staged different candidate bytes")
    require(all(debian_version_is_newer(candidate["version"],
                                        record["installed"]["version"])
                for record in records),
            "candidate version is not strictly newer on every node")
    identity_sets = {
        json.dumps(record["vg_identities"], sort_keys=True,
                   separators=(",", ":")) for record in records
    }
    require(len(identity_sets) == 1, "nodes observed different VG identities")

    body = {
        "schema": "slt-layout-plan/v1",
        "phase": "PLAN",
        "plan_id": os.urandom(16).hex(),
        "verdict": "READY_FOR_MAINTENANCE_PREPARE",
        "authorization": "NONE",
        "mutation_performed": False,
        "cluster_name": arguments.cluster_name,
        "expected_nodes": [{"name": record["node"], "nodeid": record["nodeid"]}
                           for record in sorted(records, key=lambda item: item["node"])],
        "corosync_conf_sha256": next(iter(corosync)),
        "boot_ids": {record["node"]: record["boot_id"] for record in records},
        "evidence_interval": {"first": min(observed), "last": max(observed)},
        "candidate": candidate,
        "baseline_storage_config_sha256": config_sha,
        "target_storage_config_sha256": sha256_bytes(target),
        "changes": sorted(changes, key=lambda item: item["storage_id"]),
        "vg_identities": records[0]["vg_identities"],
        "expires_at": min(observed) + arguments.max_age_sec,
        "next_phase": "PREPARE_REQUIRES_EXPLICIT_MAINTENANCE_TRANSACTION",
    }
    body["plan_sha256"] = sha256_bytes(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return body


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--cluster-name", required=True)
    result.add_argument("--expected-node", action="append", required=True)
    result.add_argument("--storage-config", required=True)
    result.add_argument("--candidate-deb", required=True)
    result.add_argument("--node-evidence", action="append", required=True)
    result.add_argument("--max-age-sec", type=int, default=300)
    result.add_argument("--max-skew-sec", type=int, default=60)
    result.add_argument("--now", type=int, help=argparse.SUPPRESS)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        require(30 <= arguments.max_age_sec <= 900, "max evidence age is unsafe")
        require(0 <= arguments.max_skew_sec <= 120, "max evidence skew is unsafe")
        result = plan(arguments)
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
