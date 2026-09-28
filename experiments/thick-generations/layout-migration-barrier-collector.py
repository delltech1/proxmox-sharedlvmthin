#!/usr/bin/env python3
"""Read-only node-local barrier evidence; no SSH, service or storage effects."""
from __future__ import annotations
import argparse
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import socket
import stat
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


B = load("barrier_collector_backend", "layout-migration-barrier-backend.py")
V1 = load("barrier_collector_v1", "layout-migration-node-evidence.py")
Refusal, require = B.Refusal, B.require
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def strict(raw):
    def pairs(rows):
        result = {}
        for key, value in rows:
            require(key not in result, "duplicate JSON key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs)


def read(path, maximum=16 * 1024 * 1024, *, pseudo=False, empty=False):
    path = Path(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    fd = os.open(path, flags)
    try:
        first = os.fstat(fd)
        require(stat.S_ISREG(first.st_mode), f"not regular: {path}")
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(fd, min(65536, maximum + 1 - len(raw)))
            if not chunk: break
            raw.extend(chunk)
        last, named = os.fstat(fd), path.lstat()
        identity = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns)
        require(identity(first) == identity(last) == identity(named)
                and len(raw) <= maximum and (empty or raw)
                and (pseudo or len(raw) == first.st_size), f"unstable read: {path}")
        return bytes(raw)
    finally:
        os.close(fd)


def command(argv):
    # All callers below construct fixed read-only argv; never take CLI argv.
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=30, env=ENV, check=False)
    require(result.returncode == 0 and len(result.stdout) <= 32 * 1024 * 1024,
            f"read-only probe failed: {argv[0]}")
    return result.stdout


def properties(unit):
    require(unit in B.WATCHED, "unit outside fixed watch set")
    fields = ("ActiveState", "SubState", "MainPID", "ControlPID", "ControlGroup", "Job", "LoadState")
    raw = command(["/usr/bin/systemctl", "show", unit, *["--property=" + f for f in fields]])
    result = {}
    for line in raw.decode("utf-8").splitlines():
        key, sep, value = line.partition("=")
        require(sep and key in fields and key not in result, "systemd property framing invalid")
        result[key] = value
    require(set(result) == set(fields) and result["LoadState"] in ("loaded", "masked"),
            "service absent or systemd response incomplete")
    return result


def cgroup_empty(group):
    if not group: return True
    require(group.startswith("/") and ".." not in PurePosixPath(group).parts,
            "unsafe systemd cgroup path")
    root = Path("/sys/fs/cgroup") / group.lstrip("/")
    require(root.is_dir() and not root.is_symlink(), "cgroup disappeared")
    paths = [root]
    first = []
    for parent in paths:
        require(len(paths) <= 4096, "cgroup inventory exceeds bound")
        for child in parent.iterdir():
            info = child.lstat()
            require(not stat.S_ISLNK(info.st_mode), "cgroup symlink unexpected")
            if stat.S_ISDIR(info.st_mode): paths.append(child)
        data = read(parent / "cgroup.procs", pseudo=True, empty=True)
        require(all(line.isdigit() for line in data.splitlines()), "cgroup PID format invalid")
        first.append((parent, data))
    require(all(read(p / "cgroup.procs", pseudo=True, empty=True) == raw for p, raw in first),
            "cgroup changed during observation")
    return not any(raw.strip() for _, raw in first)


def persistent_mask(unit):
    runtime = Path("/run/systemd/system") / unit
    require(not runtime.exists() and not runtime.is_symlink(), "runtime override/mask exists")
    path = Path("/etc/systemd/system") / unit
    try: info = path.lstat()
    except FileNotFoundError: return None
    require(stat.S_ISLNK(info.st_mode) and os.readlink(path) == "/dev/null",
            "local service override is not exact persistent mask")
    after = path.lstat()
    require((info.st_dev, info.st_ino, info.st_mtime_ns) == (after.st_dev, after.st_ino, after.st_mtime_ns),
            "mask changed during observation")
    return "/etc/systemd/system:/dev/null"


def services():
    rows = []
    for unit in B.WATCHED:
        before = properties(unit)
        empty = cgroup_empty(before["ControlGroup"])
        mask = persistent_mask(unit)
        after = properties(unit)
        require(before == after, "service changed during collection")
        require(before["MainPID"].isdigit() and before["ControlPID"].isdigit(), "service PID invalid")
        rows.append({"unit": unit, "active_state": before["ActiveState"], "sub_state": before["SubState"],
                     "main_pid": int(before["MainPID"]), "control_pid": int(before["ControlPID"]),
                     "cgroup_empty": empty, "job": None if not before["Job"] else before["Job"], "mask_target": mask})
    return rows


def process_inventory():
    relevant = []
    guests = {}
    for path in Path("/proc").iterdir():
        if not path.name.isdigit() or int(path.name) == os.getpid(): continue
        try:
            raw_stat = read(path / "stat", pseudo=True)
            tail = raw_stat.rsplit(b") ", 1)[1].split()
            state, flags, started = tail[0], int(tail[6]), int(tail[19])
            raw = read(path / "cmdline", pseudo=True, empty=True)
            argv = [x for x in raw.split(b"\0") if x]
            # A freshly forked userspace process can briefly expose an empty
            # cmdline before exec publishes argv. This is not proof of safety,
            # but it is also not a durable unknown: retry the same PID/starttime
            # for a tiny bounded window, then retain the existing fail-closed
            # refusal if it never becomes classifiable.
            if not argv and not (flags & 0x00200000 or state == b"Z"):
                for _ in range(3):
                    time.sleep(0.01)
                    again = read(path / "stat", pseudo=True).rsplit(b") ", 1)[1].split()
                    require(int(again[19]) == started, "process identity reused")
                    raw = read(path / "cmdline", pseudo=True, empty=True)
                    argv = [x for x in raw.split(b"\0") if x]
                    if argv:
                        state, flags = again[0], int(again[6])
                        break
            if not argv:
                require(flags & 0x00200000 or state == b"Z", "unclassified empty-cmdline process")
                continue
            executable = os.readlink(path / "exe")
            name = Path(executable).name
            identity = {"pid": int(path.name), "starttime": started, "command_sha256": B.digest(raw)}
            if name in ("qemu-system-x86_64", "kvm"):
                positions = [i for i, value in enumerate(argv) if value == b"-id"]
                require(len(positions) == 1 and positions[0] + 1 < len(argv)
                        and argv[positions[0] + 1].isdigit(), "QEMU guest identity unknown")
                vmid = int(argv[positions[0] + 1]); require(vmid not in guests, "duplicate QEMU guest process")
                guests[vmid] = identity
            elif name == "lxc-monitord":
                require(executable == "/usr/libexec/lxc/lxc-monitord"
                        and argv == [b"/usr/libexec/lxc/lxc-monitord", b"--daemon"],
                        "LXC monitor daemon identity unknown")
            elif name.startswith("lxc-"):
                # Explicit refusal is preferable to calling stopped PVE daemons
                # or silently missing an independently launched container.
                raise Refusal("live LXC process requires qualified runtime inventory")
            if V1.storage_writer_command(raw): relevant.append(identity)
            last = read(path / "stat", pseudo=True).rsplit(b") ", 1)[1].split()
            require(int(last[19]) == started, "process identity reused")
        except (FileNotFoundError, ProcessLookupError):
            continue
    return guests, sorted(relevant, key=lambda p: p["pid"])


def tasks(processes):
    raw = read("/var/log/pve/tasks/active", empty=True)
    active = []
    for line in raw.decode("ascii").splitlines():
        fields = line.split()
        require(len(fields) >= 2, "task record incomplete")
        match = re.fullmatch(r"UPID:([^:]+):([0-9A-F]+):([0-9A-F]+):([0-9A-F]+):[^:]*:[^:]*:[^:]*:", fields[0])
        require(match is not None and fields[1] in ("0", "1"), "task record invalid")
        pid, started = int(match.group(2), 16), int(match.group(3), 16)
        try:
            tail = read(f"/proc/{pid}/stat", pseudo=True).rsplit(b") ", 1)[1].split()
            if int(tail[19]) == started: active.append(fields[0])
        except (FileNotFoundError, ProcessLookupError): pass
    require(read("/var/log/pve/tasks/active", empty=True) == raw, "task registry changed")
    return sorted(active)


def dm_inventory(storage):
    blocks = V1.parse_storage(storage)
    vgnames = set()
    for block in blocks:
        if block["kind"] != "sharedlvmthin": continue
        props = block["properties"]
        name = props.get("slt-vgname", props.get("vgname", ""))
        require(NAME.fullmatch(name), "managed VG name absent")
        vgnames.add(name)
    vgs = strict(command(["/usr/sbin/vgs", "--readonly", "--reportformat", "json", "-o", "vg_name,vg_uuid"]))["report"][0]["vg"]
    found = {r["vg_name"].strip(): r["vg_uuid"].strip().replace("-", "") for r in vgs if r["vg_name"].strip() in vgnames}
    # Control-only nodes can see no assigned VGs. Any local managed mapper
    # still refuses below, including plugin frontends outside an LVM UUID.
    rows = []
    for path in sorted(Path("/sys/block").glob("dm-*")):
        name = read(path / "dm/name", pseudo=True).decode().strip()
        uuid = read(path / "dm/uuid", pseudo=True, empty=True).decode().strip()
        if name.startswith(("slt-", "sltg-", "sltl-")) or uuid.startswith(("SLT", "slt")) or any(uuid.startswith("LVM-" + u) for u in found.values()):
            rows.append({"name": name, "uuid": uuid})
    return rows


def ha_state(node, service_rows):
    resources = read("/etc/pve/ha/resources.cfg", empty=True)
    require(not any(line.strip() and not line.lstrip().startswith(b"#") for line in resources.splitlines()),
            "HA resources are configured")
    lrm = next(r for r in service_rows if r["unit"] == "pve-ha-lrm.service")
    crm = next(r for r in service_rows if r["unit"] == "pve-ha-crm.service")
    if lrm["active_state"] == "inactive":
        require(lrm["cgroup_empty"] and lrm["main_pid"] == lrm["control_pid"] == 0,
                "inactive HA LRM still has executors")
        require(crm["active_state"] == "inactive" and crm["cgroup_empty"]
                and crm["main_pid"] == crm["control_pid"] == 0, "HA CRM not drained")
        require(read("/etc/pve/ha/resources.cfg", empty=True) == resources, "HA resources changed")
        return "DRAINED"
    raw = read(f"/etc/pve/nodes/{node}/lrm_status")
    status = strict(raw)
    require(type(status.get("timestamp")) is int and 0 <= time.time() - status["timestamp"] <= 30,
            "HA local status stale")
    require(status.get("state") == "wait_for_agent_lock" and status.get("mode") in ("active", "disarm")
            and status.get("results") == {}, "HA LRM is not empty and idle")
    require(read("/etc/pve/ha/resources.cfg", empty=True) == resources, "HA resources changed")
    # This is an initial prerequisite, not a claim of persistent disarm.
    # The final barrier requires DRAINED with both HA cgroups empty.
    return "EMPTY_IDLE"


def collect(plan, workload):
    MODEL = B.MODEL
    now = int(time.time()); MODEL.validate_plan(plan, now)
    node = socket.gethostname()
    require(NAME.fullmatch(node), "local node name invalid")
    names = [p["node"] for p in plan["participants"]]
    require(node in names, "node outside plan")
    boot = read("/proc/sys/kernel/random/boot_id", pseudo=True).decode().strip()
    storage = read("/etc/pve/storage.cfg")
    members = read("/etc/pve/.members")
    membership = strict(members)
    require(sorted(membership["nodelist"]) == names and membership.get("cluster", {}).get("quorate") == 1
            and all(r.get("online") == 1 for r in membership["nodelist"].values()), "membership/quorum not exact")
    vmlist_raw = read("/etc/pve/.vmlist")
    vmlist = strict(vmlist_raw)["ids"]
    procs_before, workers = process_inventory()
    guests, configs = [], {}
    local_ids = set()
    for vmid_text, item in vmlist.items():
        require(vmid_text.isdigit() and NAME.fullmatch(item["node"]) and item["type"] in ("qemu", "lxc"), "guest registry invalid")
        if item["node"] != node: continue
        vmid = int(vmid_text); local_ids.add(vmid)
        folder = "qemu-server" if item["type"] == "qemu" else "lxc"
        path = Path(f"/etc/pve/nodes/{node}/{folder}/{vmid}.conf")
        raw = read(path); configs[path] = raw
        if item["type"] == "lxc":
            output = command(["/usr/bin/lxc-info", "-n", str(vmid), "-sH"]).decode().strip()
            require(output in ("STOPPED", "RUNNING"), "LXC status unknown")
            running = output == "RUNNING"
        else: running = vmid in procs_before
        guests.append({"vmid": vmid, "type": item["type"], "node": node,
                       "status": "running" if running else "stopped", "config": raw.decode()})
    require(set(procs_before) <= local_ids, "unregistered QEMU process")
    service_rows = services()
    dm_before = dm_inventory(storage)
    active_tasks = tasks(procs_before)
    jobs_raw = command(["/usr/bin/systemctl", "list-jobs", "--no-legend", "--plain"])
    pending = [line.decode() for line in jobs_raw.splitlines() if line.strip()]
    quiescent_ha = ha_state(node, service_rows)
    procs_after, workers_after = process_inventory()
    require(procs_before == procs_after and workers == workers_after,
            "guest/worker process inventory changed")
    require(dm_inventory(storage) == dm_before, "DM inventory changed")
    require(read("/etc/pve/storage.cfg") == storage and read("/etc/pve/.members") == members
            and read("/etc/pve/.vmlist") == vmlist_raw
            and read("/proc/sys/kernel/random/boot_id", pseudo=True).decode().strip() == boot,
            "identity changed during collection")
    require(all(read(path) == raw for path, raw in configs.items()), "guest config changed")
    obs = {"node": node, "boot_id": boot, "membership": names, "storage_cfg_sha256": B.digest(storage),
           "workload_snapshot_sha256": workload.get("evidence_sha256"), "quorate": True,
           "ha_state": quiescent_ha, "tasks": active_tasks, "workers": workers, "pending_jobs": pending}
    # No managed DM mapping means no open managed LVM block device exists.
    # lvs --readonly reports open status as unknown and is not used as proof.
    return {"observation": obs, "storage_config": storage.decode(), "workload": workload,
            "guests": sorted(guests, key=lambda g: g["vmid"]), "inventory_complete": True,
            "managed_mappers": dm_before, "managed_open_lvs": dm_before, "services": service_rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--workload", required=True)
    args = parser.parse_args()
    try:
        plan, workload = strict(read(args.plan)), strict(read(args.workload))
        evidence = collect(plan, workload)
        result = {"schema": "slt-barrier-node-readonly/v1", "authorization": "NONE", "mutation_performed": False,
                  "evidence": evidence, "verdict": "OBSERVED"}
        try: B.scoped_observation(evidence, plan, evidence["observation"]["node"])
        except (B.Refusal, B.WORKLOAD.Refusal) as error: result.update(verdict="BLOCKED", reason=str(error))
        print(json.dumps(result, sort_keys=True)); return 0 if result["verdict"] == "OBSERVED" else 3
    except (OSError, ValueError, KeyError, IndexError, TypeError, Refusal, V1.Refusal, subprocess.TimeoutExpired) as error:
        print(json.dumps({"schema": "slt-barrier-node-readonly/v1", "authorization": "NONE", "mutation_performed": False,
                          "verdict": "BLOCKED", "reason": str(error)}, sort_keys=True)); return 2


if __name__ == "__main__": sys.exit(main())
