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
- Anchor schema v5 persists the operation, exact snapshot name, clone source, and selected
  dm-clone region size so recovery does not infer rollback/snapshot semantics
  or depend on the version of the userspace policy that happens to run later.
- A clean live allocation and snapshot transition recreated the disposable
  multipath-backed test object with schema v4 and reached a destination-only
  MATERIALIZED linear state.
- Persistent transition metadata has its own transaction-scoped ownership and
  integrity tags.
- A pure read-only recovery classifier covers crash points C0 through C9. Only
  a materialized anchor, present authoritative HEAD, no OPEN intent, and an
  absent or matching linear runtime frontend can return `SAFE_FOR_MUTATION=1`.
  Every partial or contradictory combination returns `RECOVERY_REQUIRED`;
  contradictory data evidence is classified `AMBIGUOUS` rather than guessed.
- Recovery classification is operation-aware. A rollback clone must prove that
  its runtime source is the signed retained snapshot, not the superseded HEAD.
  A completed rollback is healthy only after clone metadata and the superseded
  HEAD are absent while the retained source snapshot and new HEAD remain.
- At most one potentially blocking probe of each type may exist.
- Timed-out probes must terminate before a later probe is permitted.
- The experimental recovery evidence collector requires an exact multipath
  mapper, scopes its one-shot `vgs` and `lvs` inventories to that device, reads
  device-mapper table/status with `--noflush`, and never overwrites an existing
  evidence directory. It captures evidence only and performs no reconstruction.
- A live capture of the materialized rollback object on disposable shared
  storage classified `HEALTHY` only after proving a linear frontend with one
  dependency on the new HEAD, a retained signed source snapshot, no superseded
  HEAD, no transition metadata, no OPEN intent, healthy quorum, and zero
  D-state tasks. Removing the source from a copied evidence set classified
  `RECOVERY_REQUIRED`/`AMBIGUOUS`; reusing the evidence directory was refused
  before any probe ran.
- Crash boundaries C0 through C9 now exist as explicit, production-inert method
  calls. They cannot be enabled by storage configuration or environment. A
  disposable qualification driver must deliberately override the no-op method
  in its own process, allowing precise process termination without adding a
  fault switch to the installed product.
- Live process termination at C0 left no persistent mutation and classified
  healthy. Termination at C1 left only the signed OPEN intent and classified
  `PREPARE_INCOMPLETE` with mutation blocked. An exact recovery helper acquired
  the normal cluster lock, revalidated storage identity, anchor, frontend, and
  the absence of transition metadata or a second HEAD, then removed only that
  intent. Post-recovery classification was healthy with quorum and zero D-state
  tasks. A lock directory left by `_exit(137)` was released through the normal
  pmxcfs lock-request mechanism; it was never deleted manually.
- Live termination at C2 left the signed destination and clone-metadata LVs but
  did not advance the anchor or runtime frontend. The classifier distinguished
  this from C1 as `PREPARE_UNRECORDED`. The C1 recovery helper refused the C2
  state. A separate exact recovery path verified both ownership proofs,
  autoactivation-disabled and inactive state, the original linear frontend,
  storage identity, quorum, and cluster lock before deleting only those two
  unrecorded objects and clearing the exact intent. The original data SHA-256
  remained unchanged and post-recovery state was healthy with zero D-state.

Current automated result:

```ini
ANCHOR_GEOMETRY_AND_C0_C9_TESTS=70/70_PASS
ONE_LIVE_PROBE_INVARIANT=PASS
EXISTING_THIN_PYTHON_REGRESSION=79_PASS
COMBINED_PERL_REGRESSION=177_PASS
LIVE_READ_ONLY_RECOVERY_CLASSIFICATION=PASS
TAMPERED_SOURCE_EVIDENCE_FAIL_CLOSED=PASS
LIVE_PROCESS_CRASH_C0=PASS
LIVE_PROCESS_CRASH_C1=PASS
LIVE_C1_EXACT_RECOVERY=PASS
LIVE_PROCESS_CRASH_C2=PASS
LIVE_C2_EXACT_RECOVERY=PASS
LIVE_PROCESS_CRASH_C3=PASS
LIVE_C3_SNAPSHOT_IDENTITY_PERSISTED=PASS
LIVE_C3_EXACT_PRIOR_EVIDENCE_RECOVERY=PASS
LIVE_C3_FORWARD_RECOVERY=PASS
LIVE_C3_FORWARD_SNAPSHOT_SHA=PASS
LIVE_C3_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_PROCESS_CRASH_C4=PASS
LIVE_C4_EXACT_SOURCE_MAPPER_RECOVERY=PASS
LIVE_C4_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_PROCESS_CRASH_C5=PASS
LIVE_C5_SOURCE_READY_RECOVERY=PASS
LIVE_C5_NO_METADATA_REINITIALIZATION=PASS
LIVE_C5_FORWARD_SNAPSHOT_SHA=PASS
LIVE_C5_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_C5_TRANSITION_ARTIFACT_CLEANUP=PASS
LIVE_C5_DSTATE_AFTER_RECOVERY=0
```

Anchor schema v5 persists the exact snapshot name for SNAPSHOT and ROLLBACK
transactions. A live C3 process-crash test proved that PREPARED state retains
the requested snapshot identity while the original linear HEAD and its SHA-256
remain authoritative. The recovery classifier returned `RECOVERY_REQUIRED`
with mutations disabled, and the exact prior-evidence recovery restored the
signed MATERIALIZED predecessor before removing only the inactive destination
and metadata objects. This closes the lost-request-context defect exposed by
the earlier v4 C3 test.

The same live C3 transaction was then resumed from its signed v5 PREPARED
anchor and exact OPEN VG intent. A mismatched snapshot request was refused
without changing the object inventory. The matching request reused the
existing destination and metadata objects, completed hydration, published a
destination-only linear frontend, removed the detached metadata object, and
cleared the intent. The independent read-only snapshot and recovered HEAD both
matched the pre-crash SHA-256, with no relevant D-state processes.

A live C4 crash left the signed PREPARED transaction with its exact immutable
source mapper already present. Recovery accepted that mapper only after
verifying its transaction UUID, read-only mode, linear table, sector count,
and single source-generation dependency. It reused rather than recreated the
mapper, completed materialization, removed the source mapper and transition
metadata, and left a destination-only linear HEAD with the original SHA-256.
The orphaned pmxcfs lock expired through the normal cfs lock protocol; it was
not removed manually and recovery retries were bounded and serial.

A live C5 crash stopped the worker after the stable frontend had entered the
suspended state. It exposed one remaining unscoped autoactivation verification
inside the transition-metadata validator. That probe waited behind the
suspended dependency chain, while the transaction itself performed no further
mutation. Restoring the frontend allowed the probe to exit, confirming the
known suspended-frontend/LVM-scan dependency rather than a corrupt metadata
state.

The transaction model now persists `SOURCE_READY` after clone metadata is
initialized and the exact immutable source mapper is verified, but before the
atomic frontend cutover. Recovery from `SOURCE_READY` never initializes clone
metadata again. It requires the exact source mapper UUID, read-only flag,
linear table, sector count, and single dependency, and it distinguishes an
already suspended frontend from an active one before continuing. All LVM
inventory, identity, autoactivation, tag, activation, creation, permission,
and removal commands in this transition path are scoped to the pinned
multipath device.

The matching C5 recovery reused the persisted source view, published the new
generation, completed hydration and the canonical linear pivot, and removed
only the transaction metadata and temporary source mapper. The final frontend
was active and depended only on generation 4. Its SHA-256 matched the pre-crash
baseline, the old generation became the exact signed read-only snapshot, the
classifier returned `HEALTHY` and `SAFE_FOR_MUTATION=YES`, quorum remained
healthy, and no D-state task remained.

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

Allocation, list/path resolution, activation, deactivation, snapshot creation,
grow-only resize, and exact delete are now present on the isolated branch.
Snapshot creation is unit-qualified through the persisted PREPARED,
SOURCE_READY, COMMITTED, HYDRATING, HYDRATION_COMPLETE, LINEAR_PIVOTED, and
MATERIALIZED phases. It
requires an immutable read-only source, exact signed transition artifacts, one
bounded event-numbered hydration wait, and a verified destination-only linear
pivot before clearing the VG intent.

The first live snapshot attempt on a disposable multipath-backed VG exposed a
real udev ordering defect: an unscoped LVM command issued while the stable
frontend was suspended waited behind that frontend. The harness restored the
original linear table, preserved the PREPARED transaction evidence, and
performed no speculative retry. A focused qualification proved that LVM
commands scoped with `--devices /dev/mapper/<pinned-WWID>` complete while the
unrelated frontend is suspended. The snapshot cutover now prepares the
read-only source mapper before suspension and scopes every transition-specific
LVM command to the pinned multipath device.

The corrected live retry reached MATERIALIZED. The final frontend was a single
linear segment with only the new generation as a dependency, the transition
metadata and source mapper were absent, the old generation was a signed
read-only snapshot, and a foreground write to the new HEAD left the snapshot
SHA-256 unchanged.

```ini
LIVE_MULTIPATH_SNAPSHOT=PASS
SUSPENDED_LVM_DEVICE_SCOPING=PASS
FINAL_FRONTEND_LINEAR=PASS
DESTINATION_ONLY_DEPENDENCY=PASS
SNAPSHOT_IMMUTABLE=PASS
HEAD_WRITE_INDEPENDENT=PASS
TRANSITION_ARTIFACTS_DETACHED=PASS
```

Delete refuses active
frontends and any dependent or ambiguous generation. Resize uses one
`lvextend`, zeroes and flushes the new range before publication, and changes an
active frontend through verified inactive-table load followed by explicit
`suspend --noflush`, `resume`, and live-table verification. Any uncertain
outcome preserves the OPEN VG intent and is never retried automatically.

Snapshot delete now resolves exactly one signed generation, refuses the
authoritative HEAD and any open snapshot, brackets the operation with a
dedicated `REMOVE_SNAPSHOT` intent, removes only that exact generation, and
proves that HEAD and generation did not change before clearing the intent. Its
first live execution removed the independent snapshot while the linear HEAD,
its SHA-256, and storage identity remained intact.

```ini
LIVE_SNAPSHOT_DELETE=PASS
HEAD_UNCHANGED_AFTER_SNAPSHOT_DELETE=PASS
SNAPSHOT_DELETE_INTENT=PASS
```

Rollback uses the same persistent dm-clone materialization engine with an
explicit `ROLLBACK` anchor operation. Its source is the exact signed snapshot,
its `old` object is the superseded HEAD, and its `new` object is a fresh fully
allocated generation. A live multipath-backed test wrote divergent data to the
current HEAD, rolled back, and proved that the new HEAD SHA-256 exactly matched
the retained snapshot. The divergent HEAD was removed only after a verified
linear pivot; the snapshot remained read-only and all temporary transition
objects were absent.

```ini
LIVE_ROLLBACK=PASS
ROLLBACK_DATA_MATCH=PASS
ROLLBACK_SOURCE_RETAINED=PASS
SUPERSEDED_HEAD_REMOVED_AFTER_PIVOT=PASS
ROLLBACK_TRANSITION_ARTIFACTS_DETACHED=PASS
ROLLBACK_DSTATE=0
```

## Open gates

1. Repeat snapshot, delete, and rollback with data-bearing active-QEMU
   workloads, then complete recovery lifecycle code.
2. Qualify full-hydration metadata occupancy and geometry performance.
3. Extend deterministic recovery to the persisted C6 through C9 states.
4. Execute process-crash tests at C6 through C9.
5. Execute reboot recovery on disposable local storage.
6. Execute fenced cross-node reconstruction on a disposable shared test LUN.
7. Qualify single-path and total-path loss without automatic repair.
8. Integrate PVE create, snapshot, rollback, clone, migration, backup, restore,
   and thin-to-thick and thick-to-thin storage moves.
9. Run Linux and Windows data-integrity workloads and a long-duration soak.
