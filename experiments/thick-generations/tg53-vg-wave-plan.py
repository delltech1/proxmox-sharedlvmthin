#!/usr/bin/python3
"""Read-only finite batch planner: never overlap VMs sharing a physical VG."""

import argparse
import json
import re
import subprocess


DISK_KEY = re.compile(r"^(?:(?:scsi|virtio|sata|ide)\d+|efidisk0|tpmstate0)$")


def run_json(argv):
    result = subprocess.run(
        argv + ["--output-format", "json"], text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"read-only PVE query failed: {' '.join(argv)}")
    value = json.loads(result.stdout)
    return value


def disk_storage_ids(config):
    result = set()
    for key, value in config.items():
        if not DISK_KEY.fullmatch(key) or not isinstance(value, str):
            continue
        fields = value.split(",")
        if any(field == "media=cdrom" for field in fields):
            continue
        volid = fields[0]
        if ":" not in volid or volid.endswith(":cloudinit"):
            continue
        storage = volid.split(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", storage):
            raise ValueError(f"VM disk {key} has unsafe storage identity")
        result.add(storage)
    if not result:
        raise ValueError("VM has no schedulable writable disk")
    return result


def canonical_resource(storage, config):
    if config.get("type") != "sharedlvmthin":
        return f"storage:{storage}"
    required = ("slt-expected-vg-uuid", "slt-expected-wwid", "slt-vgname")
    values = [config.get(key) for key in required]
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError(f"sharedlvmthin storage {storage} lacks exact VG identity")
    return "sharedvg:" + ":".join(values)


def greedy_waves(entries):
    waves = []
    assignment = {}
    for vmid, resources in entries:
        for index, occupied in enumerate(waves):
            if occupied.isdisjoint(resources):
                occupied.update(resources)
                assignment[vmid] = index
                break
        else:
            waves.append(set(resources))
            assignment[vmid] = len(waves) - 1
    return assignment


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--vmids", required=True)
    args = parser.parse_args()
    vmids = args.vmids.split(",")
    if len(vmids) != 10 or len(set(vmids)) != len(vmids):
        raise SystemExit("exactly ten unique VMIDs are required")
    if any(not re.fullmatch(r"[1-9][0-9]{2,8}", item) for item in vmids):
        raise SystemExit("invalid VMID")

    resources = run_json(["pvesh", "get", "/cluster/resources", "--type", "vm"])
    owners = {}
    for item in resources:
        value = str(item.get("vmid", ""))
        if value in vmids and item.get("type") == "qemu":
            if value in owners:
                raise RuntimeError(f"duplicate cluster owner for VM {value}")
            owners[value] = item.get("node")
    if set(owners) != set(vmids):
        raise RuntimeError("one or more VM owners are unavailable")

    storage_cache = {}
    entries = []
    detail = {}
    for vmid in vmids:
        conf = run_json(["pvesh", "get", f"/nodes/{owners[vmid]}/qemu/{vmid}/config"])
        physical = set()
        for storage in disk_storage_ids(conf):
            if storage not in storage_cache:
                storage_cache[storage] = run_json(["pvesh", "get", f"/storage/{storage}"])
            physical.add(canonical_resource(storage, storage_cache[storage]))
        entries.append((vmid, physical))
        detail[vmid] = physical
    assignment = greedy_waves(entries)
    for vmid in vmids:
        print(f"{assignment[vmid]}\t{vmid}\t{','.join(sorted(detail[vmid]))}")


if __name__ == "__main__":
    main()
