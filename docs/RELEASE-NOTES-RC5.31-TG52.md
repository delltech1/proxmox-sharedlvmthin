# SharedLvmThin RC5.31 TG52

## Experimental pre-release — disposable labs only

Both Thin and Thick Generations are experimental. Use this release only on
disposable Proxmox VE hosts, disposable shared storage and disposable guest
data. It is not production-ready, supported, certified or warranted. Use can
cause data loss, corruption, unavailability, failed migration or recovery,
and host or cluster downtime. Operators remain responsible for fencing,
tested independent backups, recovery, SAN identity, multipath policy,
capacity and all deployment decisions.

## What changed

- Thick Generations now provides Eager Zeroed and Lazy Zeroed provisioning.
  Lazy keeps a persistent guarded transition until verified materialization
  and linear pivot; it is not Thin provisioning.
- A separate `pve-sharedlvmthin-thick` package exposes Thick Generations only.
  It and the DUAL `pve-sharedlvmthin` package are mutually exclusive and share
  the same Thick implementation and on-disk format.
- Thin and Thick must use physically separate dedicated VGs. Eager and Lazy
  may share one dedicated Thick VG because both reserve full capacity.
- Mutation, transition, package replacement, upgrade, reboot and recovery
  paths gained additional fail-closed identity and postcondition checks.
- Compatibility coverage includes PVE Storage API 14 and 15 and a warned,
  deliberately constrained two-node cluster path. This does not replace a
  correct quorum and fencing design.

## Package choices

- `pve-sharedlvmthin_0.9.0.rc5.31.tg52_all.deb`: DUAL Thin + Thick profile.
- `pve-sharedlvmthin-thick_0.9.0.rc5.31.tg52_all.deb`: Thick-only profile.

Verify `SHA256SUMS`, choose exactly one profile, and install the identical
version on every participating node one node at a time. Run
`sharedlvmthin upgrade-check` immediately before a change and validate Doctor,
storage recovery checks and guest I/O before advancing. Profile replacement
has stricter requirements; follow [the Thick-only package boundary](thick-only-package.md).

## Qualification evidence

The final source candidate passed 1,962 Python tests with one intentional skip,
the 12 root-only package-replacement tests, and 1,224 Perl tests. Both
artifacts were reproduced independently, scanned for forbidden/private content
and checked for common shared-payload parity. The same executable lineage
previously passed a rolling DUAL upgrade and reboot with Eager, Lazy and Thin
guest lifecycle checks. The TG52 public-byte artifacts have an offline package
gate only; that earlier live run must not be represented as an exact-byte TG52
installation result.

This evidence is not universal certification. It does not establish safety
for untested arrays, firmware, kernels, LVM/device-mapper versions, multipath
policies, topologies, scale, workloads or failure orderings.

The exact component versions and qualification scope are maintained in the
[compatibility matrix](compatibility.md). The original TG52 SAN lifecycle
qualification used `libpve-storage-perl 9.1.10`, `qemu-server 9.2.7`,
`pve-qemu-kvm 11.0.3-3`, kernel `7.0.14-16-pve` and the recorded API 15
PVE/common package tuple. Newer versions are not made compatible merely by
installing successfully. In particular, storage 9.1.11 and qemu-server 9.2.10
must be treated as one upstream-coupled pair and require the targeted gate
listed in the compatibility document.

Read [installation](installation.md), [known issues](known-issues.md),
[compatibility](compatibility.md), and the
[risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md) before use.
