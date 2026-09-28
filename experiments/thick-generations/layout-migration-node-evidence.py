#!/usr/bin/env python3
"""Collect one fail-closed, read-only layout-migration evidence record.

This command is deliberately node-local.  It does not use SSH, modify pmxcfs,
start or stop services, call an LVM mutator, or install/unpack a package.  A
coordinator must provide a fresh per-node challenge and later evaluate exactly
one record from every expected cluster member.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path


SHA256 = re.compile(r"^[0-9a-f]{64}$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_VERSION = re.compile(r"^[A-Za-z0-9.+:~_-]+$")
THICK_MODES = {"thick-generations", "thick-generations-lazy"}
PATH = "/usr/sbin:/usr/bin:/sbin:/bin"


class Refusal(Exception):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def regular_bytes(path: Path, label: str, maximum: int) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise Refusal(f"{label} is unavailable: {error}") from error
    require(stat.S_ISREG(before.st_mode) and not path.is_symlink(),
            f"{label} is not a regular non-symlink file")
    require(0 < before.st_size <= maximum, f"{label} size is outside the bound")
    data = path.read_bytes()
    after = path.lstat()
    require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            f"{label} changed while being read")
    require(len(data) == before.st_size, f"{label} read was incomplete")
    return data


def pseudo_bytes(path: Path, label: str, maximum: int, *, allow_empty=False) -> bytes:
    """Bound a procfs/sysfs read whose stat size is commonly zero."""
    try:
        before = path.lstat()
        require(stat.S_ISREG(before.st_mode) and not path.is_symlink(),
                f"{label} is not a regular non-symlink pseudo-file")
        with path.open("rb", buffering=0) as stream:
            data = stream.read(maximum + 1)
        after = path.lstat()
    except OSError as error:
        raise Refusal(f"{label} is unavailable: {error}") from error
    require((before.st_dev, before.st_ino) == (after.st_dev, after.st_ino),
            f"{label} changed identity while being read")
    require((allow_empty or len(data) > 0) and len(data) <= maximum,
            f"{label} size is outside the bound")
    return data


STORAGE_WRITER_NAMES = {
    b"lvm", b"lvcreate", b"lvchange", b"lvconvert", b"lvextend", b"lvreduce",
    b"lvremove", b"lvrename", b"pvcreate", b"pvremove", b"pvresize",
    b"vgchange", b"vgcreate", b"vgextend", b"vgreduce", b"vgremove",
    b"dmsetup", b"dd", b"blkdiscard", b"wipefs",
}


def storage_writer_command(cmdline: bytes) -> bool:
    """Classify helpers that may survive their original PVE/plugin parent."""
    if not cmdline:
        return False
    argv = [item for item in cmdline.split(b"\0") if item]
    if not argv:
        return False
    name = argv[0].rsplit(b"/", 1)[-1]
    if name in STORAGE_WRITER_NAMES:
        return True
    return any(marker in cmdline for marker in (
        b"sharedlvmthin-thick-materialize", b"fault-driver.pl",
        b"resize-fault-driver.pl", b"snapshot-delete-fault-driver.pl"))


def command(argv, *, timeout=15, maximum=16 * 1024 * 1024) -> bytes:
    try:
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=timeout, check=False,
            env={"PATH": PATH, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise Refusal(f"read-only command failed: {argv[0]}: {error}") from error
    require(result.returncode == 0,
            f"read-only command returned {result.returncode}: {argv[0]}")
    require(len(result.stdout) <= maximum, f"command output exceeded bound: {argv[0]}")
    return result.stdout


def parse_storage(data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise Refusal("storage.cfg is not UTF-8") from error
    require(text.endswith("\n") and "\x00" not in text,
            "storage.cfg framing is invalid")
    blocks = []
    current = None
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            if current is not None:
                blocks.append(current)
            kind, rest = line.split(":", 1)
            current = {"kind": kind, "sid": rest.strip(), "properties": {}}
        elif current is not None and line[:1].isspace():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            fields = stripped.split(None, 1)
            key = fields[0]
            require(key not in current["properties"],
                    f"storage '{current['sid']}' repeats property '{key}'")
            current["properties"][key] = fields[1].strip() if len(fields) == 2 else ""
    if current is not None:
        blocks.append(current)
    return blocks


def expected_mixed_vgs(storage: bytes) -> list[dict]:
    by_vg = {}
    for block in parse_storage(storage):
        if block["kind"] != "sharedlvmthin":
            continue
        props = block["properties"]
        vg = props.get("slt-vgname", props.get("vgname", ""))
        mode = props.get("slt-allocation-mode", "thin")
        require(SAFE_NAME.fullmatch(block["sid"]) and SAFE_NAME.fullmatch(vg),
                "storage or VG identity is invalid")
        require(mode == "thin" or mode in THICK_MODES,
                f"storage '{block['sid']}' has unsupported mode")
        by_vg.setdefault(vg, []).append((block["sid"], mode, props))
    result = []
    for vg, entries in sorted(by_vg.items()):
        if not (any(mode == "thin" for _, mode, _ in entries)
                and any(mode in THICK_MODES for _, mode, _ in entries)):
            continue
        require(len(entries) == 2, f"mixed VG '{vg}' does not have exactly two aliases")
        identities = []
        for _, _, props in entries:
            identity = (
                props.get("slt-expected-vg-uuid", ""),
                props.get("slt-expected-pv-uuid", ""),
                props.get("slt-expected-wwid", ""),
            )
            require(all(identity), f"mixed VG '{vg}' lacks a pinned physical identity")
            identities.append(identity)
        require(identities[0] == identities[1],
                f"mixed VG '{vg}' aliases disagree on physical identity")
        result.append({"vg_name": vg, "vg_uuid": identities[0][0],
                       "pv_uuid": identities[0][1], "wwid": identities[0][2]})
    require(len(result) == 2, "expected exactly two legacy mixed VGs")
    return result


def actual_lvm_identities(expected: list[dict]) -> list[dict]:
    vgs_raw = command(["vgs", "--readonly", "--reportformat", "json",
                       "--units", "b", "--nosuffix", "-o", "vg_name,vg_uuid"])
    pvs_raw = command(["pvs", "--readonly", "--reportformat", "json",
                       "-o", "vg_name,pv_uuid,pv_name"])
    try:
        vgs = json.loads(vgs_raw)["report"][0]["vg"]
        pvs = json.loads(pvs_raw)["report"][0]["pv"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise Refusal(f"LVM JSON inventory is invalid: {error}") from error
    vg_rows = {row["vg_name"].strip(): row["vg_uuid"].strip() for row in vgs}
    pv_rows = {}
    for row in pvs:
        pv_rows.setdefault(row["vg_name"].strip(), []).append(
            (row["pv_uuid"].strip(), row["pv_name"].strip()))
    result = []
    for item in expected:
        vg = item["vg_name"]
        require(vg_rows.get(vg) == item["vg_uuid"], f"VG UUID mismatch for '{vg}'")
        require(len(pv_rows.get(vg, [])) == 1, f"VG '{vg}' does not have exactly one PV")
        pv_uuid, pv_name = pv_rows[vg][0]
        require(pv_uuid == item["pv_uuid"], f"PV UUID mismatch for '{vg}'")
        require(pv_name == f"/dev/mapper/{item['wwid']}",
                f"PV device identity mismatch for '{vg}'")
        result.append(item)
    return result


def proc_starttime(pid: int) -> int:
    data = pseudo_bytes(Path(f"/proc/{pid}/stat"), "process stat", 64 * 1024)
    try:
        tail = data.decode("ascii").rsplit(") ", 1)[1].split()
        value = int(tail[19])
    except (UnicodeDecodeError, IndexError, ValueError) as error:
        raise Refusal("process starttime is unavailable") from error
    require(value > 0, "process starttime is invalid")
    return value


def proc_state_flags_starttime(pid: int) -> tuple[str, int, int]:
    data = pseudo_bytes(Path(f"/proc/{pid}/stat"), "process stat", 64 * 1024)
    try:
        tail = data.decode("ascii").rsplit(") ", 1)[1].split()
        state, flags, started = tail[0], int(tail[6]), int(tail[19])
    except (UnicodeDecodeError, IndexError, ValueError) as error:
        raise Refusal("process state identity is unavailable") from error
    require(len(state) == 1 and flags >= 0 and started > 0,
            "process state identity is invalid")
    return state, flags, started


def empty_cmdline_is_inert(state: str, flags: int) -> bool:
    require(type(state) is str and len(state) == 1
            and type(flags) is int and not isinstance(flags, bool) and flags >= 0,
            "empty-cmdline identity is invalid")
    return bool(flags & 0x00200000) or state == "Z"


def systemd_property(unit: str, prop: str) -> str:
    data = command(["systemctl", "show", "--property", prop, "--value", unit])
    try:
        value = data.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise Refusal(f"systemd property for {unit} is invalid") from error
    require("\n" not in value, f"systemd property for {unit} is ambiguous")
    return value


def guard_sample(path: Path) -> dict:
    request_id = os.urandom(16).hex()
    payload = json.dumps({"version": 1, "op": "STATUS",
                          "request_id": request_id},
                         sort_keys=True, separators=(",", ":")).encode() + b"\n"
    response = b""
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(10)
        client.connect(str(path))
        client.sendall(payload)
        while b"\n" not in response and len(response) <= 16384:
            chunk = client.recv(4096)
            require(chunk, "ThinGuard closed the status connection")
            response += chunk
        client.close()
    except (OSError, socket.timeout) as error:
        raise Refusal(f"ThinGuard status failed: {error}") from error
    require(response.endswith(b"\n") and response.count(b"\n") == 1
            and len(response) <= 16384, "ThinGuard response framing is invalid")
    try:
        decoded = json.loads(response)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Refusal(f"ThinGuard response is invalid: {error}") from error
    require(type(decoded) is dict and set(decoded) == {
        "version", "ok", "request_id", "state", "action", "message"},
        "ThinGuard response fields are invalid")
    require(decoded["version"] == 1 and decoded["ok"] is True
            and decoded["request_id"] == request_id,
            "ThinGuard response identity or result is invalid")
    require(decoded["state"] == "IDLE" and decoded["action"] == "NONE",
            "ThinGuard is not idle")
    match = re.search(r"(?:^|; )watchdog=([A-Z]+)$", decoded["message"])
    require(match is not None, "ThinGuard watchdog status is absent")
    return {"request_id": request_id, "observed_at": int(time.time()),
            "state": decoded["state"], "action": decoded["action"],
            "watchdog": match.group(1), "response_sha256": digest(response)}


def guard_evidence() -> dict:
    socket_path = Path("/run/pve-sharedlvmthin/thin-guard.sock")
    info = socket_path.lstat()
    require(stat.S_ISSOCK(info.st_mode), "ThinGuard socket is not a socket")
    active = systemd_property("pve-sharedlvmthin-thin-guard.service", "ActiveState")
    require(active == "active", "ThinGuard service is not active")
    pid_text = systemd_property("pve-sharedlvmthin-thin-guard.service", "MainPID")
    require(pid_text.isdigit() and int(pid_text) > 1, "ThinGuard MainPID is invalid")
    pid = int(pid_text)
    first = guard_sample(socket_path)
    time.sleep(0.05)
    second = guard_sample(socket_path)
    require(first["request_id"] != second["request_id"],
            "ThinGuard request identities repeated")
    require(socket_path.lstat().st_ino == info.st_ino,
            "ThinGuard socket changed during sampling")
    require(systemd_property("pve-sharedlvmthin-thin-guard.service", "MainPID")
            == pid_text, "ThinGuard daemon changed during sampling")
    return {"service_active_state": active, "daemon_pid": pid,
            "daemon_starttime": proc_starttime(pid), "socket_inode": info.st_ino,
            "samples": [first, second]}


def active_cluster_tasks(nodes: list[str]) -> list[dict]:
    result = []
    for node in nodes:
        start = 0
        for _page in range(100):
            raw = command(["pvesh", "get", f"/nodes/{node}/tasks", "--source",
                           "active", "--start", str(start), "--limit", "500",
                           "--output-format", "json"], timeout=30)
            try:
                rows = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise Refusal(f"PVE active-task inventory is invalid: {error}") from error
            require(type(rows) is list and len(rows) <= 500,
                    "PVE active-task page is invalid")
            for row in rows:
                require(type(row) is dict and type(row.get("upid")) is str
                        and row.get("node") == node,
                        "PVE active-task identity is incomplete")
                result.append(row)
            if len(rows) < 500:
                break
            start += len(rows)
        else:
            raise Refusal("PVE active-task pagination exceeded bound")
    upids = [row["upid"] for row in result]
    require(len(upids) == len(set(upids)), "PVE active task is duplicated")
    return sorted(result, key=lambda row: row["upid"])


def worker_evidence(nodes: list[str]) -> dict:
    units_raw = command(["systemctl", "list-units", "--all", "--no-legend",
                         "--plain", "pve-sharedlvmthin-tg-*"])
    units = [line.decode("utf-8", "strict").strip()
             for line in units_raw.splitlines() if line.strip()]
    processes = []
    try:
        proc_entries = list(Path("/proc").iterdir())
    except OSError as error:
        raise Refusal(f"process inventory failed: {error}") from error
    for entry in proc_entries:
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            cmdline = pseudo_bytes(entry / "cmdline", "process command line",
                                   1024 * 1024, allow_empty=True)
        except FileNotFoundError:
            continue
        except Refusal as error:
            # A process may exit between directory enumeration and opening its
            # cmdline. Only ENOENT is a benign race; every other incomplete
            # observation remains fail-closed.
            if isinstance(error.__cause__, FileNotFoundError):
                continue
            raise Refusal(f"process inventory is incomplete: {error}") from error
        if not cmdline:
            state, flags, started = proc_state_flags_starttime(int(entry.name))
            require(proc_starttime(int(entry.name)) == started,
                    "empty-cmdline process identity changed")
            if empty_cmdline_is_inert(state, flags):
                continue
            processes.append({"pid": int(entry.name), "starttime": started,
                              "command_sha256": digest(cmdline),
                              "classification": "UNKNOWN_EMPTY_CMDLINE"})
        elif storage_writer_command(cmdline):
            processes.append({"pid": int(entry.name), "starttime": proc_starttime(int(entry.name)),
                              "command_sha256": digest(cmdline),
                              "classification": "KNOWN_STORAGE_WRITER"})
    tasks = active_cluster_tasks(nodes)
    return {"proc_inventory_complete": True, "systemd_inventory_complete": True,
            "storage_processes": processes, "blocking_transient_units": units,
            "active_pve_tasks": tasks}


def consumer_evidence() -> list[dict]:
    result = []
    for unit in ("pvedaemon.service", "pvestatd.service", "pveproxy.service",
                 "pve-ha-lrm.service"):
        active = systemd_property(unit, "ActiveState")
        if active != "active":
            continue
        pid_text = systemd_property(unit, "MainPID")
        require(pid_text.isdigit() and int(pid_text) > 1,
                f"active consumer {unit} has invalid MainPID")
        pid = int(pid_text)
        result.append({"unit": unit, "pid": pid,
                       "process_starttime": proc_starttime(pid)})
    return result


def installed_identity() -> dict:
    output = command(["dpkg-query", "-W", "-f=${db:Status-Abbrev}|${Version}",
                      "pve-sharedlvmthin"])
    try:
        status, version = output.decode("ascii").strip().split("|", 1)
    except (UnicodeDecodeError, ValueError) as error:
        raise Refusal("installed package identity is invalid") from error
    require(status == "ii " and SAFE_VERSION.fullmatch(version),
            "DUAL package is not fully configured")
    flavor = regular_bytes(Path("/usr/share/pve-sharedlvmthin/package-flavor"),
                           "installed flavor", 64).decode("ascii").strip()
    artifact = regular_bytes(
        Path("/usr/share/pve-sharedlvmthin/package-artifact-sha256"),
        "installed artifact identity", 128).decode("ascii").strip()
    require(flavor == "dual" and SHA256.fullmatch(artifact),
            "installed package profile or artifact identity is invalid")
    return {"package": "pve-sharedlvmthin", "version": version,
            "flavor": flavor, "artifact_sha256": artifact,
            "dpkg_state": "installed"}


def membership(expected_nodes: list[str]) -> tuple[str, int, bool, list[str]]:
    members_raw = regular_bytes(Path("/etc/pve/.members"), "pmxcfs membership", 1024 * 1024)
    try:
        members = json.loads(members_raw)
        rows = members["nodelist"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as error:
        raise Refusal(f"pmxcfs membership is invalid: {error}") from error
    require(type(rows) is dict, "pmxcfs node list is invalid")
    observed = sorted(rows)
    require(observed == expected_nodes, "pmxcfs membership differs from expected nodes")
    local = socket.gethostname()
    require(local in rows, "local hostname is absent from pmxcfs membership")
    nodeid = rows[local].get("id")
    require(type(nodeid) is int and nodeid > 0, "local node ID is invalid")
    status = command(["pvecm", "status"], timeout=20).decode("utf-8", "strict")
    quorate = re.search(r"^Quorate:\s+Yes\s*$", status, re.MULTILINE) is not None
    return local, nodeid, quorate, observed


def collect(args) -> dict:
    storage_path = Path("/etc/pve/storage.cfg")
    corosync_path = Path("/etc/pve/corosync.conf")
    storage_before = regular_bytes(storage_path, "storage configuration", 16 * 1024 * 1024)
    corosync = regular_bytes(corosync_path, "corosync configuration", 16 * 1024 * 1024)
    corosync_text = corosync.decode("utf-8", "strict")
    match = re.search(r"^\s*cluster_name:\s*(\S+)\s*$", corosync_text, re.MULTILINE)
    require(match is not None and match.group(1) == args.cluster_name,
            "cluster name does not match corosync configuration")
    local, nodeid, quorate, nodes = membership(sorted(args.expected_node))
    candidate = regular_bytes(Path(args.candidate_deb), "candidate package",
                              512 * 1024 * 1024)
    expected = expected_mixed_vgs(storage_before)
    record = {
        "schema": 1, "challenge": args.challenge,
        "cluster_name": args.cluster_name, "node": local, "nodeid": nodeid,
        "boot_id": pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"),
                                "boot identity", 128).decode("ascii").strip(),
        "cluster_nodes": nodes, "quorate": quorate,
        "observed_at": int(time.time()),
        "corosync_conf_sha256": digest(corosync),
        "storage_config_sha256_before": digest(storage_before),
        "storage_config_sha256_after": "",
        "candidate_deb_sha256": digest(candidate),
        "installed": installed_identity(), "thinguard": guard_evidence(),
        "workers": worker_evidence(nodes), "consumer_processes": consumer_evidence(),
        "vg_identities": actual_lvm_identities(expected),
    }
    storage_after = regular_bytes(storage_path, "storage configuration", 16 * 1024 * 1024)
    record["storage_config_sha256_after"] = digest(storage_after)
    require(record["storage_config_sha256_before"] == record["storage_config_sha256_after"],
            "storage configuration changed during collection")
    require(record["quorate"] is True, "cluster is not quorate")
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--expected-node", action="append", required=True)
    parser.add_argument("--candidate-deb", required=True)
    parser.add_argument("--challenge", required=True)
    args = parser.parse_args()
    try:
        require(SAFE_NAME.fullmatch(args.cluster_name), "cluster name is invalid")
        require(len(args.expected_node) >= 3 and len(set(args.expected_node)) == len(args.expected_node)
                and all(SAFE_NAME.fullmatch(node) for node in args.expected_node),
                "expected node set is invalid")
        require(re.fullmatch(r"[0-9a-f]{32}", args.challenge), "challenge is invalid")
        result = collect(args)
    except (Refusal, OSError, UnicodeDecodeError) as error:
        print(json.dumps({"schema": 1, "decision": "BLOCKED",
                          "authorizes_mutation": False,
                          "mutation_performed": False, "reason": str(error)},
                         sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
