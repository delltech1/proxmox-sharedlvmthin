# Thick Generations: experimental design

The public RC5 line remains thin-only. Thick Generations is developed on a
separate experimental branch and must pass its complete release gate before it
is merged or published.

Exactly two SharedLvmThin storage entries may expose `thin` and
`thick-generations` choices over one shared VG. The pair is accepted only when
both entries are shared, use the same PVE node scope, pin the same VG UUID, PV
UUID, and multipath WWID, and configure identical reserve and expected-path
policies. A third SharedLvmThin alias, duplicate allocation mode, or native PVE
`lvm`/`lvmthin` definition for the VG is rejected. All metadata-changing
operations, including thin-pool autogrow, then serialize on a canonical lock
derived from the VG UUID rather than on either storage ID. Legacy unpinned
aliases retain their historical per-storage lock and are not qualified for
same-VG mixed mode.

Both aliases report the same physical VG capacity because neither owns a
partition of it. These per-alias figures are accurate but are not independent
and must not be summed. Doctor reports the relationship rather than inventing
divided capacity values.

The mode uses fully allocated LVs and independent immutable generations. Its
steady-state guest path is deliberately ordinary:

```text
QEMU -> stable device-mapper frontend -> linear -> current HEAD LV
```

### Large-capacity boundary

The geometry implementation is integer-checked through 128 PiB and rejects the
next sector rather than wrapping. Simulated thin-pool inventory tests cover
1 PiB with both suitable and undersized chunk geometry and the 16 PiB
chunk-count boundary. These tests qualify arithmetic, overflow handling, and
diagnostics only. Petabyte-scale physical SAN operation, materialization time,
and recovery time remain unqualified until tested on representative hardware.
Health JSON parses LVM byte counters as exact decimal integers rather than
passing them through binary floating-point. Simulated values above 2^53 and at
128 PiB verify that capacity, free-space, and payload calculations do not lose
low-order bits before the dashboard formats them as GiB, TiB, or PiB.

A snapshot transition temporarily becomes:

```text
immutable source + persistent dm-clone metadata -> writable destination HEAD
```

Before that frontend is published, the complete destination HEAD is explicitly
zeroed and flushed. This is a correctness and data-isolation requirement:
dm-clone marks an unhydrated region valid when it receives a DISCARD, while the
table deliberately uses `no_discard_passdown`. A destination containing old
free-extent data would therefore be unsafe. If zeroing is interrupted, the
persisted `PREPARED` phase repeats the full exact range; it never resumes from
an unproven offset. Hardware BLKZEROOUT is preferred and a full direct-zero
write is the fail-safe fallback, so large or slow destinations can extend the
snapshot callback duration before any guest-visible cutover.

Every direct zero or metadata write additionally proves that the kernel DM
node's `LVM-<VG UUID><LV UUID>` identity matches the exact LV returned by a
device-scoped LVM query. Names alone never authorize a raw write, so a stale
mapper or duplicate-VG name cannot redirect initialization to another device.
The same rule protects reads and device-mapper table construction: all Thick
LV activation is centralized in an activate-and-prove primitive that validates
every requested LV separately before any caller can use its pathname.
Deactivation is symmetric: an existing mapper is UUID-proved before the LVM
command, and authoritative kernel inventory must show every exact mapper
absent afterward. An already-inactive LV can be repeated safely.

When hydration completes, the frontend is atomically pivoted back to a linear
table whose only dependency is the destination generation. dm-clone is a
transaction transport, not the permanent storage format.

The pivot tolerates an exact already-suspended clone left by interruption. It
loads and verifies the deterministic inactive linear table, proves the mapper
is suspended, and then rechecks complete hydration plus writable dm-clone
metadata before resume publishes the destination-only mapping. Unknown,
read-only, failed, or geometrically inconsistent status remains suspended and
fails closed rather than publishing the linear table.

The read-only recovery classifier reports that exact suspended boundary as
`PIVOT_READY`, never as healthy or mutation-safe. This keeps diagnostics and
the explicit resume implementation consistent without allowing an ordinary
operation to adopt or bypass the interrupted transition.

For a running guest, PVE keeps QEMU paused until the storage snapshot callback
returns. Online materialization is therefore asynchronous by default. The
callback may return only after it has persisted and positively verified the
committed transition and scheduled an exact transaction-scoped worker. The
worker performs bounded hydration, the linear pivot, and exact cleanup.

During asynchronous materialization:

- the published clone frontend remains the authoritative guest path;
- the immutable source and writable destination identities are fixed;
- the VG-wide intent remains authoritative through clone publication and its
  positive verification;
- the callback then hands ownership to the signed non-MATERIALIZED anchor and
  removes the VG-wide intent under the same cluster/VG lock;
- that anchor blocks every dependency-changing mutation of the same volume,
  while an independent volume in the VG may open its own exact transaction;
- a worker may coexist with an unrelated short-lived VG intent only when its
  own anchor still proves the exact transaction identity and phase;
- read-only inventory continues to report the current HEAD;
- a guest stop must not dismantle worker-owned dependencies;
- any ambiguous identity, table, status, or intent fails closed;
- failure to schedule the worker falls back to synchronous completion.

The optional `slt-tg-online-materialization synchronous` setting exists for
diagnostic qualification. It intentionally keeps the PVE snapshot callback
open until hydration completes and is not the normal online mode.

If the worker host is lost, transient device-mapper tables disappear but the
anchor, generations, and clone metadata remain authoritative. A VM
start on another node must fail closed until an operator runs:

```text
sharedlvmthin thick-resume <storage-id> <volume>
```

The command accepts no caller-supplied snapshot or transaction identity. It
derives those fields from the signed persistent state, requires either its
exact VG intent or the exact anchor-scoped handoff, and reconstructs runtime
tables only when their persisted phase and exact dependencies permit it.
`PREPARED`, `SOURCE_READY`, `COMMITTED`, `HYDRATING`,
`HYDRATION_COMPLETE`, and `LINEAR_PIVOTED` are explicit resume inputs. A
partial pre-pivot runtime is ambiguous and is refused. Successful resumption
continues persistent dm-clone
progress, completes the linear pivot, and clears only the exact transition
metadata and any matching intent. `LINEAR_PIVOTED` is itself persistent
authority: recovery first proves that the stable frontend maps only the signed
new HEAD, then idempotently finishes any remaining source-mapper, metadata-LV
and superseded rollback-HEAD cleanup. An already absent cleanup object is
accepted only in that post-pivot phase; a present object is revalidated before
its single removal attempt. Each LVM cleanup object is passed through the
device-scoped prove-and-deactivate gate before `lvremove`; a present kernel
mapper must match the authoritative LVM UUID and must be confirmed absent after
deactivation. If a reboot removed the frontend after the recorded
pivot, recovery activates only the signed new HEAD and reconstructs its exact
linear frontend; it never recreates dm-clone metadata or a source mapper.

The same command also resumes a persisted `ROLLBACK` transition. The operation
type is derived exclusively from the signed anchor; callers cannot select or
change it. Before the linear pivot, recovery requires the rollback snapshot
generation, superseded HEAD, new HEAD, metadata LV, transaction ID, geometry,
and `DM_PIVOT` intent to match exactly. After a proven `LINEAR_PIVOTED`
boundary, the superseded HEAD and metadata may already be absent because their
exact cleanup completed before interruption. A stale PVE `lock: rollback` is
not modified by the storage plugin. It
may be cleared with native PVE tooling only after materialization, storage
health, and restored data authority have been positively verified.

Snapshot deletion uses a separate crash-classifiable transaction. It first
records an exact `REMOVE_SNAPSHOT` intent, rebases the materialized anchor to
the canonical HEAD-only state, and only then removes the signed immutable
generation. A crash at any boundary is reported as `SNAPSHOT_DELETE_PREPARED`,
`SNAPSHOT_DELETE_READY`, or `SNAPSHOT_DELETE_FINALIZE`. After read-only
classification, an operator can resume only that exact transaction with:

```text
sharedlvmthin thick-recover-delete <storage-id> <volume>
```

The command accepts neither a snapshot name nor a transaction identifier. It
derives both from persistent signed state, holds the canonical VG lock,
requires quorum and pinned storage identity, scopes LVM mutations to the
expected multipath device, refuses an open or foreign object, performs at most
one exact removal attempt, proves kernel deactivation independently of udev
pathname presence, verifies the canonical HEAD, and clears only the
matching intent. If the object is already absent after the verified rebase, it
performs finalize-only recovery without retrying removal.

An interrupted online grow remains fenced by its `OPEN EXTEND` intent. When
the exact stable linear frontend is still present, recovery is available with:

```text
sharedlvmthin thick-recover-resize <storage-id> <volume>
```

The command accepts no size from the operator. It derives the previously
published byte boundary from the UUID- and dependency-verified linear frontend
and the target boundary from the signed authoritative HEAD LV. If the target is
larger, it repeats and flushes the complete unpublished zero tail, revalidates
the intent and object identities, then publishes the larger linear table via a
verified suspend/resume cutover. If publication already completed, it only
verifies the final map and clears the matching intent. A missing frontend,
smaller backing LV, non-linear table, identity mismatch, or changed authority
is ambiguous and remains fail-closed for manual investigation.

An interrupted restore or allocation may leave exactly one generation-zero
HEAD and its PREPARED anchor behind an OPEN `ALLOC` intent. Recovery is never
automatic and never searches by a similar name. After confirming that no PVE
VM or container configuration references the volume, an operator may run:

```text
sharedlvmthin thick-recover-partial-alloc <storage-id> <volume>
```

The command requires the exact canonical volume name, pinned storage identity,
quorum, the matching OPEN intent, a canonical PREPARED anchor, one signed
generation-zero HEAD, no frontend, no additional generation, disabled
autoactivation, and no cluster-wide PVE reference. It deactivates only an
active exact HEAD, removes the HEAD and anchor once, proves both are absent,
and clears the intent last. Any mismatch remains `RECOVERY_REQUIRED`.

A completed but unreferenced materialized destination is also never removed
automatically. A generation-zero allocation with no snapshots can be removed
with `thick-recover-orphan-alloc`. A materialized orphan that owns signed
snapshots requires the stronger tree operation:

```text
sharedlvmthin thick-recover-orphan-tree <storage-id> <volume>
```

The tree operation first proves pinned identity, quorum, a canonical
materialized anchor, absence of transition metadata and absence of every PVE
VM or container reference. It enumerates only generations whose signed tags
bind them to the exact storage and volume. Each snapshot is then removed by an
independent crash-recoverable `REMOVE_SNAPSHOT` transaction which repeats the
reference check under the canonical VG lock. Only after every snapshot is
absent may the exact HEAD and anchor be removed. A crash can therefore be
resumed without broad name matching or speculative cleanup.

The same fail-closed rule applies in the opposite conversion direction. An
interrupted Thick-to-Thin full copy can leave a conventional per-VM thin disk
and pool that are not referenced by PVE while the Thick source remains
authoritative. The thin recovery gate reports this as `RECOVERY_REQUIRED`.
After independently proving which source remains authoritative, an operator
may remove only the exact unreferenced conventional thin disk with:

```text
sharedlvmthin thin-recover-orphan <storage-id> <volume>
```

The command accepts only a canonical guest disk, repeats the exact cluster-wide
PVE reference check under the normal mutation lock, and delegates to the
existing owned-volume deletion path. Snapshot and per-VM-pool cleanup retain
their normal ownership and last-member checks. No orphan cleanup is automatic.

The mode does not weaken the existing per-VM thin-pool lifecycle. Thin and
Thick Generations are separate storage definitions with independent allocation
semantics, while PVE storage move provides the explicit conversion boundary.
A raw full-copy does not guarantee sparse-range preservation: converting a
fully allocated thick generation to thin may allocate the complete virtual
size in the destination thin LV. Capacity preflight and elastic growth must
therefore treat full virtual size as the safe worst case for that direction.
