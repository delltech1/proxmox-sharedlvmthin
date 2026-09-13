# SharedLvmThin RC5.4 TG24 laboratory release notes

TG24 is an experimental dual-mode candidate for Proxmox VE 9 Storage API 14
and 15. It keeps the established per-VM LVM-thin mode and adds Thick
Generations as an explicitly selected companion mode. Thick Generations uses
temporary persistent `dm-clone` mappings for transitions and returns to an
ordinary independent linear LV after materialization.

## What changed since TG12

- Added deterministic Thick snapshot, rollback, recovery and linear-pivot
  lifecycle handling with signed, transaction-scoped persistent anchors.
- Added same-VG Thin and Thick coexistence and native Thin-to-Thick and
  Thick-to-Thin storage moves.
- Fixed asynchronous multi-disk snapshot handoff so an immediate stop/start
  validates the signed anchor after the VG-wide intent has been cleared.
- Added one-inventory-per-run Thick Doctor collection and prevented a Thick
  alias from relabelling same-VG Thin pools.
- Replaced the complete installation-time Doctor with a bounded `doctor
  --quick` preflight. Full per-volume and anchor diagnostics remain explicit;
  mutation callbacks always perform fresh authoritative checks.

## Qualified laboratory envelope

The disposable three-node Proxmox VE 9.2.x lab ran 150 Thin and 150 Thick
small VMs concurrently. The TG24 evidence includes:

- rolling install/reinstall on API 14 and API 15;
- 60/60 bounded parallel Thick migrations;
- 50/50 native PVE HA relocations and exact restoration of changed placement;
- Thin and Thick allocate, resize, snapshot, rollback, delete, backup and
  restore lifecycle coverage;
- stopped Thin-to-Thick and running Thick-to-Thin storage moves;
- a clean uninterrupted four-hour dual-mode endurance retest;
- exact storage cleanup and return to the recorded VG free-space baseline;
- zero relevant D-state tasks on all three nodes at final verification;
- 160 Python and 244 Perl regression tests;
- successful GitHub CI on the accepted source commit.

This is a medium-scale laboratory result, not validation of a 200--500 VM
enterprise deployment. VM count alone also does not qualify a different SAN,
array firmware, multipath policy, workload or failure domain.

## Package identity

```text
pve-sharedlvmthin_0.9.0~rc5.4~tg24_all.deb
SHA256 6309f058305468533c3a48f681f69069867ecbc0b18f6eaf30523eede1f1401f
```

Two isolated builds produced a byte-identical package. Deploy the same package
on every participating node and first qualify it on disposable storage that
matches the intended production SAN and multipath configuration.

## Important boundaries

- Physical FC HBA, fabric and enterprise-array behavior was not available for
  qualification. Linux VN2VN/tcm_fc lab behavior is not a substitute.
- The plugin does not configure SAN sessions, multipath, fencing, quorum or
  watchdog policy and never performs speculative PV/VG creation or metadata
  repair.
- Existing guest I/O during total path loss follows the host kernel,
  device-mapper, multipath, QEMU and SAN policies. New plugin mutations fail
  closed when identity, quorum or transition health cannot be proven.
- Active dm-thin and active dm-clone total-path-loss outcomes remain documented
  host-stack boundaries; recovered paths alone do not authorize mutation.

Read the installation, known-issues, recovery and qualification documents
before enabling either mode.
