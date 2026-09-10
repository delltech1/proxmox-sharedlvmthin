# Thick Generations: experimental design

The public RC5 line remains thin-only. Thick Generations is developed on a
separate experimental branch and must pass its complete release gate before it
is merged or published.

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
- the OPEN VG intent blocks another dependency-changing operation;
- read-only inventory continues to report the current HEAD;
- a guest stop must not dismantle worker-owned dependencies;
- any ambiguous identity, table, status, or intent fails closed;
- failure to schedule the worker falls back to synchronous completion.

The optional `slt-tg-online-materialization synchronous` setting exists for
diagnostic qualification. It intentionally keeps the PVE snapshot callback
open until hydration completes and is not the normal online mode.

The mode does not weaken the existing per-VM thin-pool lifecycle. Thin and
Thick Generations are separate storage definitions with independent allocation
semantics, while PVE storage move provides the explicit conversion boundary.
