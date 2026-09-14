# SharedLvmThin RC5.4.1 TG25 maintenance hotfix

TG25 is a compatible maintenance update to the TG24 dual-mode candidate for
Proxmox VE 9 Storage API 14 and 15. It does not change the Thin layout, Thick
Generations anchor schema, volume naming, or steady-state device model.

## Fixes and hardening

- Prevents Debian's LVM initramfs hook from embedding host-only
  `/etc/lvm/archive` and `/etc/lvm/backup` recovery copies. Large archive sets
  can otherwise exhaust early-boot memory while an initramfs is decompressed.
  The hook removes only copies in the private mkinitramfs staging directory;
  host recovery evidence is never deleted.
- Rebuilds an existing initramfs only when an embedded LVM archive/backup copy
  is actually detected. A normal same-version reinstall remains idempotent and
  skips the expensive rebuild.
- Adds `sharedlvmthin compat-check`, a bounded read-only runtime gate covering
  required commands, PVE Storage API 14/15, plugin hook availability, services,
  quorum, initramfs contents, D-state and every applicable storage recovery
  check.

## Qualification performed

- Full PVE package upgrade to the then-current Proxmox VE 9.2.x package set and
  kernel, followed by repeated cold host reboots at the original memory size.
- Automatic iSCSI, multipath and lab FCoE recovery, exact PV/VG identity,
  storage availability and original non-test VM startup verification.
- Thin and Thick allocate, activate, direct write/read, resize, snapshot,
  materialization, rollback SHA-256, snapshot delete and exact cleanup.
- Stopped Thin and Thick round-trip migration between updated cluster nodes,
  with matching SHA-256 on both nodes and no leftover LVM objects.
- Same-version package reinstall and deterministic package-version ordering.
- 163 Python and 244 Perl regression tests.

The transport auto-login configuration used by a site remains administrator
owned. SharedLvmThin verifies recovered identity and health but does not create
or repair SAN sessions, multipath policy, fencing or quorum.

## Package identity

```text
pve-sharedlvmthin_0.9.0~rc5.4.1~tg25_all.deb
SHA256 68a350292fb58e72bd145065bfc5fe049a3b73647c01932edef476ad98663209
```
