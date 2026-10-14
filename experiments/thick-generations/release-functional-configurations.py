#!/usr/bin/env python3
"""Inert, auditable configuration coverage for the functional release runner."""
import itertools
import json
import posixpath


def case_options(row, *, vmid, storage, image, bridge):
    """Compile literal qm options only; caller owns live admission and identity.

    Disk factor counts data disks; a separate boot disk and optional EFI state
    are additional. This function neither executes nor qualifies the case.
    """
    if set(row) != set(FACTORS) | {"case_id"}:
        raise ValueError("exact matrix row fields required")
    for index, factor in enumerate(FACTORS):
        if type(row[factor]) is not type(ROWS[0][index]) or row[factor] not in {r[index] for r in ROWS}:
            raise ValueError("unsupported factor: " + factor)
    approved = [dict(zip(FACTORS, values), case_id=f"config-{i:02d}")
                for i, values in enumerate(ROWS, 1)]
    if row not in approved:
        raise ValueError("case identity does not match exact matrix row")
    if type(vmid) is not int or not 100 <= vmid <= 999999999:
        raise ValueError("explicit disposable VM ID required")
    for value in (storage, bridge):
        if not isinstance(value, str) or not value or not all(c.isascii() and (c.isalnum() or c in "_-") for c in value):
            raise ValueError("literal storage/bridge identity required")
    if not isinstance(image, str) or not posixpath.isabs(image) or image.startswith("//") or any(c in image for c in ",\n\r\0"):
        raise ValueError("absolute literal boot image required")
    controller = row["controller"]
    prefix = {"virtio-blk": "virtio", "virtio-scsi-single": "scsi", "sata": "sata"}[controller]
    keys = [prefix + str(i) for i in range(row["disks"] + 1)]
    options = ["--vmid", str(vmid), "--name", "functional-" + row["case_id"],
               "--onboot", "0", "--memory", "1024", "--cores", "1",
               "--machine", "pc" if row["machine"] == "i440fx" else "q35", "--bios", row["firmware"],
               "--ostype", "l26", "--serial0", "socket", "--boot", "order=" + keys[0],
               "--net0", row["nic"] + ",bridge=" + bridge + ",link_down=1",
               "--vmstatestorage", storage]
    if controller == "virtio-scsi-single":
        options += ["--scsihw", "virtio-scsi-single"]
    options += ["--" + keys[0], storage + ":0,import-from=" + image + ",format=raw"]
    for key in keys[1:]:
        options += ["--" + key, storage + ":0.0625"]
    if row["firmware"] == "ovmf":
        options += ["--efidisk0", storage + ":1,efitype=4m,pre-enrolled-keys=0"]
    return {"execution": "OPTIONS_ONLY_NO_LIVE_AUTHORITY", "argv_options": options,
            "boot_key": keys[0], "data_keys": keys[1:], "machine_option": "pc" if row["machine"] == "i440fx" else "q35", "memory_mib": 1024,
            "data_disk_bytes": 67108864, "mode": row["mode"]}

FACTORS = ("machine", "firmware", "mode", "controller", "disks", "nic")
REQUIRED_OPERATIONS = (
    "create-boot-guest-data-sha", "disk-snapshot-rollback-delete-sha",
    "ram-snapshot-rollback-ram-and-disk-sha", "add-second-and-third-disk",
    "multi-disk-snapshot-rollback-sha", "grow-preserve-data-and-zero-extension",
    "shrink-native-refusal-preserve-size-and-data", "native-backup-restore-new-id-sha",
    "offline-migration-roundtrip-sha", "live-migration-or-explicit-mode-refusal",
    "six-directed-storage-moves-sha", "active-lazy-reopen-and-recovery",
    "materialized-lazy-linear-identity-and-sha", "owned-resource-cleanup-proof",
)
ROWS = (
    ("q35", "seabios", "thin", "virtio-blk", 1, "virtio"),
    ("q35", "ovmf", "eager", "virtio-scsi-single", 2, "e1000"),
    ("i440fx", "seabios", "eager", "sata", 3, "rtl8139"),
    ("i440fx", "ovmf", "lazy", "virtio-blk", 1, "e1000"),
    ("q35", "ovmf", "lazy", "sata", 3, "virtio"),
    ("i440fx", "seabios", "thin", "virtio-scsi-single", 2, "virtio"),
    ("q35", "seabios", "lazy", "virtio-blk", 2, "rtl8139"),
    ("q35", "seabios", "thin", "sata", 3, "e1000"),
    ("q35", "ovmf", "thin", "virtio-scsi-single", 1, "rtl8139"),
    ("q35", "seabios", "eager", "virtio-blk", 1, "virtio"),
    ("q35", "seabios", "lazy", "virtio-scsi-single", 3, "virtio"),
    ("q35", "seabios", "thin", "virtio-blk", 3, "virtio"),
    ("q35", "seabios", "thin", "sata", 1, "virtio"),
    ("q35", "seabios", "thin", "sata", 2, "virtio"),
)


def missing_pairs(rows=ROWS):
    """Unsupported/skipped rows must be removed before recalculating coverage."""
    missing = []
    for a, b in itertools.combinations(range(len(FACTORS)), 2):
        expected = set(itertools.product({r[a] for r in ROWS}, {r[b] for r in ROWS}))
        actual = {(r[a], r[b]) for r in rows}
        missing.extend((FACTORS[a], FACTORS[b], x, y) for x, y in sorted(expected - actual))
    return missing


def manifest():
    return {
        "execution": "PLAN_ONLY_NO_LIVE_AUTHORITY",
        "coverage": "pairwise configurations, not exhaustive lifecycle qualification",
        "disk_factor": "data disks; separate boot disk and optional EFI-state volume are additional",
        "rows": [dict(zip(FACTORS, row), case_id=f"config-{i:02d}")
                 for i, row in enumerate(ROWS, 1)],
        "missing_pairs": missing_pairs(),
        "required_operations": list(REQUIRED_OPERATIONS),
        "campaigns": {
            "release-functional": "Small disks; every applicable operation with independent postconditions.",
            "burst": "Concurrent snapshots, migrations and evacuation using explicit disposable VM IDs.",
            "fault": "Separately admitted reboot/path-loss tests; never interrupt unknown or unowned work.",
        },
        "result_policy": {
            "slow": "Observe the same exact native task; no automatic retry or cancellation.",
            "ambiguous": "UNKNOWN; stop further dispatch and retain evidence.",
            "missing_adapter": "NOT_RUN; never count as qualified coverage.",
            "unsupported": "Explicit documented refusal plus unchanged data/config verification, not success by omission.",
        },
        "additional_required_cases": [
            "mixed-thin-eager-lazy-three-disk", "no-nic", "mixed-nics",
            "virtio-link-down", "virtio-firewall-vlan-rate", "efi-tpm-cloudinit",
            "active-lazy-versus-materialized-lazy", "six-directed-storage-moves",
            "thick-only-independent-platform-and-san-qualification",
        ],
    }


if __name__ == "__main__":
    print(json.dumps(manifest(), indent=2))
