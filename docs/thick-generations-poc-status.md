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
generation and left the recovered HEAD healthy.

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
FINAL_FRONTEND_LINEAR=PASS
POST_TEST_DSTATE=0
```

## Open gates

1. Repeat snapshot, delete, rollback, and resize with data-bearing active-QEMU
   workloads, then complete any recovery paths exposed by those tests.
2. Qualify full-hydration metadata occupancy and geometry performance.
3. Qualify physical FC/FCoE path loss and active-guest application outcomes.
4. Run Linux and Windows data-integrity workloads and a long-duration soak.
