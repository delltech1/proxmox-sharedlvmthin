# Thick Generations: experimental design

The public RC5 line remains thin-only. Thick Generations is developed on a
separate experimental branch and must pass its complete release gate before it
is merged or published.

Two storage entries may eventually expose `thin` and `thick-generations`
choices over one shared VG, but this is safe only when both entries pin the
same VG UUID, PV UUID, and multipath WWID. All metadata-changing operations,
including thin-pool autogrow, then serialize on a canonical lock derived from
the VG UUID rather than on either storage ID. Legacy unpinned aliases retain
their historical per-storage lock and are not qualified for same-VG mixed
mode.

The mode uses fully allocated LVs and independent immutable generations. Its
steady-state guest path is deliberately ordinary:

```text
QEMU -> stable device-mapper frontend -> linear -> current HEAD LV
```

A snapshot transition temporarily becomes:

```text
immutable source + persistent dm-clone metadata -> writable destination HEAD
```

When hydration completes, the frontend is atomically pivoted back to a linear
table whose only dependency is the destination generation. dm-clone is a
transaction transport, not the permanent storage format.

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
tables only when all transition mappers are absent. A partial runtime is
ambiguous and is refused. Successful resumption continues persistent dm-clone
progress, completes the linear pivot, and clears only the exact transition
metadata and any matching intent.

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
one exact removal attempt, verifies the canonical HEAD, and clears only the
matching intent. If the object is already absent after the verified rebase, it
performs finalize-only recovery without retrying removal.

The mode does not weaken the existing per-VM thin-pool lifecycle. Thin and
Thick Generations are separate storage definitions with independent allocation
semantics, while PVE storage move provides the explicit conversion boundary.
A raw full-copy does not guarantee sparse-range preservation: converting a
fully allocated thick generation to thin may allocate the complete virtual
size in the destination thin LV. Capacity preflight and elastic growth must
therefore treat full virtual size as the safe worst case for that direction.
