# BASTRIX SharedLVM TG31+fix2

Package version: `0.9.0~rc5.10~tg31+fix2`. Hotfix **pre-release** for
Proxmox VE 9 and disposable-lab qualification.

## What changed

TG31+fix2 extends the TG31+fix1 kernel-inventory correction to every central
device-existence gate in the storage plugin. A missing udev-created path under
`/dev/mapper` or `/dev/<VG>/<LV>` is no longer accepted as proof that the
corresponding device-mapper object is absent.

The helper derives the canonical DM name and reads the kernel DM inventory.
This protects Thick recovery, reconstruction, rollback, resize and cleanup as
well as Thin allocation publication from stale, delayed or removed udev nodes.
Paths outside the device-mapper and LVM namespaces retain the normal block-node
check.

The Thin allocator also preserves the durable ownership boundary when another
disk is added to an existing per-VM pool. An unowned, stopped-VM pool is
republished fully inactive after allocation; a locally owned running pool stays
active for hot-add; and a pool owned by another node is rejected before any
mutation.

The same durable boundary now protects Thin removal: offline removal is allowed
only from an unowned pool, online hot-remove only from the local owner, and a
foreign-owner removal is rejected before the first `lvremove`.

## Safety boundary

This change does not enable overlapping activation of one dm-thin pool in two
kernels and does not change the documented Thin migration boundary. Unknown,
malformed or duplicate kernel inventory remains fail-closed. The package is a
pre-release and must be staged on disposable storage matching the production
SAN, multipath and cluster configuration.

## Verification

- 799 Perl regression assertions pass.
- 198 Python regression tests pass.
- Exact tests cover udev-independent mapper presence, escaped LVM aliases and
  kernel-confirmed absence.

Installed-package qualification also covered all three lab nodes, the four-way
Thin/Thick online disk add/remove matrix, exact two-disk offline cleanup, and a
two-disk Thin-to-Thick-to-Thin materialized migration bridge with byte-identical
seed verification. Direct overlapping activation of one Thin pool on two hosts
remains intentionally refused.

An additional four-VM Thin gate used 100 GiB virtual disks on two owner nodes.
Concurrent QEMU-path pattern write/flush/read completed on all four VMs;
concurrent secondary-disk hot-add/hot-remove completed without residue; foreign
add and foreign remove were refused before mutation; and final offline cleanup
restored the exact pre-test VG free-space baseline.

Live installed-package qualification evidence is recorded separately and must
pass before publication.
