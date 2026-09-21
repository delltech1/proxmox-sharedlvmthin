# Thin stability and mobility research roadmap

## Verified PVE 9 lifecycle boundary

The installed PVE 9 migration path starts the target VM with
`qm start --migratedfrom`. Inside `vm_start_nolock`, PVE calls
`PVE::Storage::activate_volumes()` before constructing and starting the
incoming QEMU process. The public storage hints currently describe the guest
OS and whether a plugin may deactivate a volume; they do not provide an
exclusive source-closed cutover callback.

Consequences:

- waiting in target `activate_volume()` for the source owner to disappear
  deadlocks normal shared-storage live migration;
- activating immediately violates the single-kernel dm-thin invariant;
- a pre-start hook runs before target volume activation, but the source must
  remain active for memory migration, so it cannot safely transfer ownership;
- a storage plugin alone cannot reorder the QEMU migration switchover.

TG26 must therefore continue to refuse overlapping Thin live migration.

## Recommended architecture

### Layer 1: TG26 deterministic ownership

Keep the existing node/epoch tags, runtime mapper correlation, quorum and
identity gates. These remain the durable audit and recovery model.

### Layer 2: PVE-native Thin Pool LeaseGuard

The default-only implementation adds an opt-in remote kernel-mapper audit. It
uses PVE cluster membership and the existing authenticated root SSH transport
to require the exact hidden `-tpool` DM UUID to be absent from every configured
peer before an unowned pool may be claimed. An offline peer, failed SSH probe,
missing helper, malformed output or present mapper is fail-closed.

LeaseGuard strengthens clean handoff detection. It does not make dm-thin
shared-writable, does not infer fencing from an expired timer and does not
independently enable native overlapping Thin live migration. Existing storage
keeps `disabled` behavior until an administrator explicitly selects
`remote-audit`.

Guarded activation audits every configured peer even when the durable owner
already names the local node.  The owner epoch is not accepted as proof that an
older plugin, partial handoff, manual activation, or stale dmeventd instance did
not leave the exact pool mapper loaded in another kernel.  Missing or conflicting
peer evidence blocks reactivation without changing LVM metadata.

Immediately before `lvchange -ay`, guarded activation repeats the exact peer
mapper audit and re-reads the durable local owner epoch.  A peer conflict or an
epoch change after claim/guardian preparation blocks activation.  This narrow
commit barrier reduces lifecycle races without depending on private PVE
migration internals.

Remote evidence enumerates kernel `thin-pool` targets and compares their DM
UUIDs, not only their device names.  The exact pool is therefore reported as
present even if an older tool or manual operation loaded it under a
non-canonical mapper name; a canonical-name/UUID mismatch is ambiguous and
fails closed.

### Layer 3: Isolated-copy Thin-to-Thin mobility

The primary Thin mobility design keeps both endpoints Thin but never opens the
same thin-pool metadata in two kernels. The source and target are separate,
generation-scoped per-VM pools with independent metadata LVs:

```text
source owner: thin pool generation A
target owner: newly allocated thin pool generation B
PVE/QEMU managed mirror A -> B
bounded cutover with positive source-close and target-open proof
retain generation A until the whole transaction is committed
```

This requires an explicit pool-generation identity in volume tags and cannot
be implemented by merely setting `shared=0` or by reusing `sltp-<vmid>` on the
target. Legacy pools keep their existing identity and remain supported. No
target pool, source cleanup or ownership change may be inferred from a name.

### Capacity-conserving Thin Relay Handoff

The preferred research direction is now a zero-copy ownership handoff.  The
source remains the only active dm-thin metadata owner during RAM pre-copy and
exports a temporary QEMU/NBD relay.  At the bounded switchover the coordinator
quiesces and flushes I/O, proves the source mapping closed (or the source
fenced), commits ownership with compare-and-swap semantics, activates the same
pool on the target, and pivots the target QEMU block graph to the local LV.

This avoids the Thick capacity reservation, but requires deeper integration
with the PVE/QEMU migration state machine.  It is therefore disabled until the
fault matrix in `thin-relay-handoff.md` is qualified.  It never permits
concurrent dm-thin activation.

### Layer 4: Optional Materialized Thick bridge

Provide an orchestrated safe-live-mobility command using supported PVE
operations rather than private QMP calls:

```text
preflight capacity, quorum, identity, owner and recovery state
online PVE drive mirror: Thin -> independent materialized Thick LV
prove QEMU pivot and source release
prove Thick HEAD is one ordinary linear dependency
native PVE shared-storage live migration of the materialized Thick disk
optional online PVE drive mirror: Thick -> new target-owned Thin pool
prove target-only owner, data canaries and cleanup
```

The optional bridge exchanges temporary capacity and copy time for safety. Cross-node
movement occurs only while the disk is independent linear storage. It can be
implemented as an external orchestrator over supported PVE API tasks, leaving
the storage plugin fail-closed if any task or postcondition is ambiguous.

For a multi-disk VM, all disks must reach the materialized state before VM
migration begins. A failure never converts only the remaining subset or
deletes a source volume whose QEMU pivot was not positively observed.

## Capacity Stability Governor

Static percentages are insufficient when workloads and pool sizes differ.
Add a read-mostly governor that evaluates both percentage and time-to-full:

```text
data_runway_seconds = free_data_bytes / bounded_peak_allocation_rate
meta_runway_seconds = free_metadata_blocks / bounded_peak_metadata_rate
safety_horizon = observed_extend_latency_p99
               + cluster_lock_latency_p99
               + configured_reaction_margin
```

Growth becomes eligible before either runway falls below the safety horizon.
The rates use bounded EWMA plus a recent peak, persist no guest data and reset
conservatively after restart. Kernel low-water events remain a trigger, but a
periodic reconciliation catches the documented case in which no new edge
event is emitted.

Required safeguards:

- exact per-pool and VG free-space reservation under the canonical lock;
- metadata and data evaluated independently;
- no overcommit assumption when multiple pools request growth concurrently;
- maximum extension size and minimum retained VG reserve;
- `error_if_no_space` versus queue policy reported explicitly, never silently
  rewritten;
- growth refusal while ownership, paths, quorum or recovery state is unknown.

## Transaction fingerprint

Before activation and after every ownership transfer, record and compare:

- pool LV UUID, metadata LV UUID and data LV UUID;
- dm-thin transaction ID and `needs_check`/metadata mode;
- exact table hash and dependency device numbers;
- owner node, owner epoch and optional LeaseGuard generation.

The transaction ID is not a distributed lock. It is an additional stale-view
detector. A regression, unexpected change while no legitimate owner existed,
or mismatch between the durable fingerprint and freshly activated target
causes `RECOVERY_REQUIRED`.

## Read-only metadata validation

Use the dm-thin reserved metadata snapshot mechanism only through a bounded,
qualified helper, then run `thin_check` against that frozen root. Never parse
or repair live metadata directly. A timeout, unsupported tool version or
failure to release the reserved metadata snapshot is `UNKNOWN`/blocked, not a
PASS and not an automatic repair request.

This is a diagnostic and release gate, not a per-I/O operation.

## Adaptive pool geometry for new pools

Chunk size is immutable after pool creation. New-pool policy may choose from a
small qualified set using disk size and declared workload profile:

- snapshot-heavy: smaller chunks to reduce COW amplification;
- capacity/sequential-heavy: larger chunks to reduce metadata and allocation
  overhead;
- balanced: the qualified default.

The selected chunk size and calculated metadata headroom become immutable
volume metadata and are displayed by Doctor. Existing pools are never silently
converted. Metadata remains bounded by the kernel/LVM supported maximum.

## Mechanisms deliberately rejected

- Whole-LUN SCSI persistent reservation as a per-VM lock: all pools share the
  LUN, so exclusive reservation would block unrelated owners.
- Shared activation through lvmlockd: LVM explicitly prohibits shared
  activation for thin, cache, RAID, mirror and snapshot LV types.
- Read-only second dm-thin instance followed by an in-place RW switch: it can
  retain a stale in-memory metadata view and still lacks a supported PVE
  source-closed callback.
- A suspended or error target presented to incoming QEMU without lifecycle
  support: target startup may issue block operations and can hang or fail.
- Automatic `thin_repair`, multipath restart, SCSI rescan or ownership
  guessing: these destroy evidence or broaden the failure domain.
- A custom kernel target in the current release: it creates a kernel ABI,
  packaging and long-term maintenance burden before userspace options are
  exhausted.

## Upstream opportunity

A clean future PVE extension would expose an exclusive-storage handoff class:

```text
prepare target without opening mutable backend
pause source and drain block I/O
source plugin deactivate and prove mapper absence
transfer versioned lease
target plugin activate freshly
attach/reopen target backend
resume target
```

QEMU already exposes pause-before-switchover and block graph reopen/mirror
primitives, but PVE must orchestrate them. This belongs in an upstream storage
and migration API proposal, not in an out-of-tree monkey patch.

## Implementation order

1. Keep TG26 behavior and claims unchanged.
2. Qualify the opt-in PVE-native remote mapper LeaseGuard without additional
   packages or watchdog ownership.
3. Implement the Capacity Stability Governor as observation-only telemetry.
4. Add transaction fingerprints and bounded metadata-snapshot validation.
5. Prototype generation-scoped Thin pools and isolated Thin-to-Thin copying
   without changing legacy pool identity or enabling overlapping activation.
6. Prototype the optional Materialized Migration Bridge with one disposable
   single-disk VM, then multi-disk rollback and crash matrices.
7. Expose neither path until PVE task, QEMU pivot, source-close and multi-disk
   atomicity postconditions are positively proven.
8. Draft the exclusive-handoff API proposal for upstream PVE.

## TG28 development result: ThinGuard and Thin Generation Mobility

The first implementation slice now exists as two pure, packageable decision
engines:

- `PVE::SharedLvmThinGuard` implements quorum-fenced activation admission,
  irreversible runtime uncertainty, aggregate node-watchdog decisions and a
  strict no-overlap planned handoff.
- `PVE::SharedLvmThinMobility` implements generation names, canonical
  fingerprints and the source-to-target authority state machine for native
  QEMU mirroring between independent per-VM thin metadata domains.

The node guard deliberately uses one aggregate watchdog client, not one client
per VM. The qualified PVE watchdog multiplexer has a finite client table; a
per-pool design would fail at scale. One uncertain locally active pool blocks
the aggregate refresh and therefore cannot be hidden by healthy sibling pools.

The current exhaustive unit qualification covers all 256 Boolean combinations
of runtime authority evidence. Exactly one combination permits a watchdog
refresh. The combined source tree currently passes 177 Python and 588 Perl
tests and builds a content-validated DEB. This is a development result, not a
production enablement claim: the privileged guardian remains unarmed until a
disposable node proves real watchdog expiry, fencing order and post-reboot
takeover.
