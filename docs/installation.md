# Installation

Prerequisites:

1. Present the same shared block identity to every participating PVE node.
2. Configure multipath and LVM device discovery outside this package.
3. Create the shared VG once and verify identical PV/VG identity on every node.
4. Ensure the PVE cluster and fencing design are appropriate for shared writable storage.

Install the package with `apt install ./pve-sharedlvmthin_<version>_all.deb`. Existing `/etc/pve-sharedlvmthin/web.conf` and TLS files are preserved during upgrade. Non-interactive installation never waits for the optional web wizard.

Configure storage through PVE using type `sharedlvmthin`, `slt-vgname`, and an
optional allocation-headroom policy. The compatibility default is `fixed` and
uses `slt-initial-pool-size`; it does not guarantee that asynchronous autogrow
can absorb a fast clone/restore. `proportional` adds
`slt-initial-pool-percent` and an optional `slt-initial-pool-max`. `full`
admits live used bytes plus the complete new disk and fails closed if an
explicit maximum is insufficient. See `clone-restore-burst-capacity.md`.
Mark genuinely shared storage as shared and restrict nodes when appropriate.

Validate with `sharedlvmthin doctor` before allocating data. `sharedlvmthin doctor --deep` is destructive, CLI-only, and must be run only against explicitly approved disposable test space.

The package installs a marked dmeventd autogrow fragment in
`/etc/lvm/lvmlocal.conf` only when no administrator-owned thin command or
autoextend settings already exist. Existing policy is preserved and reported,
never overwritten. Purge removes only the marked package fragment and private
Python cache/state. PVE Storage APIs 14 and 15 are explicitly supported; other
runtime API versions refuse mutation until separately qualified.

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
