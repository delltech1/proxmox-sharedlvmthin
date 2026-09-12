# Installation

SharedLvmThin consumes an **existing shared block device and an existing
dedicated LVM VG**. It does not turn local disks into shared storage and it
does not configure a SAN. Complete the storage preparation below before
installing or registering the plugin.

## 1. Prepare the SAN and hosts

The storage/SAN administrator must first:

1. Create one LUN on the array for this SharedLvmThin storage.
2. Map that same LUN read/write to every participating PVE host using FC,
   FCoE, iSCSI, or another qualified shared-block transport.
3. Configure zoning, host groups, initiator ACLs and redundant fabrics or
   networks according to the array vendor's documentation.
4. Ensure every host sees the same stable SCSI identity and capacity.
5. Configure device-mapper multipath on every host when more than one path is
   presented. Use the array vendor's supported multipath profile and a
   documented, bounded all-path-loss policy.

The plugin does not create sessions, zoning, ACLs or multipath policy. Do not
use a raw `/dev/sdX` path when the LUN is multipathed.

For worked transport examples, continue with
[iSCSI, FC and FCoE setup examples](transport-setup-examples.md), then return
to this guide for common PV/VG and plugin configuration.

Install the required host packages as appropriate for the selected transport.
For a typical multipathed block device this includes `lvm2` and
`multipath-tools`; FC/iSCSI packages and configuration are vendor- and
transport-specific.

## 2. Prove one shared multipath identity

Run these read-only checks on **every** participating node. Substitute the
actual mapper device and never copy identifiers from this documentation.

```bash
multipath -ll
lsblk -S -o NAME,HCTL,TRAN,VENDOR,MODEL,SERIAL,WWN
readlink -f /dev/mapper/<WWID>
```

Required result:

- exactly one multipath map for the intended LUN on each node;
- the same WWID, vendor/model, serial and size on every node;
- the expected number of paths, with each path `active ready running`;
- no raw path and mapper duplicate presented to LVM;
- unrelated storage and the PVE system disk are clearly excluded.

Stop if any identity differs. Do not initialize a device merely because its
Linux device name looks familiar; `/dev/sdX` names are not stable identities.

## 3. Create the PV and dedicated VG exactly once

This step is destructive to the selected LUN. Perform it only after a second
administrator or equivalent change-control check has positively matched the
array LUN, mapper WWID and intended empty device. If the LUN already contains
data or LVM metadata, skip creation and investigate it read-only.

On **one node only**, and using the multipath mapper rather than a component
path:

```bash
wipefs -n /dev/mapper/<WWID>
pvs --reportformat json
vgs --reportformat json

# DESTRUCTIVE PROVISIONING EXAMPLE -- replace both placeholders and run once:
pvcreate /dev/mapper/<WWID>
vgcreate <DEDICATED_VG_NAME> /dev/mapper/<WWID>
```

Never run `pvcreate`, `vgcreate`, `pvremove`, `vgremove` or `wipefs` as a
recovery action. Never repeat `pvcreate`/`vgcreate` on the other nodes. The
other nodes must discover the metadata created by the first node.

Record the authoritative identities immediately:

```bash
pvs --noheadings -o pv_name,pv_uuid,vg_name /dev/mapper/<WWID>
vgs --noheadings -o vg_name,vg_uuid,pv_count,vg_size,vg_free <DEDICATED_VG_NAME>
```

For the current single-PV safety model, `pv_count` must be exactly `1`. Keep
the WWID, PV UUID and VG UUID in protected operational documentation.

## 4. Verify discovery on every remaining node

Without creating anything, verify on every other node:

```bash
pvs --noheadings -o pv_name,pv_uuid,vg_name
vgs --noheadings -o vg_name,vg_uuid,pv_count,vg_size,vg_free
multipath -ll
```

Every node must report the exact same WWID, PV UUID, VG name and VG UUID. The
VG must be dedicated to SharedLvmThin and must not contain unrelated LVs.
Resolve delayed SAN discovery, LVM device filtering and multipath problems
before registering the plugin.

## 5. Install the plugin on every node

Install the same package version one node at a time:

```bash
sha256sum --check SHA256SUMS
apt install ./pve-sharedlvmthin_<version>_all.deb
```

Existing `/etc/pve-sharedlvmthin/web.conf` and TLS files are preserved during
upgrade. Non-interactive installation never waits for the optional web
wizard.

## 6. Register the existing VG in PVE

Use a neutral storage ID and substitute the identities recorded above. A
representative PVE CLI definition is:

```bash
pvesm add sharedlvmthin <STORAGE_ID> \
  --slt-vgname <DEDICATED_VG_NAME> \
  --slt-expected-vg-uuid <VG_UUID> \
  --slt-expected-pv-uuid <PV_UUID> \
  --slt-expected-wwid <WWID> \
  --slt-expected-min-paths <EXPECTED_PATH_COUNT> \
  --shared 1 \
  --content images,rootdir
```

Use `--nodes <node1,node2,...>` when the LUN is intentionally presented only
to a subset of the cluster. Do not mark a locally attached VG as shared. The
identity pins are strongly recommended: they make a wrong LUN, PV or VG fail
closed before mutation.

For new production deployments, configure `elastic` absolute headroom so that
multi-terabyte virtual disks do not reserve a proportional fraction of their
logical size:

```bash
pvesm set <STORAGE_ID> \
  --slt-initial-pool-mode elastic \
  --slt-burst-headroom-gib 64 \
  --slt-vg-reserve-percent 5
```

The 64 GiB value is a conservative starting point, not a universal throughput
guarantee. Qualify it against the maximum expected write rate and grow
latency. Existing `fixed`, `proportional`, and `full` policies remain available
for compatibility and explicit operational choices. See
`clone-restore-burst-capacity.md`.

### Thin and Thick Generations coexistence

The dual-mode package does not guess which capacity model an administrator
wants. Create one storage definition for each mode you intend to expose. To
offer both modes over one VG, create exactly two SharedLvmThin definitions.
Both must use the same identity, reserve, expected-path, shared, and node-scope
values:

```bash
pvesm add sharedlvmthin <THIN_STORAGE_ID> \
  --slt-vgname <DEDICATED_VG_NAME> \
  --slt-allocation-mode thin \
  --slt-initial-pool-mode elastic \
  --slt-burst-headroom-gib 64 \
  --slt-expected-vg-uuid <VG_UUID> \
  --slt-expected-pv-uuid <PV_UUID> \
  --slt-expected-wwid <WWID> \
  --slt-expected-min-paths <EXPECTED_PATH_COUNT> \
  --slt-vg-reserve-percent 5 \
  --nodes <node1,node2,node3> \
  --shared 1 --content images,rootdir

pvesm add sharedlvmthin <THICK_STORAGE_ID> \
  --slt-vgname <DEDICATED_VG_NAME> \
  --slt-allocation-mode thick-generations \
  --slt-expected-vg-uuid <VG_UUID> \
  --slt-expected-pv-uuid <PV_UUID> \
  --slt-expected-wwid <WWID> \
  --slt-expected-min-paths <EXPECTED_PATH_COUNT> \
  --slt-vg-reserve-percent 5 \
  --nodes <node1,node2,node3> \
  --shared 1 --content images,rootdir
```

Omit `--nodes` from both definitions when the LUN is intentionally available
on every cluster node. Never configure a third SharedLvmThin alias or a native
PVE `lvm`/`lvmthin` storage over this VG. The plugin rejects mismatched or
half-visible pairs before activation or mutation.

Both storage entries report the same physical VG capacity. They are two views
of one allocation domain, not two independent capacity pools; never add their
reported capacities together. Doctor validates and explains this relationship.
The chosen PVE storage ID is the user-visible mode selector when creating,
restoring or moving a disk. Existing volumes retain their own allocation mode;
changing the default does not silently convert them. Use a normal PVE Storage
Move for an explicit Thin-to-Thick or Thick-to-Thin conversion.

## 7. Validate before storing a VM

```bash
pvesm status
sharedlvmthin doctor
sharedlvmthin recovery-check <STORAGE_ID>
```

Run these checks on every participating node. Require matching identity,
healthy multipath, quorum and `SAFE_FOR_MUTATION=YES`. First validate with a
disposable VM and disposable data. `sharedlvmthin doctor --deep` is
destructive, CLI-only, and must be run only against explicitly approved
disposable test space.

## Operational prerequisites

1. Present the same shared block identity to every participating PVE node.
2. Configure multipath and LVM device discovery outside this package.
3. Create the shared VG once and verify identical PV/VG identity on every node.
4. Ensure the PVE cluster and fencing design are appropriate for shared writable storage.

Configure storage through PVE using type `sharedlvmthin`, `slt-vgname`, and an
optional allocation-headroom policy. The compatibility default is `fixed` and
uses `slt-initial-pool-size`; it does not guarantee that asynchronous autogrow
can absorb a fast clone/restore. `proportional` adds
`slt-initial-pool-percent` and an optional `slt-initial-pool-max`. `full`
admits live used bytes plus the complete new disk and fails closed if an
explicit maximum is insufficient. See `clone-restore-burst-capacity.md`.
Mark genuinely shared storage as shared and restrict nodes when appropriate.

The package installs a marked dmeventd autogrow fragment in
`/etc/lvm/lvmlocal.conf` only when no administrator-owned thin command or
autoextend settings already exist. Existing policy is preserved and reported,
never overwritten. Purge removes only the marked package fragment and private
Python cache/state. PVE Storage APIs 14 and 15 are explicitly supported; other
runtime API versions refuse mutation until separately qualified.

## Rolling package upgrade

A package upgrade replaces the plugin and diagnostic files and refreshes only
active PVE management consumers. It does not restart QEMU, deactivate an LVM
volume, reload a device-mapper table, restart multipath or modify the SAN. A
qualified compatible upgrade can therefore preserve running guest I/O, while
the PVE API and web interface can be briefly unavailable during service
refresh.

Upgrade one node at a time:

1. Run `sharedlvmthin upgrade-check`. It inventories every enabled
   SharedLvmThin storage, runs its bounded read-only recovery gate and requires
   one exact `STATE=HEALTHY`, `SAFE_FOR_MUTATION=YES` and
   `THICK_ANCHORS_HEALTHY=PASS` result. Explicitly disabled storage is reported
   and skipped; a duplicate storage ID, timeout, unavailable probe or ambiguous
   output fails closed. Continue only when the final line is
   `UPGRADE_SAFE=YES`.
2. Require every Thick Generations anchor to be `MATERIALIZED`. Do not upgrade
   a node that owns a hydration, rollback, snapshot deletion or recovery
   transaction. This condition is enforced by the upgrade check through the
   Thick anchor recovery gate.
3. Verify the package checksum and confirm that the target release explicitly
   supports the installed anchor schema and PVE Storage API.
4. Install the package on one node. Do not restart QEMU, LVM, multipath or the
   SAN merely because the plugin package changed.
5. Verify the installed version, PVE services, Doctor, recovery checks, running
   VM state and guest I/O before continuing to the next node.

Never assume that an arbitrary downgrade is safe. A release that removes
support for an existing configuration property or persistent Thick Generations
anchor schema requires an explicit downgrade procedure. If compatibility
cannot be positively proven, stop mutations and keep the current package.

`upgrade-check` is advisory and read-only: it never installs a package,
deactivates storage, changes an anchor or repairs a failed gate. It is not run
automatically by `dpkg`, because blocking package replacement can also prevent
installation of a recovery or security fix. The administrator remains in
control of the maintenance transaction.

## RC5 to RC4 rollback

RC4 does not know the RC5-only identity and reserve properties. Before installing
RC4, record their exact values and remove them through `pvesm set <storage>
--delete ...`. Downgrading while they remain in `/etc/pve/storage.cfg` leaves the
storage available but produces parser errors in RC4 services.

Required order:

1. Record `slt-expected-vg-uuid`, `slt-expected-pv-uuid`,
   `slt-expected-wwid`, `slt-vg-reserve-percent`, and
   `slt-vg-reserve-gib`, plus `slt-initial-pool-mode`,
   `slt-initial-pool-percent`, and `slt-initial-pool-max` when configured, for
   every SharedLvmThin storage.
2. Remove only those RC5 properties using the PVE storage API.
3. Install the known-good RC4 package and reload PVE management services one
   node at a time. Do not restart VMs or storage transports.
4. Verify quorum, both storage backends, running VMs, Doctor, and dashboard.
5. When returning to RC5, restore the recorded properties exactly and rerun
   Doctor on both nodes.

Never edit LVM metadata, multipath policy, or the backing LUN as part of plugin
rollback.
