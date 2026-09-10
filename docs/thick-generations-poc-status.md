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

## Online storage-move cancellation and network qualification

- A bounded online thick-to-thin move completed at 60 MiB/s after replacing
  emulated high-throughput lab NICs with paravirtualized adapters. The guest
  remained online, quorum was retained, and the destination thin pool did not
  grow speculatively while its usage was unavailable.
- A reverse thin-to-thick move was deliberately cancelled by the evidence
  guard after a cluster-network warning. The source remained authoritative.
- The cancellation exposed a cleanup gap: PVE can call `free_image()` without
  first deactivating an idle Thick Generations frontend. Cleanup now verifies
  the exact frontend identity and dependency graph, requires an unambiguous
  zero open count, removes the idle frontend, and then deletes only the exact
  owned head and anchor under the VG transaction lock.
- The real orphan produced by the cancelled move was removed through this
  corrected lifecycle path. The original thin source remained attached and
  running, the complete thick allocation was returned to its VG, quorum
  remained healthy, and no D-state process survived.
- A complete 60 MiB/s reverse move is still an open qualification item. The
  cancellation result proves safe abort and cleanup, not round-trip success.

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
Concurrent multi-disk and mixed thin/thick transactions are qualified below;
automatic HA orchestration remains open qualification work.

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

1. Qualify full-hydration metadata occupancy and geometry performance.
2. Qualify physical FC path loss and active-guest application outcomes on a
   target that does not reproduce the Linux VN2VN/tcm_fc recovery deadlock.
3. Complete a long-duration Windows data-integrity soak and interrupted
   Windows-operation recovery tests.
4. Qualify native HA reaction to complete worker-host loss during active
   hydration, followed by the already-qualified explicit recovery primitive,
   and complete repeated mixed-mode transactions under a long soak.
