# Thin Generation Mobility (research prototype)

Thin Generation Mobility is the preferred copy-based live-mobility design for
SharedLvmThin. It avoids a fully allocated Thick intermediate and does not
activate one dm-thin metadata domain in two kernels.

```text
source node                         target node
generation A thin pool             generation B thin pool
one metadata UUID                  a different metadata UUID
       \                              /
        +--- PVE/QEMU NBD mirror ----+
```

Both pools may be active during the copy because they are independent
metadata domains. QEMU active-sync provides write ordering until its atomic
pivot. Before pivot, generation A is authoritative. After positive QMP,
configuration and owner evidence prove the pivot, generation B is
authoritative and generation A can be retired.

## Why this is preferable to the Thick bridge

- no fully allocated linear LV;
- target consumes Thin data plus bounded pool metadata/headroom;
- uses PVE's existing NBD and QEMU mirror implementation;
- keeps ordinary per-VM Thin steady state on both sides;
- no sanlock, DLM, new package or permanent I/O proxy;
- PVE package updates cannot silently overwrite a patched migration module.

It cannot remove the fundamental temporary capacity requirement of a live
copy. Zero additional data capacity requires the separate Relay Handoff and a
versioned PVE switchover integration point. The coordinator may select Relay
only after that path is qualified; otherwise Thin Generation Mobility is the
preferred fallback before the Thick bridge.

## PVE integration design

The coordinator creates transaction-scoped source and target storage aliases
that make only this VM's exact volume eligible for PVE's supported
local-storage migration path. The aliases are not general-purpose storage and
must include the immutable canonical storage identity, transaction ID, node
scope and expected generation. Target allocation uses deterministic
generation-suffixed LV names, so it cannot collide with the source objects in
the same VG.

No administrator-visible storage entry is globally relabelled from shared to
local. Existing VM configurations and existing storage definitions remain
unchanged. Alias creation, config rebinding and cleanup occur under one
transaction journal and the canonical cluster lock.

## Authority model

`PVE::SharedLvmThinMobility` provides deterministic generation names, a
canonical transaction fingerprint and the fail-closed reference state
machine:

```text
PREPARED -> TARGET_ALLOCATED -> MIRRORING -> MIRROR_READY
         -> PIVOT_COMMITTED -> SOURCE_RETIRED -> COMPLETED
```

Any missing identity, journal integrity, quorum, QMP path, mirror state,
configuration reference or owner evidence becomes `RECOVERY_REQUIRED`.
Pre-pivot abort restores source authority and may delete only a positively
unreferenced target. Post-pivot recovery only completes toward target.

## Enablement gates

This remains disabled until a disposable same-VG prototype proves:

- PVE accepts transaction aliases without changing any canonical storage;
- target generation names survive every storage API callback;
- active-sync QEMU mirror, RAM migration and pivot are ordered exactly;
- crash recovery at every phase is deterministic;
- multi-disk transactions are all-or-nothing at VM authority level;
- path loss, target loss and source loss never authorize both generations;
- source cleanup occurs only after exact target QMP and pmxcfs references;
- upgrades reject an unsupported PVE migration contract before mutation.
