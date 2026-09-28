# Lazy-zeroed Thick RAW: bounded feasibility review

Review date: 2026-09-23. Base checkout: `f354e409f30c71aa9eb32edc4fec954db5fe788c`;
concurrent TG33 packaging work is outside this review. This document changes no
plugin, package, storage, service or configuration. No kernel experiment was run.

Verdict: **GO for isolated research design; NO-GO for production wiring or
claiming qualified lazy allocation**. Exact-kernel discard containment and
cluster-wide exclusive metadata ownership remain P0 gates. The existing eager
mode stays supported and default. This does not supersede the stabilization
sequence or authorize a disposable storage experiment by itself.

## What is feasible, and what is not yet proved

The candidate keeps the same storage type and fully reserves the data LV before
publication. The final materialized layout can remain ordinary RAW bytes in that
LV. During lazy operation, however, the usable disk is the guarded frontend plus
persistent clone metadata, not the destination alone. RAW data layout compatibility
does not imply unchanged metadata protocol, migration semantics or recovery.

The upstream [dm-clone documentation](https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html)
supports source-backed reads, region initialization before partial writes,
persistent progress and a final linear mapping. Its discard semantics are not
zeroing semantics. [dm-zero](https://docs.kernel.org/admin-guide/device-mapper/zero.html)
supplies zero reads but drops writes, so the source must additionally be verified
read-only and must never be confused with the writable destination.

The private lab record used kernel `7.0.14-16-pve`. A subsequent read-only
inventory on that exact boot proved `CONFIG_DM_CLONE=m`, `CONFIG_DM_ZERO=m`,
the signed in-tree `dm-clone.ko`, and an available `clone` target v1.0.0. That
proves availability only; no clone table was created and no exact-kernel I/O,
discard, write-zeroes, persistence or recovery behavior was qualified. This
review inspected upstream Linux **v7.0**, not the exact PVE-patched source.
The [PVE kernel build](https://github.com/proxmox/pve-kernel/blob/master/Makefile)
uses its own source/configuration/patch pipeline; a matching major version or
target version string is not enough. Before any experiment collect the exact
running kernel/package/module hashes, boot ID, `CONFIG_DM_CLONE`/`CONFIG_DM_ZERO`,
and available targets without loading a missing module automatically. Module
absence means unqualified, not permission to install or load something.

## Two source-level corrections to the earlier hypothesis

In upstream [v7.0 dm-clone](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-clone-target.c),
`clone_ctr` enables discard handling but does not set `num_write_zeroes_bios`.
`clone_status` can commit metadata unless the NOFLUSH status flag is used.
Consequently diagnostic collection for this research must use `dmsetup status
--noflush`; ordinary status must not be assumed non-mutating.

[dm-table](https://github.com/torvalds/linux/blob/v7.0/drivers/md/dm-table.c)
disables advertised native write-zeroes support when a target lacks it. Replace
the earlier unconditional claim that WRITE ZEROES remains allowed with three
separate obligations: native write-zeroes rejection or proven support;
`BLKZEROOUT` behavior; and QEMU/tool fallback to ordinary zero writes. A zeroing
request that becomes DISCARD/UNMAP must never bypass the discard barrier.

The writable queue setting is not a persisted DM table feature.
[blk-sysfs](https://github.com/torvalds/linux/blob/v7.0/block/blk-sysfs.c)
sets a user discard limit; [blk-settings](https://github.com/torvalds/linux/blob/v7.0/block/blk-settings.c)
combines user/hardware limits and initializes stacking limits.
[blk-core](https://github.com/torvalds/linux/blob/v7.0/block/blk-core.c) and
[block ioctls](https://github.com/torvalds/linux/blob/v7.0/block/ioctl.c)
contain rejection checks for unsupported discard. This makes a zero queue limit
a plausible barrier, not a qualified lifecycle guarantee: table replacement
recomputes limits and needs explicit testing. Never assume a value survives
reload, reconstruction or a new kernel.

## Existing code boundaries requiring deliberate changes

All plugin functions below are in
`usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm`.

| Current boundary | Consequence for a lazy design |
| --- | --- |
| `_thick_alloc_image` | Creates a signed PREPARED allocation, zeros and flushes the complete HEAD, then marks MATERIALIZED. Removing zeroing alone is unsafe. |
| `SharedLvmThinThick.pm` anchor fields, phase set and transition validator | No lazy state or zero-source role exists. Add a versioned protocol that old code refuses; do not masquerade as MATERIALIZED or HYDRATING. |
| `_thick_verify_source_mapper`, `_thick_verify_clone_frontend` | Source verification expects a generation-backed linear mapper. A zero target needs its own exact role verifier; do not weaken the existing verifier. |
| `_thick_activate_volume` | Missing non-materialized frontend currently requires recovery. There is no lazy cluster-wide owner claim here. |
| `_thick_deactivate_volume` | Non-materialized runtime is retained for a worker. A long-lived lazy state needs explicit close/flush/teardown rules, without losing persistent progress. |
| `_thick_materialization_admission` | Every supported non-MATERIALIZED anchor occupies a worker slot. LAZY_READY must not consume that slot. |
| `_thick_anchor`, `_thick_volume_snapshot`, `_thick_volume_resize`, `_thick_recover_resize` | Keep materialized-only guards until a separate operation has safely materialized the disk. |
| `_thick_filesystem_path` | Current HEAD resolves through the stable frontend; snapshot paths resolve directly to snapshot LVs. Never create a lazy snapshot and expose it through that direct path. |
| `volume_has_feature` | Feature claims are currently broad. Audit sparse initialization, copy, snapshot and resize against state; neither a feature probe nor a raw format label authorizes direct destination access. |

Health, recovery, package gates, inventory and both profiles must understand the
new protocol consistently. A single old participating node is not an acceptable
mixed-version lazy writer. Existing legacy/materialized objects retain their
current interpretation; unsupported new objects refuse without conversion.

## P0 conflicts and smallest acceptable scope

1. **Discard:** `no_discard_passdown` alone cannot protect recycled destination
   bytes. QEMU `discard=ignore`/`detect-zeroes=off` is defense in depth, not the
   host I/O barrier. Every supported frontend I/O route must be contained.
2. **Publication:** applying a sysfs restriction after resuming an already-open
   frontend creates an exposure window. The first prototype must not reload a
   published lazy frontend. Reconstruction must remain unpublished until all
   guards and identities are proved. If this cannot be enforced on the tested
   kernel, reject this candidate instead of using a polling watchdog as repair.
3. **Ownership:** a cluster operation lock, quorum or absent userspace worker
   does not stop a remote kernel using the same clone metadata. Durable exclusive
   ownership, no TTL takeover, exact executor admission and proven old-owner
   quiescence remain required. The current inert admission models are not an
   implementation of these prerequisites.
4. **Live migration:** overlapping source/destination activation of the same
   lazy clone metadata is forbidden. The smallest scope requires explicit
   materialization before ordinary shared-storage live migration. Keeping the
   disk lazy throughout normal overlapping live migration conflicts with the
   single-kernel metadata requirement. Do not silently replace live migration
   with downtime or a new transport.
5. **Recovery:** missing/old/corrupt clone metadata must never be recreated for
   an already-used lazy disk. Doing so can lose guest writes; using destination
   bytes directly can disclose stale data. Freeze/refuse and retain evidence.

An extra private clone mapper below a stable linear frontend is worth a later
comparison if it permits discard containment before public mapper creation.
It adds a new graph/identity contract and does not itself prove containment.
Do not add cryptographic wrappers, exotic target compositions, a new daemon or
custom kernel code merely to evade the first failed qualification gate.

## Minimal proposed protocol (design, not implemented)

Proposed states are `LAZY_PREPARED`, `LAZY_READY`, `MATERIALIZING`,
`PIVOT_READY`, `LINEAR_PIVOTED`, then the existing materialized representation.
These names describe obligations, not an approved encoding or transition API.

1. Under canonical VG admission, reserve the complete data capacity plus metadata
   and anchor overhead. Record exact VG/LV identities, size, region geometry,
   allocation transaction, metadata identity and initialization mode. All LVs
   disable autoactivation. Eagerly initialize only the small new metadata object.
   No raw destination path is returned to a consumer.
2. Acquire the non-expiring exclusive runtime owner before activating clone
   metadata. Construct a per-object read-only zero source of exact length, prove
   UUID/table/no dependencies, and build the clone with verified numeric device
   roles and background hydration disabled.
3. Establish and prove discard containment while unpublished. Record LAZY_READY
   durably and verify its exact runtime before returning the stable frontend.
   Crashes at an uncertain publication boundary never select a direct linear
   destination or automatically repeat an executor.
4. While LAZY_READY, foreground guest I/O uses only this exact guarded frontend.
   Intentional inactivity is healthy, not a stalled background job. Explicit
   stop/reopen requires flush, terminal kernel/dependency evidence, and the same
   persistent metadata; uncertain removal retains ownership. Cross-node takeover
   or authority loss requires separately qualified recovery/fencing.
5. Explicit materialization obtains one proven executor and one worker slot.
   The I/O guard remains active throughout. At completion, preserve existing
   exact table/identity/suspended-pivot checks, prove durable data and metadata
   plus zero outstanding hydration, and only then publish linear HEAD.
6. Persist LINEAR_PIVOTED before deleting dependencies. After exact cleanup and
   representation verification, mark MATERIALIZED. Metadata/source cleanup must
   never precede the durable cutover proof. A 100% counter alone is insufficient.

For the first implementation proposal, snapshot/rollback, resize, migration,
backup/export, storage move, and generic restore tooling require materialization
first. This is a deliberate capability restriction, not unchanged feature parity.
An ordinary restore through a guarded lazy frontend may be researched separately;
tools that write the LV directly or convert zeros into discard remain excluded.
Old/eager objects need no new restriction. No automatic eager re-zeroing of an
existing lazy disk is a valid fallback: it would erase acknowledged guest data.

## Ordered disposable qualification matrix

No commands below are executed by this document. Each mutation row needs a fresh
explicitly approved fixture and exact source/package/kernel evidence. Begin with
a small file/loop sandbox with no existing VG or production storage access, then
repeat applicable rows on a separately approved fully allocated 2--4 GiB lab LV.
Sparse loop files do not qualify LVM reservation or SAN durability.

| Gate | Required experiment and decisive result |
| --- | --- |
| L0 read-only | Exact kernel/config/module/target/tool/QEMU identity and source-contract review. Use NOFLUSH status. Unknown evidence blocks. |
| L1 positive control | Nonzero canaries throughout fresh destination; dm-zero/clone initial full frontend read is zero. No claim from a pre-zeroed destination. |
| L2 negative discard control | A different isolated fixture without protection must expose the expected discard hazard; preserve evidence and never publish this fixture. |
| L3 discard barrier | Guarded frontend rejects partial, aligned full-region, multi-region and full-range discard; data and hydration bitmap/counters stay unchanged. Include ioctl, guest trim and supported asynchronous discard interfaces. |
| L4 guard lifetime | Create, suspend/resume, reload, teardown/reopen, failed guard write and controller interruption. No open consumer may observe an unguarded lazy interval. Any reset disables publication until a proven guard exists. |
| L5 writes | Sector/4 KiB/region writes, region-edge crossings and concurrent writes to one region; written bytes exact and all untouched neighbors zero. |
| L6 zero operations | Distinguish native write-zeroes, BLKZEROOUT, ordinary zero writes, QEMU zero detection and unmap requests. Unsupported is acceptable; successful stale-data exposure or silent mis-zeroing is not. |
| L7 persist/reopen | FLUSH/FUA, clean reopen, then separately authorized crash/power-loss tests. An independent oracle records acknowledged durable ranges; metadata rollback/corruption remains refusal. Process kill alone is not power-loss qualification. |
| L8 ownership | A stopped userspace worker with live kernel mapping still blocks B. Unreachable peer, old boot, delayed A and ambiguous owner release never permit another clone activation. Test logic/model first; no deliberate live dual activation. |
| L9 materialize/pivot | Complete guarded hydration; exact inactive linear table and suspended boundary; crash before/after pivot and cleanup. Final raw LV equals the independent logical oracle. |
| L10 integration | Explicit materialize-first refusals; subsequent snapshot/rollback/resize/backup/restore and both directions of migration on the exact old/new tuple. Both package profiles reject unsupported lazy metadata. |
| L11 scale | Concurrent lazy disks do not consume background slots or trigger stall alarms. Measure metadata/RAM cost and first-write amplification at proposed region sizes; arithmetic tests do not qualify large-SAN performance. |

Future harness cleanup must own only fresh identities, preserve evidence on
UNKNOWN/D-state, and never blindly remove a mapper by a reused name. Existing
`stage1-loop-poc.sh` and `first-write-latency-probe.sh` contain useful assertions,
but are not authorized lazy-zero harnesses: do not repoint them at the endurance
objects or copy their cleanup lifecycle without a new review.

Minimal next step: qualify L0, then design/review L1--L4 together. Do not expand
the production state machine until the discard/publication barrier is real.
All matrix rows remain **NOT EXECUTED by this review**; compatibility,
production admission and release authorization remain false.

The follow-up read-only collector is
`experiments/thick-generations/lazy-zero-l0-inventory.sh`. It refuses a host
mismatch, never loads a module or touches a DM table, and binds config, module,
target and tool evidence to the current boot. On the disposable node it records both modules
but classifies L0 as `RETEST_AFTER_EXPLICIT_DM_ZERO_LOAD`, because the zero
target is not currently registered. This is expected fail-closed evidence, not
permission for an implicit module load.
