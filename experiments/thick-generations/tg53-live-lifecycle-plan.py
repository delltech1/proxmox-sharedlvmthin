#!/usr/bin/env python3
"""Render a bounded disposable three-node lifecycle plan; never execute it."""

import argparse
import json
import re


SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def need(value, label):
    if not SAFE.fullmatch(value or ""):
        raise SystemExit(f"unsafe {label}: {value!r}")
    return value


def step(name, command, *, destructive=True, failure=False, evidence=(), cleanup=()):
    return {
        "id": name,
        "command": command,
        "destructive_gate": destructive,
        "failure_injection_gate": failure,
        "expected_evidence": list(evidence),
        "cleanup": list(cleanup),
    }


def build(args):
    nodes = [need(value, "node") for value in args.nodes.split(",")]
    if len(nodes) != 3 or len(set(nodes)) != 3:
        raise SystemExit("exactly three distinct nodes are required")
    stores = {
        "thin": need(args.thin, "Thin storage"),
        "eager": need(args.eager, "Eager storage"),
        "lazy": need(args.lazy, "Lazy storage"),
    }
    backup = need(args.backup, "backup storage")
    template = int(args.template_vmid)
    base = int(args.vmid_base)
    if not (100 <= template <= 999999999 and 100 <= base <= 999999989):
        raise SystemExit("VMID outside PVE range")

    # Pairwise coverage: every provisioning mode has 1/2/4 managed data disks;
    # NIC/firmware/special-device dimensions are deliberately distributed.
    layouts = [
        ("thin", 1, "none", "bios", False, False),
        ("thin", 2, "mixed", "efi", False, False),
        ("thin", 4, "e1000", "bios", False, True),
        ("eager", 1, "mixed", "efi", False, True),
        ("eager", 2, "e1000", "bios", False, True),
        ("eager", 4, "virtio", "efi", True, False),
        ("lazy", 1, "none", "efi", False, True),
        ("lazy", 2, "virtio", "bios", True, False),
        ("lazy", 4, "mixed", "efi", True, True),
        ("lazy", 1, "virtio", "bios", False, False),
    ]
    vms = []
    setup = []
    for offset, (mode, disks, nic, firmware, tpm, cloudinit) in enumerate(layouts):
        vmid = base + offset
        node = nodes[offset % 3]
        storage = stores[mode]
        name = f"SLT-AUTOTEST-{mode}-{vmid}"
        commands = [
            f"ssh root@{node} -- qm clone {template} {vmid} --full 1 --storage {storage} --name {name}",
            f"ssh root@{node} -- qm set {vmid} --scsihw virtio-scsi-single",
        ]
        for disk in range(disks):
            commands.append(f"ssh root@{node} -- qm set {vmid} --scsi{disk + 1} {storage}:1,iothread=1")
        if nic == "none":
            commands.append(f"ssh root@{node} -- qm set {vmid} --delete net0")
        elif nic == "e1000":
            commands.append(f"ssh root@{node} -- qm set {vmid} --net0 e1000,bridge=vmbr0")
        elif nic == "virtio":
            commands.append(f"ssh root@{node} -- qm set {vmid} --net0 virtio,bridge=vmbr0")
        else:
            commands.extend([
                f"ssh root@{node} -- qm set {vmid} --net0 virtio,bridge=vmbr0",
                f"ssh root@{node} -- qm set {vmid} --net1 e1000,bridge=vmbr0",
            ])
        if firmware == "efi":
            commands.extend([
                f"ssh root@{node} -- qm set {vmid} --bios ovmf",
                f"ssh root@{node} -- qm set {vmid} --efidisk0 {storage}:1,efitype=4m,pre-enrolled-keys=1",
            ])
        if tpm:
            commands.append(f"ssh root@{node} -- qm set {vmid} --tpmstate0 {storage}:1,version=v2.0")
        if cloudinit:
            commands.append(f"ssh root@{node} -- qm set {vmid} --ide2 {storage}:cloudinit")
        vms.append({"vmid": vmid, "node": node, "mode": mode, "storage": storage,
                    "managed_data_disks": disks, "nic": nic, "firmware": firmware,
                    "tpm": tpm, "cloud_init": cloudinit})
        setup.append(step(f"setup-{vmid}", " && ".join(commands), evidence=(
            f"qm config {vmid} contains exact requested slots",
            f"pvesm list {storage} --vmid {vmid} has no unexpected volume",
            "sharedlvmthin health and cluster quorum remain healthy",
        ), cleanup=(f"ssh root@{node} -- qm destroy {vmid} --purge 1 --destroy-unreferenced-disks 1",)))

    inventory = [
        step("cluster-inventory",
             " && ".join(f"ssh root@{node} -- 'hostname; pveversion -v; pvecm status; pvesm status; multipath -ll; sharedlvmthin doctor'" for node in nodes),
             destructive=False, evidence=("three exact node identities", "one quorate membership view",
                                         "identical plugin/package profile", "all expected storage IDs active")),
        step("reserved-vmid-inventory",
             " && ".join(f"ssh root@{node} -- 'for id in $(seq {base} {base + 20}); do qm config $id >/dev/null 2>&1 && echo COLLISION:$id; done'" for node in nodes),
             destructive=False, evidence=("no COLLISION output",)),
    ]

    primary = vms[0]
    pvm, pnode = primary["vmid"], primary["node"]
    lifecycle = []
    for ram in (0,):
        snap = f"slt-fast-{'ram' if ram else 'disk'}"
        lifecycle.extend([
            step(f"snapshot-{snap}",
                 f"ssh root@{pnode} -- pvesh create /nodes/{pnode}/qemu/{pvm}/snapshot --snapname {snap} --vmstate {ram}",
                 evidence=(f"qm config {pvm} --snapshot {snap}", "UPID exitstatus=OK",
                           "pre/post canary SHA256 unchanged")),
            step(f"rollback-{snap}", f"ssh root@{pnode} -- qm rollback {pvm} {snap} --start 0",
                 evidence=("UPID/process exit 0", "exact HEAD/generation postcondition", "canary SHA256 equals snapshot baseline")),
            step(f"delete-{snap}", f"ssh root@{pnode} -- qm delsnapshot {pvm} {snap}",
                 evidence=(f"qm listsnapshot {pvm} lacks {snap}", "no VM lock", "no orphan generation/vmstate")),
        ])
    ram_vm = next(vm for vm in vms if vm["nic"] == "virtio" and vm["mode"] == "lazy")
    rvm, rnode = ram_vm["vmid"], ram_vm["node"]
    lifecycle.extend([
        step("snapshot-ram-virtio", " && ".join([
            f"ssh root@{rnode} -- qm set {rvm} --vmstatestorage {ram_vm['storage']}",
            f"ssh root@{rnode} -- qm start {rvm}",
            f"ssh root@{rnode} -- /usr/sbin/sharedlvmthin snapshot-preflight {rvm} --ram",
            f"ssh root@{rnode} -- pvesh create /nodes/{rnode}/qemu/{rvm}/snapshot --snapname slt-fast-ram --vmstate 1",
        ]), evidence=(f"qm config {rvm} --snapshot slt-fast-ram parses", "vmstate volume is referenced",
                      "UPID exitstatus=OK", "guest canary SHA256 unchanged")),
        step("rollback-ram-virtio", f"ssh root@{rnode} -- qm rollback {rvm} slt-fast-ram --start 1",
             evidence=("VM returns running", "vmstate consumed through native path", "guest canary SHA256 unchanged")),
        step("delete-ram-virtio", f"ssh root@{rnode} -- qm delsnapshot {rvm} slt-fast-ram",
             evidence=("snapshot and vmstate absent", "no VM lock/orphan")),
    ])
    lifecycle.append(step("resize", f"ssh root@{pnode} -- qm resize {pvm} scsi1 +1G",
                          evidence=("qm config reports exact larger size", "LV size matches", "canary SHA256 unchanged")))
    lifecycle.extend([
        step("backup-stop", f"ssh root@{pnode} -- vzdump {pvm} --storage {backup} --mode stop --compress zstd",
             evidence=("vzdump UPID exitstatus=OK", "archive exists and is non-empty")),
        step("restore-new-vmid",
             f"ssh root@{pnode} -- sh -c 'qmrestore /mnt/pve/{backup}/dump/vzdump-qemu-{pvm}-*.vma.zst {base + 20} --storage {stores['thin']} --unique 1 && qm set {base + 20} --name SLT-RESTORED-{base + 20}'",
             evidence=(f"qm config {base + 20} parses with exact new name SLT-RESTORED-{base + 20}",
                       "restored disk count, bus slots and sizes equal the backup manifest",
                       "no stale snapshot or vmstate reference points to the source VMID",
                       "restored VM boots independently and its canary SHA256 equals source",
                       "source VM configuration and volumes remain byte-identical"),
             cleanup=(f"ssh root@{pnode} -- qm destroy {base + 20} --purge 1 --destroy-unreferenced-disks 1",)),
    ])

    moves = []
    sequence = [("thin", "eager"), ("eager", "lazy"), ("lazy", "thin"),
                ("thin", "lazy"), ("lazy", "eager"), ("eager", "thin")]
    for source, target in sequence:
        moves.append(step(f"move-{source}-to-{target}",
            f"ssh root@{pnode} -- sh -c '/usr/sbin/sharedlvmthin storage-move-preflight {pvm} scsi1 {stores[target]} && qm move_disk {pvm} scsi1 {stores[target]} --delete 1'",
            evidence=(f"config scsi1 is on {stores[target]}", "source volume absent",
                      "destination volume present", "canary SHA256 unchanged", "no VM lock/orphan")))

    migrations = []
    for index, online in enumerate((0, 1)):
        source, target = nodes[index], nodes[index + 1]
        migrations.append(step(f"migration-{'online' if online else 'offline'}",
            f"ssh root@{source} -- qm migrate {pvm} {target} --online {online}",
            evidence=(f"VM owner becomes {target}", "source mapper inactive", "target mapper exact",
                      "config digest/volume IDs unchanged", "guest canary SHA256 unchanged")))

    burst_commands = [f"ssh root@{vm['node']} -- qm start {vm['vmid']}" for vm in vms]
    burst = [step("ten-vm-start-burst", " & ".join(burst_commands) + " ; wait",
                  evidence=("10 exact UPIDs/process results", "all VMs running or explicit qualified refusal",
                            "no stuck locks", "cluster/storage health unchanged")),
             step("ten-vm-offline-evacuation",
                  f"ssh root@{nodes[0]} -- experiments/thin-guard/bulk-offline-evacuation.sh migrate /root/SLT-AUTOTEST.manifest {nodes[0]} {nodes[1]} 2",
                  evidence=("manifest identities unchanged", "each VM has one owner", "source activations absent",
                            "target activations exact", "all canary SHA256 values unchanged"))]

    failure_gates = [
        step("failure-path-loss-during-one-move", "MANUAL_APPROVAL_REQUIRED: isolate one SAN path, never all paths",
             failure=True, evidence=("multipath remains healthy/degraded as expected", "operation succeeds or fails closed",
                                     "source/destination ownership unambiguous")),
        step("failure-reboot-during-one-operation", "MANUAL_APPROVAL_REQUIRED: reboot exactly one non-quorum-critical node",
             failure=True, evidence=("quorum retained", "journal/recovery state exact", "no double activation",
                                     "explicit recovery completes or remains fail-closed")),
    ]

    cleanup = [step("cleanup-all", " && ".join(
        f"ssh root@{vm['node']} -- qm destroy {vm['vmid']} --purge 1 --destroy-unreferenced-disks 1" for vm in vms),
        evidence=("all reserved VM configs absent", "all owned volumes absent", "no locks/tasks",
                  "quorum, HA, schedulers, multipath and sharedlvmthin health PASS"))]

    return {
        "schema": "sharedlvmthin-live-lifecycle-dry-run/v1",
        "authority": "NONE",
        "execution": "FORBIDDEN_THIS_OUTPUT_IS_A_PLAN",
        "scope": {"nodes": nodes, "storages": stores, "backup": backup,
                  "template_vmid": template, "vmid_range": [base, base + 20]},
        "preconditions": [
            "all VMIDs are reserved and absent cluster-wide",
            "template is disposable, bootable, shutdown, and has no snapshots",
            "all three storage IDs are active on all nodes and map to expected dedicated VGs",
            "Thin and Thick use separate VGs; Eager and Lazy may share the dedicated Thick VG",
            "quorum, HA, corosync, multipath and plugin doctor are healthy",
            "no package update, recovery-required state, mutation latch or foreign LV exists",
            "backup storage has capacity; every destructive/failure gate has explicit operator approval",
        ],
        "source_basis": [
            "experiments/thick-generations/rc548-six-direction-cycle.sh",
            "experiments/thick-generations/tg53-nic-ram-matrix.sh",
            "experiments/thick-generations/tg53-mixed-three-disk-cycle.sh",
            "experiments/thick-generations/tg53-tenway-snapshot-batch.sh",
            "experiments/thin-guard/bulk-offline-evacuation.sh",
            "outputs/tg53-upstream-rollback/api15/QemuConfig.pm",
            "outputs/tg53-upstream-rollback/api15/Storage.pm",
        ],
        "vms": vms,
        "phases": {"inventory": inventory, "setup": setup, "lifecycle": lifecycle, "six_storage_moves": moves,
                   "migrations": migrations, "burst_and_evacuation": burst,
                   "manual_failure_injection": failure_gates, "cleanup": cleanup},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nodes", default="node-a,node-b,node-c")
    parser.add_argument("--thin", required=True)
    parser.add_argument("--eager", required=True)
    parser.add_argument("--lazy", required=True)
    parser.add_argument("--backup", required=True)
    parser.add_argument("--template-vmid", required=True, type=int)
    parser.add_argument("--vmid-base", default=995100, type=int)
    args = parser.parse_args()
    print(json.dumps(build(args), sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
