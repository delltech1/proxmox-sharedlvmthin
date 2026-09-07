# Shared block transport setup examples

These examples prepare an existing SAN LUN for SharedLvmThin on Proxmox VE 9.
They are intentionally generic. Array-side configuration, supported multipath
policy, timeouts and firmware settings must come from the storage/HBA vendor.

Replace every value enclosed in `<...>`. Never paste example identifiers into
a production system. Complete the procedure on disposable storage first.

## Common SAN preparation

Before configuring a PVE node, the SAN administrator must:

- create one LUN dedicated to one SharedLvmThin VG;
- map the **same** LUN to every intended PVE initiator;
- use redundant target ports/controllers and independent fabrics or networks;
- record the expected size, serial and WWID;
- confirm that no filesystem, PV or production data already exists on it.

The plugin does not perform these operations.

## Option A: iSCSI with two portals

The example assumes one target IQN reachable through two independent storage
networks. Each PVE node should have a dedicated initiator address on each
network. Do not route iSCSI through management, corosync or migration networks.

### 1. Install and inspect the initiator

```bash
apt update
apt install open-iscsi multipath-tools lvm2
systemctl enable --now iscsid

cat /etc/iscsi/initiatorname.iscsi
ip -br address
```

Map the displayed initiator IQN on the array. Generate a unique initiator IQN
only during initial host provisioning; changing it after LUN mappings exist
can remove access.

### 2. Discover both target portals

```bash
iscsiadm --mode discoverydb --type sendtargets \
  --portal <TARGET_PORTAL_A_IP>:3260 --discover

iscsiadm --mode discoverydb --type sendtargets \
  --portal <TARGET_PORTAL_B_IP>:3260 --discover

iscsiadm --mode node
```

Confirm that the expected `<TARGET_IQN>` appears through both portals. Stop if
discovery returns an unexpected target or LUN.

If the design requires binding sessions to dedicated iSCSI interfaces, create
and validate open-iscsi iface records according to the NIC and vendor design,
then add `--interface <ISCSI_IFACE_NAME>` consistently to discovery, node
updates and login. Merely having two IP addresses does not prove path
independence.

### 3. Configure optional CHAP and automatic login

For each portal, scope updates to the exact target IQN and portal:

```bash
iscsiadm --mode node --targetname <TARGET_IQN> \
  --portal <TARGET_PORTAL_A_IP>:3260 \
  --op update --name node.startup --value automatic

iscsiadm --mode node --targetname <TARGET_IQN> \
  --portal <TARGET_PORTAL_B_IP>:3260 \
  --op update --name node.startup --value automatic
```

When CHAP is required, also set `node.session.auth.authmethod`,
`node.session.auth.username` and `node.session.auth.password` for each exact
node record. Do not put a real CHAP secret in documentation, tickets, shell
history or process-capture output. Use the site's approved secret-injection
procedure and verify that `/etc/iscsi/nodes` remains root-only.

### 4. Login and verify both sessions

```bash
iscsiadm --mode node --targetname <TARGET_IQN> \
  --portal <TARGET_PORTAL_A_IP>:3260 --login

iscsiadm --mode node --targetname <TARGET_IQN> \
  --portal <TARGET_PORTAL_B_IP>:3260 --login

iscsiadm --mode session --print 3
systemctl enable open-iscsi
```

Required result: two sessions to the intended target, one through each
independent network. Repeat this configuration on every participating PVE
node using that node's own initiator IQN.

### 5. Create and verify the multipath map

Enable multipath only after applying the array vendor's supported device
profile:

```bash
systemctl enable --now multipathd
multipath -r
multipath -ll
lsblk -S -o NAME,HCTL,TRAN,VENDOR,MODEL,SERIAL,WWN
```

There must be one mapper device for the LUN and the expected paths beneath it.
Both portals must resolve to the same WWID. Do not initialize `/dev/sdX` paths.
Inspect the effective policy with `multipath -t` and `multipathd show config`.
An indefinite `queue_if_no_path` policy can block PVE/LVM indefinitely after
complete path loss; choose a bounded policy with the array vendor rather than
copying a generic timeout from this project.

### 6. Boot persistence test

Before production, reboot a disposable/canary node and prove this automatic
sequence without creating another PV or VG:

```text
network online -> iSCSI login -> multipath map -> existing PV/VG discovery
```

After boot, repeat `iscsiadm --mode session`, `multipath -ll`, `pvs` and `vgs`.

## Option B: physical Fibre Channel

Physical FC normally uses HBA drivers and array/fabric configuration rather
than `iscsiadm`.

### 1. Record initiator WWPNs

```bash
for host in /sys/class/fc_host/host*; do
  echo "=== $host ==="
  cat "$host/port_name"
  cat "$host/node_name"
  cat "$host/port_state"
  cat "$host/speed"
done
```

The SAN administrator must zone each host HBA port only to the intended target
ports, add the initiator WWPNs to the correct host/host-group, and map the same
LUN ID and LUN identity to every cluster node. Use two independent fabrics;
do not bridge Fabric A and Fabric B.

### 2. Verify login and discovered remote ports

```bash
for port in /sys/class/fc_remote_ports/rport-*; do
  [ -e "$port" ] || continue
  echo "=== $port ==="
  cat "$port/port_name" 2>/dev/null
  cat "$port/port_state" 2>/dev/null
  cat "$port/roles" 2>/dev/null
done

lsblk -S -o NAME,HCTL,TRAN,VENDOR,MODEL,SERIAL,WWN
```

Expected target remote ports must be `Online`. Do not treat an FC host being
`Online` as proof that the correct target or LUN is logged in.

If a newly mapped LUN is not discovered, use the HBA/vendor-supported scan
procedure. A generic SCSI scan is shown only for deliberate commissioning:

```bash
for host in /sys/class/scsi_host/host*; do
  echo "- - -" >"$host/scan"
done
```

This scan does not repair a missing FC login. Do not run repeated scans during
a transport incident.

### 3. Multipath and recovery policy

```bash
systemctl enable --now multipathd
multipath -r
multipath -ll
multipathd show paths
```

Confirm one map, the same WWID on every node, and paths through both fabrics.
Align `fast_io_fail_tmo`, `dev_loss_tmo`, path checking and `no_path_retry`
with the HBA, array and multipath vendor guidance. SharedLvmThin diagnoses the
result but does not set FC timers or SAN policy.

## Option C: FCoE

FCoE uses the FC/SCSI and multipath validation above but additionally requires
a supported Ethernet/DCE design, VLAN/priority-flow-control configuration and
either an FCF fabric or a deliberately supported VN2VN topology. Interface and
controller creation differs between hardware offload, drivers and software
FCoE implementations.

Do not copy a lab-specific sysfs controller recipe into production. Follow the
NIC, switch and operating-system vendor procedure, then prove:

- each fabric uses a different physical NIC and isolated Layer-2 domain;
- expected FC hosts and target remote ports are `Online`;
- both paths expose the same serial and WWID;
- link loss and automatic recovery work before placing data on the LUN.

Software FCoE and Linux LIO/tcm_fc test targets are not equivalent to
qualification of a physical enterprise FC SAN.

## Create the PV and VG after transport validation

The transport-specific steps end when `/dev/mapper/<WWID>` is stable and
identical on all nodes. Only then, on **one node only**:

```bash
wipefs -n /dev/mapper/<WWID>
pvs --reportformat json
vgs --reportformat json

# DESTRUCTIVE: run once, only on the positively identified empty SAN LUN.
pvcreate /dev/mapper/<WWID>
vgcreate <DEDICATED_VG_NAME> /dev/mapper/<WWID>
```

On all other nodes, discover and verify the existing metadata; never recreate
it:

```bash
pvs --noheadings -o pv_name,pv_uuid,vg_name
vgs --noheadings -o vg_name,vg_uuid,pv_count,vg_size,vg_free
multipath -ll
```

Continue with the main [installation guide](installation.md) to record the
WWID/PV/VG identity and register the existing VG in PVE.
