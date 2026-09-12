# Thick Generations qualification status

This document records experimental evidence only. Thick Generations is not a
production feature and is not included in a public release.

The concise requirement-by-requirement view is maintained in the
[Thick Generations release gate](thick-generations-release-gate.md).
Externally reported failure classes and their applicability are tracked in the
[Thick and Thin incident research matrix](thick-thin-incident-research.md).

## Safety boundary

- The public default remains `thin`.
- Experimental artifacts are developed on an unpushed local branch.
- No test accepts a caller-supplied block device.
- Stage 1 tests use unique file-backed loop devices and transaction-scoped
  cleanup only.
- Ambiguous persistent state must block reconstruction, mutation, and cleanup.

## Online storage-move cancellation and network qualification

- A bounded online thick-to-thin move completed at 60 MiB/s after replacing
  emulated high-throughput lab NICs with paravirtualized adapters. The guest
  remained online, quorum was retained, and the destination thin pool did not
  grow speculatively while its usage was unavailable.
- An initial reverse thin-to-thick move was deliberately cancelled by the
  evidence guard after a cluster-network warning. The source remained
  authoritative.
- The cancellation exposed a cleanup gap: PVE can call `free_image()` without
  first deactivating an idle Thick Generations frontend. Cleanup now verifies
  the exact frontend identity and dependency graph, requires an unambiguous
  zero open count, removes the idle frontend, and then deletes only the exact
  owned head and anchor under the VG transaction lock.
- The real orphan produced by the cancelled move was removed through this
  corrected lifecycle path. The original thin source remained attached and
  running, the complete thick allocation was returned to its VG, quorum
  remained healthy, and no D-state process survived.
- After the nested datastore fault was isolated and its snapshot redo layers
  were consolidated, the complete reverse move passed at 60 MiB/s. Later
  mixed-mode qualification also completed online thin-to-thick and
  thick-to-thin moves while preserving filesystem identity and data canaries.

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

## Adaptive clone-geometry construction matrix

The production geometry function was exercised against sparse loop-backed
source and destination devices at 32 GiB, 1 TiB, and 30 TiB. Each metadata
device was sized by the same userspace policy used by the plugin. The harness
positively parsed the kernel status fields and required the reported region
size, metadata block count, and total region count to match the requested
geometry exactly.

The first run exposed a transient udev open during immediate mapper removal.
The qualification harness now uses device-mapper's bounded `--retry` removal
instead of treating the first busy response as final. The complete matrix then
created and removed every mapping without a mapper, loop device, or working
directory left behind.

```ini
GEOMETRY_32_GIB_REGION_SECTORS=8
GEOMETRY_32_GIB_METADATA_MIB=28
GEOMETRY_32_GIB_INITIAL_BLOCKS_USED=265_OF_7168
GEOMETRY_1_TIB_REGION_SECTORS=16
GEOMETRY_1_TIB_METADATA_MIB=144
GEOMETRY_1_TIB_INITIAL_BLOCKS_USED=4145_OF_36864
GEOMETRY_30_TIB_REGION_SECTORS=512
GEOMETRY_30_TIB_METADATA_MIB=136
GEOMETRY_30_TIB_INITIAL_BLOCKS_USED=3886_OF_34816
GEOMETRY_STATUS_MODE=RW
GEOMETRY_STATUS_FAIL=0
GEOMETRY_UDEV_BUSY_RETRY=PASS
GEOMETRY_MATRIX_CLEANUP=PASS
```

The same harness family then held an isolated 8 GiB clone at its fully hydrated
state before any linear pivot. The source loop device was kernel read-only, the
status was sampled once per second, and every sample remained writable and free
of the `Fail` state. Hydration completed in 140 seconds, the source and
destination compared byte-for-byte, and the final status reported every one of
2,097,152 regions hydrated with no region still in flight.

The 20 MiB metadata device used 70 of its 5,120 4 KiB blocks at the measured
high-water mark and at completion. This is substantially below the conservative
allocation policy while retaining structural headroom. The qualification
removed its exact mapper, three loop devices, image files, and working
directory after the comparison.

The production mixed-mode snapshot test independently materialized 1.25 GiB
and 7 GiB thick disks while the guest continuously wrote and flushed matching
payloads to both thick filesystems and one conventional thin filesystem. The
two production workers completed in approximately 32 and 187 seconds, each
peaked at approximately 120 MiB of resident memory, and both returned to a
single destination-only linear dependency. Stable canaries on all three disks
remained exact.

```ini
FULL_HYDRATION_8_GIB=PASS
FULL_HYDRATION_SOURCE_READ_ONLY=PASS
FULL_HYDRATION_REGIONS=2097152_OF_2097152
FULL_HYDRATION_IN_FLIGHT_AT_END=0
FULL_HYDRATION_METADATA_MODE=RW
FULL_HYDRATION_METADATA_HIGH_WATER=70_OF_5120
FULL_HYDRATION_ELAPSED_SECONDS=140
FULL_HYDRATION_BYTE_COMPARE=PASS
FULL_HYDRATION_CLEANUP=PASS
PRODUCTION_MIXED_HYDRATION_1_25_GIB_SECONDS=32
PRODUCTION_MIXED_HYDRATION_7_GIB_SECONDS=187
PRODUCTION_WORKER_MEMORY_PEAK_MIB=120
PRODUCTION_FINAL_FRONTENDS_LINEAR=2_OF_2
PRODUCTION_STABLE_CANARIES=3_OF_3
```

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
COMBINED_PERL_REGRESSION=180_PASS
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
LIVE_PROCESS_CRASH_C6=PASS
LIVE_C6_CLASSIFICATION=PUBLISH_REQUIRED
LIVE_C6_EXACT_FORWARD_RECOVERY=PASS
LIVE_C6_SNAPSHOT_AND_HEAD_SHA=PASS
LIVE_C6_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_C6_DSTATE_AFTER_RECOVERY=0
LIVE_PROCESS_CRASH_C7=PASS
LIVE_C7_IDEMPOTENT_READ_ONLY_RECOVERY=PASS
LIVE_C7_SNAPSHOT_AND_HEAD_SHA=PASS
LIVE_C7_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_C7_DSTATE_AFTER_RECOVERY=0
LIVE_PROCESS_CRASH_C8=PASS
LIVE_C8_HYDRATING_RECOVERY=PASS
LIVE_C8_SNAPSHOT_AND_HEAD_SHA=PASS
LIVE_C8_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_C8_DSTATE_AFTER_RECOVERY=0
LIVE_PROCESS_CRASH_C9=PASS
LIVE_C9_CLASSIFICATION=PIVOT_READY
LIVE_C9_EXACT_PIVOT_RECOVERY=PASS
LIVE_C9_SNAPSHOT_AND_HEAD_SHA=PASS
LIVE_C9_FORWARD_LINEAR_DEPENDENCY=PASS
LIVE_C9_TRANSITION_ARTIFACT_CLEANUP=PASS
LIVE_C9_DSTATE_AFTER_RECOVERY=0
LIVE_REBOOT_RUNTIME_STATE_LOST=PASS
LIVE_REBOOT_ANCHOR_AND_HEAD_PRESERVED=PASS
LIVE_REBOOT_LINEAR_RECONSTRUCTION=PASS
LIVE_REBOOT_DATA_INTEGRITY=PASS
LIVE_CROSS_NODE_IDENTITY=PASS
LIVE_CROSS_NODE_LINEAR_RECONSTRUCTION=PASS
LIVE_CROSS_NODE_DATA_INTEGRITY=PASS
LIVE_CROSS_NODE_ROUND_TRIP=PASS
LIVE_ISCSI_SINGLE_PATH_2_TO_1_TO_2=PASS
LIVE_ISCSI_SINGLE_PATH_READ_ONLY_IO=PASS
LIVE_ISCSI_TOTAL_PATH_LOSS_BOUNDED=PASS
LIVE_ISCSI_TOTAL_PATH_LOSS_QEMU_OUTCOME=BOUNDED_EIO
LIVE_ISCSI_TOTAL_PATH_RECOVERY_2_OF_2=PASS
LIVE_ISCSI_TOTAL_PATH_RECOVERY_DATA_INTEGRITY=PASS
LIVE_ISCSI_TOTAL_PATH_RECOVERY_DSTATE=0
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

A live C6 crash terminated the worker after the anchor had atomically
committed the new HEAD and the previous HEAD had become the signed snapshot,
but before the clone table was loaded. The runtime frontend therefore remained
the exact suspended old-generation linear table. The classifier distinguishes
this state as `DATA_STATE=VALID`, `TRANSACTION_STATE=COMMITTED`, and
`MATERIALIZATION_STATE=PUBLISH_REQUIRED`; an active old-generation table after
COMMITTED is rejected as ambiguous.

Exact C6 recovery derived both generation numbers from their signed object
names, verified the consecutive generation relationship, transaction objects,
immutable source mapper, pinned device identity, and suspended old table, and
then performed only the pending clone publication and later lifecycle phases.
The recovered snapshot and destination HEAD both matched the original
SHA-256. Final state was an active destination-only linear frontend with the
transition objects removed, healthy quorum, and zero D-state tasks.

A live C7 crash terminated the worker after the clone frontend was published
and the previous HEAD was already the signed read-only snapshot, but before the
anchor advanced to HYDRATING. The first recovery attempt exposed an
idempotency defect: repeating `lvchange -pr` against an already read-only LV
returns an error. Recovery now consumes the scoped `lv_attr` evidence, changes
permissions only while the exact LV remains writable, and otherwise verifies
the existing read-only state. Exact recovery completed with matching snapshot
and HEAD SHA-256, a destination-only linear frontend, no transition artifacts,
healthy quorum, and zero D-state tasks.

A live C8 crash left the signed anchor in HYDRATING with an active exact clone
frontend whose background hydration was still disabled. The classifier kept
mutation blocked while reporting valid data and an authoritative clone source.
Exact recovery reused the existing transaction objects, enabled and completed
hydration, persisted HYDRATION_COMPLETE before pivoting, and finalized a clean
linear HEAD. The recovered snapshot and HEAD both matched the original
SHA-256; the clone metadata and source mapper were absent afterward.

A live C9 crash terminated the worker after full clone hydration and the
durable HYDRATION_COMPLETE anchor update, but before any linear pivot. The
read-only classifier proved an unsuspended exact clone at full region count and
reported `MATERIALIZATION_STATE=PIVOT_READY`; it did not treat path health or
completed copying alone as permission to mutate. After the normal stale-lock
window, exact recovery verified the complete clone and persisted identities,
performed only the linear pivot and transaction-scoped cleanup, and returned
the object to HEALTHY. The final frontend depended only on generation 8, the
snapshot and HEAD matched the original SHA-256, no transition artifact or
D-state task remained, and quorum stayed healthy.

A controlled reboot of the canary node started with a healthy materialized
frontend and no guest consumer. After reboot, the volatile device-mapper
frontend was absent while the schema-v5 anchor and generation-8 HEAD remained
present, inactive, and protected from autoactivation. Reconstruction first
rejected an incomplete test configuration without mutation. With the complete
pinned identity and reserve policy, activation recreated the exact `SLT-TG2`
UUID as a destination-only linear frontend. Its SHA-256 matched the pre-reboot
baseline, quorum remained healthy, and no D-state task appeared.

Cross-node qualification used the same disposable shared LUN on a second PVE
node. Because `find_multipaths strict` was active, the target's second LUN was
not mapped until its exact WWID was registered locally; adding the WWID did not
alter the existing shared-thin LUN or its automatic iSCSI startup policy. The
test positively matched WWID, PV UUID, and VG UUID before activation. It then
deactivated the zero-open frontend on the source node, proved its absence,
reconstructed the same `SLT-TG2` linear frontend on the destination node, and
verified the destination-only dependency and original SHA-256. A second
serialized handoff returned the object to the original node with identical
results, healthy quorum, and zero D-state tasks.

The iSCSI path-loss gate first removed and restored one portal on the canary
node while the second portal remained active. Both the existing shared-thin
map and the disposable Thick Generations map changed from two paths to one and
back to two. Forty repeated direct read-only QEMU reads completed with the
original SHA-256. A one-second passive sampler observed no D-state task during
the 15-second single-path workload, and the iSCSI node startup policy remained
unchanged.

The isolated total-path-loss test ran only after proving that the canary node
had no guest, container, active shared-thin pool, or open Thick Generations
frontend consumer. The effective policy reported numeric `no_path_retry 12`,
five-second polling, and `queue_without_daemon no`. Both iSCSI network links
were disabled with an independent transient systemd recovery timer already
armed. Exactly one direct read-only QEMU request was issued; no LVM or PVE
probe was spawned during loss. Multipath exposed a decreasing bounded queue
window, changed queueing to `off` after approximately 75 seconds in this run,
and the QEMU request returned a controlled `EIO` instead of remaining blocked.
This measured duration is evidence for this lab stack, not a general timing
guarantee.

After both links returned, the two iSCSI maps recovered to two usable paths.
The Thick Generations frontend remained a canonical linear mapping to the same
signed generation-8 HEAD, its SHA-256 matched the pre-loss baseline, quorum was
healthy, and no D-state task remained. This qualifies the disposable iSCSI
canary path only. Physical FC/FCoE targets and guest-visible application
behavior still require their own transport-specific qualification.

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

The delete transaction is now ordered as intent, canonical anchor rebase,
exact object removal, postcondition verification, and exact intent clear.
Recovery classifies the three possible interrupted states as
`SNAPSHOT_DELETE_PREPARED`, `SNAPSHOT_DELETE_READY`, and
`SNAPSHOT_DELETE_FINALIZE`. The explicit
`sharedlvmthin thick-recover-delete <storage-id> <volume>` command derives the
object and transaction from persistent state, never accepts caller-supplied
identity, and never retries removal when the exact object is already absent.
Its unit qualification covers pre-rebase continuation and post-delete
finalization. The complete D0-D4 disposable multipath matrix is recorded in
the snapshot-delete crash recovery section below.

The production hook remains inert and has no configuration or environment
switch. The separate disposable qualification driver can terminate only its
own process at D0 before intent, D1 after intent, D2 after canonical rebase,
D3 after exact removal, or D4 after intent clear. This makes each persistent
boundary observable without adding an operational fault-injection interface.

```ini
SNAPSHOT_DELETE_CRASH_CLASSIFICATION=PASS
SNAPSHOT_DELETE_RECOVERY_UNIT=PASS
SNAPSHOT_DELETE_RECOVERY_MULTIPATH=PASS
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

## PVE lifecycle integration

The experimental storage was registered on a three-node PVE 9 cluster with a
mixed Storage API 14/15 test matrix. The same plugin build loaded through the
appropriate API on every node while an existing SharedLvmThin thin-mode
storage remained active and unchanged.

A disposable VM completed allocation, start/stop, offline snapshot, divergent
write, rollback, snapshot deletion, grow-only resize, offline migration, and
live migration. Every published Thick Generations frontend was a single
linear target. Snapshot and rollback generations remained independent, and
the newly added resize range was fully zeroed before publication.

A stopped-mode PVE backup and restore created a new independently owned thick
disk. The restored disk matched the source over its full advertised size and
completed a start/stop cycle. A separate round trip moved that restored disk
from Thick Generations to the existing thin mode and back to Thick
Generations. Both conversions preserved the full-volume digest. Each source
object was removed only after the destination had been published, and the
temporary per-VM thin pool was absent after the return move.

```ini
MIXED_API_14_15_CLUSTER=PASS
THIN_MODE_COEXISTENCE=PASS
PVE_ALLOCATE_START_STOP=PASS
PVE_SNAPSHOT_ROLLBACK_DELETE=PASS
PVE_GROW_RESIZE_ZERO_TAIL=PASS
PVE_OFFLINE_MIGRATION=PASS
PVE_LIVE_MIGRATION=PASS
PVE_BACKUP_RESTORE=PASS
THICK_TO_THIN_MOVE=PASS
THIN_TO_THICK_MOVE=PASS
FULL_VOLUME_DIGEST_AFTER_ROUND_TRIP=PASS
SOURCE_CLEANUP_AFTER_PUBLISH=PASS
POST_TEST_DSTATE=0
POST_TEST_QUORUM=PASS
```

PVE full-clone qualification copied the complete thick disk into a separately
owned anchor and generation. The clone's full-volume digest matched the
source, it completed a start/stop cycle through a destination-only linear
frontend, and deleting the clone removed only its two owned objects. The
source was started again after cleanup and retained its original full-volume
digest.

```ini
PVE_FULL_CLONE=PASS
CLONE_INDEPENDENT_OWNERSHIP=PASS
CLONE_FULL_VOLUME_DIGEST=PASS
CLONE_LINEAR_FRONTEND=PASS
CLONE_EXACT_CLEANUP=PASS
SOURCE_UNCHANGED_AFTER_CLONE_DELETE=PASS
```

A stopped Windows-system disk move from thin mode to Thick Generations was
interrupted at approximately 13 percent by a simultaneous reset of two nested
PVE nodes on the shared lab hypervisor. The surviving node lost quorum and
correctly prevented further mutations until the cluster recovered. PVE had not
committed the destination to the VM configuration and had not deleted the thin
source. The fully allocated but partially copied destination remained an
unreferenced orphan, as can also occur with other storage backends when the
generic `qemu-img convert` worker and its cleanup handler disappear with the
host.

After quorum returned, exact config and inventory evidence proved that the VM
still referenced only the original thin source. The orphan destination was
then removed by its exact volume identity; the source and VM configuration
remained unchanged. This is a fail-safe interruption result from the first
Windows move attempt. Doctor now reports unreferenced Thick Generations
anchors as a read-only operational warning. It scans cluster-wide VM and
container configurations on an arbitrary number of nodes, includes snapshot
and unused-disk references, and reuses the existing per-VG LVM inventory
instead of spawning another LVM probe. Cleanup remains explicit and never
infers that an unreferenced volume is disposable.

```ini
INTERRUPTED_WINDOWS_THIN_TO_THICK=BLOCKED_LAB_INFRA
SOURCE_PRESERVED=PASS
DESTINATION_NOT_PUBLISHED=PASS
NO_SOURCE_DELETE_BEFORE_COPY_COMMIT=PASS
EXACT_ORPHAN_CLEANUP_AFTER_IDENTITY_PROOF=PASS
MULTINODE_ORPHAN_DIAGNOSTIC=PASS
ORPHAN_DIAGNOSTIC_SIDE_EFFECTS=0
FIRST_WINDOWS_THIN_TO_THICK=INTERRUPTED_BY_LAB_INFRA
```

The same stopped 32 GiB Windows system disk was subsequently moved from thin
mode to Thick Generations with PVE's native bandwidth control. The full copy
completed, PVE deleted the thin source only after publishing the destination,
and the VM configuration referenced the new Thick Generations volume. The
anchor identified generation zero as the authoritative HEAD, while the live
frontend was a single destination-only linear target. Both multipath maps
retained two usable paths, the cluster remained quorate, no D-state task was
present after the operation, and Windows started from the moved disk.

The successful retry used 20 MiB/s for the individual operation. A 60 MiB/s
cluster migration limit was then selected for subsequent tests. These are
protective limits for this nested lab, whose NAS and all PVE nodes share one
physical hypervisor datastore. They are not plugin defaults or production
throughput recommendations. The earlier unrestricted attempt coincided with a
confirmed 4.726-second physical datastore I/O latency excursion and a prolonged
datastore degradation interval, followed by two nested PVE guest-requested hard
resets. ESXi and the NAS guest did not reboot. This evidence classifies that
event as a lab-infrastructure failure rather than a Thick Generations data-path
failure.

```ini
WINDOWS_THIN_TO_THICK=PASS
WINDOWS_SYSTEM_DISK_SIZE=32_GIB
FULL_COPY=PASS
SOURCE_DELETE_ONLY_AFTER_COMMIT=PASS
AUTHORITATIVE_HEAD=PASS
FINAL_FRONTEND_LINEAR=PASS
FINAL_DEPENDENCY_DESTINATION_ONLY=PASS
WINDOWS_START_AFTER_MOVE=PASS
MULTIPATH_PATHS=2_OF_2
CLUSTER_QUORUM=PASS
POST_MOVE_DSTATE=0
UNRESTRICTED_NESTED_LAB_MOVE=INFRASTRUCTURE_FAIL
LAB_MIGRATION_BWLIMIT=60_MIB_PER_SECOND
```

## Online hydration tuning qualification

The kernel-recommended 4 KiB region size remains unchanged because it bounds
foreground copy-on-write amplification. The dm-clone defaults of one active
region and one-region copy requests were nevertheless too conservative for a
shared-SAN VM disk: a data-bearing online snapshot progressed at only roughly
18 MiB/s and slowed further while guest I/O was active.

Hydration concurrency and contiguous-copy batching are now explicit,
independently configurable storage properties. The prototype defaults both to
32 regions, producing at most 32 concurrent 4 KiB regions and 128 KiB
contiguous background copy requests. Values outside 1..256, or a batch size
larger than the threshold, fail closed. Recovery verifies the exact live clone
table, including both configured tuning values, before it may continue.

An aggressive runtime-only 256/256 experiment greatly increased hydration
throughput but coincided with a reset of the shared lab hypervisor datastore
and is rejected as a default. The persistent transaction survived that reset,
reconstructed the same committed generation, completed materialization after
boot, and returned to a destination-only linear frontend without ambiguity.
This is useful crash-recovery evidence, not a performance qualification.

A subsequent controlled online test used the proposed 32/32 defaults. A 6 GiB
disk materialized in approximately 80 seconds while a guest completed and
flushed a 768 MiB write. The guest remained reachable, the written file digest
verified, no D-state task appeared, quorum and both storage modes remained
healthy, and the final frontend was again a single destination-only linear
target. A second snapshot followed by rollback restored an fsynced canary to
its exact pre-snapshot digest, restarted the guest, and materialized a fresh
independent linear HEAD in approximately 82 seconds. Deleting the earlier
independent snapshot while the guest was active removed only its signed
generation and left the recovered HEAD healthy. Finally, an active-QEMU grow
from 6 GiB to 7 GiB completed while the guest was writing. The guest observed
the new block-device size immediately, its existing canary digest remained
unchanged, and a full read of the newly published 1 GiB tail matched the digest
of an equally sized all-zero stream.

```ini
FOREGROUND_REGION_SIZE_4_KIB=PASS
DEFAULT_1_1_PERFORMANCE=FAIL
AGGRESSIVE_256_256_DEFAULT=REJECTED
CRASH_RECOVERY_DURING_HYDRATION=PASS
CONSERVATIVE_32_32_ONLINE_SNAPSHOT=PASS
ONLINE_SNAPSHOT_ELAPSED_APPROX=80_SECONDS
CONSERVATIVE_32_32_ROLLBACK=PASS
ROLLBACK_CANARY_DIGEST=PASS
ONLINE_SNAPSHOT_DELETE_AFTER_TUNING=PASS
ACTIVE_QEMU_GROW_WITH_GUEST_WRITE=PASS
ACTIVE_QEMU_GROW_ZERO_TAIL=PASS
FINAL_FRONTEND_LINEAR=PASS
POST_TEST_DSTATE=0
```

The first bounded steady-state soak completed 300 consecutive 256 MiB
write/sync/byte-compare cycles. Source and final destination SHA-256 digests
were identical. A deliberately coarse global-state sampler observed seven
short D-state samples but had not recorded their process identity, so that
observation was not misclassified as either a storage PASS or FAIL.

A follow-up 60-cycle run used a corrected collector that captured PID, task,
wait channel, command line, and the exact frontend table whenever any D-state
task appeared. It identified only unrelated host ZFS transaction-group and
RCU waits; QEMU, device-mapper, multipath, LVM, and the test process were not
blocked. The scoped recovery check remained healthy and the frontend remained
linear throughout. This confirms why the production health gate must remain
dependency-scoped instead of rejecting a host merely because its global
D-state count is temporarily non-zero.

```ini
STEADY_STATE_300_WRITE_SYNC_COMPARE_CYCLES=PASS
STEADY_STATE_FINAL_DIGEST=PASS
SCOPED_DSTATE_COLLECTOR=PASS
RELEVANT_STORAGE_DSTATE=0
UNRELATED_HOST_DSTATE_DOES_NOT_POISON_STORAGE=PASS
```

## Nested-lab COW isolation and 60 MiB/s online move

The direction-dependent cluster stalls seen during earlier online storage
moves were traced below the plugin. The nested storage target and the PVE
guests were all backed by VMware snapshot redo layers on one physical SATA
datastore. The two active target LUNs alone had accumulated approximately
65 GiB of copy-on-write redo data. This made the test a compound benchmark of
guest block storage, target-side snapshot COW, and host-side snapshot COW.

The target VM snapshot was consolidated while every storage consumer was
offline. The operation completed without error, preserved all virtual-disk
capacities and target mappings, and removed every target-side redo file. The
cluster then returned with the same multipath identities, two usable paths per
map, the same PV and VG identities, three-node quorum, both storage modes
active, and no D-state task. Obsolete pre-update snapshots on the two current
API nodes were also consolidated; the deliberately retained older-API
qualification snapshot was not modified.

After consolidation, the previously failing online 32 GiB thin-to-thick move
was repeated with a 60 MiB/s QEMU mirror limit. The operation first performed
the mandatory full-volume zero initialization of the new thick generation,
then copied 32.4 GiB of live disk data in 9 minutes 41 seconds. It completed
with a zero exit status, published the Thick Generations destination, and
deleted the thin source only after the mirror finalized successfully.

The resulting frontend was a single linear target with exactly one dependency
on the new generation. The anchor identified that generation as the
materialized authoritative HEAD, the old thin LV was absent, the Windows VM
remained runnable, both multipath maps retained two usable paths, quorum
remained three of three, and the final D-state count was zero. One peer had an
approximately one-second Corosync link flap near the end of the copy, but
there was no token timeout, membership change, loss of quorum, or failed I/O.
The remaining brief flap is tracked as a nested-host scheduling/network
qualification signal; it does not invalidate the completed storage
transaction.

The full-volume initialization is intentionally retained. LVM's normal
`--zero` behavior clears only the beginning of a conventional linear LV and
does not guarantee that every previously allocated extent reads as zero. The
storage allocation API also does not provide a trustworthy distinction
between a new blank guest disk and a destination that will subsequently be
fully overwritten. Skipping initialization based on inferred caller intent
would therefore risk exposing residual VG data.

```ini
TARGET_VM_SNAPSHOT_CONSOLIDATION=PASS
TARGET_VM_REDO_FILES_AFTER_CONSOLIDATION=0
MULTIPATH_IDENTITY_AFTER_CONSOLIDATION=PASS
PV_VG_IDENTITY_AFTER_CONSOLIDATION=PASS
CURRENT_API_NODE_SNAPSHOT_CONSOLIDATION=PASS
OLDER_API_QUALIFICATION_SNAPSHOT=PRESERVED
ONLINE_THIN_TO_THICK_60_MIB_PER_SECOND=PASS
ONLINE_MOVE_QEMU_COPY=32.4_GIB
ONLINE_MOVE_QEMU_ELAPSED=9_MIN_41_SEC
SOURCE_DELETE_ONLY_AFTER_MIRROR_COMMIT=PASS
FINAL_FRONTEND_LINEAR=PASS
FINAL_DEPENDENCY_DESTINATION_ONLY=PASS
AUTHORITATIVE_HEAD_MATERIALIZED=PASS
POST_MOVE_PATHS_PER_MAP=2_OF_2
POST_MOVE_QUORUM=3_OF_3
POST_MOVE_DSTATE=0
COROSYNC_TOKEN_TIMEOUT=0
COROSYNC_MEMBERSHIP_CHANGE=0
BRIEF_PEER_LINK_FLAP=OBSERVED_NO_SERVICE_IMPACT
FULL_THICK_ZERO_INITIALIZATION=RETAINED_FOR_DATA_ISOLATION
```

The same running Windows VM then completed cross-node migrations in both
directions while its thick disk remained on shared storage. With the lab's
global 20 MiB/s RAM-migration limit, the first 4 GiB migration took 5 minutes
19 seconds because the running guest temporarily dirtied memory faster than
the limit. PVE increased its convergence downtime allowance, but the actual
measured downtime was 524 milliseconds and the migration completed normally.

The return migration used an operation-specific 100 MiB/s limit on the
dedicated internal migration network. It completed in 47 seconds at an
average of 100.4 MiB/s with 4 milliseconds of downtime. After each direction,
the exact Thick Generations frontend existed with open count one only on the
destination node and was absent on both non-owner nodes. Corosync remained
clean, quorum stayed three of three, and the D-state count remained zero.
The operation-specific limit did not change the cluster-wide conservative
default and is not a production recommendation.

```ini
WINDOWS_THICK_LIVE_MIGRATION_FORWARD=PASS
WINDOWS_THICK_LIVE_MIGRATION_REVERSE=PASS
SHARED_DISK_COPY_DURING_NODE_MIGRATION=NOT_REQUIRED
FRONTEND_DESTINATION_ONLY_AFTER_EACH_MOVE=PASS
STALE_FRONTEND_ON_NON_OWNER_NODES=0
MIGRATION_20_MIB_PER_SECOND=PASS_SLOW_CONVERGENCE
MIGRATION_20_MIB_PER_SECOND_ELAPSED=5_MIN_19_SEC
MIGRATION_20_MIB_PER_SECOND_DOWNTIME=524_MS
MIGRATION_100_MIB_PER_SECOND=PASS
MIGRATION_100_MIB_PER_SECOND_ELAPSED=47_SEC
MIGRATION_100_MIB_PER_SECOND_DOWNTIME=4_MS
POST_MIGRATION_QUORUM=3_OF_3
POST_MIGRATION_DSTATE=0
COROSYNC_ERRORS_DURING_NODE_MIGRATIONS=0
```

## Full-size Windows snapshot and rollback oracle

The shared test LUN was expanded in place twice to provide enough disposable
capacity for a full-size Windows qualification. Each expansion preserved the
same multipath, PV, and VG identities. No replacement PV or VG was created.

With the Windows guest running on a 32 GiB thick generation, an online
snapshot completed through the normal PVE snapshot API. The transition
retained generation zero as a read-only named snapshot, materialized a new
writable generation-one HEAD, removed the temporary clone metadata, and
pivoted the stable frontend back to a single linear dependency. The guest
remained running throughout the operation.

The guest was then stopped cleanly and complete SHA-256 oracles were recorded
for both independent generations. Their digests differed, proving that the
subsequent rollback could not pass merely by leaving the current HEAD in
place. Rollback through the normal PVE API created generation two from the
immutable generation-zero snapshot, completed persistent hydration, pivoted
to a destination-only linear frontend, removed generation one and the
temporary metadata LV, and committed generation two as the materialized HEAD.

A complete 32 GiB read of generation two produced the exact generation-zero
SHA-256 digest and not the former generation-one digest. The Windows guest
then started successfully from the restored linear HEAD. The final state had
one destination dependency, no relevant D-state task, two usable paths for
each test map, and three-node quorum.

The online snapshot used the prototype's 32/32 hydration settings. The
rollback deliberately used a per-storage 16/16 override. Both storage
transactions completed correctly, but each run coincided with brief Corosync
link stalls in this two-vCPU nested lab; the rollback included one token
timeout followed by an immediate three-member reconfiguration without loss of
quorum. Interface counters, sustained packet probes, kernel logs, and sampled
hypervisor CPU-ready values did not show a persistent NIC, guest-kernel, or
CPU-contention fault. VMXNET3 therefore remains functional but does not by
itself eliminate the sporadic nested-host stall. Neither 32/32 nor 16/16 is
promoted to a generally qualified production tuning value by this result.

```ini
IN_PLACE_TEST_LUN_EXPANSION=PASS_IDENTITY_PRESERVED
WINDOWS_32_GIB_ONLINE_SNAPSHOT=PASS
SNAPSHOT_GENERATION_IMMUTABLE=PASS
PRE_ROLLBACK_GENERATION_DIGESTS_DIFFER=PASS
WINDOWS_32_GIB_ROLLBACK=PASS
ROLLBACK_FULL_DEVICE_DIGEST=PASS_EXACT_SOURCE_MATCH
SUPERSEDED_HEAD_REMOVED=PASS
TEMPORARY_CLONE_METADATA_REMOVED=PASS
FINAL_FRONTEND_LINEAR=PASS
FINAL_DEPENDENCY_DESTINATION_ONLY=PASS
WINDOWS_START_AFTER_ROLLBACK=PASS
POST_ROLLBACK_PATHS_PER_MAP=2_OF_2
POST_ROLLBACK_QUORUM=3_OF_3
POST_ROLLBACK_DSTATE=0
HYDRATION_32_32_STORAGE_CORRECTNESS=PASS
HYDRATION_16_16_STORAGE_CORRECTNESS=PASS
HYDRATION_TUNING_PRODUCTION_QUALIFICATION=OPEN
NESTED_LAB_COROSYNC_STALL=OBSERVED
```

## Conservative 8/8 hydration cycle

A separate 7 GiB Linux canary was moved offline to a current-API node and the
storage was configured with an eight-region hydration threshold and an
eight-region contiguous-copy batch. The stopped-guest snapshot completed in
3 minutes 6 seconds. At full hydration the clone reported 62 of 5120 metadata
blocks in use, approximately 1.2 percent of the allocated metadata LV. The
temporary metadata LV was then removed and the frontend pivoted to a single
linear dependency on generation five.

Complete device reads proved that immutable generation four and the new HEAD
were initially bit-identical. The Linux guest was started, wrote and flushed a
new filesystem canary, and shut down cleanly. A second complete read proved
that generation five now differed from generation four, establishing a real
rollback oracle.

Rollback materialized generation six from generation four in 4 minutes
6 seconds. Its complete-device SHA-256 exactly matched generation four and
differed from the discarded generation-five HEAD. The guest started from the
restored generation, the post-snapshot filesystem canary was absent, and an
online snapshot delete removed only generation four. The final frontend
remained a one-dependency linear target, the guest remained reachable, quorum
was three of three, and no relevant D-state task remained.

Neither the snapshot nor rollback interval contained a Corosync event. One
brief peer-link event occurred before rollback began and is therefore not
attributed to hydration. This clean bounded cycle makes 8/8 the current
conservative lab candidate, but one 7 GiB cycle is insufficient evidence for
a universal production default. Full-size, foreground-I/O, and repeated soak
qualification remain required.

```ini
HYDRATION_8_8_SNAPSHOT=PASS
HYDRATION_8_8_SNAPSHOT_ELAPSED=3_MIN_6_SEC
HYDRATION_8_8_METADATA_BLOCKS_USED=62_OF_5120
HYDRATION_8_8_PRE_WRITE_FULL_DIGEST_MATCH=PASS
POST_SNAPSHOT_GUEST_WRITE_AND_FLUSH=PASS
POST_WRITE_HEAD_DIGEST_DIFFERS=PASS
HYDRATION_8_8_ROLLBACK=PASS
HYDRATION_8_8_ROLLBACK_ELAPSED=4_MIN_6_SEC
ROLLBACK_FULL_DEVICE_DIGEST=PASS_EXACT_SOURCE_MATCH
ROLLBACK_FILESYSTEM_CANARY=PASS
ONLINE_SNAPSHOT_DELETE_AFTER_ROLLBACK=PASS
FINAL_FRONTEND_LINEAR=PASS
FINAL_DEPENDENCY_DESTINATION_ONLY=PASS
POST_CYCLE_QUORUM=3_OF_3
POST_CYCLE_DSTATE=0
COROSYNC_EVENTS_DURING_SNAPSHOT=0
COROSYNC_EVENTS_DURING_ROLLBACK=0
HYDRATION_8_8_PRODUCTION_QUALIFICATION=OPEN
```

## Asynchronous online materialization

The first running-guest integration exposed an important PVE lifecycle detail:
PVE keeps a running QEMU disk paused in `save-vm` until the storage snapshot
callback returns. Waiting for complete dm-clone hydration inside that callback
therefore converted a background copy into several minutes of guest downtime.

The prototype now publishes the committed clone transition first and schedules
a transaction-scoped, bounded systemd worker. The callback returns only after
the persistent anchor, immutable snapshot generation, writable HEAD, exact
clone table, intent handoff, and worker identity have all been positively
verified. The VG-wide intent protects preparation and publication; it is then
replaced under the same lock by the signed non-MATERIALIZED anchor. That anchor
blocks dependency-changing mutations of the same volume while allowing an
independent disk in the VG to start its own transaction. If worker scheduling
fails, the callback completes materialization synchronously instead of
acknowledging an unsupervised transition.

A live 7 GiB Linux qualification used the conservative 8/8 hydration profile
while a foreground workload repeatedly replaced and fdatasync'ed a 128 MiB
file. The snapshot callback returned successfully in 8.731 seconds and PVE
immediately reported the VM as running. The transaction-specific worker then
continued hydration while the guest completed 178 foreground write/flush
cycles. A second snapshot request during hydration failed closed in 4.406
seconds: the LV count was unchanged and no PVE snapshot entry was created.

After foreground I/O was stopped, hydration completed and the worker exited
successfully. The stable frontend was a linear target with exactly one
dependency on generation nine; the source helper mapping, metadata LV, and VG
intent were absent. A stopped-guest rollback materialized generation ten from
the immutable snapshot. After restart, a stable 64 MiB guest canary retained
its exact SHA-256. Snapshot deletion removed only the snapshot generation.
The final state contained one destination-only linear HEAD, no relevant
D-state task, active storage, and three-of-three cluster quorum.

The active-worker host-loss gate then powered off the complete source PVE node
while the clone was 427523 of 1835008 regions hydrated, approximately 23.3
percent, and the guest had completed 35 additional write/flush cycles. The two
surviving nodes remained quorate. On a different node, the persistent anchor
still identified the exact HYDRATING phase, transaction UUID, immutable source,
destination, and metadata LV, while no transient mapper existed.

An attempted VM start before recovery failed closed with an explicit missing
materialization-frontend error and created no D-state task. The first recovery
implementation also failed closed because it incorrectly required the
source-host runtime helper mapping. This exposed a real cross-node recovery
defect without changing persistent data.

The corrected `sharedlvmthin thick-resume <storage-id> <volume>` command derives
the request exclusively from the verified anchor and requires either its exact
VG intent or the exact anchor-scoped handoff. When every transient mapper is
absent, it activates only the signed source, destination, and metadata objects,
reconstructs the exact read-only source and persistent clone tables, verifies
their UUIDs, tables, dependencies, and clone status, and resumes hydration. A
partial runtime still fails closed rather than being overwritten.

The real cross-node resume continued from the persisted dm-clone progress,
completed in 134 seconds, pivoted to a destination-only linear frontend, and
removed the metadata LV and VG intent. The guest then started on the surviving
node, systemd reported a running system, the stable 64 MiB canary retained its
exact SHA-256, and the interrupted 128 MiB workload file remained present.
Snapshot deletion removed only the immutable source generation. The original
node subsequently rejoined, restoring three-of-three quorum and both 2-of-2
multipath maps.

This proves the normal asynchronous lifecycle, fail-closed concurrent mutation
gate, and explicit cross-node recovery after complete source-host loss.
Concurrent multi-disk and mixed thin/thick transactions are qualified below.
Native HA orchestration of the same failure class is qualified separately
below.

```ini
ONLINE_SNAPSHOT_CALLBACK=PASS
ONLINE_SNAPSHOT_CALLBACK_ELAPSED_MS=8731
PVE_VM_RUNNING_AFTER_CALLBACK=PASS
TRANSACTION_SCOPED_WORKER=PASS
FOREGROUND_WRITE_FLUSH_CYCLES_DURING_HYDRATION=178
STABLE_GUEST_CANARY_DURING_HYDRATION=PASS
CONCURRENT_SNAPSHOT_FAIL_CLOSED=PASS
CONCURRENT_SNAPSHOT_ELAPSED_MS=4406
CONCURRENT_SNAPSHOT_LV_DELTA=0
WORKER_RESULT=SUCCESS
FINAL_FRONTEND_LINEAR=PASS
FINAL_DEPENDENCY_DESTINATION_ONLY=PASS
SOURCE_HELPER_REMOVED=PASS
METADATA_LV_REMOVED=PASS
VG_INTENT_CLEARED=PASS
ROLLBACK_AFTER_ASYNC_SNAPSHOT=PASS
POST_ROLLBACK_GUEST_CANARY=PASS
SNAPSHOT_DELETE_EXACT=PASS
POST_CYCLE_QUORUM=3_OF_3
POST_CYCLE_DSTATE=0
ASYNC_HOST_LOSS_AT_HYDRATED_REGIONS=427523_OF_1835008
SURVIVOR_QUORUM=2_OF_2
PRE_RECOVERY_VM_START_FAIL_CLOSED=PASS
PRE_RECOVERY_DSTATE=0
PARTIAL_RUNTIME_RECONSTRUCTION=REFUSED
EXACT_CROSS_NODE_RUNTIME_RECONSTRUCTION=PASS
RESUME_CONTINUED_PERSISTED_PROGRESS=PASS
EXPLICIT_CROSS_NODE_RESUME_ELAPSED_S=134
POST_HOST_LOSS_FRONTEND_LINEAR=PASS
POST_HOST_LOSS_DEPENDENCY_DESTINATION_ONLY=PASS
POST_HOST_LOSS_GUEST_BOOT=PASS
POST_HOST_LOSS_GUEST_SYSTEM_STATE=RUNNING
POST_HOST_LOSS_STABLE_CANARY=PASS
POST_HOST_LOSS_SNAPSHOT_DELETE_EXACT=PASS
SOURCE_NODE_REJOIN=PASS
FINAL_QUORUM=3_OF_3
FINAL_PATHS_PER_MAP=2_OF_2
ASYNC_HOST_LOSS_RECOVERY=PASS
ASYNC_MULTI_DISK_QUALIFICATION=PASS_SEE_CONCURRENT_GATE_BELOW
```

## Concurrent multi-disk materialization

The first two-disk online snapshot exposed a safety-versus-composability defect:
disk zero correctly published its asynchronous transition, but its original
VG-wide intent caused disk one's callback to fail closed. PVE created no
snapshot entry. Disk zero completed materialization safely and its otherwise
orphaned immutable generation was removed through the exact storage snapshot
delete primitive. No data or runtime artifact was lost.

The corrected handoff retains the VG-wide intent through preparation, atomic
clone publication, and positive verification. Immediately before scheduling
the asynchronous worker, it converts that intent to the signed non-MATERIALIZED
anchor state and removes the global tag under the same cluster/VG lock. A
mutation of that volume remains blocked by its anchor, while a different volume
in the same VG can open its own transaction. Workers accept an unrelated
short-lived VG intent only when their own anchor still proves the exact
transaction. A partially present runtime remains non-reconstructable.

A running Linux VM with independent 7 GiB and 1 GiB Thick Generations disks
then completed a native PVE two-disk snapshot in 10.488 seconds. Both callbacks
created distinct transaction UUIDs, both workers ran concurrently, the VG had
no unresolved global intent after handoff, and PVE recorded one coherent VM
snapshot. During hydration the guest completed 38 paired write-and-fdatasync
cycles across two ext4 filesystems and both pre-existing SHA-256 canaries
remained exact.

The 1 GiB disk materialized first and the 7 GiB disk followed; both workers
exited successfully and both frontends became one-dependency linear targets.
A stopped-guest PVE rollback then materialized both disks in 224 seconds.
After restart, independently synchronized post-snapshot markers were absent
from both filesystems, both stable canary hashes matched, and systemd reported
a running system. Native PVE snapshot deletion removed exactly both immutable
source generations. The VM remained running with no relevant D-state task and
three-of-three quorum.

```ini
INITIAL_MULTI_DISK_ATTEMPT=FAIL_CLOSED_VG_INTENT_SCOPE
INITIAL_MULTI_DISK_PVE_SNAPSHOT_ENTRY=ABSENT
INITIAL_PARTIAL_DISK_CLEANUP=PASS_EXACT
ANCHOR_SCOPED_ASYNC_INTENT=PASS
MULTI_DISK_ONLINE_SNAPSHOT=PASS
MULTI_DISK_CALLBACK_ELAPSED_MS=10488
MULTI_DISK_TRANSACTION_UUIDS=DISTINCT
CONCURRENT_MATERIALIZATION_WORKERS=2
VG_INTENT_AFTER_HANDOFF=NONE
PAIRED_GUEST_WRITE_FLUSH_CYCLES=38
PRIMARY_CANARY_DURING_HYDRATION=PASS
SECONDARY_CANARY_DURING_HYDRATION=PASS
PRIMARY_FINAL_FRONTEND_LINEAR=PASS
SECONDARY_FINAL_FRONTEND_LINEAR=PASS
MULTI_DISK_ROLLBACK=PASS
MULTI_DISK_ROLLBACK_ELAPSED_S=224
PRIMARY_POST_SNAPSHOT_MARKER_ABSENT=PASS
SECONDARY_POST_SNAPSHOT_MARKER_ABSENT=PASS
PRIMARY_POST_ROLLBACK_CANARY=PASS
SECONDARY_POST_ROLLBACK_CANARY=PASS
MULTI_DISK_SNAPSHOT_DELETE_EXACT=PASS
POST_MULTI_DISK_VM_RUNNING=PASS
POST_MULTI_DISK_DSTATE=0
POST_MULTI_DISK_QUORUM=3_OF_3
ASYNC_MULTI_DISK_QUALIFICATION=PASS
```

## Mixed thin and Thick Generations transaction

A running Linux guest was qualified with two independent Thick Generations
disks and one conventional per-VM thin volume. One native PVE snapshot created
two concurrent asynchronous clone transitions and one LVM-thin snapshot. Both
materialization workers completed successfully, both thick frontends returned
to destination-only linear tables, and the thin pool remained healthy. Stable
SHA-256 canaries on all three filesystems remained exact during the online
transition.

After materialization, synchronized marker files were written to every
filesystem and the guest was stopped. One native PVE rollback restored all
three disks. After restart, all post-snapshot markers were absent and all three
pre-snapshot canaries matched. Filesystems were identified by persistent label
and UUID because Linux block-device enumeration order changed after restart;
the test did not rely on `/dev/sdX` identity. Native PVE snapshot deletion then
removed both exact immutable thick sources and the exact thin snapshot. The
guest remained running, the cluster remained quorate, and the relevant D-state
count was zero.

```ini
MIXED_THIN_THICK_ONLINE_SNAPSHOT=PASS
MIXED_THICK_MATERIALIZATION_WORKERS=2
MIXED_PRIMARY_THICK_CANARY=PASS
MIXED_SECONDARY_THICK_CANARY=PASS
MIXED_THIN_CANARY=PASS
MIXED_THIN_POOL_HEALTH=PASS
MIXED_ROLLBACK=PASS
MIXED_POST_SNAPSHOT_MARKERS_ABSENT=3_OF_3
MIXED_POST_ROLLBACK_CANARIES=3_OF_3
MIXED_DEVICE_IDENTITY=LABEL_AND_UUID
MIXED_SNAPSHOT_DELETE_EXACT=PASS
POST_MIXED_VM_RUNNING=PASS
POST_MIXED_DSTATE=0
POST_MIXED_QUORUM=3_OF_3
MIXED_THIN_THICK_QUALIFICATION=PASS
```

The same running mixed-mode guest then completed a live migration from a PVE
Storage API 15 node to an API 14 node and back. Migration used the dedicated
cluster migration network and moved VM state only; all shared thin and thick
volumes remained on their pinned multipath devices. A bounded guest workload
completed writes and `fdatasync` operations on all three filesystems across the
round trip. The guest remained running, every stable canary retained its exact
SHA-256, both storages remained active, both multipath maps remained healthy,
and the relevant D-state count was zero.

```ini
MIXED_LIVE_MIGRATION_API15_TO_API14=PASS
MIXED_LIVE_MIGRATION_API14_TO_API15=PASS
MIXED_MIGRATION_GUEST_WRITE_FLUSH=PASS
MIXED_MIGRATION_CANARIES=3_OF_3
MIXED_MIGRATION_STORAGE_COPY=NONE_SHARED
POST_MIXED_MIGRATION_VM_RUNNING=PASS
POST_MIXED_MIGRATION_STORAGES_ACTIVE=PASS
POST_MIXED_MIGRATION_DSTATE=0
POST_MIXED_MIGRATION_QUORUM=3_OF_3
```

While the mixed-mode guest remained online, one Thick Generations data disk
and one conventional thin data disk were each extended by 256 MiB through the
native PVE resize operation. The thick LV and its stable frontend both exposed
the new exact sector count while retaining a single destination-only linear
dependency. The thin volume exposed its larger virtual size without changing
the per-VM pool boundary. Both ext4 filesystems expanded online by persistent
filesystem label, retained their pre-resize canaries, and completed new
write-and-fdatasync probes in the added address range. Both storages remained
active and the relevant D-state count was zero.

```ini
MIXED_ONLINE_THICK_RESIZE=PASS
MIXED_ONLINE_THIN_RESIZE=PASS
THICK_POST_RESIZE_LINEAR=PASS
THICK_POST_RESIZE_DEPENDENCIES=DESTINATION_ONLY
MIXED_ONLINE_FILESYSTEM_GROW=2_OF_2
MIXED_PRE_RESIZE_CANARIES=2_OF_2
MIXED_POST_RESIZE_WRITE_FLUSH=2_OF_2
POST_MIXED_RESIZE_STORAGES_ACTIVE=PASS
POST_MIXED_RESIZE_DSTATE=0
```

The resized conventional thin data disk then completed an online PVE storage
move to Thick Generations and an online move back to the thin storage. Each
direction used QEMU drive mirror with a bounded bandwidth limit and deleted the
source only after the mirror job reported successful completion. The first
move produced an ordinary destination-only linear generation; the return move
removed its exact generation and anchor after creating a new owned per-VM thin
pool. Filesystem label, filesystem UUID, every pre-move canary, and a new
post-round-trip write-and-fdatasync probe all matched.

The return full-copy also demonstrated an important allocation property: raw
PVE drive mirror copies the complete virtual address space, including zeroed
ranges. A sparse guest filesystem can therefore become a fully allocated thin
LV during Thick Generations-to-thin conversion. Elastic pool growth kept the
operation healthy, but conversion planning must reserve for this worst case;
the plugin must not claim that sparseness is preserved by a raw storage move.

```ini
ONLINE_STORAGE_MOVE_THIN_TO_THICK=PASS
ONLINE_STORAGE_MOVE_THICK_TO_THIN=PASS
MOVE_SOURCE_DELETE_AFTER_MIRROR_SUCCESS=PASS
MOVE_FILESYSTEM_UUID_STABLE=PASS
MOVE_CANARIES_STABLE=PASS
MOVE_POST_ROUNDTRIP_WRITE_FLUSH=PASS
THICK_TO_THIN_SPARSE_PRESERVATION=NOT_GUARANTEED
THICK_TO_THIN_WORST_CASE_RESERVATION=FULL_VIRTUAL_SIZE
THIN_POOL_ELASTIC_GROW_DURING_IMPORT=PASS
POST_MOVE_DOCTOR_FAILS=0
POST_MOVE_DSTATE=0
POST_MOVE_VM_RUNNING=PASS
```

A native snapshot-mode `vzdump` backup of the running mixed-mode guest then
completed successfully. The archive represented 9.5 GiB of virtual disk data,
reported 28 percent zero data, and compressed to 6.13 GiB. It was restored to
a new disposable guest with all three disks targeted at Thick Generations.
Restore allocated three independent anchor/generation pairs, preserved sparse
archive ranges, and completed without a partial allocation.

The restored guest booted after applying its original lab network identity
while the source guest was stopped to prevent an address conflict. Both data
filesystems retained their exact labels and UUIDs, and all five selected
pre-backup canaries matched. The disposable guest was then shut down and
destroyed through native PVE lifecycle operations. All three exact generations
and all three exact anchors were removed, no matching artifact remained, and
the original guest restarted with its three primary canaries intact.

```ini
MIXED_RUNNING_VZDUMP_SNAPSHOT_MODE=PASS
MIXED_VZDUMP_SPARSE_ARCHIVE=PASS
MIXED_RESTORE_TO_THICK_GENERATIONS=PASS
RESTORED_THICK_DISKS=3
RESTORED_FILESYSTEM_IDENTITIES=PASS
RESTORED_DATA_CANARIES=5_OF_5
RESTORED_GUEST_BOOT=PASS
RESTORE_DISPOSABLE_DELETE_EXACT=PASS
RESTORE_ARTIFACTS_REMAINING=0
ORIGINAL_GUEST_RESTART=PASS
ORIGINAL_POST_RESTORE_CANARIES=3_OF_3
POST_RESTORE_DSTATE=0
```

The restore path was subsequently interrupted by a forced worker-node reboot
after the first Thick Generations destination had been allocated but before
the archive stream published a usable guest. Persistent state contained one
OPEN `ALLOC` intent, its canonical PREPARED anchor, and exactly one signed
generation-zero HEAD. The read-only recovery gate refused all mutation after
reboot and did not adopt or delete the partial volume automatically.

The explicit `thick-recover-partial-alloc` operation required the exact
volume, intent, anchor, HEAD, storage identity, quorum, inactive frontend,
disabled autoactivation, and absence of every cluster-wide PVE reference. It
removed only that pair, proved absence, and cleared the intent last. A complete
retry of the archive then restored all three disks, reached a serial login
prompt, and left three materialized linear HEADs. Read-only filesystem checks
completed without structural errors. Both root atomic slots, both
conventional-thin atomic slots, and the last durable second-Thick slot matched
their committed SHA-256 values. The alternate second-Thick slot captured while
a guest write was outstanding did not match its older sidecar; live backup
does not guarantee an application-consistent view of an unquiesced write.
Native PVE deletion then removed all six exact destination objects, the anchor
count returned to its baseline, the recovery gate was healthy, and the
original guest restarted.

```ini
HOST_LOSS_DURING_RESTORE=PASS_BOUNDED_LAB
POST_REBOOT_PARTIAL_STATE=PREPARED_EXACT_PAIR
POST_REBOOT_MUTATION_GATE=BLOCKED
AUTOMATIC_PARTIAL_ADOPTION=NO
AUTOMATIC_PARTIAL_CLEANUP=NO
EXPLICIT_PARTIAL_ALLOCATION_RECOVERY=PASS
RESTORE_RETRY=PASS
RESTORED_GUEST_SERIAL_BOOT=PASS
RESTORED_FILESYSTEM_STRUCTURE=PASS
RESTORED_ROOT_ATOMIC_SLOTS=2_OF_2
RESTORED_THIN_ATOMIC_SLOTS=2_OF_2
RESTORED_SECOND_THICK_LAST_DURABLE_SLOT=PASS
OUTSTANDING_GUEST_WRITE_AT_BACKUP=NOT_GUARANTEED
RESTORE_RETRY_DELETE_EXACT=PASS
POST_DELETE_ANCHOR_COUNT=BASELINE
POST_DELETE_RECOVERY_GATE=HEALTHY
ORIGINAL_GUEST_RESTART=PASS
```

A separate disposable 8 GiB Thick Generations disk qualified host loss during
rollback. Generation zero was materialized as snapshot `rollback-base` with a
deterministic 64 MiB pattern. Its successor HEAD was overwritten with a
different pattern before rollback. The worker node was forcibly rebooted while
the persisted anchor reported `ROLLBACK/HYDRATING` and dm-clone had hydrated
only part of the destination.

After reboot, quorum and storage identity were healthy, transient mappings and
the worker were absent, and the recovery gate returned `RECOVERY_REQUIRED`.
The first explicit resume attempt exposed and then drove a correction to the
worker dispatcher: resume had accepted only `SNAPSHOT`, although the core
state machine already supported `ROLLBACK`. The corrected dispatcher accepts
`ROLLBACK` only when it is derived from the signed anchor, expects the matching
`DM_PIVOT` intent, and does not allow a caller-supplied rollback operation.

The same persisted transaction resumed from its dm-clone metadata, completed
hydration, pivoted to a destination-only linear HEAD, removed the superseded
HEAD and transition metadata, and returned the recovery gate to healthy. The
new HEAD's 64 MiB SHA-256 matched the immutable snapshot pattern and differed
from the discarded successor pattern. A stale native PVE rollback lock was
cleared only after those proofs. Snapshot deletion and VM deletion then removed
the exact remaining snapshot, HEAD, and anchor, returning the anchor inventory
to its baseline.

```ini
HOST_LOSS_DURING_ROLLBACK=PASS_BOUNDED_LAB
FAULT_PHASE=ROLLBACK_HYDRATING
POST_REBOOT_TRANSIENT_RUNTIME=ABSENT
POST_REBOOT_RECOVERY_GATE=RECOVERY_REQUIRED
RESUME_OPERATION_SOURCE=SIGNED_ANCHOR_ONLY
RESUME_TRANSACTION_REUSED=YES
ROLLBACK_DM_CLONE_PROGRESS_RESUMED=PASS
ROLLBACK_LINEAR_PIVOT=PASS
SUPERSEDED_HEAD_REMOVED=PASS
ROLLBACK_HEAD_SHA_MATCHES_SNAPSHOT=PASS
ROLLBACK_HEAD_SHA_MATCHES_DISCARDED_SUCCESSOR=NO
STALE_PVE_LOCK_CLEARED_AFTER_DATA_PROOF=YES
ROLLBACK_DISPOSABLE_CLEANUP=PASS
POST_CLEANUP_ANCHOR_COUNT=BASELINE
POST_CLEANUP_RECOVERY_GATE=HEALTHY
```

A second disposable transition qualified loss of the worker host during an
explicit recovery itself. A 4 GiB disk with a deterministic 64 MiB pattern
entered `SNAPSHOT/HYDRATING` and the worker host was forcibly rebooted after
206144 of 1048576 regions. The first `thick-resume` reconstructed the exact
transaction and advanced persistent hydration to 449424 regions before the
host was forcibly rebooted again.

After both reboots, transient mappings were absent, the signed anchor retained
the same transaction ID and generation topology, and the read-only recovery
gate remained `RECOVERY_REQUIRED`. A second explicit resume reconstructed the
same transaction again, completed hydration and the linear pivot, and removed
only the exact metadata LV. The destination-only linear frontend matched the
original 64 MiB SHA-256. Native snapshot and VM cleanup then removed the
snapshot, HEAD, and anchor and returned the health gate and inventory to their
baseline.

```ini
HOST_LOSS_DURING_EXPLICIT_RECOVERY=PASS_BOUNDED_LAB
WORKER_HOST_LOSSES_IN_ONE_TRANSACTION=2
TRANSACTION_ID_STABLE_ACROSS_REBOOTS=PASS
FIRST_FAULT_PROGRESS=206144_OF_1048576
SECOND_FAULT_PROGRESS=449424_OF_1048576
POST_FAULT_TRANSIENT_RUNTIME=ABSENT
POST_FAULT_RECOVERY_GATE=RECOVERY_REQUIRED
METADATA_REINITIALIZED=NO
DUPLICATE_FRONTEND_CREATED=NO
FINAL_FRONTEND_TARGET=LINEAR
FINAL_DATA_SHA=PASS
TRANSITION_METADATA_REMOVED=PASS
DISPOSABLE_CLEANUP=PASS
POST_CLEANUP_ANCHOR_COUNT=BASELINE
POST_CLEANUP_RECOVERY_GATE=HEALTHY
```

The mixed-mode guest was then enrolled as a native PVE HA resource while the
cluster watchdog was armed. HA performed a controlled online relocation from
an API 15 node to an API 14 node and back. The first relocation intentionally
overlapped the bounded guest write soak. PVE correctly reported that the guest
dirty-memory rate exceeded the configured 20 MiB/s migration limit and raised
its convergence downtime in bounded steps. Stopping only the workload allowed
the same live migration task to converge; no retry or storage recovery action
was required. The return relocation completed normally.

The guest remained running after both HA relocations and all three canaries
matched. The HA resource was then removed cleanly, leaving no managed-resource
entry. This also confirms the plugin does not contain or infer a watchdog or
Corosync timeout: fencing, retry, and migration timing remain native PVE policy
and therefore follow site-specific cluster configuration.

```ini
PVE_HA_RESOURCE_ENROLLMENT=PASS
PVE_HA_WATCHDOG_ARMED=PASS
HA_RELOCATION_API15_TO_API14=PASS
HA_RELOCATION_API14_TO_API15=PASS
DIRTY_RATE_ABOVE_MIGRATION_LIMIT=DETECTED_BY_PVE
MIGRATION_CONVERGENCE_AFTER_WORKLOAD_STOP=PASS
PLUGIN_WATCHDOG_TIMEOUT_ASSUMPTION=NONE
HA_RELOCATION_CANARIES=3_OF_3
HA_RESOURCE_REMOVAL=PASS
POST_HA_VM_RUNNING=PASS
POST_HA_DSTATE=0
POST_HA_QUORUM=3_OF_3
```

## Native HA worker-host loss during materialization

The mixed-mode Linux guest was enrolled as a native PVE HA resource and a
bounded write-and-flush workload was started on its primary thick disk,
secondary thick disk, and conventional thin disk. One native online snapshot
then created two independent asynchronous Thick Generations transactions and
one conventional thin snapshot. Both thick anchors were positively verified in
the `HYDRATING` phase with distinct transaction identities and live
transaction-scoped workers before fault injection.

The complete worker node was then powered off without a guest shutdown. The two
surviving cluster nodes retained quorum and an unrelated Windows guest remained
running. Native PVE HA observed the node loss, waited for fencing ownership, and
attempted recovery on surviving nodes. Each premature guest start failed closed
because the signed non-materialized frontend was absent. No duplicate QEMU
process was created, no storage object was deleted or repaired, and no identity
was guessed.

After fencing was acknowledged, both exact materialization transactions were
reconstructed on a surviving node exclusively from their persistent anchors.
They resumed independently, completed hydration, pivoted to destination-only
linear frontends, and removed their exact transition artifacts. Only after both
frontends were positively verified as materialized was the HA resource allowed
to start. The guest booted with all three persistent filesystem identities and
all three pre-fault data canaries intact.

The failed node later rejoined automatically. The cluster returned to
three-of-three membership, both storage modes were active on every node, and no
relevant D-state process or materialization worker remained. Native snapshot
deletion then removed the two exact immutable thick source generations and the
exact conventional thin snapshot. A final live migration returned the guest to
the intended node without copying shared storage and preserved all three data
canaries.

```ini
HA_ACTIVE_MATERIALIZATION_WORKERS=2
HA_MIXED_THIN_SNAPSHOT=PASS
HA_COMPLETE_WORKER_NODE_LOSS=PASS
HA_SURVIVOR_QUORUM=PASS
HA_UNRELATED_GUEST_CONTINUITY=PASS
HA_FENCING_ACKNOWLEDGED=PASS
HA_PREMATURE_START_FAIL_CLOSED=PASS
HA_DUPLICATE_QEMU=0
HA_SPECULATIVE_STORAGE_MUTATIONS=0
HA_CROSS_NODE_TRANSACTION_RECOVERY=2_OF_2
HA_FINAL_FRONTENDS_LINEAR=2_OF_2
HA_GUEST_RESTART_AFTER_RECOVERY=PASS
HA_POST_RECOVERY_CANARIES=3_OF_3
HA_FAILED_NODE_REJOIN=PASS
HA_POST_REJOIN_QUORUM=3_OF_3
HA_POST_REJOIN_STORAGES_ACTIVE=PASS
HA_SNAPSHOT_DELETE_EXACT=PASS
HA_FINAL_LIVE_MIGRATION=PASS
HA_FINAL_CANARIES=3_OF_3
HA_FINAL_DSTATE=0
HA_FINAL_MATERIALIZATION_WORKERS=0
```

A second mixed-mode online snapshot was created while a bounded guest soak
continuously overwrote and flushed fixed-size files on all three filesystems.
Both asynchronous thick workers and the conventional thin snapshot coexisted
for more than ten minutes. The thin pool's increasing CoW allocation was
observed explicitly and the workload was stopped before capacity risk could
replace the intended lifecycle test. Both workers then completed successfully,
both frontends returned to destination-only linear tables, every stable canary
matched, and native snapshot deletion removed the two exact thick sources and
the exact thin snapshot.

```ini
REPEATED_MIXED_ONLINE_SNAPSHOT=PASS
REPEATED_MIXED_SNAPSHOT_UNDER_WRITE_SOAK=PASS
REPEATED_MIXED_CONCURRENT_WORKERS=2
THIN_COW_CAPACITY_OBSERVED=PASS
SOAK_STOPPED_BEFORE_CAPACITY_RISK=PASS
REPEATED_MIXED_MATERIALIZATION=PASS
REPEATED_MIXED_CANARIES=3_OF_3
REPEATED_MIXED_SNAPSHOT_DELETE_EXACT=PASS
REPEATED_MIXED_DSTATE=0
```

## Isolated FCoE transport qualification

The disposable FCoE LUN was discovered through two independent VN2VN paths on
all three nodes. Its exact SCSI identity, capacity, multipath identity, and
read samples matched everywhere. A flushed one-MiB write was readable with the
same digest from every node, and ordinary one-MiB-block sequential I/O remained
bounded with no D-state tasks.

The lab target nevertheless exposed two transport-specific limitations that
are below the plugin. Single requests up to eight MiB completed normally, while
single 16-MiB and 64-MiB writes through the Linux `tcm_fc` target repeatedly
incurred approximately 30 seconds of latency. Both raw FC paths became fast
again when their advertised request limit was constrained to eight MiB, but a
large request submitted through device-mapper could still fan out into the
problematic target pattern. Normal VM-sized one-MiB requests did not reproduce
that latency.

An isolated active-path 2-to-1 event then failed over correctly and a bounded
one-MiB-block write completed at 179 MiB/s on the surviving path. The return to
2 paths did not recover correctly. FC ports reported Online while the SCSI
paths remained failed, and the target accumulated uninterruptible workers in
`target_wait_for_cmds`, `ft_prlo`, `fc_rport_logoff`,
`fcoe_ctlr_vn_timeout`, and `fc_rport_recv_flogi_req`. An explicit initiator
relogin could not recover the target; a target power cycle was required. The
unrelated iSCSI LUNs then returned with their original identities and two paths,
both PVE storages became active, all three mixed-mode guest canaries matched,
and D-state returned to zero.

This result qualifies ordinary isolated FCoE I/O but fails Linux VN2VN/tcm_fc
path-return recovery in the current lab kernel. It does not qualify a physical
FC array and does not change the Thick Generations or SharedLvmThin lifecycle
result.

```ini
FCOE_THREE_NODE_IDENTITY=PASS
FCOE_PATHS_PER_NODE=2
FCOE_CROSS_NODE_READ_DIGEST=PASS
FCOE_ORDINARY_IO=PASS
FCOE_SINGLE_REQUEST_1M=PASS
FCOE_SINGLE_REQUEST_8M=PASS
FCOE_SINGLE_REQUEST_16M=TARGET_LIMIT_REPRODUCED
FCOE_ACTIVE_PATH_2_TO_1=PASS
FCOE_SURVIVING_PATH_IO=PASS
FCOE_PATH_RETURN_1_TO_2=FAIL_TARGET_DSTATE
FCOE_INITIATOR_RELOGIN_RECOVERY=FAIL
FCOE_TARGET_REBOOT_REQUIRED=YES
ISCSI_IDENTITY_AFTER_TARGET_REBOOT=PASS
MIXED_GUEST_CANARIES_AFTER_TARGET_REBOOT=3_OF_3
PLUGIN_INVOLVEMENT=NONE
PHYSICAL_FC_ARRAY_QUALIFICATION=OPEN
```

## Open gates

1. Qualify physical FC path loss and active-guest application outcomes on a
   target that does not reproduce the Linux VN2VN/tcm_fc recovery deadlock.
2. Complete the cluster-topology matrix for a two-node cluster with QDevice and
   the explicitly warned two-node/no-QDevice mode.

The long Windows integrity, interrupted Windows materialization and final
cross-node mixed-mode health soaks are complete and recorded below.

## Perl taint-mode integration

A live PVE resize request exposed a missing untaint boundary while recording
the pre-mutation `OPEN EXTEND` VG intent. Perl rejected the command before
`vgchange`; the VG retained no intent and the thick volume retained its exact
original size. The command boundary was then centralized and hardened before
the operation was retried.

The corrected build completed the same live 32 GiB to 36 GiB grow. The new
range was zeroed before publication, the stable frontend was reloaded to
exactly 75,497,472 sectors, QEMU retained the same mapper path, the existing
32 GiB snapshot was unchanged, and the transaction intent was cleared. A
separate two-GiB native PVE snapshot on the API-14 node exercised allocation,
tag transitions, worker scheduling, hydration, linear pivot, and finalization
under the same taint-enforcing build.

```ini
TAINT_REJECTION_BEFORE_FIRST_MUTATION=PASS
TAINT_REGRESSION_EXECUTED_WITH_PERL_T=PASS
TAINT_ARGV_BOUNDARY=PASS
TAINT_OPTION_INJECTION_REJECTED=PASS
TAINT_CONTROL_INJECTION_REJECTED=PASS
THICK_LIVE_RESIZE_32_TO_36_GIB=PASS
THICK_RESIZE_FRONTEND_SECTORS=75497472
THICK_RESIZE_EXISTING_SNAPSHOT_UNCHANGED=PASS
THICK_RESIZE_INTENT_CLEARED=PASS
API14_SNAPSHOT_TAINT_INTEGRATION=PASS
```

The 90-minute mixed Linux workload completed 525 overwrite, flush, and hash
verification cycles across two Thick Generations disks and one conventional
thin disk. All three stable canaries retained their baseline hashes. The
cross-node monitor run is deliberately not counted as a clean qualification:
an administrator-requested live resize overlapped iteration 13, and its direct
zero-initialization process was correctly observed in storage-scoped D-state.
The workload result is valid; the non-interference health-monitor gate must be
repeated without concurrent administration.

```ini
LONG_MIXED_GUEST_RUNTIME=90_MINUTES
LONG_MIXED_GUEST_CYCLES=525
LONG_MIXED_GUEST_RESULT=PASS
LONG_MIXED_CANARIES=3_OF_3
LONG_CROSS_NODE_HEALTH_MONITOR=INVALIDATED_BY_CONCURRENT_RESIZE
LONG_CROSS_NODE_HEALTH_MONITOR_REPEAT=OPEN
```

The recovery checker subsequently encountered a short storage-scoped
`pvs --readonly` wait in the kernel AIO teardown path. A single instantaneous
D-state sample could not distinguish this normal transient wait from a stuck
storage transaction. Recovery qualification now requires a bounded persistence
confirmation: a detected task must disappear during the two-second interval
and the fresh scan must positively return `PASS`; otherwise the original
`FAIL` or `UNKNOWN` classification remains fail-closed. A live 30-iteration
candidate soak completed 90 checks across three storage definitions, reported
one transient observation, and ended with no failed or ambiguous result.

```ini
RECOVERY_DSTATE_PERSISTENCE_CONFIRMATION=PASS
RECOVERY_CANDIDATE_ITERATIONS=30
RECOVERY_CANDIDATE_STORAGE_CHECKS=90
TRANSIENT_STORAGE_SCOPED_DSTATE_RECHECKED=1
PERSISTENT_DSTATE_FAILURES=0
AMBIGUOUS_DSTATE_RESULTS=0
```

## Physical reservation accounting qualification

The standard PVE storage gauge reports physical extents allocated from the
shared VG. This is intentionally not replaced with thin-pool `Data%`: reserved
slack inside one per-VM pool is not available for allocation to another VM.
The health JSON, Doctor, and dashboard now expose the physical reservation,
approximate payload, and reserved slack separately. An inactive pool continues
to expose its reservation while payload and slack remain explicitly unknown.

A legacy 68-GiB pool containing only one EFI LV, one TPM-state LV, and their
snapshots reproduced the apparently unexplained 88.50% storage usage. Moving
the two exact auxiliary volumes to Thick Generations and deleting their exact
snapshots removed the now-empty legacy pool and returned its extents to the VG.
PVE usage fell to 3.40%. The Windows guest then booted with TPM ready, its
partition healthy, and its pre-move write-through canary unchanged.

Fresh API-15 and API-14 disposable matrices each exercised a one-GiB elastic
pool with a normal disk plus EFI and TPM LVs, then the auxiliary-only state,
and finally exact cleanup. Reported reservation and payload arithmetic matched
LVM, and VG free bytes before and after the complete lifecycle were identical.

```ini
PVE_GAUGE_PHYSICAL_VG_ACCOUNTING=PASS
LEGACY_AUX_ONLY_RESERVATION_REPRODUCED=PASS
LEGACY_POOL_EXACT_RECLAIM=PASS
WINDOWS_CANARY_AFTER_AUX_MOVE=PASS
WINDOWS_TPM_AFTER_AUX_MOVE=PASS
API15_RESERVATION_MATRIX=PASS
API14_RESERVATION_MATRIX=PASS
INACTIVE_POOL_RESERVATION_VISIBLE=PASS
VG_FREE_BYTES_AFTER_COMPLETE_LIFECYCLE_DELTA=0
```

## Snapshot-delete crash recovery lock behavior

The D0 and D1 process-termination tests also exposed an important Proxmox lock
property. `PVE::Cluster::cfs_lock` is represented by a directory under the
cluster filesystem. A deliberate `_exit(137)` bypasses normal cleanup, so a
subsequent recovery request fails closed until Proxmox processes the stale-lock
unlock request within its 120-second locked-command timeout. Recovery must
never delete or bypass this lock speculatively. The operator retries only after
the bounded lock window and after persistent transaction state is revalidated.

D1 and D2 recovery removed only the signed snapshot object, cleared the exact
VG intent, and preserved the canonical HEAD. At D3 the snapshot was already
absent; recovery did not repeat `lvremove` and only verified and finalized the
transaction. D4 proved the post-commit boundary with both the object and intent
already absent. The SHA-256 of the authoritative HEAD was identical before and
after the complete D0-D4 matrix.

```ini
SNAPSHOT_DELETE_D0_PRE_INTENT=PASS
SNAPSHOT_DELETE_D1_OPEN_INTENT=PASS
SNAPSHOT_DELETE_D1_EXACT_RECOVERY=PASS
SNAPSHOT_DELETE_D2_REBASED_RECOVERY=PASS
SNAPSHOT_DELETE_D3_NO_REDELETE_FINALIZE=PASS
SNAPSHOT_DELETE_D4_POST_COMMIT=PASS
SNAPSHOT_DELETE_HEAD_SHA_AFTER_D0_D4=PASS
PVE_CFS_STALE_LOCK_FAIL_CLOSED=PASS
PVE_CFS_STALE_LOCK_WINDOW=120_SECONDS
SNAPSHOT_DELETE_D0_D4_MULTIPATH=PASS
```

## Same-VG thin and Thick Generations lock domain

The earlier mixed-mode qualification used separate VGs. Coexistence over one
shared LUN/VG adds a stricter concurrency requirement: storage aliases cannot
lock by storage ID because a thin lifecycle operation or dmeventd autogrow
could otherwise change the same VG metadata while a Thick Generations
transaction is open.

Pinned thin and Thick Generations configurations now derive one canonical
cluster lock from the expected VG UUID. Thin allocation, free, resize,
snapshot, snapshot delete, rollback, and thin-pool autogrow all use that lock.
Before entering any pinned thin mutation, the plugin also proves under the
canonical lock that the VG carries no OPEN Thick Generations intent. This
closes the inter-phase window where a thick transaction has persisted its
intent but temporarily released the lock for bounded data work. An
anchor-scoped asynchronous hydration with no global intent remains compatible
with independent thin operations.
The autogrow callback re-reads the storage configuration after acquisition and
refuses mutation if its canonical lock identity changed while waiting. Unit
tests prove that two different storage IDs with the same VG UUID collide on
the same lock and that different VG UUIDs cannot collide. A combined inventory
test also proves that the thin alias exposes only thin guest LVs in its owned
per-VM pools, while the Thick Generations alias exposes only signed anchors and
HEADs belonging to its own storage ID. A live same-VG matrix will run after the
current non-interference monitor completes.

The configuration gate accepts no more than those two aliases, requires
identical identity, reserve, and expected-path settings, and rejects a native
PVE `lvm` or `lvmthin` definition that references the same VG outside the
canonical lock domain.

```ini
SAME_VG_CANONICAL_LOCK_IMPLEMENTATION=PASS
SAME_VG_THIN_LIFECYCLE_LOCK_COVERAGE=PASS
SAME_VG_AUTOGROW_LOCK_COVERAGE=PASS
SAME_VG_LOCK_UNIT_QUALIFICATION=PASS
SAME_VG_OPEN_INTENT_BLOCKS_THIN_MUTATION=PASS
SAME_VG_INVENTORY_ISOLATION_UNIT=PASS
SAME_VG_FOREIGN_PVE_LVM_ALIAS_REJECTED=PASS
SAME_VG_LIVE_COEXISTENCE=PASS
```

The disposable live matrix is encoded in
`experiments/thick-generations/same-vg-coexistence-qualification.sh`. It
validates the canonical pair before mutation, stabilizes the LVM-owned metadata
spare before its free-space baseline, exercises both aliases through PVE
allocation, inventory, snapshot, resize, rollback, deletion, and exact cleanup,
and refuses automatic cleanup after any unexpected failure so evidence remains
available.

```ini
SAME_VG_LIVE_DRIVER_STATIC_GATE=PASS
SAME_VG_LIVE_DRIVER_EXECUTION=PASS
```

All direct LVM lifecycle commands for Thick Generations allocation,
activation, deactivation, resize, snapshot transition, delete, and recovery
are now restricted to the pinned multipath mapper with per-command
`--devices`. Unit and source gates reject a reintroduction of a global LVM
mutation into those paths. The same-VG live lifecycle now qualifies this
scope on the disposable multipath VG.

```ini
THICK_LIFECYCLE_DEVICE_SCOPE_IMPLEMENTATION=PASS
THICK_LIFECYCLE_DEVICE_SCOPE_UNIT_GATE=PASS
THICK_LIFECYCLE_INVENTORY_SCOPE_UNIT_GATE=PASS
THICK_LIFECYCLE_DEVICE_SCOPE_LIVE=PASS
```

PVE evaluates storage capacity per storage ID. A canonical thin/thick pair over
one VG therefore reports the same physical VG capacity on each alias. Those
figures are truthful per alias but are not independent and must not be summed.
Doctor validates the alias topology and reports this presentation constraint;
the plugin deliberately does not fabricate divided capacity values.

```ini
SAME_VG_PER_ALIAS_CAPACITY_TRUTHFUL=PASS
SAME_VG_CAPACITY_AGGREGATION_WARNING=PASS
```

## Pre-coexistence non-interference soak

Before installing the same-VG qualification build, all three cluster nodes
completed an identical 90-minute read-only health soak against the existing
thin storage and the dedicated Thick Generations storage. Each node completed
18 iterations and 36 bounded `recovery-check` executions. Storage identity,
multipath state, relevant D-state inspection, LVM probes, PVE storage health,
and quorum remained healthy throughout. No surviving probe, recovery-required
state, error marker, or mutation was observed.

This result qualifies the baseline only. It does not replace the still-open
live same-VG coexistence, migration, backup/restore, or fault-injection gates.

```ini
PRE_COEXISTENCE_SOAK_NODES=3
PRE_COEXISTENCE_SOAK_DURATION_MINUTES=90
PRE_COEXISTENCE_SOAK_ITERATIONS_PER_NODE=18
PRE_COEXISTENCE_RECOVERY_CHECKS_PER_NODE=36
PRE_COEXISTENCE_PROBE_SURVIVORS=0
PRE_COEXISTENCE_HEALTH_FAILURES=0
PRE_COEXISTENCE_NON_INTERFERENCE=PASS
```

## Live same-VG coexistence result

The disposable same-VG driver completed the full mixed lifecycle through the
PVE storage API. A thin volume and a Thick Generations volume were allocated
on separate aliases of the same pinned VG. Inventory isolation, snapshots,
grow-only resize, rollback, snapshot deletion, exact VM-volume cleanup, and
canonical lock contention all passed. The final VG free-space value exactly
matched the stabilized pre-test baseline.

```ini
SAME_VG_CANONICAL_LOCK_CONTENTION=PASS
SAME_VG_INVENTORY_ISOLATION_LIVE=PASS
SAME_VG_THIN_AND_THICK_SNAPSHOT=PASS
SAME_VG_THIN_AND_THICK_RESIZE=PASS
SAME_VG_THIN_AND_THICK_ROLLBACK=PASS
SAME_VG_THIN_AND_THICK_DELETE=PASS
SAME_VG_FREE_SPACE_DELTA_BYTES=0
```

The live run also exposed and closed three fail-closed implementation gaps:
PVE represents a node scope as a hash in the runtime parser; reused extents
may contain signatures that otherwise cause an interactive `lvcreate` prompt;
and activation-skip LVs require an explicit creation override before their
disabled-autoactivation postcondition is verified. All fixes are covered by
source, lifecycle, taint, and live tests.

An interrupted allocation can persist its exact VG intent before the first LV
exists. Recovery for this state is now explicit and transaction-scoped. It
requires the exact signed `OPEN ALLOC` intent and proves that no related LV or
device-mapper frontend exists before clearing only that intent. Foreign,
partial, or ambiguous evidence is refused without mutation. The command was
validated against a deliberately created intent-only crash state on disposable
multipath storage.

```ini
EMPTY_ALLOCATION_RECOVERY_UNIT=PASS
EMPTY_ALLOCATION_RECOVERY_LIVE=PASS
EMPTY_ALLOCATION_PARTIAL_OBJECT_REFUSAL=PASS
EMPTY_ALLOCATION_FOREIGN_INTENT_REFUSAL=PASS
```

## Large-capacity and dual-mode monitoring gates

Clone geometry and capacity diagnostics have simulated 1 PiB, 16 PiB, and
128 PiB volumes. Region counts, persistent metadata sizing, power-of-two
alignment, and integer boundaries remain bounded. Geometry beyond the exact
qualified arithmetic boundary is rejected without integer wrapping. This is
software arithmetic qualification only; no physical PB storage claim is made.

The read-only web dashboard now identifies Thin and Thick Generations aliases
separately. Thin pool reservation details remain visible only for thin mode;
Thick Generations shows signed anchors, lifecycle phase, generation, PVE
reference count, and diagnostic reason. Every alias reports the truthful VG
capacity and the dashboard explicitly warns that same-VG alias capacities must
not be summed. Thick mode no longer inherits thin headroom/autogrow diagnostics.

```ini
PB_ARITHMETIC_1_16_128_PIB=PASS
PB_INTEGER_OVERFLOW_REFUSAL=PASS
PB_HEALTH_JSON_EXACT_ABOVE_2_POW_53=PASS
PB_HEALTH_JSON_128_PIB=PASS
PB_PAYLOAD_PERCENT_DECIMAL_ARITHMETIC=PASS
PB_HEALTH_JSON_LIVE_CANDIDATE=PASS
PHYSICAL_PB_QUALIFICATION=NOT_TESTED
WEB_THIN_MODE_RENDERING_UNIT=PASS
WEB_THICK_GENERATIONS_RENDERING_UNIT=PASS
WEB_SAME_VG_CAPACITY_WARNING_UNIT=PASS
WEB_DUAL_MODE_LIVE_BROWSER=PASS
WEB_CONFIGURATOR_LIVE_SERVICE_START=PASS
```

The first live configurator run exposed a pipefail-sensitive service-unit
detection pipeline: configuration was written, but the existing unit could be
misclassified and left inactive. Unit discovery now uses a direct
`systemctl cat` probe. A reinstall followed by interactive configuration,
service enable/start, HTTPS request, PVE authentication, health API retrieval,
and a headless browser rendering check all passed.

## Long Windows write-through integrity soak

A Windows guest completed 900 consecutive bounded data cycles on a Thick
Generations disk. Every cycle created fresh payload data, opened the file with
write-through semantics, issued an explicit durable flush, reopened the file,
and verified its SHA-256 digest. The scheduled workload returned success and
no cycle reported a write, flush, reopen, or digest failure.

This result qualifies sustained steady-state guest I/O. It does not replace
the separate interrupted-materialization and host-loss recovery gates.

```ini
WINDOWS_WRITE_THROUGH_CYCLES=900
WINDOWS_EXPLICIT_FLUSH=PASS
WINDOWS_PER_CYCLE_SHA256=PASS
WINDOWS_SOAK_PROCESS_RESULT=0
WINDOWS_LONG_DATA_INTEGRITY_SOAK=PASS
```

## Repeated cross-node non-interference soak

All three mixed-version cluster nodes completed the final read-only health
soak against a dedicated Thick Generations alias, a same-VG thin alias, and an
independent thin alias. Each node performed 18 iterations and 54 bounded
recovery checks. All 162 checks positively verified storage identity,
multipath state, relevant D-state absence, bounded LVM probes, PVE storage
health, and quorum. Every result was `HEALTHY` and safe for mutation; no error
or recovery-required marker was emitted.

The Windows write-through workload overlapped most of this observation period.
No storage lifecycle mutation was injected until all three node-local monitors
had reached their terminal PASS result.

```ini
LONG_CROSS_NODE_SOAK_NODES=3
LONG_CROSS_NODE_STORAGE_ALIASES_PER_NODE=3
LONG_CROSS_NODE_ITERATIONS_PER_NODE=18
LONG_CROSS_NODE_RECOVERY_CHECKS_TOTAL=162
LONG_CROSS_NODE_HEALTHY_RESULTS=162
LONG_CROSS_NODE_SAFE_FOR_MUTATION_RESULTS=162
LONG_CROSS_NODE_DSTATE_FAILURES=0
LONG_CROSS_NODE_ERROR_MARKERS=0
LONG_CROSS_NODE_HEALTH_MONITOR_REPEAT=PASS
```

## Online Windows materialization interruption and recovery

An online snapshot of a running Windows system disk was started while the
guest issued write-through writes, explicit durable flushes, reopen reads, and
SHA-256 verification. The exact transaction-scoped materialization worker was
terminated after persistent dm-clone progress had reached approximately nine
percent. The guest remained running, the anchor stayed unambiguously in
`HYDRATING`, the immutable source and writable destination remained present,
and no materialization worker survived.

The explicit `thick-resume` command derived the transaction entirely from the
signed anchor, reopened the existing persistent clone state, continued from
the recorded progress, completed hydration, and pivoted the published frontend
to a canonical linear mapping. The final dependency graph contained exactly
the authoritative destination LV. The transition metadata LV, source helper
mapping, and VG intent were absent. Repeating `thick-resume` against the
materialized anchor failed with the exact non-resumable reason and made no LVM
inventory change.

The overlapping Windows workload completed 300 of 300 cycles. Its final file
digest matched the logged digest after recovery and again after exact snapshot
deletion. An online NTFS scan reported no file-system problems and no bad
sectors. The VM remained online throughout the worker interruption, recovery,
verification, and snapshot deletion.

```ini
WINDOWS_ONLINE_SNAPSHOT_INTERRUPTION=PASS
WINDOWS_INTERRUPTED_PHASE=HYDRATING
WINDOWS_PERSISTENT_PROGRESS_REOPEN=PASS
WINDOWS_EXPLICIT_THICK_RESUME=PASS
WINDOWS_CONCURRENT_WRITE_THROUGH_CYCLES=300
WINDOWS_CONCURRENT_FLUSH_AND_SHA256=PASS
WINDOWS_LINEAR_PIVOT=PASS
WINDOWS_DESTINATION_ONLY_DEPENDENCY=PASS
WINDOWS_TRANSITION_ARTIFACT_CLEANUP=PASS
WINDOWS_REPEAT_RESUME_NO_MUTATION=PASS
WINDOWS_NTFS_ONLINE_SCAN=PASS
WINDOWS_SNAPSHOT_DELETE_AND_FINAL_SHA256=PASS
WINDOWS_VM_REMAINED_RUNNING=PASS
```

The interruption also exposed a monitoring gap: the original read-only health
payload classified anchors only by PVE reference count. It could therefore
display an interrupted `HYDRATING` anchor as reference-consistent without
showing that explicit recovery was required. The candidate now reports
`IN_PROGRESS` only while the exact transaction worker or exact explicit-resume
process is present. A non-materialized anchor without that exact worker is
`RECOVERY_REQUIRED`; invalid phases fail closed. The CLI Doctor consumes the
same classification and no longer applies thin-pool headroom policy to a
Thick Generations alias.

```ini
THICK_MONITOR_ACTIVE_WORKER_CLASSIFICATION=PASS
THICK_MONITOR_EXPLICIT_RESUME_CLASSIFICATION=PASS
THICK_MONITOR_MISSING_WORKER_RECOVERY_REQUIRED=PASS
THICK_MONITOR_AMBIGUOUS_PHASE_FAIL_CLOSED=PASS
THICK_DOCTOR_MODE_SPECIFIC_POLICY=PASS
```

The classifier was then exercised against a second live disposable online
snapshot. After the exact worker was terminated, both the JSON endpoint and
Doctor reported the signed `HYDRATING` transaction as `RECOVERY_REQUIRED` with
the worker absent. Explicit resume completed the persistent clone, returned
the anchor to `MATERIALIZED`, and changed the same JSON object to `PASS`. The
published frontend was a linear table with exactly one destination dependency.
An idempotency retry was refused, and exact snapshot and VM deletion left no
matching LVM or PVE inventory object.

The JSON monitor also now applies the same scoped `dmeventd` rule as Doctor.
An inactive service is a failure only while a locally owned thin pool is
active. When no owned thin pool requires monitoring, the condition is an
explicit warning rather than a false storage failure. Unit coverage includes
active, inactive, and foreign-pool cases; the inactive/no-active-pool branch
was verified against the live lab inventory.

```ini
THICK_MONITOR_LIVE_RECOVERY_REQUIRED=PASS
THICK_MONITOR_LIVE_POST_RESUME_MATERIALIZED=PASS
THICK_MONITOR_LIVE_LINEAR_DESTINATION_ONLY=PASS
THICK_MONITOR_LIVE_EXACT_CLEANUP=PASS
DMEVENTD_REQUIREMENT_SCOPED_TO_ACTIVE_OWNED_THIN_POOL=PASS
```

## Single-node reboot and exact rejoin

The API-14 qualification node was rebooted while the API-15 nodes hosted an
independent running Windows guest and a running mixed-mode Linux guest. During
the observed two-node membership interval the surviving nodes retained native
quorum and both guests remained running. The rebooted node rejoined with a new
uptime; membership, total votes, and storage visibility returned to three of
three without a manual vote override.

Read-only recovery checks on the rejoined node positively verified the pinned
WWID, PV UUID, VG UUID, two healthy paths, bounded LVM probes, PVE storage
health, quorum, and absence of relevant persistent D-state for both storage
modes. A Thick Generations snapshot created through the rejoined node reached
`MATERIALIZED` and was deleted exactly. A separate disposable one-GiB thin
allocation on that node was created and destroyed; its per-VM pool and payload
were absent afterward and the shared VG free-byte count returned exactly to
the pre-operation value.

```ini
SINGLE_NODE_REBOOT_SURVIVOR_QUORUM=PASS
SINGLE_NODE_REBOOT_RUNNING_GUESTS_UNINTERRUPTED=PASS
SINGLE_NODE_REJOIN_API14_STORAGE_IDENTITY=PASS
SINGLE_NODE_REJOIN_THICK_SNAPSHOT_DELETE=PASS
SINGLE_NODE_REJOIN_THIN_ALLOC_DELETE=PASS
SINGLE_NODE_REJOIN_THIN_VG_FREE_DELTA=0
```

## Complete quorum loss

Corosync was stopped on two nodes with independent transient auto-restore
timers armed before fault injection. The remaining node reported one vote,
`Quorate: No`, and an activity-blocked quorum. Direct allocation requests were
issued once against the thin and Thick Generations aliases while this exact
state was captured.

The outer native PVE storage lock refused both requests with `no quorum`. Each
request used the host's bounded cluster-lock wait and returned failure after
approximately ten seconds. Neither request reached a plugin LVM command. Exact
LVM JSON inventories before the requests, during quorum loss, and after quorum
restoration were byte-identical; no test volume, pool, anchor, generation,
mapper, tag, or PVE configuration object appeared.

The plugin additionally performs a read-only native quorum preflight before
its own mutation-lock entry and repeats the same check under the acquired lock.
This gives direct hook callers an immediate refusal while preserving the
mutation-boundary race check. PVE may still perform its own outer lock attempt
before invoking a storage plugin; the plugin does not override or guess that
native timeout. After both Corosync services returned, the cluster recovered
to three votes and both storage modes again reported `HEALTHY` and
`SAFE_FOR_MUTATION=YES`.

```ini
QUORUM_LOSS_NODES=1
QUORUM_LOSS_QUORATE=NO
QUORUM_LOSS_THIN_MUTATION=REFUSED
QUORUM_LOSS_THICK_MUTATION=REFUSED
QUORUM_LOSS_LVM_INVENTORY_DELTA=0
QUORUM_RESTORE_NODES=3
QUORUM_RESTORE_STORAGE_HEALTH=PASS
```

## Repeated iSCSI single-path fault with mixed-mode guest I/O

One of the two isolated iSCSI target interfaces was disabled after an
independent transient auto-restore timer had been armed. Both multipath maps
changed from two active paths to one active and one failed path for fourteen
consecutive two-second samples, then returned to two active, running, ready
paths with their original identities.

A running Linux guest concurrently completed 300 independent cycles on a
conventional per-VM thin disk and 300 cycles on a Thick Generations linear
disk. Every cycle wrote four MiB of fresh data with `fsync`, reopened and
verified the payload by SHA-256, atomically renamed it, and flushed the final
file. Both terminal workload results were `PASS`.

One instantaneous sample observed a `vgs` process in the kernel AIO teardown
wait path during failover. It was absent from every later sample and from the
post-fault process inventory; no guest request failed or stalled. The final
read-only gates for both storage aliases positively verified two healthy
paths, pinned identity, quorum, bounded LVM probes, no relevant persistent
D-state, `STATE=HEALTHY`, and `SAFE_FOR_MUTATION=YES`.

```ini
ISCSI_SINGLE_PATH_SAMPLES=14
ISCSI_SINGLE_PATH_THIN_WRITE_FLUSH_SHA_CYCLES=300
ISCSI_SINGLE_PATH_THICK_WRITE_FLUSH_SHA_CYCLES=300
ISCSI_SINGLE_PATH_GUEST_ERRORS=0
ISCSI_SINGLE_PATH_PERSISTENT_DSTATE=0
ISCSI_SINGLE_PATH_RETURN_2_OF_2=PASS
ISCSI_SINGLE_PATH_RECOVERY_GATE_BOTH_MODES=PASS
```

## Active dm-thin short total-path-loss failure

Both isolated iSCSI target interfaces were disabled only after an independent
twenty-second auto-restore timer had been armed. The test node hosted a running
guest with one conventional per-VM thin disk and two Thick Generations linear
disks. All underlying SCSI path devices later returned to `running`, and both
iSCSI sessions were visible with their original portals and target identity.

The host storage stack did not recover. A kernel worker remained permanently
blocked in `dm_pool_issue_prefetches`; its captured stack was
`dm_pool_issue_prefetches -> do_worker -> process_one_work -> worker_thread`.
The state persisted across repeated passive samples after every SCSI device
had returned. `multipathd` diagnostics and the bounded recovery check timed
out. Consequently restored path count was correctly not accepted as storage
health.

A normal `systemctl reboot` could not complete while the dm-thin worker was
blocked. With the other two nodes quorate and the incident evidence already
copied off-host, a forced reboot of only the affected disposable node was
required. After reboot both aliases positively verified the same WWID, PV
UUID, VG UUID, two paths, quorum, bounded probes, and zero relevant D-state.
The test VM was started manually. Its thin and Thick Generations ext4 volumes
mounted normally, and their final cycle-400 SHA-256 digests exactly matched
the durable workload logs; no ext4 or block-I/O error was reported after boot.

This is a host-side dm-thin recovery failure, not a successful total-path-loss
qualification. It proves the recovery gate must remain closed on persistent
storage-scoped D-state even after identity and paths return. Because the same
host also owned an active thin pool, this run does not independently qualify
a Thick-only total-loss outcome; that isolated case remains open.

```ini
ISCSI_TOTAL_PATH_LOSS_WINDOW_SECONDS=20
ISCSI_PATH_DEVICES_RETURNED_RUNNING=PASS
ISCSI_SESSIONS_RETURNED=PASS
DM_THIN_PERSISTENT_DSTATE=FAIL_HOST_DM_THIN
NORMAL_HOST_REBOOT=BLOCKED
FORCED_HOST_REBOOT_REQUIRED=YES
POST_REBOOT_STORAGE_IDENTITY=PASS
POST_REBOOT_THIN_DATA_SHA256=PASS
POST_REBOOT_THICK_DATA_SHA256=PASS
THICK_ONLY_TOTAL_PATH_LOSS_QUALIFICATION=OPEN
```

## Isolated Thick-only short total-path-loss qualification

A separate run moved the Linux workload guest to a node on which its
conventional thin disk was detached and its per-VM thin pool remained inactive.
Only two canonical Thick Generations linear frontends were active. The guest
wrote fresh random 4 MiB files to two independent ext4 filesystems, issued
fsync and directory sync, renamed each file, and verified SHA-256 on every
cycle.

Both isolated iSCSI target interfaces were disabled after an independent
twenty-second auto-restore timer was armed. The guest completed cycle 369 as
the fault was applied, paused while all paths were unavailable, resumed at
cycle 370 after the map became usable, and completed all 400 cycles. Both final
files matched their durable SHA-256 records. The initiator required longer than
the interface-down interval to reinstate every SCSI path; all four path devices
eventually reported `active ready running` and both maps returned to two healthy
paths.

No persistent D-state appeared. The bounded recovery gate positively verified
the original WWID, PV UUID, VG UUID, two healthy paths, bounded LVM and PVE
probes, quorum, and `SAFE_FOR_MUTATION=YES`. A subsequent two-disk Thick
snapshot completed, both transitions materialized and pivoted back to canonical
linear mappings, and the snapshot was removed. The VM was then returned to its
original node and its detached thin disk was reattached without recreation;
both storage modes passed their recovery gates.

An intentionally premature snapshot-delete request, issued while one exact
materialization worker was still running, was correctly refused but left the
standard PVE `snapshot-delete` VM lock in place. After the worker disappeared
and the anchor positively proved `MATERIALIZED`, an explicit `qm unlock` and
retry completed. Automatic lock clearing is deliberately not implemented;
this integration behavior remains tracked for runbook and user-interface
handling.

```ini
ISCSI_THICK_ONLY_FAULT_ACTIVE_THIN_POOL=NO
ISCSI_THICK_ONLY_TARGET_INTERFACE_OUTAGE_SECONDS=20
ISCSI_THICK_ONLY_GUEST_IO_PAUSED_AND_RESUMED=PASS
ISCSI_THICK_ONLY_ROOT_SHA256=PASS
ISCSI_THICK_ONLY_SECOND_DISK_SHA256=PASS
ISCSI_THICK_ONLY_PERSISTENT_DSTATE=0
ISCSI_THICK_ONLY_RETURN_2_OF_2=PASS
ISCSI_THICK_ONLY_RECOVERY_GATE=PASS
ISCSI_THICK_ONLY_POST_RECOVERY_SNAPSHOT=PASS
ISCSI_THICK_ONLY_POST_RECOVERY_LINEAR_PIVOT=PASS
ISCSI_THICK_ONLY_POST_RECOVERY_DELETE=PASS
ISCSI_THICK_ONLY_TOTAL_PATH_LOSS=PASS_BOUNDED_LAB_POLICY
ISCSI_HYDRATING_TOTAL_PATH_LOSS=FAIL_HOST_DM_CLONE
PVE_PREMATURE_SNAPSHOT_DELETE_LOCK=OBSERVED
```

## Active dm-clone short total-path-loss failure and explicit recovery

A separate run isolated two actively hydrating Thick Generations disks on a
node with no active thin-pool target. Both iSCSI target interfaces were disabled
only after the same independent twenty-second auto-restore timer had been
armed. The original iSCSI sessions and path devices returned, but the active
dm-clone transactions did not recover. Two kernel workers remained persistently
blocked in `dm_bufio_write_dirty_buffers -> dm_bm_flush -> dm_tm_pre_commit ->
dm_clone_metadata_commit -> commit_metadata -> do_worker`. Bounded `vgs` and
`dmsetup status` probes also blocked in the dm-clone metadata path.

A normal host reboot could not complete. After evidence was copied off-host and
the other two cluster nodes were confirmed quorate, only the affected disposable
node was forcibly rebooted. Persistent anchors then classified both disks as
`HYDRATING` with their exact transaction identity and no worker. No transition
was resumed automatically.

The incident exposed a recovery-check defect: the original read-only checker
accepted healthy paths and identity while ignoring non-materialized Thick
Generations anchors. The checker now validates every anchor in the pinned VG,
including its deterministic VG-UUID-derived name, storage ownership, complete
canonical schema, digest, generation, region geometry, operation semantics and
materialized HEAD relationship. Any non-materialized, malformed, foreign or
internally inconsistent anchor produces `THICK_ANCHORS_HEALTHY=FAIL`,
`STATE=RECOVERY_REQUIRED` and `SAFE_FOR_MUTATION=NO`. The complete Python and
Perl regression suites passed, and the preserved live incident proved that only
the two interrupted anchors failed while six canonical materialized anchors
passed.

Each disk was then resumed explicitly from its original signed transaction.
Both reconstructed the persistent clone, completed hydration, pivoted to a
destination-only linear frontend and removed only their exact transition
metadata LV. The recovery gate then returned healthy. The guest booted, ext4
journal recovery completed, the secondary filesystem passed a read-only
`e2fsck`, and new durable writes retained identical SHA-256 values across a
flush and reopen. Online migration from the API 14 recovery node to an API 15
node completed, and the existing conventional thin disk was reattached without
recreation. The recovered snapshot was subsequently deleted; only its two exact
immutable source generations were removed and both anchors remained canonical.

The original workload manifest cannot be used as a cryptographic post-crash
oracle. Its shell redirection truncated each checksum file before writing, and
the forced host reset left the fixed-size files zero-filled. The payloads remain
readable, but the test therefore records filesystem and lifecycle recovery, not
a pre-fault payload SHA pass. The private fault workload was corrected to write
and fsync a temporary manifest, atomically rename it, fsync the directory and
log the digest independently before future destructive runs.

```ini
ISCSI_HYDRATING_FAULT_ACTIVE_THIN_POOL=NO
ISCSI_HYDRATING_TARGET_INTERFACE_OUTAGE_SECONDS=20
DM_CLONE_PERSISTENT_DSTATE=FAIL_HOST_DM_CLONE
NORMAL_HOST_REBOOT=BLOCKED
FORCED_HOST_REBOOT_REQUIRED=YES
POST_REBOOT_AUTOMATIC_RESUME=NO
POST_REBOOT_ANCHOR_CLASSIFICATION=RECOVERY_REQUIRED
RECOVERY_CHECK_NON_MATERIALIZED_ANCHOR_GATE=PASS
EXPLICIT_EXACT_TRANSACTION_RESUME=PASS
POST_RESUME_LINEAR_DEPENDENCY=PASS
POST_RESUME_TRANSITION_METADATA_CLEANUP=PASS
POST_RESUME_FILESYSTEM_RECOVERY=PASS
POST_RESUME_NEW_WRITE_SHA256=PASS
PRE_FAULT_PAYLOAD_SHA256=NOT_PROVEN_TEST_ORACLE_INVALID
POST_RECOVERY_API14_TO_API15_LIVE_MIGRATION=PASS
POST_RECOVERY_THIN_REATTACH=PASS
POST_RECOVERY_SNAPSHOT_DELETE=PASS
ISCSI_HYDRATING_TOTAL_PATH_LOSS=FAIL_HOST_DM_CLONE
```

## Fresh shared-VG qualification on the original three-node cluster

A new 64 GiB sparse TrueNAS iSCSI LUN was mapped through two paths on each of
the three PVE nodes and initialized exactly once as a disposable qualification
VG.  Thin and Thick Generations aliases used the same pinned WWID, PV UUID, VG
UUID, reserve policy and canonical VG lock.  Recovery admission returned
`HEALTHY` and `SAFE_FOR_MUTATION=YES` for both aliases on Storage API 15 nodes
and on the Storage API 14 node.

The identical tg8 package was then reinstalled on every node.  The cluster
storage configuration digest and all running QEMU PIDs were unchanged.  Nodes
with an existing dashboard configuration preserved and restarted that service;
the unconfigured API 14 node remained disabled and did not expose a listener.
The node-scope correction also classified the FCoE alias as not applicable on
the API 14 node instead of probing unavailable hardware.

The fail-closed same-VG driver completed thin and thick allocation, inventory
isolation, snapshot, grow-only resize, rollback, snapshot deletion and exact
cleanup.  Final VG free space equalled the stabilized baseline byte-for-byte.
A separate thick disk was written on node 1, deactivated, reconstructed as a
destination-only linear frontend on node 2, verified with the same whole-disk
SHA-256, returned to node 1 and removed exactly.

A two-disk mixed-mode disposable VM then completed start, stop and online
migration from node 1 to node 2 and back.  Both offline whole-disk SHA-256
values remained unchanged.  PVE Storage Move copied the thin disk to Thick
Generations and the thick disk to thin mode with matching destination digests
before successful source removal.  A mixed-mode backup restored both disks to
Thick Generations, and a second all-thick backup restored both disks to thin
mode.  Every restored whole-disk digest matched its original.

```ini
FRESH_ISCSI_LUN_PATHS_ALL_NODES=2_OF_2
FRESH_ISCSI_IDENTITY_ALL_NODES=PASS
TG8_ROLLING_INSTALL_API15_API14=PASS
TG8_SAME_VERSION_REINSTALL_ALL_NODES=PASS
REINSTALL_STORAGE_CFG_UNCHANGED=PASS
REINSTALL_RUNNING_QEMU_PID_UNCHANGED=PASS
NODE_SCOPE_FCOE_SKIP_API14=PASS
FRESH_SAME_VG_COEXISTENCE=PASS
FRESH_SAME_VG_FINAL_FREE_DELTA_BYTES=0
FRESH_CROSS_NODE_LINEAR_RECONSTRUCTION=PASS
FRESH_CROSS_NODE_WHOLE_DISK_SHA256=PASS
FRESH_MIXED_MODE_LIVE_MIGRATION_ROUND_TRIP=PASS
FRESH_STORAGE_MOVE_THIN_TO_THICK_SHA256=PASS
FRESH_STORAGE_MOVE_THICK_TO_THIN_SHA256=PASS
FRESH_BACKUP_MIXED_RESTORE_THICK_SHA256=PASS
FRESH_BACKUP_THICK_RESTORE_THIN_SHA256=PASS
TG9_REPRODUCIBLE_DEB_SHA256=430c21e0be195a9f709a631570999da10bc3d922667f795638c021916f58df3f
TG9_ROLLING_INSTALL_API15_API14=PASS
TG9_STORAGE_CFG_UNCHANGED=PASS
TG9_RUNNING_QEMU_PID_UNCHANGED=PASS
TG9_WEB_REDIRECT_STRICT_CLIENT=PASS
FRESH_DISPOSABLE_CLEANUP=PASS
FRESH_FINAL_VG_FREE_DELTA_BYTES=0
LIVE_TWO_NODE_NO_QDEVICE_WARNING=PASS
LIVE_TWO_NODE_THIN_THICK_LIFECYCLE=PASS
LIVE_TWO_NODE_FINAL_VG_FREE_DELTA_BYTES=0
LIVE_TWO_NODE_ONE_LOST_NATIVE_QUORUM=FAIL_AS_EXPECTED
LIVE_TWO_NODE_NO_QUORUM_THIN_ALLOC_RC=13
LIVE_TWO_NODE_NO_QUORUM_THICK_ALLOC_RC=13
LIVE_TWO_NODE_NO_QUORUM_LVM_INVENTORY_UNCHANGED=PASS
LIVE_TWO_NODE_NO_QUORUM_SAFE_FOR_MUTATION=NO
PRIVATE_VOTE_OVERRIDE_USED=NO
THREE_NODE_QDEVICE_BASELINE_RESTORED=PASS
THREE_NODE_ALL_PATHS_2_OF_2=PASS
THREE_NODE_BOTH_MODES_HEALTHY=PASS
```

## Host loss during a mixed Thin and Thick backup

A running guest with two Thick Generations disks and one conventional thin disk
was placed under a continuous three-filesystem write, fsync and SHA-256 workload.
Each filesystem used two alternating payload slots with an independently fsynced
and atomically renamed manifest, so a host crash could invalidate at most the
slot whose write was outstanding while preserving the preceding durable slot.

A throttled PVE snapshot-mode backup was confirmed active after QMP publication
and after it had begun reading all three disks. The hosting node was then
forcibly rebooted while both backup and guest writes were active. The other two
nodes retained quorum. On return, the VM remained stopped with no stale lock;
both storage recovery checks positively verified identity, paths, pool and
anchor health, bounded probes, quorum and no relevant D-state.

The interrupted backup was not advertised as a completed archive. PVE left one
transaction-identifiable temporary directory and one incomplete `.vma.dat`
file. After their exact names and prior-boot journal were captured, only those
two partial objects were removed. No volume or snapshot cleanup was performed.

The guest restarted normally. Journal replay completed, and offline read-only
checks after replay found both secondary ext4 filesystems structurally clean.
Both rotating root slots, both thin slots and the previous Thick secondary slot
matched their durable manifests. The alternate Thick slot that was being
overwritten at the instant of host loss did not match its older manifest, which
is the expected outstanding-write boundary. The preceding durable slot remained
valid, proving the two-slot oracle worked as designed; this test does not claim
completion of an outstanding guest write.

```ini
HOST_LOSS_DURING_BACKUP=PASS_BOUNDED_LAB
BACKUP_INCLUDED_THICK_DISKS=2
BACKUP_INCLUDED_THIN_DISKS=1
SURVIVOR_QUORUM=PASS
PARTIAL_BACKUP_ADVERTISED_COMPLETE=NO
PARTIAL_BACKUP_EXACT_CLEANUP=PASS
POST_REBOOT_VM_LOCK=ABSENT
POST_REBOOT_THIN_RECOVERY_GATE=PASS
POST_REBOOT_THICK_RECOVERY_GATE=PASS
POST_REBOOT_SECONDARY_FILESYSTEMS=PASS
LAST_DURABLE_SLOT_ALL_FILESYSTEMS=PASS
OUTSTANDING_GUEST_WRITE_COMPLETION=NOT_GUARANTEED
```

## Interrupted and completed same-VG Thin/Thick storage moves

A stopped disposable VM was allocated on the conventional per-VM thin alias
in the same shared VG as the Thick Generations alias. Because the thin source
already owned the requested raw guest name, the Thick allocator selected the
first candidate for which the raw name, deterministic anchor and generation
zero HEAD were all absent. It did not weaken any auxiliary-name collision
gate.

The first move was interrupted by a host reset during the PVE full-copy phase.
The VM configuration still referenced the intact thin source, while the
unpublished Thick destination was fully materialized but unreferenced. The
recovery checker now classifies such an anchor as `RECOVERY_REQUIRED` instead
of treating materialization alone as health. An explicit orphan-allocation
recovery removed only that exact signed generation and anchor after proving
zero PVE references.

A subsequent uninterrupted Thin-to-Thick move completed, changed the PVE
configuration to the collision-free Thick volume and removed the thin source.
The first 64 MiB SHA-256 matched the source exactly. The reverse Thick-to-Thin
move then completed into the newly available original guest name, removed the
exact Thick HEAD and anchor, and retained the same SHA-256. Final deletion of
the disposable VM removed its per-VM thin pool and disk. Both same-VG aliases
ended `HEALTHY` with `SAFE_FOR_MUTATION=YES` and no test objects.

During a separate start/stop verification, PVE cleanup reached the plugin
before QEMU's final mapper close and the strict zero-open check initially left
the stable frontend active. Deactivation now absorbs only this bounded close
race, revalidates the exact mapper table after observing zero opens, and still
refuses a persistently open mapper. A repeated start/stop left no frontend.

```ini
SAME_VG_INTERRUPTED_THIN_TO_THICK_SOURCE_AUTHORITY=PASS
SAME_VG_UNREFERENCED_DESTINATION_GATE=PASS
SAME_VG_EXACT_ORPHAN_CLEANUP=PASS
SAME_VG_THIN_TO_THICK=PASS
SAME_VG_THICK_TO_THIN=PASS
SAME_VG_ROUNDTRIP_SHA256=PASS
SAME_VG_SOURCE_CLEANUP=PASS
SAME_VG_FINAL_RECOVERY_GATES=PASS
QMEVENTD_CLOSE_RACE_BOUNDED=PASS
PERSISTENTLY_OPEN_FRONTEND_FORCE_REMOVE=NO
```

The complete conversion was repeated with the `0.9.0~rc5.4~tg4` candidate
after its package-upgrade qualification. A one-GiB disposable Thick disk was
moved to the coexisting conventional Thin alias, activated through a real VM
start and suspended for a direct block-device measurement. It was then moved
back to the Thick Generations alias, activated and measured again. The Thick
source, active Thin destination and returned Thick destination all produced
the same SHA-256. Both recovery gates positively reported `HEALTHY` and
`SAFE_FOR_MUTATION=YES` before final deletion. Exact cleanup removed the VM,
the per-VM thin pool, thin disk, Thick generation and anchor.

```ini
TG4_REPEAT_THICK_TO_THIN=PASS
TG4_REPEAT_THIN_DEVICE_DIRECT_SHA256=PASS
TG4_REPEAT_THIN_TO_THICK=PASS
TG4_REPEAT_RETURNED_THICK_DIRECT_SHA256=PASS
TG4_REPEAT_CROSS_MODE_SHA256=1447de7062795327abea422eeecdd4240967cf004ab2dbc53eba403eef96df39
TG4_REPEAT_BOTH_RECOVERY_GATES=PASS
TG4_REPEAT_EXACT_CLEANUP=PASS
```

## Interrupted Thick-to-Thin same-VG move

A second disposable run exercised the opposite conversion direction. The
source was a canonical materialized Thick generation. Its first MiB was
explicitly zeroed to prevent accidental guest boot code, and a random 64 MiB
payload beginning at offset one MiB was recorded by SHA-256. A forced host
reset occurred after PVE had allocated the conventional per-VM thin target and
while the full-copy operation was still responsible for publication.

After reboot, the PVE configuration still referenced the Thick source. Its
zero boot region and payload SHA-256 both matched the pre-fault values. The
unreferenced thin target and its owned per-VM pool remained present. This
exposed a recovery-check gap: healthy thin-pool flags alone did not prove that
the pool's guest disks were referenced by PVE.

The thin recovery gate now inventories owned pools and their canonical guest
disks, performs exact cluster-wide PVE reference checks, and fails closed on an
unreferenced disk. The explicit `thin-recover-orphan` command then removed only
the unreferenced disk and, through existing ownership rules, its empty owned
pool. Both storage aliases returned healthy. A normal Thick-to-Thin retry
completed, retained both SHA-256 values, removed the exact Thick source, and
the final VM deletion left no disposable object.

```ini
SAME_VG_INTERRUPTED_THICK_TO_THIN_SOURCE_AUTHORITY=PASS
SAME_VG_INTERRUPTED_THICK_TO_THIN_SOURCE_SHA256=PASS
SAME_VG_THIN_ORPHAN_DETECTION=PASS
SAME_VG_THIN_ORPHAN_EXPLICIT_CLEANUP=PASS
SAME_VG_THICK_TO_THIN_RETRY=PASS
SAME_VG_THICK_TO_THIN_RETRY_SHA256=PASS
SAME_VG_REVERSE_MOVE_FINAL_CLEANUP=PASS
SAME_VG_REVERSE_MOVE_FINAL_RECOVERY_GATES=PASS
AUTOMATIC_THIN_ORPHAN_CLEANUP=NO
```

## Windows 11 multi-volume snapshot, rollback and online deletion

A UEFI Windows 11 guest with one 36 GiB system disk, a four MiB EFI disk and
a four MiB TPM state disk exercised the complete Thick Generations lifecycle.
Before the snapshot, a 64 MiB NTFS oracle was durably flushed and recorded by
SHA-256. All three snapshot transitions completed, and each frontend returned
to a destination-only linear mapping. While the system-disk transition was
still hydrating, Windows overwrote and durably flushed the oracle with a second
pattern. This also exercised foreground writes through the temporary clone
mapping.

Windows was shut down from inside the guest. Rollback created fresh independent
HEAD generations for the system, EFI and TPM volumes, fully materialized them,
and removed the replaced HEADs. The guest then booted successfully and the NTFS
oracle matched its exact pre-snapshot SHA-256 rather than the post-snapshot
pattern. Online snapshot deletion while the restored guest was running removed
exactly the three immutable snapshot generations. The three current HEADs and
anchors remained materialized, no transition metadata LV or worker remained,
and the complete recovery gate returned `HEALTHY` with
`SAFE_FOR_MUTATION=YES`.

```ini
WINDOWS_11_UEFI_TPM_SNAPSHOT=PASS
WINDOWS_FOREGROUND_WRITE_DURING_HYDRATION=PASS
WINDOWS_CLEAN_SHUTDOWN_DURING_MATERIALIZED_HEAD=PASS
WINDOWS_MULTI_VOLUME_ROLLBACK=PASS
WINDOWS_POST_ROLLBACK_BOOT=PASS
WINDOWS_POST_ROLLBACK_NTFS_SHA256=PASS
WINDOWS_ONLINE_SNAPSHOT_DELETE=PASS
WINDOWS_SNAPSHOT_GENERATION_CLEANUP=PASS
WINDOWS_CURRENT_HEADS_PRESERVED=PASS
WINDOWS_POST_LIFECYCLE_RECOVERY_GATE=PASS
POST_WINDOWS_PYTHON_REGRESSION=133/133_PASS
POST_WINDOWS_PERL_REGRESSION=235/235_PASS
```

The same running Windows guest then exercised an online grow from 36 GiB to
40 GiB. The plugin extended the exact current HEAD, zero-initialized and
flushed the new range, atomically reloaded the frontend to the larger linear
table and left the recovery gate healthy. Windows detected the added capacity
after an explicit storage rescan without a guest reboot. NTFS was extended to
its reported maximum and the oracle SHA-256 remained unchanged before and
after the filesystem grow.

```ini
WINDOWS_ONLINE_THICK_GROW=PASS
WINDOWS_ONLINE_CAPACITY_RESCAN=PASS
WINDOWS_ONLINE_NTFS_GROW=PASS
WINDOWS_POST_GROW_ORACLE_SHA256=PASS
WINDOWS_POST_GROW_RECOVERY_GATE=PASS
```

## Repeated host loss during same-VG storage move

A fully allocated two GiB disposable disk was moved from the conventional Thin
alias to Thick Generations on a worker node. The first worker reset landed
after the mirror had committed but before the caller observed completion. PVE
referenced the Thick destination, the Thin source was absent, and the complete
destination SHA-256 matched the flushed source. This proves the post-commit
side of the storage-move boundary.

The reverse Thick-to-Thin move was then rate-limited to create an observable
copy window. The worker node was reset after PVE reported approximately 3.2%
transferred. After rejoin, the PVE configuration still referenced the intact
Thick source and its complete SHA-256 matched the pre-fault value. The partial
Thin disk and its owned pool were unreferenced, so the Thin recovery gate
returned `RECOVERY_REQUIRED` and blocked mutation. Explicit
`thin-recover-orphan` removed only that exact destination disk and empty owned
pool. A normal retry completed, switched the PVE reference, removed the Thick
source, and retained the complete SHA-256. Final PVE deletion left no object in
either allocation mode and both recovery gates returned healthy.

```ini
STORAGE_MOVE_POST_COMMIT_HOST_LOSS=PASS
STORAGE_MOVE_MID_COPY_HOST_LOSS=PASS
STORAGE_MOVE_SOURCE_AUTHORITY=PASS
STORAGE_MOVE_SOURCE_SHA256=PASS
STORAGE_MOVE_PARTIAL_TARGET_GATE=PASS
STORAGE_MOVE_EXACT_ORPHAN_RECOVERY=PASS
STORAGE_MOVE_RETRY=PASS
STORAGE_MOVE_RETRY_SHA256=PASS
STORAGE_MOVE_FINAL_CLEANUP=PASS
STORAGE_MOVE_FINAL_RECOVERY_GATES=PASS
```

## Node reboot after complete single-path recovery

On an otherwise idle cluster node, one iSCSI network path was taken down. Both
the conventional Thin and Thick Generations LUNs remained available through
their surviving paths, while their configured two-path recovery gates correctly
returned `RECOVERY_REQUIRED` and blocked new mutations. After the link returned,
all four SCSI paths became `active ready running`; both recovery gates returned
healthy and the exact WWID, PV UUID and VG UUID pairs matched their pinned
identities.

That node was then reset. It rejoined the three-node quorum and automatically
rediscovered both LUNs with two active paths each. Both storage recovery gates
again returned `HEALTHY` with `SAFE_FOR_MUTATION=YES`. A running mixed-mode
Linux guest on another node retained identical 64 MiB canary hashes on its two
Thick disks and one Thin disk across the complete path flap and peer reboot.

```ini
ISCSI_SINGLE_PATH_MUTATION_GATE=PASS_BOTH_MODES
ISCSI_PATH_RETURN_2_OF_2=PASS_BOTH_MODES
POST_PATH_RECOVERY_NODE_REBOOT=PASS
POST_REBOOT_CLUSTER_QUORUM=PASS
POST_REBOOT_STORAGE_IDENTITY=PASS_BOTH_MODES
POST_REBOOT_RECOVERY_GATES=PASS_BOTH_MODES
POST_REBOOT_MIXED_GUEST_HASHES=PASS
```

## Native Thin snapshot callback host loss and exact recovery

A stopped disposable guest with twelve one GiB Thin disks was snapshot on a
worker node. A private fault watcher stopped the PVE snapshot process as soon
as its first live `lvcreate` child appeared, and the worker node was reset. The
cluster configuration retained `lock: snapshot` and a snapshot section in
`snapstate: prepare` for all twelve disks, while only nine corresponding
snapshot LVs existed. All twelve base disks remained present.

This fault exposed a P0 recovery-check gap: the original Thin reference check
proved only that canonical base disks had PVE references and incorrectly
returned `HEALTHY`. The checker now constructs the exact set of snapshot pairs
declared in all cluster VM and container configuration files and compares it
with the snapshot LV inventory of each owned per-VM pool. It fails closed on a
missing, excess, malformed or wrong-pool snapshot object and excludes only disk
entries explicitly marked `snapshot=0`. Configuration-section state is reset at
every file boundary so duplicate cluster views cannot create cross-file false
references.

On the preserved fault state the corrected gate reported exactly the three
missing objects and returned `RECOVERY_REQUIRED`. The first supported delete
attempt correctly refused the stale PVE snapshot lock without changing data.
After positively proving that the guest was stopped, the lock was exactly
`snapshot`, and the target section was exactly `snapstate: prepare`, the manual
recovery used `qm unlock` followed by `qm delsnapshot --force`. PVE removed the
nine existing snapshot LVs, reported the three absent objects, removed the
stale section and lock, and preserved all twelve base disks. The first 64 MiB
canary of disk zero had the same SHA-256 before and after recovery. No snapshot
LV for the transaction remained and the corrected gate returned `HEALTHY`.

```ini
THIN_SNAPSHOT_CALLBACK_LIVE_CHILD_FAULT=PASS
THIN_SNAPSHOT_PARTIAL_INVENTORY_REPRODUCED=PASS_9_OF_12
THIN_SNAPSHOT_MISSING_OBJECT_GATE=PASS_3_EXACT
THIN_SNAPSHOT_FALSE_HEALTHY_DEFECT=FIXED
THIN_SNAPSHOT_STALE_LOCK_REFUSAL=PASS
THIN_SNAPSHOT_MANUAL_RECOVERY_PRECONDITIONS=PASS
THIN_SNAPSHOT_EXACT_PARTIAL_CLEANUP=PASS
THIN_SNAPSHOT_BASE_DISKS_PRESERVED=PASS_12_OF_12
THIN_SNAPSHOT_SOURCE_CANARY_SHA256=PASS
THIN_SNAPSHOT_POST_RECOVERY_GATE=PASS
POST_FIX_PYTHON_REGRESSION=137/137_PASS
POST_FIX_PERL_REGRESSION=235/235_PASS
AUTOMATIC_PARTIAL_SNAPSHOT_CLEANUP=NO
```

## FCoE baseline failure under a small Linux workload

A fresh two-path Linux VN2VN target was exercised by a stopped disposable
guest using independent one GiB Thin and Thick Generations data disks. The
test did not inject a path failure. During ordinary filesystem writes, the
target rejected an oversized WRITE SAME request emitted while initializing
the Thin pool, one FC path disappeared, and target teardown entered permanent
uninterruptible sleep.

The primary blocked stack was `ft_prlo -> target_wait_for_sess_cmds` waiting
for retained commands. LUN reset blocked in `target_put_cmd_and_wait`, while
VN2VN discovery and timeout workers serialized behind the same teardown path.
The initiator subsequently blocked device-mapper status in
`dm_pool_commit_metadata`. A target restart followed by an initiator restart
restored the exact LUN identity and two healthy paths on every node.

The preserved historical experimental `tcm_fc` patch was not installed. Its
cleanup predicate covers only an orphaned partial software WRITE before core
abort ownership. The new failure had commands already owned by abort/TMF state,
so extending that patch would cross target-core lifetime rules without proof.

Read-only `e2fsck -n` after recovery found both interrupted filesystems
unclean, as expected after forced host termination. No automatic filesystem or
target metadata repair was attempted. This result is a transport qualification
failure below the plugin and does not alter the successful iSCSI Thick
Generations lifecycle evidence.

```ini
FCOE_BASELINE_PATH_FAULT_INJECTION=NONE
FCOE_SMALL_LINUX_THICK_WRITE=COMPLETED_BEFORE_FAILURE
FCOE_THIN_ZEROING_WRITE_SAME_LIMIT=EXCEEDED
FCOE_TARGET_SESSION_TEARDOWN=FAIL_DSTATE
FCOE_INITIATOR_DM_THIN_STATUS=FAIL_DSTATE_SECONDARY
FCOE_IDENTITY_AFTER_EXPLICIT_RESTART=PASS
FCOE_PATHS_AFTER_EXPLICIT_RESTART=2_OF_2_ALL_NODES
AUTOMATIC_TARGET_REPAIR=NO
HISTORICAL_NARROW_KERNEL_PATCH_APPLIED=NO
FCOE_PRODUCTION_QUALIFICATION=FAIL_TARGET_TCM_FC
```

## Dual-mode endurance run interrupted below the plugin

A four-hour small-Linux workload was started with one conventional Thin disk
and one materialized Thick Generations disk. Both workers repeatedly wrote a
64 MiB random payload, issued an explicit filesystem flush, and verified the
payload through a direct read. The accompanying host monitors continuously ran
the bounded storage recovery gate on every cluster node.

The run produced more than 2,700 successful write/flush/direct-read
verifications without a checksum mismatch or storage-gate failure. It ended
after approximately 35 minutes when the local ESXi datastore backing the
nested cluster and storage-target virtual machines temporarily disappeared.
The hypervisor log recorded SATA command aborts, a controller reset, VMFS
heartbeat timeouts, aborted virtual-disk I/O and multi-second device latency.
All nested cluster nodes rebooted within the same short interval while the
physical hypervisor remained online.

After restart, every node revalidated the original shared-storage identity,
two healthy paths, quorum and zero relevant storage-scoped D-state. Both tested
storage aliases returned `HEALTHY` and `SAFE_FOR_MUTATION=YES`. This is useful
infrastructure evidence, but it is not an endurance PASS and it is not
classified as a plugin failure. A complete uninterrupted run remains required
on stable backing storage.

The private workload harness now records a run identifier and Linux boot ID,
preserves an incomplete-run marker, refuses to overwrite unclassified evidence
and requires every host-side health gate to remain positive. A rebooted or
terminated run therefore cannot be reported as complete.

```ini
DUAL_MODE_ENDURANCE_TARGET_DURATION=4_HOURS
DUAL_MODE_ENDURANCE_ELAPSED_APPROXIMATELY=35_MINUTES
DUAL_MODE_VERIFICATIONS_BEFORE_INTERRUPTION=MORE_THAN_2700
DUAL_MODE_CHECKSUM_FAILURES=0
PLUGIN_HEALTH_GATE_FAILURES_BEFORE_INTERRUPTION=0
INTERRUPTION_CLASSIFICATION=FAIL_TEST_INFRA_ESXI_DATASTORE
POST_REBOOT_STORAGE_IDENTITY=PASS_ALL_NODES
POST_REBOOT_RECOVERY_GATES=PASS_ALL_NODES
INTERRUPTED_RUN_COUNTED_AS_PASS=NO
DUAL_MODE_ENDURANCE_QUALIFICATION=OPEN
```

## Post-interruption static safety regression

Development continued without issuing storage mutations against the unstable
test infrastructure. A production-source audit found no implicit PV/VG
initialization, metadata repair, forced device-mapper removal, global udev
settle, multipath-service restart or plugin-defined cluster-watchdog timeout in
the Thick Generations runtime path. The destructive initialization commands in
the tree remain confined to explicitly disposable, acknowledgement-gated
Stage 1 experiments.

The complete Python suite, the duplicate-probe integration harness and syntax
validation of every shipped or experimental shell entry point were repeated.
The private endurance harness separately proved that a changed Linux boot ID
causes a prior run to be classified as incomplete rather than complete.

```ini
POST_INTERRUPTION_PYTHON_REGRESSION=137/137_PASS
POST_INTERRUPTION_PROBE_GUARD=PASS
POST_INTERRUPTION_SHELL_SYNTAX=16/16_PASS
ENDURANCE_REBOOT_CLASSIFICATION=PASS
AUTOMATIC_STORAGE_REPAIR_PATHS_FOUND=0
PLUGIN_CLUSTER_WATCHDOG_ASSUMPTIONS_FOUND=0
```

## Mixed-API package reinstall and live-migration repetition

The experimental package was installed and then reinstalled from the identical
artifact on three cluster nodes. Two nodes ran Storage API 15 and one retained
Storage API 14. Package checksum, installed version, plugin API, active PVE
services, global Doctor readiness and the scoped Thin and Thick recovery gates
were verified after installation. Every node returned zero Doctor failures.

This repetition exposed a diagnostic edge case rather than a storage-lifecycle
failure. PVE serializes `disable 1` as a bare `disable` property. Doctor and the
JSON dashboard now recognize both that canonical representation and explicit
boolean forms. Only explicitly disabled storage skips operational probes;
enabled but unavailable storage still fails closed. Disabled FCoE aliases and
their retained unused-volume references were preserved.

A running disposable Linux guest then exercised one materialized Thick root
disk, one additional Thick disk and one conventional Thin disk concurrently.
Distinct 16 MiB canaries were synchronously written to the auxiliary Thick and
Thin devices. The guest migrated online from an API 15 node to another API 15
node and back over the dedicated migration network. Both canaries matched
before migration, on the destination node and after return. The Thin and Thick
recovery gates remained healthy on the destination and source.

The same running mixed-mode guest was then migrated from an API 15 node to the
API 14 node and back. The Thick and Thin canaries again matched on the API 14
destination and after return, and both storage recovery gates remained
`HEALTHY` and safe for mutation. This proves the tested cross-version runtime
path rather than merely proving that the package loads independently on both
Storage API versions.

PVE correctly refused an earlier attempt while the guest configuration still
contained unused references to disabled FCoE storage. The references were
temporarily removed from the disposable VM configuration only after preserving
the exact configuration, and were restored after the guest was stopped. No
FCoE volume was deleted or modified.

```ini
EXPERIMENTAL_PACKAGE_VERSION=0.9.0~rc5.4~tg4
EXACT_PACKAGE_SHA256=716f7c853c8313b56e7c331c8242dd746f9ff5e2b447255fc6118fb72656aba3
PYTHON_REGRESSION=144/144_PASS
PERL_REGRESSION=235/235_PASS
REPRODUCIBLE_PACKAGE=PASS
PACKAGE_CONTENT_AND_PRIVACY_GATE=PASS
API15_INSTALL_AND_REINSTALL=PASS_2_NODES
API14_INSTALL_AND_REINSTALL=PASS_1_NODE
GLOBAL_DOCTOR_FAILURES=0_ALL_NODES
CANONICAL_BARE_DISABLE_PARSING=PASS
ENABLED_UNAVAILABLE_STORAGE_FAIL_CLOSED=YES
THIN_AND_THICK_ONLINE_MIGRATION_ROUND_TRIP=PASS
CROSS_API15_API14_ONLINE_MIGRATION_ROUND_TRIP=PASS
THICK_CANARY_SHA256=PASS_BEFORE_DESTINATION_AFTER_RETURN
THIN_CANARY_SHA256=PASS_BEFORE_DESTINATION_AFTER_RETURN
POST_MIGRATION_RECOVERY_GATES=PASS_BOTH_MODES
DISABLED_FCOE_REFERENCES_RESTORED=PASS
```

## Current-package mixed-mode snapshot rollback and cleanup

A fresh stopped disposable VM was allocated with one one-GiB Thick Generations
disk and one one-GiB conventional Thin disk using the exact experimental
package installed on the cluster. Identical base canaries were flushed to both
devices before a single PVE snapshot covered both storage modes. The active
heads were then overwritten with a distinct canary.

PVE rollback created a new independent Thick generation and used the guarded
Thin replacement flow. After rollback, the first 16 MiB of both devices exactly
matched the original base SHA-256 rather than the post-snapshot mutation. The
snapshot was deleted through PVE, both recovery gates returned `HEALTHY` and
safe for mutation, and VM destruction removed the exact Thick generation,
anchor, Thin volume and per-VM Thin pool. No object containing the disposable
VM identifier remained in either VG.

```ini
MIXED_MODE_DISPOSABLE_VM_CREATE=PASS
MIXED_MODE_SNAPSHOT_CREATE=PASS
THICK_AND_THIN_POST_SNAPSHOT_MUTATION=PASS
MIXED_MODE_ROLLBACK=PASS
THICK_ROLLBACK_CANARY_SHA256=PASS
THIN_ROLLBACK_CANARY_SHA256=PASS
MIXED_MODE_SNAPSHOT_DELETE=PASS
POST_DELETE_RECOVERY_GATES=PASS_BOTH_MODES
MIXED_MODE_VM_DESTROY=PASS
TRANSACTION_SCOPED_LVM_CLEANUP=PASS
```

## Mixed-mode backup, restore and live rolling upgrade

A stopped disposable VM containing one one-GiB Thick Generations disk and one
one-GiB conventional Thin disk was backed up with `vzdump` in snapshot mode.
The VMA archive was sparse and retained distinct flushed 16 MiB canaries on
both disks. After deleting the source VM, `qmrestore` recreated both disks
under a new VM identifier and preserved their intended storage modes. Both
restored canaries matched their source SHA-256 values. The source and restored
VMs, exact LVM objects, archive and log were then removed, and both recovery
gates remained healthy.

A separate running Linux VM with a Thick root disk, an auxiliary Thick disk
and a conventional Thin disk exercised package replacement while QEMU remained
active. The guest completed 600 consecutive cycles; every cycle flushed a root
file and a 4 KiB record on each auxiliary device. The installed package was
first replaced by the earlier `0.9.0~rc5.4~tg1` build and then upgraded to the
exact `0.9.0~rc5.4~tg4` candidate. The original QEMU process identifier existed
unchanged after both package operations. The final root, Thick and Thin records
all contained cycle 600, and both storage recovery gates remained healthy.

Package installation refreshes active PVE management consumers so they load
the new Perl module. It does not restart QEMU, deactivate storage, reload
multipath or replace active device-mapper tables. Consequently a compatible
rolling upgrade can preserve guest I/O, although the management API can be
briefly unavailable. This result does not authorize arbitrary downgrade across
an incompatible on-disk anchor schema. Future upgrades must preserve schema
read compatibility or provide an explicit pre-upgrade gate and migration.

```ini
MIXED_MODE_VZDUMP=PASS
MIXED_MODE_QMRESTORE_NEW_VMID=PASS
THICK_RESTORE_CANARY_SHA256=PASS
THIN_RESTORE_CANARY_SHA256=PASS
BACKUP_RESTORE_EXACT_CLEANUP=PASS
LIVE_PACKAGE_SOURCE_VERSION=0.9.0~rc5.4~tg1
LIVE_PACKAGE_TARGET_VERSION=0.9.0~rc5.4~tg4
QEMU_PID_UNCHANGED_ACROSS_PACKAGE_REPLACEMENT=PASS
GUEST_ROOT_FSYNC_CYCLES=600_PASS
GUEST_THICK_FSYNC_CYCLES=600_PASS
GUEST_THIN_FSYNC_CYCLES=600_PASS
POST_UPGRADE_RECOVERY_GATES=PASS_BOTH_MODES
STORAGE_DEACTIVATION_DURING_UPGRADE=NO
QEMU_RESTART_DURING_UPGRADE=NO
MANAGEMENT_SERVICE_REFRESH_DURING_UPGRADE=YES
ARBITRARY_INCOMPATIBLE_SCHEMA_DOWNGRADE_SUPPORTED=NO
```

The incompatible boundary was subsequently exercised rather than inferred. A
disposable schema-v5 Thick disk was opened by a running QEMU process on one
node. Only that node was temporarily downgraded to the earlier schema-v4
implementation while the other nodes retained the current candidate. A native
snapshot request encountered the unknown schema-v5 `snapshot` anchor field and
failed before any LVM change. The complete VG inventory remained identical,
the existing QEMU PID stayed alive and unchanged, and the disk canary retained
its SHA-256. The node was then restored to the exact tg10 package, its recovery
gate returned mutation-safe, and exact VM deletion returned the VG to baseline.

This proves the intended compatibility split: already-open QEMU block I/O does
not traverse the management plugin after activation, while a node that cannot
decode the authoritative anchor cannot perform a new Thick mutation. The test
does not claim arbitrary downgrade support.

```ini
INCOMPATIBLE_NODE_VERSION=0.9.0~rc5.3.1_SCHEMA_V4
AUTHORITATIVE_ANCHOR_SCHEMA=5
INCOMPATIBLE_THICK_MUTATION_FAIL_CLOSED=PASS
INCOMPATIBLE_THICK_MUTATION_LVM_CHANGE=0
OPEN_QEMU_PID_UNCHANGED=PASS
OPEN_DISK_SHA256_UNCHANGED=PASS
CURRENT_PACKAGE_RESTORED=PASS
POST_RESTORE_RECOVERY_GATE=PASS
INCOMPATIBLE_SCHEMA_EXACT_CLEANUP=PASS
INCOMPATIBLE_SCHEMA_VG_FREE_DELTA=0
```

## tg11 clean-source and rolling installation gate

The final software candidate was built from a clean `git archive`, not the
working tree or private lab evidence. The archive completed 155 Python and 235
Perl tests, all syntax checks, ShellCheck and package-content validation. Two
independent extractions produced byte-identical Debian packages.

The exact package was then installed and reinstalled on both Storage API 15
nodes and the Storage API 14 node. Every node verified the same SHA-256 before
installation. The canonical PVE storage config, complete qualification-VG LVM
inventory and every already-running QEMU PID were identical before and after,
and all three qualification storage aliases returned mutation-safe recovery
results.

The global upgrade preflight also continued to refuse the pre-existing legacy
orphan on the unrelated original lab VG. That object was neither adopted nor
deleted; qualification proceeded only after each isolated disposable alias
independently proved healthy. This is expected fail-closed behavior, not an
installation regression.

```ini
TG11_PYTHON_TESTS=155_PASS
TG11_PERL_TESTS=235_PASS
TG11_SYNTAX_AND_SHELLCHECK=PASS
TG11_PACKAGE_CONTENT_GATE=PASS
TG11_REPRODUCIBLE_BUILD=PASS
TG11_DEB_SHA256=465451b55976e08e000369a7c95f0a478057037b9e6f0f97c1c452ebe11c222c
TG11_INSTALL_API15_NODE_1=PASS
TG11_REINSTALL_API15_NODE_1=PASS
TG11_INSTALL_API15_NODE_2=PASS
TG11_REINSTALL_API15_NODE_2=PASS
TG11_INSTALL_API14_NODE=PASS
TG11_REINSTALL_API14_NODE=PASS
TG11_STORAGE_CONFIG_CHANGE=0
TG11_LVM_INVENTORY_CHANGE=0
TG11_RUNNING_QEMU_PID_CHANGE=0
TG11_POST_INSTALL_RECOVERY_GATES=PASS_ALL_NODES
GLOBAL_PREFLIGHT_LEGACY_ORPHAN_BLOCK=PASS
```

The installed tg11 candidate completed one final native PVE backup/restore
oracle. A disposable VM contained one Thick Generations disk and one
conventional Thin disk on the same pinned VG, each with a distinct flushed
16-MiB canary. Snapshot-mode `vzdump` completed, the source VM was deleted, and
`qmrestore` recreated both disks under a new VMID while preserving their
respective storage aliases. Both restored SHA-256 values matched. Exact cleanup
removed the VM, archive and all transaction objects, returned the VG to its
byte-for-byte baseline, and left both recovery gates mutation-safe.

```ini
TG11_FINAL_VZDUMP=PASS
TG11_FINAL_RESTORE_MIXED_MODES=PASS
TG11_FINAL_THICK_SHA256=PASS
TG11_FINAL_THIN_SHA256=PASS
TG11_FINAL_BACKUP_RESTORE_EXACT_CLEANUP=PASS
TG11_FINAL_BACKUP_RESTORE_VG_FREE_DELTA=0
TG11_FINAL_BACKUP_RESTORE_RECOVERY_GATES=PASS
```

The independent 16-GiB Thin move VG was then retired as a transaction-scoped
disposable asset. Its PVE alias was removed only after the VG was proven empty
and the expected VG, PV and WWID identities matched. The exact VG and PV label,
iSCSI sessions and node records, multipath map, TrueNAS target mapping, extent,
target and zvol were removed. No other target or map was selected. All three
nodes subsequently proved the tg11 package, native three-node quorum, two
healthy paths to the retained primary qualification LUN, mutation-safe Thin
and Thick gates, zero D-state tasks and the exact primary VG free-space
baseline.

```ini
SECONDARY_STORAGE_ALIAS_REMOVED=PASS
SECONDARY_VG_PV_IDENTITY_BEFORE_DELETE=PASS
SECONDARY_VG_PV_REMOVED=PASS
SECONDARY_ISCSI_REMOVED=PASS_ALL_NODES
SECONDARY_MULTIPATH_MAP_REMOVED=PASS_ALL_NODES
SECONDARY_TARGET_MAPPING_EXTENT_TARGET_ZVOL_REMOVED=PASS
TG11_POST_CLEANUP_BASELINE=PASS_ALL_NODES
PRIMARY_PATHS=2_OF_2_ALL_NODES
PRIMARY_RECOVERY_GATES=PASS_ALL_NODES
POST_CLEANUP_DSTATE=0_ALL_NODES
PRIMARY_VG_FREE_BYTES=68711088128
```

The next experimental candidate adds a read-only `sharedlvmthin upgrade-check`
preflight. It inventories the canonical PVE storage configuration and runs the
bounded recovery gate for every enabled SharedLvmThin alias. It requires one
unambiguous healthy, mutation-safe result for each alias and fails closed on a
timeout, duplicate ID, unsupported enabled allocation mode or recovery state.
Disabled aliases are visible but deliberately not probed.

The hardened reproducible `0.9.0~rc5.4~tg6` package was installed one node at a
time on two Storage API 15 nodes and one Storage API 14 node. On every node the
preflight checked the enabled conventional Thin, Thick Generations and
same-VG Thin coexistence aliases, skipped both explicitly disabled lab
transport aliases, and returned `UPGRADE_SAFE=YES`. Package installation and
the existing global operational health gate passed on all three nodes.

```ini
UPGRADE_CHECK_READ_ONLY=YES
UPGRADE_CHECK_ENABLED_STORAGES_PER_NODE=3
UPGRADE_CHECK_DISABLED_STORAGES_SKIPPED_PER_NODE=2
UPGRADE_CHECK_API15_NODE_1=PASS
UPGRADE_CHECK_API15_NODE_2=PASS
UPGRADE_CHECK_API14_NODE=PASS
UPGRADE_CHECK_FAIL_CLOSED_UNIT_CASES=PASS
TG5_SAME_VERSION_REINSTALL_ALL_NODES=PASS
TG6_SIGNAL_AND_TEMPFILE_HARDENING=PASS
TG6_ROLLING_INSTALL_ALL_NODES=PASS
TG6_PYTHON_REGRESSION=149_PASS
TG6_PERL_REGRESSION=235_PASS
TG6_REPRODUCIBLE_DEB=PASS
TG6_DEB_SHA256=ca21702ef95b220b2bf496849d151da46cb0b1835cff92e5c90b41efb2b34c83
```

The preflight was also invoked five times while a running mixed-mode guest
completed 100 consecutive root, Thick and Thin fsync cycles. Every preflight
returned `UPGRADE_SAFE=YES`, the workload completed, and the QEMU process
identifier remained unchanged. Both recovery gates were healthy after the VM
stopped. PVE separately reported deactivation warnings for two pre-existing
`unused` references on an explicitly disabled and unavailable laboratory
transport; those references were preserved as evidence and were not treated
as a preflight or active-storage failure.

```ini
UPGRADE_CHECK_CONCURRENT_INVOCATIONS=5_PASS
UPGRADE_CHECK_CONCURRENT_MIXED_FSYNC_CYCLES=100_PASS
UPGRADE_CHECK_CONCURRENT_QEMU_PID_UNCHANGED=PASS
UPGRADE_CHECK_POST_WORKLOAD_RECOVERY_GATES=PASS_BOTH_MODES
UNRELATED_DISABLED_UNUSED_REFERENCE_MUTATION=NO
```

## Three-node QDevice-unavailable qualification

A temporary external QDevice was configured through the native PVE cluster
tool on the healthy three-node laboratory cluster. PVE required its explicit
dangerous-operation override because an external QDevice is not a recommended
steady-state topology for an odd node count. With QNetd connected, the cluster
reported five expected and five available votes.

QNetd was then stopped while all three PVE nodes remained online. The cluster
retained exactly the three native node votes, quorum remained positive at
three, and the QDevice client reported `Connect failed`. The plugin neither
invented a vote nor applied a private watchdog rule. Bounded recovery checks
for the Thick Generations alias and the same-VG conventional Thin alias both
reported healthy identity, healthy paths, positive native quorum and
`SAFE_FOR_MUTATION=YES`.

One 4 MiB Thick Generations volume and one 4 MiB conventional Thin volume were
allocated and removed while QNetd was unavailable. Exact inventory checks
proved that both transaction-scoped test volumes and the temporary per-VM Thin
pool were absent afterward. QNetd was restarted, all five votes returned, and
the temporary QDevice configuration was removed through the native PVE tool.
The final baseline was the original three-node, three-vote quorate cluster;
both storage recovery checks remained healthy and no D-state process existed.

```ini
THREE_NODE_QDEVICE_CONNECTED_VOTES=5
THREE_NODE_QDEVICE_UNAVAILABLE_NATIVE_VOTES=3
THREE_NODE_QDEVICE_UNAVAILABLE_QUORUM=PASS
QDEVICE_PRIVATE_QUORUM_OVERRIDE=NO
QDEVICE_UNAVAILABLE_THICK_RECOVERY_GATE=PASS
QDEVICE_UNAVAILABLE_THIN_RECOVERY_GATE=PASS
QDEVICE_UNAVAILABLE_THICK_ALLOCATE_DELETE=PASS
QDEVICE_UNAVAILABLE_THIN_ALLOCATE_DELETE=PASS
QDEVICE_UNAVAILABLE_EXACT_CLEANUP=PASS_BOTH_MODES
QDEVICE_REMOVED_AFTER_TEST=PASS
FINAL_CLUSTER_NATIVE_VOTES=3
FINAL_DSTATE=0
```

## TG10 rolling installation and native two-node quorum qualification

The reproducible `0.9.0~rc5.4~tg10` candidate was installed one node at a
time on both Storage API 15 nodes and the Storage API 14 node. The exact
package checksum, canonical PVE storage configuration and the identifiers of
both running QEMU processes were recorded before each installation. All three
nodes finished on the identical package; the configuration digest and QEMU
identifiers remained unchanged. Both same-VG Thin and Thick recovery checks
returned `HEALTHY` and `SAFE_FOR_MUTATION=YES` on every node.

The cluster was also temporarily and explicitly converted to a native
two-node topology without QDevice. At 2/2 votes Doctor emitted the intended
availability warning while allowing identity-verified operations. A complete
same-VG lifecycle returned the VG to its byte-exact free-space baseline. With
one Corosync member stopped, native quorum became 1/2 and both Thin and Thick
allocation attempts failed before mutation; the LVM inventory digest was
unchanged. The third node and original QDevice topology were then restored,
and all three nodes returned to the healthy two-path baseline.

```ini
TG10_PYTHON_REGRESSION=155_PASS
TG10_PERL_REGRESSION=235_PASS
TG10_STATIC_AND_SHELLCHECK=PASS
TG10_REPRODUCIBLE_BUILD=PASS
TG10_DEB_SHA256=5bef479744873e6e5a052ad9612cbe830bca9a2b243f83d5e0d6ba6e2619c7e0
TG10_ROLLING_INSTALL_API15_NODE_1=PASS
TG10_ROLLING_INSTALL_API15_NODE_2=PASS
TG10_ROLLING_INSTALL_API14_NODE=PASS
TG10_STORAGE_CONFIG_UNCHANGED=PASS
TG10_RUNNING_QEMU_PIDS_UNCHANGED=PASS
TG10_RECOVERY_GATES_ALL_NODES=PASS_BOTH_MODES
TWO_NODE_NO_QDEVICE_2_OF_2_DOCTOR=WARN
TWO_NODE_NO_QDEVICE_LIFECYCLE=PASS
TWO_NODE_NO_QDEVICE_1_OF_2_MUTATION=FAIL_CLOSED
TWO_NODE_NO_QDEVICE_FAILED_ATTEMPT_LVM_CHANGE=0
FINAL_THREE_NODE_QDEVICE_BASELINE=PASS
```

The native three-node quorum-loss gate was then exercised separately. There
were no HA-managed resources and fencing was in standby before the test.
Corosync was stopped on nodes two and three without stopping their QEMU
processes or touching storage. Node one reported one available vote of four
and `Quorate: No`. Thin and Thick allocations used fresh VMIDs, failed with
the native PVE lock/quorum result, and produced an identical before/after LVM
inventory digest. No matching LV existed afterward. Both members rejoined,
the QDevice returned, and the final cluster reported four of four votes;
read-only recovery gates were healthy in both modes.

```ini
THREE_NODE_QUORUM_LOSS_NATIVE_VOTES=1_OF_4
THREE_NODE_QUORUM_LOSS_THIN_RC=13
THREE_NODE_QUORUM_LOSS_THICK_RC=13
THREE_NODE_QUORUM_LOSS_LVM_INVENTORY_UNCHANGED=PASS
THREE_NODE_QUORUM_LOSS_NEW_OBJECTS=0
THREE_NODE_QUORUM_REJOIN=PASS_4_OF_4
THREE_NODE_QUORUM_REJOIN_RECOVERY_GATES=PASS_BOTH_MODES
```

## Clean-archive and namespace-isolation gate

The exact tracked tree at commit `078b4b3` was exported with `git archive`,
transferred to the API 14 node and extracted into a new temporary directory.
The first observation stopped after the complete Perl suite because that node
did not yet have the build-only `node` executable used for JavaScript syntax
validation. No test was restarted or called successful on that partial result.
After installing Debian's packaged Node.js test dependency, the complete clean
archive gate passed Python, Perl taint-mode, shell and language syntax, static
analysis, package content/security validation and package construction. Its DEB
was byte-identical to the previously qualified tg10 build.

A live namespace-isolation test then allocated one fresh Thick volume. A second
allocation of the same volume in the same Thick storage namespace failed and
the complete LVM inventory digest remained unchanged. Creating the same PVE
volume name in the conventional Thin alias succeeded by design: the aliases
have distinct ownership namespaces and must temporarily coexist during a
Thin-to-Thick or Thick-to-Thin storage move. Their resolved device paths were
different. Exact deletion of both volumes removed every matching object and
returned the disposable VG to its byte-exact free-space baseline.

The temporary three-node lab topology was also normalized after the QDevice
matrix. The third node had joined without a QDevice client, producing an
asymmetric local vote view. The QDevice was removed with the native PVE command;
Corosync was restarted one node at a time, with quorum checked after each
restart. The final working baseline is three native votes of three, no QDevice
runtime, healthy Thin and Thick recovery gates on every node, two usable iSCSI
paths and zero D-state tasks. The separately recorded two-node-plus-QDevice and
two-node-without-QDevice results remain valid qualification evidence.

```ini
CLEAN_ARCHIVE_GIT_SHA256=94f14b352eb529e912235bad4d3dff62e5925567b2471de54c2b57a09e80210f
CLEAN_ARCHIVE_PYTHON_REGRESSION=155_PASS
CLEAN_ARCHIVE_PERL_REGRESSION=235_PASS
CLEAN_ARCHIVE_STATIC_AND_SYNTAX=PASS
CLEAN_ARCHIVE_PACKAGE_SECURITY=PASS
CLEAN_ARCHIVE_DEB_SHA256=5bef479744873e6e5a052ad9612cbe830bca9a2b243f83d5e0d6ba6e2619c7e0
SAME_NAMESPACE_DUPLICATE_RC=255
SAME_NAMESPACE_DUPLICATE_LVM_CHANGE=0
CROSS_MODE_SAME_NAME_DISTINCT_PATHS=PASS
CROSS_MODE_EXACT_CLEANUP=PASS
CROSS_MODE_VG_FREE_DELTA=0
FINAL_THREE_NODE_NATIVE_QUORUM=PASS_3_OF_3
FINAL_QDEVICE_RUNTIME=ABSENT
FINAL_RECOVERY_GATES=PASS_BOTH_MODES_ALL_NODES
FINAL_DSTATE=0
```

A separate live snapshot-order gate created two fully materialized Thick
snapshots around distinct flushed block patterns. Deleting the older snapshot
while the newer snapshot remained removed only the signed older generation;
the authoritative HEAD retained its exact SHA-256. A request to delete a
nonexistent snapshot failed and the complete LVM inventory digest was
unchanged. Deleting the remaining snapshot and VM removed the exact anchor and
generation tree and returned the disposable VG to its byte-exact free-space
baseline.

```ini
THICK_TWO_SNAPSHOT_MATERIALIZATION=PASS
DELETE_OLDER_WHILE_NEWER_REMAINS=PASS
HEAD_SHA_UNCHANGED_AFTER_OLDER_DELETE=PASS
INVALID_SNAPSHOT_DELETE_RC=255
INVALID_SNAPSHOT_DELETE_LVM_CHANGE=0
SNAPSHOT_ORDER_EXACT_CLEANUP=PASS
SNAPSHOT_ORDER_VG_FREE_DELTA=0
```

The same fresh shared VG then qualified live capacity arithmetic for both
aliases. A one-GiB conventional Thin payload reserved one GiB of pool data plus
eight MiB of thin metadata. Under the configured elastic policy its initial
reserved slack was correctly zero; later burst growth remains separately
guarded by the reserve policy. A one-GiB Thick disk reserved exactly one GiB
for its HEAD plus an eight-MiB anchor. Both aliases reported the same shared-VG
total, used and available values, and PVE's used-space delta equalled the exact
VG free-space delta. Each alias still listed the guest-visible payload as one
GiB. Exact deletion returned every extent.

```ini
THIN_PAYLOAD_BYTES=1073741824
THIN_POOL_DATA_BYTES=1073741824
THIN_METADATA_AND_EXTENT_OVERHEAD_BYTES=8388608
THIN_RESERVED_SLACK_BYTES=0
THICK_HEAD_BYTES=1073741824
THICK_ANCHOR_BYTES=8388608
SHARED_ALIAS_CAPACITY_REPORTING=PASS
PVE_USED_DELTA_EQUALS_VG_FREE_DELTA=PASS
CAPACITY_ARITHMETIC_VG_FREE_DELTA=0
```

## Independent Thin-to-Thin move and snapshot-present conversion gate

A second disposable 16-GiB sparse iSCSI LUN was exported through two target
portals and admitted only after all three nodes proved the same WWID, PV UUID,
VG UUID and two healthy multipath paths. It was registered as a conventional
Thin alias with the same identity and reserve gates as the primary
qualification storage. The iSCSI nodes use explicit `manual` startup policy.

A fresh one-GiB Thin disk containing a flushed deterministic 16-MiB pattern
was moved from the primary Thin VG to the second Thin VG with source deletion.
The destination SHA-256 matched, the source per-VM pool was absent, and PVE
published the destination only after the copy completed. The reverse move
provided the same proofs. Destroying the VM returned both VGs to their exact
pre-test free-space values.

PVE's snapshot-present conversion boundary was then exercised independently.
A fresh Thin disk on the second VG retained one native LVM snapshot. A requested
Thin-to-Thick move with source deletion was refused by PVE before destination
allocation because PVE does not move the snapshot set. The complete VM config
digest, combined two-VG LVM inventory digest and source data SHA-256 were
unchanged. This is the supported fail-closed policy: operators must resolve the
snapshot graph before requesting a destructive mode conversion. After exact
snapshot and VM cleanup, both VGs again returned to baseline.

```ini
SECOND_THIN_LUN_PATHS=2_OF_2_ALL_NODES
SECOND_THIN_IDENTITY=PASS_ALL_NODES
SECOND_THIN_RECOVERY_GATE=HEALTHY_ALL_NODES
ISCSI_NODE_STARTUP_POLICY=MANUAL
THIN_TO_THIN_FORWARD_SHA256=PASS
THIN_TO_THIN_FORWARD_SOURCE_CLEANUP=PASS
THIN_TO_THIN_RETURN_SHA256=PASS
THIN_TO_THIN_RETURN_SOURCE_CLEANUP=PASS
THIN_TO_THIN_SOURCE_VG_FREE_DELTA=0
THIN_TO_THIN_DESTINATION_VG_FREE_DELTA=0
SNAPSHOT_PRESENT_CONVERSION=FAIL_CLOSED
SNAPSHOT_PRESENT_CONVERSION_CONFIG_CHANGE=0
SNAPSHOT_PRESENT_CONVERSION_LVM_CHANGE=0
SNAPSHOT_PRESENT_CONVERSION_SOURCE_SHA256=PASS
SNAPSHOT_PRESENT_CONVERSION_VG_FREE_DELTA=0_BOTH_VGS
```

The remaining iSCSI single-path/materialization combination used a fresh
four-GiB Thick disk with a flushed 64-MiB canary. A target-side transient timer
was positively armed before one iSCSI interface was disabled. A passive host
sample captured exactly one healthy path while the signed anchor remained in
`HYDRATING`; the path returned automatically and every subsequent sample saw
two healthy paths while the same transaction continued. Hydration completed,
the frontend pivoted to its destination-only linear table, the recovery gate
returned healthy and the authoritative HEAD retained the exact pre-fault
SHA-256. Snapshot and VM deletion removed the complete transaction tree and
returned the VG to its exact baseline.

The stopped-VM `qm snapshot` command waits for its storage callback, so a
post-command prepare check cannot itself observe the in-flight phase. The
qualification therefore records the independent passive sample taken while
that exact command and transaction were still live; it does not relabel the
later command return as evidence of the fault interval.

```ini
MATERIALIZATION_SINGLE_PATH_PREFAULT_PHASE=HYDRATING
MATERIALIZATION_SINGLE_PATH_MIN_HEALTHY_PATHS=1
MATERIALIZATION_SINGLE_PATH_TRANSACTION_CONTINUED=PASS
MATERIALIZATION_SINGLE_PATH_RETURN_2_OF_2=PASS
MATERIALIZATION_SINGLE_PATH_LINEAR_PIVOT=PASS
MATERIALIZATION_SINGLE_PATH_RECOVERY_GATE=PASS
MATERIALIZATION_SINGLE_PATH_HEAD_SHA256=PASS
MATERIALIZATION_SINGLE_PATH_VG_FREE_DELTA=0
```

The conventional Thin model was also exercised through PVE's native clone
lifecycle on the independent qualification VG. A stopped full clone copied a
flushed 64-MiB deterministic pattern into a distinct per-VM Thin pool. The
destination SHA-256 matched, the source remained unchanged, and exact deletion
returned the VG to its byte-for-byte free-space baseline.

Linked cloning is deliberately not advertised by SharedLvmThin. A disposable
Thin source was converted to a PVE template and a native linked-clone request
was issued without a destination-storage override. PVE refused it with
`Linked clone feature is not supported` before any LVM change. The source hash
and complete LVM inventory were unchanged, no target config was published, and
template cleanup returned the VG to baseline. This is an explicitly qualified
unsupported boundary, not an untested lifecycle.

```ini
THIN_FULL_CLONE_SOURCE_UNCHANGED=PASS
THIN_FULL_CLONE_DESTINATION_SHA256=PASS
THIN_FULL_CLONE_DISTINCT_VOLUME=PASS
THIN_FULL_CLONE_EXACT_CLEANUP=PASS
THIN_FULL_CLONE_VG_FREE_DELTA=0
THIN_LINKED_CLONE_SUPPORTED=NO
THIN_LINKED_CLONE_FAIL_CLOSED=PASS
THIN_LINKED_CLONE_LVM_CHANGE=0
THIN_LINKED_CLONE_SOURCE_SHA256=PASS
THIN_LINKED_CLONE_EXACT_CLEANUP=PASS
THIN_LINKED_CLONE_VG_FREE_DELTA=0
```

The production-relevant two-node plus QDevice topology was qualified with a
separate monotonic Corosync configuration. At two nodes plus QDevice the
cluster reported three of three votes and both modes completed exact
allocate/free cycles. With both QDevice clients stopped, two native votes
retained quorum and both cycles again passed. After restoring QDevice, node
two was stopped: node one plus QDevice retained two of three votes and both
modes again passed. Removing QDevice from that survivor produced one of three
votes and native loss of quorum; both fresh allocation attempts failed with
no change to the complete LVM inventory digest.

The second node, third node and QDevice were restored. A stale offline
Corosync configuration was updated through local pmxcfs using the exact
syntax-validated rollback file; this did not touch storage or VM processes.
The final topology reported three nodes, four of four votes and QDevice, and
both storage recovery gates were healthy.

```ini
TWO_NODE_QDEVICE_NORMAL=PASS_3_OF_3
TWO_NODE_QDEVICE_NORMAL_DUAL_MODE_LIFECYCLE=PASS
TWO_NODE_QDEVICE_LOSS=PASS_2_OF_3
TWO_NODE_QDEVICE_LOSS_DUAL_MODE_LIFECYCLE=PASS
TWO_NODE_QDEVICE_SURVIVOR=PASS_2_OF_3
TWO_NODE_QDEVICE_SURVIVOR_DUAL_MODE_LIFECYCLE=PASS
TWO_NODE_QDEVICE_SURVIVOR_WITHOUT_QDEVICE=FAIL_CLOSED_1_OF_3
TWO_NODE_QDEVICE_SURVIVOR_FAILED_ATTEMPT_LVM_CHANGE=0
TWO_NODE_QDEVICE_EXACT_VG_FREE_DELTA=0
TWO_NODE_QDEVICE_FINAL_REJOIN=PASS_4_OF_4
TWO_NODE_QDEVICE_FINAL_RECOVERY_GATES=PASS_BOTH_MODES
```

## Repeated endurance infrastructure failure

A new four-hour dual-mode endurance attempt began only after both storage
recovery gates were healthy. A ten-second preflight completed eight verified
write, fsync and direct-read SHA-256 cycles on both the Thick and conventional
Thin guest disks. A separate ESXi counter monitor pinned the local SATA device
that hosts every nested laboratory component and sampled its error counters
every 30 seconds.

The full run reproduced the lower-layer datastore fault within minutes. The
local SATA device increased from 28 to 30 failed commands and from 20 to 22
failed write operations. ESXi logged AHCI command aborts, H:0x5/D:0x22 write
failures, COMRESET and VMFS heartbeat timeouts for that exact device. PVE01
briefly lost both virtual iSCSI paths, its bounded Thick recovery probe failed
closed, and the nested host then rebooted without a clean shutdown. The guest
remained stopped with a persistent `RUNNING` marker. Seven durable verified
cycles for each disk survived in its log.

After PVE01 rejoined, the original three-node cluster was quorate, both
multipath maps had two healthy paths, both storage identities matched, and the
Thin and Thick recovery gates returned `HEALTHY` and
`SAFE_FOR_MUTATION=YES`. No plugin repair or speculative cleanup occurred.
Guest evidence was collected through a read-only mount with journal replay
disabled, and every temporary partition map and LV activation was removed.

This is a second independently observed failure of the ESXi local datastore
path and is not a plugin endurance result. No further load or fault test is
permitted on that datastore. The endurance gate remains open until the same
candidate completes on qualified infrastructure.

```ini
DUAL_MODE_REPEAT_PREFLIGHT_THIN_CYCLES=8_PASS
DUAL_MODE_REPEAT_PREFLIGHT_THICK_CYCLES=8_PASS
DUAL_MODE_REPEAT_DURABLE_THIN_CYCLES=7_PASS
DUAL_MODE_REPEAT_DURABLE_THICK_CYCLES=7_PASS
ESXI_LOCAL_FAILED_COMMANDS_DELTA=2
ESXI_LOCAL_FAILED_WRITE_OPERATIONS_DELTA=2
ESXI_LOCAL_FAILED_BLOCKS_DELTA=0
NESTED_PVE_UNCLEAN_REBOOT=YES
POST_REBOOT_CLUSTER_QUORUM=PASS
POST_REBOOT_MULTIPATH=2_OF_2_BOTH_LUNS
POST_REBOOT_RECOVERY_GATES=PASS_BOTH_MODES
PLUGIN_SPECULATIVE_RECOVERY=NO
RESULT=INFRASTRUCTURE_FAIL_ESXI_LOCAL_DATASTORE_PATH
DUAL_MODE_ENDURANCE_QUALIFICATION=OPEN
FURTHER_TESTS_ON_FAILED_DATASTORE=PROHIBITED
```

## Clean TG12 dual-mode endurance retest

After installing and reinstalling the exact reproducible TG12 candidate on all
three qualified nodes, the Windows PowerShell 5.1 worker was corrected to avoid
.NET Core-only APIs. A transaction-scoped short probe performed write-through
writes, explicit flush, reopened reads and SHA-256 verification on both the Thin
and Thick guest disks. Both modes passed and no worker remained alive.

The clean endurance run started with guest run ID
`8cf1117b953e44e2b3c583e80b121190`. Both independent workers use a monotonic
Stopwatch and are scheduled for 15,300 seconds. The correlated host run ID is
`8cf1117b953e44e2b3c583e80b121190-host-v2`; its three scheduled durations are
15,508, 15,506 and 15,505 seconds. Each exceeds the strict 14,400-second gate
and extends five minutes beyond the guest deadline.

Before host monitoring started, both guest logs were observed advancing, both
recorded worker PIDs were live and all redirected stderr files were empty. The
first two host iterations on all three nodes reported native quorum, exactly two
healthy paths for the pinned WWID, zero D-state tasks, the exact TG12 package,
healthy Thin and Thick recovery gates and sticky overall PASS.

This entry records only a running qualification. It is not a PASS until both
guest result files and all three terminal host results satisfy the strict
classifier, archives and checksums are collected, and transaction-scoped
cleanup restores the saved baseline.

```ini
TG12_WORKER_COMPATIBILITY_PROBE=PASS_BOTH_MODES
TG12_GUEST_RUN_ID=8cf1117b953e44e2b3c583e80b121190
TG12_HOST_RUN_ID=8cf1117b953e44e2b3c583e80b121190-host-v2
TG12_HOST_MINIMUM_DURATION_SECONDS=14400
TG12_INITIAL_HOST_ITERATIONS=PASS_ALL_THREE_NODES
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING
```

## Transaction-isolated storage moves during clean TG12 endurance

While the independent Windows Thin and Thick write-through workers and all
three host monitors remained live, a separate disposable VM 999952 exercised
both storage-conversion directions. The harness admitted mutation only after
positive Thin and Thick recovery checks, wrote and flushed a 16 MiB data
canary, and verified the same SHA-256 after each move. PVE removed each source
only after the corresponding destination copy completed successfully.

The final transaction-scoped cleanup removed the disposable VM, Thin pool,
Thin LV, Thick anchor and generation. The VG free-byte value returned exactly
to its saved baseline. A separate postcondition at `2026-09-11T17:21:31Z`
found no VM or LV containing VMID 999952; both storage aliases retained the
same WWID, PV UUID and VG UUID, two healthy paths, native quorum,
`STATE=HEALTHY` and `SAFE_FOR_MUTATION=YES`. The Thick and Thin guest worker
PIDs and the PVE03 host monitor were still live after cleanup.

```ini
TG12_ENDURANCE_THIN_TO_THICK_SHA256=PASS
TG12_ENDURANCE_THICK_TO_THIN_SHA256=PASS
TG12_ENDURANCE_SOURCE_DELETE_AFTER_COPY=PASS
TG12_ENDURANCE_CROSS_MODE_CLEANUP=PASS
TG12_ENDURANCE_VG_FREE_DELTA_BYTES=0
TG12_ENDURANCE_VM_999952_LEFTOVER=NONE
TG12_ENDURANCE_LV_999952_LEFTOVER=NONE
TG12_ENDURANCE_POSTCHECK_RECOVERY_GATES=PASS_BOTH_MODES
TG12_ENDURANCE_WORKERS_AFTER_MOVE=LIVE
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING
```

## Transaction-isolated Thick snapshot lifecycle during clean endurance

A second disposable transaction on VM 999966 ran concurrently with the same
independent endurance workers. It created and fully materialized two Thick
snapshots around a flushed data change, deleted the older snapshot while the
newer one remained, and proved that the authoritative HEAD SHA-256 did not
change. An intentionally invalid snapshot name returned non-zero while the
complete LVM inventory digest remained byte-identical.

Deleting the remaining snapshot and VM returned VG free bytes to the exact
saved baseline. A separate postcondition at `2026-09-11T17:26:47Z` found no VM
or LV containing VMID 999966. Both storage recovery gates remained healthy
with matching WWID/PV/VG identities, two paths and quorum. Both Windows worker
PIDs and the PVE01 host monitor remained live, and their verified guest cycles
continued increasing after the snapshot transaction.

```ini
TG12_ENDURANCE_THICK_SNAPSHOT_MATERIALIZATION=PASS_TWO_GENERATIONS
TG12_ENDURANCE_DELETE_OLDER_WITH_NEWER_PRESENT=PASS
TG12_ENDURANCE_HEAD_SHA_AFTER_DELETE=UNCHANGED
TG12_ENDURANCE_INVALID_DELETE_RC=255
TG12_ENDURANCE_INVALID_DELETE_LVM_CHANGE=0
TG12_ENDURANCE_SNAPSHOT_CLEANUP=PASS
TG12_ENDURANCE_SNAPSHOT_VG_FREE_DELTA_BYTES=0
TG12_ENDURANCE_VM_999966_LEFTOVER=NONE
TG12_ENDURANCE_LV_999966_LEFTOVER=NONE
TG12_ENDURANCE_POST_SNAPSHOT_RECOVERY_GATES=PASS_BOTH_MODES
TG12_ENDURANCE_WORKERS_AFTER_SNAPSHOT=LIVE
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING
```

## Transaction-isolated dual-mode resize during clean endurance

A ShellCheck-clean bounded harness used disposable VMIDs 999953 and 999954 to
resize one conventional Thin and one Thick Generations disk from exactly
1 GiB to 2 GiB while the independent endurance workloads continued. Each disk
received a flushed 16 MiB pattern before resize. Its exact SHA-256 remained
unchanged after resize and the block device exposed the expected larger byte
count.

Transaction cleanup returned VG free bytes to the saved baseline. A separate
postcondition at `2026-09-11T17:30:38Z` found neither disposable VM nor any
matching LV/tag. Both storage identities and recovery gates remained healthy,
the PVE03 host monitor was live, and both Windows workers continued producing
verified cycles after the resize operations.

```ini
TG12_ENDURANCE_THIN_RESIZE_OLD_BYTES=1073741824
TG12_ENDURANCE_THIN_RESIZE_NEW_BYTES=2147483648
TG12_ENDURANCE_THIN_RESIZE_SHA256=PASS
TG12_ENDURANCE_THICK_RESIZE_OLD_BYTES=1073741824
TG12_ENDURANCE_THICK_RESIZE_NEW_BYTES=2147483648
TG12_ENDURANCE_THICK_RESIZE_SHA256=PASS
TG12_ENDURANCE_DUAL_RESIZE_CLEANUP=PASS
TG12_ENDURANCE_DUAL_RESIZE_VG_FREE_DELTA_BYTES=0
TG12_ENDURANCE_RESIZE_LEFTOVERS=NONE
TG12_ENDURANCE_POST_RESIZE_RECOVERY_GATES=PASS_BOTH_MODES
TG12_ENDURANCE_WORKERS_AFTER_RESIZE=LIVE
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING
```

## Transaction-isolated native backup and restore during clean endurance

After a positive preflight proved that VMIDs 999954/999955, their LV
namespaces and the exact dump directory did not exist, a ShellCheck-clean
bounded harness created a mixed disposable VM with one Thin and one Thick
disk. It wrote and flushed distinct 16 MiB data canaries, completed native PVE
snapshot-mode `vzdump`, destroyed the source, and restored the archive to the
second VMID. The restored configuration retained the intended storage mode of
each disk and both restored SHA-256 values matched their sources.

Cleanup removed both VMs, all corresponding LVs and the exact dump directory,
returned VG free bytes to the saved baseline and left both recovery gates
healthy. An independent postcondition at `2026-09-11T17:35:50Z` confirmed the
absence of all scoped artefacts. Both Windows workers and the PVE01 host
monitor remained live, with verified guest cycles continuing after restore.

```ini
TG12_ENDURANCE_NATIVE_VZDUMP_SNAPSHOT_MODE=PASS
TG12_ENDURANCE_NATIVE_RESTORE_MIXED_MODES=PASS
TG12_ENDURANCE_RESTORED_THICK_SHA256=PASS
TG12_ENDURANCE_RESTORED_THIN_SHA256=PASS
TG12_ENDURANCE_BACKUP_RESTORE_CLEANUP=PASS
TG12_ENDURANCE_BACKUP_RESTORE_VG_FREE_DELTA_BYTES=0
TG12_ENDURANCE_BACKUP_RESTORE_LEFTOVERS=NONE
TG12_ENDURANCE_POST_RESTORE_RECOVERY_GATES=PASS_BOTH_MODES
TG12_ENDURANCE_WORKERS_AFTER_RESTORE=LIVE
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING
```

## Live cross-node migration during clean endurance

VM 100 was confirmed running on PVE02, outside HA management and with every
disk on shared storage. PVE01 was online and healthy. Native PVE online
migration used the dedicated cluster migration network, transferred
4.5 GiB of VM state, completed in 80 seconds and reported 87 ms downtime.

An independent postcondition at `2026-09-11T17:38:45Z` found VM 100 running
only on PVE01 with a new QEMU PID, absent from PVE02/PVE03, and without a stale
migration lock. The original Windows endurance process IDs 9580 and 12048
remained alive and both verified logs advanced beyond the pre-migration
cycles. All three exact host monitor PIDs remained live. Thin and Thick
recovery checks on the destination retained two healthy paths, matching
WWID/PV/VG identities, quorum, `STATE=HEALTHY` and
`SAFE_FOR_MUTATION=YES`.

The migration emitted the expected informational warning that conntrack-state
migration was unavailable; this qualification VM did not depend on preserving
a network session, and storage/data workers continued normally.

```ini
TG12_ENDURANCE_LIVE_MIGRATION=PVE02_TO_PVE01_PASS
TG12_ENDURANCE_MIGRATION_DURATION_SECONDS=80
TG12_ENDURANCE_MIGRATION_DOWNTIME_MILLISECONDS=87
TG12_ENDURANCE_SOURCE_QEMU_PID=224581
TG12_ENDURANCE_TARGET_QEMU_PID=173961
TG12_ENDURANCE_VM_SINGLE_OWNER_AFTER_MIGRATION=PASS
TG12_ENDURANCE_ORIGINAL_WINDOWS_PIDS_SURVIVED=PASS
TG12_ENDURANCE_GUEST_CYCLES_AFTER_MIGRATION=PASS
TG12_ENDURANCE_HOST_MONITORS_AFTER_MIGRATION=PASS_ALL_THREE
TG12_ENDURANCE_POST_MIGRATION_RECOVERY_GATES=PASS_BOTH_MODES
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING
```

## Bounded source-node refusal after endurance live migration

The next scheduled PVE02 host observation began while the completed live
migration was tearing down the former source. It captured the single
`pvestatd` process in `D:path_openat`; the state remained present on the
harness's two-second confirmation. Thin and Thick recovery checks still
proved healthy paths, exact WWID/PV/VG identities, pool flags, bounded LVM
probes, PVE storage health and quorum. They nevertheless returned
`NO_RELEVANT_DSTATE=UNKNOWN`, `STATE=RECOVERY_REQUIRED` and
`SAFE_FOR_MUTATION=NO`, because positive attribution of the global D-state to
an unrelated dependency was unavailable. This is the intended DS-16
fail-closed result.

At `2026-09-11T17:41:19Z`, `pvestatd` had returned to normal sleep, no D-state
task remained, and both recovery checks again returned `HEALTHY` and
`SAFE_FOR_MUTATION=YES`. Kernel logs in the interval contained only ordinary
source tap/firewall teardown. They contained no multipath, SCSI, LVM-thin or
device error. PVE logged one 30.171-second and one 5.159-second status update
during the migration interval.

The host monitor deliberately uses a sticky failure. Therefore this run can
no longer satisfy the strict uninterrupted endurance PASS even though data
workloads remain live and storage health recovered without intervention. It
must continue to its scheduled terminal time so its exact negative evidence
can be collected; it must not be restarted or reclassified opportunistically.

```ini
TG12_ENDURANCE_PVE02_ITERATION_19=FAIL_CLOSED
TG12_ENDURANCE_CAPTURED_DSTATE_PROCESS=pvestatd
TG12_ENDURANCE_CAPTURED_DSTATE_WCHAN=path_openat
TG12_ENDURANCE_CAPTURED_DSTATE_CONFIRMATION_SECONDS=2
TG12_ENDURANCE_STORAGE_IDENTITY_DURING_EVENT=PASS
TG12_ENDURANCE_BOUNDED_LVM_PROBES_DURING_EVENT=PASS
TG12_ENDURANCE_RECOVERY_GATE_DURING_EVENT=RECOVERY_REQUIRED
TG12_ENDURANCE_MUTATION_DURING_UNKNOWN_DSTATE=BLOCKED
TG12_ENDURANCE_CURRENT_DSTATE=0
TG12_ENDURANCE_RECOVERY_WITHOUT_INTERVENTION=PASS_BOTH_MODES
TG12_ENDURANCE_STRICT_PASS_POSSIBLE=NO
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING_NEGATIVE_EVIDENCE
```

## Current-tree package and regression checkpoint during endurance

The combined Thin/Thick Debian package description now uses a valid Debian
continuation field and contains no line longer than 79 characters. An isolated
current-tree validation on the Storage API 14 node passed all 156 Python tests,
all 236 Perl tests, shell syntax and ShellCheck. `dpkg-deb` accepted the package
metadata and reproduced the intended dependency list and both-mode
description. The archive transfer from Windows did not preserve executable
modes, so those modes were restored only inside the disposable validation
directory before the package parser check; this does not alter installed files
or the running endurance candidate.

```ini
TG12_CURRENT_TREE_PYTHON_REGRESSION=156_PASS
TG12_CURRENT_TREE_PERL_REGRESSION=236_PASS
TG12_CURRENT_TREE_SHELL_AND_SHELLCHECK=PASS
TG12_DEBIAN_CONTROL_PARSE=PASS
TG12_DEBIAN_CONTROL_MAX_LINE_LENGTH=79_OR_LESS
FINAL_CLEAN_COMMIT_BUILD=STILL_REQUIRED
DUAL_MODE_ENDURANCE_QUALIFICATION=RUNNING_NEGATIVE_EVIDENCE
```

## Veeam supported restore topology

The current official Veeam Backup & Replication 13 Proxmox restore guide
confirms that an entire-VM restore to a new location exposes target storage in
the supported console wizard. It also states that storage and disk type cannot
be selected separately for each VM disk. The qualification matrix therefore
matches the product contract: single-mode backups can be restored to either
the Thin or Thick alias, while a mixed backup is restored wholly to Thin or
wholly to Thick. A per-disk mixed target is not claimed or emulated through an
internal API.

Reference: <https://helpcenter.veeam.com/docs/vbr/userguide/pve_restore_entire_vm_storage.html>

```ini
VEEAM_SUPPORTED_UI_TARGET_STORAGE_SELECTION=CONFIRMED
VEEAM_PER_DISK_TARGET_STORAGE_SELECTION=NOT_AVAILABLE_BY_PRODUCT
VEEAM_SINGLE_MODE_CROSS_RESTORE=REQUIRED
VEEAM_MIXED_TO_ALL_THIN_OR_ALL_THICK=REQUIRED
```

Before any restore was created, the transaction-scoped cleanup harness was
executed as a negative test. It found the saved preflight state but no complete
restore-evidence directory, exited with code 2 before its recovery checks or
destroy loop, and left the full qualification-VG inventory digest unchanged.
Therefore an incomplete matrix is preserved rather than guessed or cleaned.

```ini
VEEAM_INCOMPLETE_EVIDENCE_CLEANUP=REFUSED
VEEAM_NEGATIVE_CLEANUP_EXIT_CODE=2
VEEAM_NEGATIVE_CLEANUP_LVM_CHANGE=0
```

## Terminal classification of the lifecycle-overlapped endurance run

The lifecycle-overlapped run was allowed to reach its original deadline and
was never restarted or opportunistically reclassified. Both Windows workers
completed 15,300 requested seconds using a monotonic clock. Thin completed
3,742 write-through, flush, reopened-read and SHA-256 cycles; Thick completed
3,745. Their longest progress gaps were 22.452 and 20.605 seconds and both
terminal results were `PASS`.

Each host monitor completed 52 iterations and more than 15,500 seconds. PVE03
passed every iteration. PVE01 retained one recovery-check failure caused by
an unscoped D-state observation. PVE02 retained two recovery-check failures,
including the persistent two-second `pvestatd` sample after live migration.
Both nodes later returned to healthy iterations without intervention, but the
sticky classifier correctly kept their terminal results at `FAIL`. No node
recorded a path-count failure, quorum loss, boot-ID change or package-version
change.

All four evidence archives were copied locally and independently SHA-256
verified before guest cleanup. The first guarded cleanup attempt exposed a
Windows CRLF parsing defect in the private qualification helper and refused
without deleting evidence. The helper now accepts LF and CRLF while retaining
exact run-ID and PID matching. A four-case regression covers both line endings,
a live PID and a wrong run ID. Cleanup then succeeded for only the exact
archived run.

```ini
TG12_NEGATIVE_GUEST_THIN_CYCLES=3742_PASS
TG12_NEGATIVE_GUEST_THICK_CYCLES=3745_PASS
TG12_NEGATIVE_PVE01=FAIL_ONE_PROBE
TG12_NEGATIVE_PVE02=FAIL_TWO_PROBES_ONE_PERSISTENT_DSTATE
TG12_NEGATIVE_PVE03=PASS
TG12_NEGATIVE_PATH_FAILURES=0
TG12_NEGATIVE_QUORUM_FAILURES=0
TG12_NEGATIVE_BOOT_CHANGES=0
TG12_NEGATIVE_CLASSIFIER=FAIL_AS_DESIGNED
TG12_NEGATIVE_EVIDENCE_ARCHIVED=PASS
TG12_CLEANUP_CRLF_REGRESSION=PASS
TG12_NEGATIVE_GUEST_CLEANUP=PASS_AFTER_ARCHIVE
```

## Isolated clean TG12 endurance run

A new run began only after the negative evidence was archived, checksummed and
the exact prior guest workspace was removed by its transaction-scoped guard.
Run `78bc8aceb1d141c6a7826db4ed3bc54e` performs the same independent Thin and
Thick Windows write-through workload for 15,300 seconds. Correlated host run
`78bc8aceb1d141c6a7826db4ed3bc54e-host-clean` runs on all three nodes for more
than 15,530 seconds. No snapshot, resize, migration, backup, restore or other
lifecycle mutation is permitted during this isolated retest.

The run completed without a lifecycle mutation. The Thin worker completed
4,045 contiguous cycles in 15,302 seconds with a maximum progress gap of
67.637 seconds. The Thick worker completed 4,167 contiguous cycles in 15,303
seconds with a maximum progress gap of 5.588 seconds. Both terminal results
were canonical `PASS`; redirected stdout and stderr remained empty.

Each host completed 52 iterations. PVE01, PVE02 and PVE03 respectively ran for
15,542, 15,540 and 15,536 monotonic seconds. Every node retained quorum,
exactly two paths and package `0.9.0~rc5.4~tg12`; there were zero failed
recovery probes, persistent D-state samples, path failures, quorum failures or
boot changes. Short global D-state observations disappeared before the
two-second persistence check and therefore did not satisfy the predeclared
failure condition. All four archives were checksummed on their source system,
downloaded and verified locally before extraction. The strict classifier
returned terminal `overall=PASS`.

```ini
TG12_CLEAN_GUEST_RUN_ID=78bc8aceb1d141c6a7826db4ed3bc54e
TG12_CLEAN_HOST_RUN_ID=78bc8aceb1d141c6a7826db4ed3bc54e-host-clean
TG12_CLEAN_NO_LIFECYCLE_MUTATIONS=ENFORCED_BY_TEST_PLAN
TG12_CLEAN_GUEST_THIN=PASS_4045_CYCLES_15302_SECONDS
TG12_CLEAN_GUEST_THICK=PASS_4167_CYCLES_15303_SECONDS
TG12_CLEAN_PVE01=PASS_52_ITERATIONS_15542_SECONDS
TG12_CLEAN_PVE02=PASS_52_ITERATIONS_15540_SECONDS
TG12_CLEAN_PVE03=PASS_52_ITERATIONS_15536_SECONDS
TG12_CLEAN_STRICT_CLASSIFIER=PASS
DUAL_MODE_ENDURANCE_QUALIFICATION=PASS
```

## Pre-final release gate after clean endurance

Exact commit `bbae95b` was exported with `git archive` and SHA-256 verified on
the API 14 node before execution. The complete Linux release gate passed: 156
Python unit tests, 236 Perl tests, shell syntax, ShellCheck, and Python bytecode
compilation. Two builds in independently extracted source directories were
byte-identical with SHA-256
`9bcc85efbec07fa3ad099b3bdd553b74fa99946e4e295cbedd18e8968464f2b7`.

This is a pre-final checkpoint. It must be repeated after the supported Veeam
restore matrix is either completed and documented or explicitly retained as
an unqualified external integration boundary.

The read-only cluster inventory showed quorum 3/3, no D-state, no scoped TG12
test VMIDs or LVs, and both `slt-tg-thin` and `slt-tg-thick` healthy on every
node. The older `sharedthin-test` alias correctly remains
`RECOVERY_REQUIRED`: owned `vm-203-disk-0` has no PVE reference. That object is
outside the TG12 transaction scope and is intentionally preserved rather than
being treated as disposable cleanup.

```ini
PRE_FINAL_COMMIT=bbae95b
PRE_FINAL_FULL_REGRESSION=PASS
PRE_FINAL_PYTHON_TESTS=156
PRE_FINAL_PERL_TESTS=236
PRE_FINAL_REPRODUCIBLE_BUILD=PASS
PRE_FINAL_DEB_SHA256=9bcc85efbec07fa3ad099b3bdd553b74fa99946e4e295cbedd18e8968464f2b7
TG12_SCOPED_LEFTOVERS=0
PREEXISTING_UNREFERENCED_OBJECT=PRESERVED_FAIL_CLOSED
```

The same reproducible candidate DEB was then installed twice consecutively on
each cluster member. PVE03 exercised Storage API 14; PVE01 and PVE02 exercised
Storage API 15. Every node preserved the exact storage configuration digest,
LVM inventory digest, running-VM set, web configuration, TLS files, service
enablement and web health. Both Thick Generations recovery checks remained
healthy and the pre-existing unreferenced Thin object remained fail-closed.

```ini
PRE_FINAL_THREE_NODE_INSTALL=PASS
PRE_FINAL_THREE_NODE_REINSTALL=PASS
PVE03_STORAGE_API=14_PASS
PVE01_PVE02_STORAGE_API=15_PASS
INSTALLED_DEB_SHA256=9bcc85efbec07fa3ad099b3bdd553b74fa99946e4e295cbedd18e8968464f2b7
PACKAGE_INSTALL_STATE_DRIFT=0
```

## Supported Veeam cross-mode restore qualification

Veeam Backup & Replication restored all six planned cases through its
supported Proxmox desktop-console workflow. Veeam selected free destination
VMIDs `107..112`; exact names, stopped state and storage placement were
verified rather than assuming the planning VMIDs:

- Thin source to Thin and Thick;
- Thick source to Thick and Thin;
- mixed Thin/Thick source to all-Thin and all-Thick destinations.

The eight destination disks matched the four saved source prefixes by
SHA-256. The three source VMs and all four source disks were independently
rehash-verified after restore. Both storage recovery gates passed before and
after verification. Evidence was archived and locally SHA-256 verified before
cleanup.

The cleanup helper accepted the six observed VMIDs only as explicit
parameters and additionally required their exact names, stopped state, disk
count, target storage and PASS evidence. It removed only those restored VMs,
proved zero PVE/LVM references, and restored the exact LVM inventory and VG
free-byte baseline. The disposable source transaction then restored the
original `VEEAMTEST` membership, reverified source data and removed only VMIDs
`999949..999951`.

```ini
VEEAM_THIN_TO_THIN_RESTORE=PASS
VEEAM_THIN_TO_THICK_RESTORE=PASS
VEEAM_THICK_TO_THICK_RESTORE=PASS
VEEAM_THICK_TO_THIN_RESTORE=PASS
VEEAM_MIXED_TO_ALL_THIN_RESTORE=PASS
VEEAM_MIXED_TO_ALL_THICK_RESTORE=PASS
VEEAM_DESTINATION_DISK_SHA_COUNT=8_PASS
VEEAM_POST_RESTORE_SOURCE_SHA_COUNT=4_PASS
VEEAM_RESTORE_EXACT_CLEANUP=PASS
VEEAM_LVM_BASELINE_RESTORED=PASS
VEEAM_VG_FREE_DELTA=0
VEEAM_SOURCE_SELECTION_RESTORED=PASS
VEEAM_FINAL_EVIDENCE_SHA256=ff75f5360d0d8637abf2e98b453698d50738d18747396004d9c37555a580a78b
```

## Post-qualification cluster baseline

After Veeam evidence had been copied and checksum-verified, both restored and
source transactions were cleaned using exact name, VMID, ownership, disk-count
and storage gates. A final read-only inventory was then collected independently
from all three nodes. Every node retained quorum 3/3, zero D-state, healthy
Thin and Thick qualification storage, zero scoped test VMIDs and zero scoped
test LVs. All observed multipath paths were active.

The final package payload is unchanged from the candidate already subjected to
two consecutive install operations on Storage API 14 and 15. A fresh export
of accepted qualification commit `8622dab` passed the full Linux regression
and produced two
byte-identical packages with the same installed SHA-256.

The unrelated older `sharedthin-test` object `vm-203-disk-0` remains
fail-closed and preserved. Its explicit `RECOVERY_REQUIRED` state is not
reclassified as a qualification failure and is not hidden from the baseline.

```ini
POST_QUALIFICATION_QUORUM=3_OF_3
POST_QUALIFICATION_DSTATE=0_ALL_NODES
POST_QUALIFICATION_TG_STORAGE=HEALTHY_ALL_NODES
POST_QUALIFICATION_TEST_VMIDS=0
POST_QUALIFICATION_TEST_LVS=0
POST_QUALIFICATION_MULTIPATH=ACTIVE
FINAL_CODE_CHECKPOINT=8622dab
FINAL_CODE_REGRESSION=PASS
FINAL_CODE_REPRODUCIBLE_BUILD=PASS
FINAL_CODE_DEB_SHA256=9bcc85efbec07fa3ad099b3bdd553b74fa99946e4e295cbedd18e8968464f2b7
```
