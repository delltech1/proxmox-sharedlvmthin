# Storage requirements

The administrator is responsible for a stable, consistently presented block device and VG. Required properties include:

- identical LUN/WWID, PV UUID, VG name, and VG UUID on all participating nodes;
- no duplicate PV visibility through both raw paths and a multipath mapper;
- reliable transport recovery and a documented bounded total-path-loss policy;
- adequate VG free space for thin-pool growth and metadata;
- adequate PV/VG text-metadata area for the expected number of per-VM pools,
  disks, snapshots, and auxiliary LVs;
- PVE quorum/fencing appropriate for concurrent shared writes.

The plugin does not create iSCSI sessions, FCoE controllers, FC zoning, SAN ACLs, LUNs, multipath maps, PVs, or VGs.

## Capacity domains and space reuse

SharedLvmThin creates a separate thin pool for each VM. This confines thin
metadata, ownership, snapshots, rollback, and most lifecycle failures to that
VM instead of placing unrelated VMs in one thin-metadata domain.

This isolation has an important capacity cost. Space has two allocation levels:

1. free extents in the shared VG, available for creating or growing any VM pool;
2. extents already assigned to a particular VM's thin pool, available only to
   volumes and snapshots in that pool.

Guest TRIM/DISCARD can make blocks reusable at the second level. It does not
shrink the thin pool or automatically return its physical extents to the shared
VG. Consequently, space freed in VM1's pool is not automatically available to
grow VM2's pool. The plugin does not run `lvreduce`, raw-device discard, or
automatic pool shrinking to reclaim such extents because doing so would require
additional relocation and data-safety guarantees that this release does not
claim.

Initial pool sizing and guarded incremental autogrow reduce over-allocation but
cannot eliminate the trade-off. Capacity planning must track both per-pool
usage/metadata pressure and VG free-space reserve. This model intentionally
chooses a smaller failure and ownership domain over the space elasticity of one
large thin pool shared by many VMs.

PV/VG metadata capacity is separate from thin-pool metadata and from free data
extents in the VG. A high object count can exhaust the metadata area even when
`vgs` still reports substantial free GiB. Doctor and the health API report the
read-only `pv_mda_size`/`pv_mda_free` state and warn below 20% or at exhaustion.
They do not resize, relocate, repair, or recreate PV metadata. Size it during
storage provisioning and validate expected object scale before production.

## Optional fail-closed identity gate

RC5 can pin a storage definition to the expected identity:

```text
slt-expected-vg-uuid <VG UUID>
slt-expected-pv-uuid <PV UUID>
slt-expected-wwid <multipath WWID>
slt-expected-min-paths <healthy path count>
```

When any identity field is configured, activation and every plugin mutation
perform a read-only preflight. A mismatch, multiple backing PVs, or an WWID
that cannot be proven from `/dev/mapper/<WWID>` blocks the operation. Omitting
all three fields preserves RC4 compatibility but does not provide the
cross-node identity guarantee.

`slt-expected-min-paths` is deliberately diagnostic. Doctor and the monitoring
API report `WARN`/`DEGRADED` when fewer paths are healthy, but the plugin does
not rewrite multipath configuration, fail an otherwise valid I/O path, or use
the value as an LVM mutation gate.

Supported claims are deliberately conservative: the design targets shared block storage presented consistently to PVE. A vendor or transport is not supported merely because it exposes SCSI.
