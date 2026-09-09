# Thick Generations qualification status

This document records experimental evidence only. Thick Generations is not a
production feature and is not included in a public release.

## Safety boundary

- The public default remains `thin`.
- Experimental artifacts are developed on an unpushed local branch.
- No test accepts a caller-supplied block device.
- Stage 1 tests use unique file-backed loop devices and transaction-scoped
  cleanup only.
- Ambiguous persistent state must block reconstruction, mutation, and cleanup.

## Restored historical evidence

The original Stage 1 experiment was performed with Proxmox VE 9.2.2, kernel
`7.0.2-6-pve`, device-mapper `4.50.0`, and the signed in-tree `dm-clone` module.
The source archive was recovered as untrusted evidence and was never executed
directly. Its relevant scripts were copied into this isolated branch and
reviewed before reuse.

## Repeated Stage 1 loop-device gate

The primitive was repeated on a clean PVE 9 test node with kernel
`7.0.2-6-pve` and `dm-clone` target `v1.0.0`.

```ini
NO_HYDRATION=PASS
SOURCE_IMMUTABLE=PASS
PERSISTENT_REOPEN=PASS
HYDRATION_COMPLETE=PASS
LINEAR_PIVOT=PASS
UDEV_SYNCHRONIZATION=PASS
DESTINATION_INDEPENDENT=PASS
DATA_INTEGRITY=PASS
```

After the pivot, the live table was canonical `linear`, the dependency graph
contained the destination only, and the source could be detached without
changing the visible SHA-256.

## Repeated Stage 1 LVM-resident gate

A second test used a unique file-backed loop PV, a unique disposable VG, and
ordinary LVM LVs for source, destination, metadata, and anchor roles.

```ini
SOURCE_LV_UUID=VERIFIED
DESTINATION_LV_UUID=VERIFIED
METADATA_LV_UUID=VERIFIED
SOURCE_LV_READ_ONLY=PASS
NO_AUTOACTIVATION=PASS
PERSISTENT_REOPEN=PASS
HYDRATION_COMPLETE=PASS
LINEAR_PIVOT=PASS
DESTINATION_ONLY_DEPENDENCY=PASS
DESTINATION_INDEPENDENT=PASS
DATA_INTEGRITY=PASS
```

The test removed the source and clone-metadata LVs after the verified linear
pivot. The destination remained readable and bit-identical. Transaction-scoped
cleanup left no VG, PV, mapper, loop device, work directory, or D-state process.

## State and probe gates

- Anchor tags use a canonical schema and digest.
- Duplicate, incomplete, unknown, foreign, or tampered state fails closed.
- Phase transitions are explicit and monotonic.
- A new snapshot transition must replace the previous transaction identifier;
  transaction identity and generation edges are immutable after PREPARED.
- Anchor schema v3 persists the selected dm-clone region size so recovery does
  not depend on the version of the userspace policy that happens to run later.
- Persistent transition metadata has its own transaction-scoped ownership and
  integrity tags.
- At most one potentially blocking probe of each type may exist.
- Timed-out probes must terminate before a later probe is permitted.

Current automated result:

```ini
ANCHOR_AND_GEOMETRY_TESTS=27/27_PASS
ONE_LIVE_PROBE_INVARIANT=PASS
EXISTING_THIN_PYTHON_REGRESSION=66_PASS
COMBINED_PERL_REGRESSION=120_PASS
```

## Geometry gate

The prototype no longer assigns a fixed 16 MiB clone-metadata LV to every
virtual disk. It bounds region cardinality, calculates metadata capacity from
the resulting geometry, rounds the result to a 4 MiB extent boundary, and
persists the region size in the anchor.

Read-only constructor qualification used sparse disposable loop devices, so
the advertised virtual sizes were not physically allocated or hydrated. The
running kernel accepted all tested geometries in read-write metadata mode:

```ini
32_GIB_4_KIB_REGIONS_24_MIB_METADATA=PASS
1_TIB_8_KIB_REGIONS_144_MIB_METADATA=PASS
30_TIB_256_KIB_REGIONS_136_MIB_METADATA=PASS
TRANSACTION_SCOPED_CLEANUP=PASS
```

This proves constructor viability, not worst-case hydration occupancy or
performance. Those remain explicit qualification gates.

## Ported lifecycle surface

Allocation, list/path resolution, activation, deactivation, grow-only resize,
and exact delete are now present on the isolated branch. Delete refuses active
frontends and any dependent or ambiguous generation. Resize uses one
`lvextend`, zeroes and flushes the new range before publication, and changes an
active frontend through verified inactive-table load followed by explicit
`suspend --noflush`, `resume`, and live-table verification. Any uncertain
outcome preserves the OPEN VG intent and is never retried automatically.

## Open gates

1. Complete snapshot, snapshot-delete, rollback, and recovery lifecycle code.
2. Qualify full-hydration metadata occupancy and geometry performance.
3. Qualify persistent anchor updates and VG intent recovery.
4. Execute process-crash tests at C0 through C9.
5. Execute reboot recovery on disposable local storage.
6. Execute fenced cross-node reconstruction on a disposable shared test LUN.
7. Qualify single-path and total-path loss without automatic repair.
8. Integrate PVE create, snapshot, rollback, clone, migration, backup, restore,
   and thin-to-thick and thick-to-thin storage moves.
9. Run Linux and Windows data-integrity workloads and a long-duration soak.
