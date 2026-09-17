# Changelog

## 0.9.0~rc5.6~tg27 (unreleased development candidate)

- Refuse Thin activation when generic LVM autoactivation is enabled on the
  exact pool or requested LV.
- Refuse to claim an unowned Thin pool when any exact local pool/child mapper
  already exists; an automatically restored hidden `-tpool` is never blessed
  retroactively.
- Extend the explicit `ALL-NODES-INACTIVE` owner-model transaction to disable
  and verify autoactivation on the exact pool and every member LV. The same
  command safely hardens an already-adopted, unowned pool.
- Add fail-closed transaction-fingerprint and bounded read-only metadata
  validation primitives.
- Add an observation-only predictive Capacity Stability Governor primitive.
- Add a deliberately non-arming sanlock-to-PVE-watchdog-mux state-machine
  prototype with fake-socket tests. No watchdog or lease service is enabled.
- Preserve all Thick Generations runtime behavior while expanding its
  mandatory regression coverage.

## 0.9.0~rc5.5~tg26 (2026-09-17, development pre-release)

- Enforce the dm-thin single-kernel invariant with a persistent, versioned
  per-pool owner record containing the exact PVE node and a fresh activation
  epoch. A foreign, malformed, torn, or legacy owner state fails closed before
  activation.
- Refuse normal overlapping PVE live migration for Thin volumes. Supported
  cross-node Thin mobility is an offline deactivate-then-activate handoff;
  materialized Thick Generations retain their independent linear-LV mobility.
- Release Thin ownership only after the last child, public pool, and hidden
  `-tpool` mapper are positively absent. A host-loss owner can be cleared only
  by an explicit administrator command after external fencing.
- Add an explicit all-nodes-inactive adoption path for pre-TG26 Thin pools.
  Package installation refuses a locally active legacy pool, and neither
  activation nor allocation silently adopts old metadata.
- Extend Doctor and recovery-check with owner-schema and torn-state gates.
  These checks classify and refuse; they never infer fencing or repair dm-thin
  metadata.
- Correlate every persistent local Thin owner with the exact hidden local
  `-tpool` mapper. Durable-claim/activation crash windows are now reported as
  recovery-required instead of looking healthy.

## 0.9.0~rc5.4~tg24 (2026-09-13)

- Separate the bounded installation preflight from the complete per-volume
  Doctor audit. `postinst` now runs `doctor --quick` with a 60-second bound and
  explicitly states that the full Doctor was not run automatically. The quick
  gate retains package/API, quorum, VG/PV/WWID, reserve, multipath and PVE
  storage-state checks; it never claims per-volume or Thick anchor health.
- Cache the read-only all-storage Thick health document once per full Doctor
  invocation and do not relabel same-VG Thin pools as objects owned by the
  Thick alias. Mutation hooks continue to re-read authoritative state and do
  not consume the diagnostic cache.
- On the concurrent 300-VM lab baseline, the new installation preflight
  completed in 37.69 seconds with 88 PASS, eight expected site-policy warnings
  and zero failures. The complete fail-closed Doctor remained available and
  took 138.98 seconds over the same high-object-count inventory.
- Qualify a disposable same-VG cross-mode lifecycle: stopped Thin-to-Thick
  Storage Move completed in 37 seconds, running Thick-to-Thin native QEMU
  mirror in 128 seconds, and exact VM/storage cleanup restored the original
  recorded VG free-space baseline with no residual object.
- Full gates pass 244 Perl and 160 Python tests.

## 0.9.0~rc5.4~tg23 (2026-09-13)

- Fix thick lifecycle verification during the intentional asynchronous
  materialization handoff. After clone publication, the signed
  non-MATERIALIZED anchor is the transaction authority and the short-lived
  VG-wide intent is deliberately cleared so unrelated volumes can progress.
  VM stop/activation now derives only that exact signed anchor-scoped identity
  and still verifies the complete generation, metadata and live DM dependency
  graph before accepting the existing frontend.
- Keep rollback and every non-snapshot transition fail-closed when no exact VG
  intent exists; a foreign same-VG intent is never adopted as this volume's
  transaction identity.
- Add a regression for snapshot-to-immediate-stop, missing VG intent, and an
  unrelated concurrent same-VG intent. Full gates now pass 244 Perl and 158
  Python tests.
- Qualify a three-disk Thin lifecycle under the concurrent 300-VM baseline:
  third-disk allocation, online grow, native multi-disk snapshot, stop,
  rollback, start, snapshot deletion and exact disk deletion all passed. No
  snapshot, rollback or temporary guest LV remained and VG free space returned
  exactly to the recorded pre-test VG free-space baseline.

## 0.9.0~rc5.4~tg22 (2026-09-13)

- Use the standard Linux `BLKZEROOUT` ioctl for a newly allocated Thick
  Generation when the exact block device supports it. Never issue discard or
  UNMAP. If zeroout is unavailable or reports any failure, rewrite the entire
  requested range with the previously qualified direct synchronous zero path;
  fail allocation if both methods fail.
- Qualify the primitive on the disposable two-path iSCSI SAN with full-device
  pre/post SHA256 verification and unchanged VG free space. Repeated small
  samples completed in about 0.50--0.81 seconds with BLKZEROOUT versus
  28.78--30.40 seconds for direct writes on this lab target. These timings are
  observations, not a performance guarantee for other arrays.
- Record 150/150 simultaneously running Thick Generations VMs alongside
  150/150 running Thin VMs (300 total) across three PVE nodes, with identical
  package/plugin hashes, mixed API 14/15,
  quorum, two healthy paths per node, pinned WWID/PV/VG identity and zero
  D-state processes after rolling TG21 installation.
- Complete a 60/60 thick online evacuation at 16-way concurrency and a 50/50
  native PVE HA maintenance relocation, then restore the exact saved placement
  with 106/106 bounded online migrations. Six full-device SHA256 samples,
  source mapper cleanup, quorum, path and D-state postconditions passed.

## 0.9.0~rc5.4~tg21 (2026-09-13)

- Add cooperative post-lock yield after successful implicit outer PVE storage
  mutations, configurable with `slt-lock-yield-ms` (default 1000 ms). It never
  sleeps inside a critical section or retries an ambiguous callback outcome.
- Extend the explicitly bounded storage-lock timeout range to 10--86400 seconds
  and require same-VG Thin/Thick aliases to use identical admission policy.
- Document VG text-metadata capacity as an independent object-scale limit that
  cannot be solved by RAM, timeout or SAN throughput tuning.

## 0.9.0~rc5.4~tg20 (2026-09-13)

- Create a fresh Thick Generations head and anchor with their complete
  ownership tags, activation-skip policy and `autoactivation=n` in the same
  `lvcreate` metadata transaction. Read-only postconditions remain mandatory.
- Reduce a qualified small thick allocation from nine metadata-changing LVM
  steps to five archive-producing commits. The isolated lab A/B completed in
  48.3 seconds; the earlier three-node contention samples commonly required
  70–120 seconds.
- Preserve fail-closed interrupted-allocation recovery. A real interruption
  left one exact `PREPARED` pair and OPEN VG intent; a later allocation was
  refused, and the transaction-scoped recovery removed only that pair before
  clearing the exact intent.

## 0.9.0~rc5.4~tg12 (2026-09-12)

- Publish one explicitly selected dual-mode package: isolated per-VM Thin
  pools or fully allocated Thick Generations over the same pinned shared VG.
- Qualify Thin/Thick Storage Move, native PVE backup/restore, Veeam HotAdd
  backup and supported-console cross-mode restore with block-hash evidence.
- Complete the accepted-build gate with 156 Python tests, 236 Perl tests,
  reproducible byte-identical packages, three-node API 14/15 reinstall, and a
  clean four-hour dual-mode endurance run.

- Add an incident-to-invariant research matrix for PVE rollback dispatch,
  stale snapshot objects, thin metadata corruption and dm-clone hydration I/O
  failure. The reports justify fail-closed gates, not automatic repair.
- Qualify native three-node quorum loss: both allocation modes fail before
  mutation, LVM inventory remains byte-identical, and the original 4/4
  QDevice topology recovers without a private vote or watchdog override.
- Qualify the complete two-node plus QDevice matrix: normal operation,
  QDevice loss with both nodes, one-node plus QDevice survival, fail-closed
  one-node without QDevice, and exact return to the three-node 4/4 baseline.
- Report a quorate two-node cluster without QDevice as an explicit Doctor
  warning, and classify a forced single-node `expected_votes=1` survivor as a
  hard safety failure. The plugin still consumes native PVE quorum and never
  changes votes, watchdog, fencing, or QDevice configuration.
- Emit an explicit zero-length response body for Web Dashboard redirects so
  strict TLS/HTTP clients do not report an unexpected EOF after a valid 303.
- Keep the complete shell helper set clean under the same ShellCheck command
  used by CI; cleanup and completion predicates now use explicit conditionals.
- Treat PVE `nodes` scope as a first-class operational boundary in Doctor,
  dashboard health, and upgrade preflight. A storage intentionally assigned
  to other nodes is reported and skipped locally; an assigned but unavailable
  storage remains fail-closed.
- Publish the tg7 local qualification candidate together with an explicit
  original-cluster test plan. The plan separates non-destructive admission,
  rolling package qualification, Thin/Thick lifecycle, topology and transport
  fault gates, and prevents the failed ESXi local datastore from being reused
  as qualification evidence.
- Treat only an explicitly disabled SharedLvmThin storage as non-operational:
  Doctor and dashboard inventory retain the entry but skip device, VG, pool,
  path, and anchor probes. Enabled but unavailable storage continues to fail
  closed and prevents an operational-ready result. Both the canonical PVE
  bare `disable` flag and explicit boolean forms are parsed consistently in
  detailed storage checks and the final PVE status gate.
- Give local Thick Generations packages a distinct `~tg` Debian pre-release
  version so experimental runtime bits can never masquerade as the published
  thin-only RC5.3.1 package.
- Enforce one argv-only execution boundary for every plugin subprocess under
  Perl taint mode. API, LVM, sysfs, and device-mapper derived arguments reject
  control characters; tainted values cannot become command options; validated
  values are untainted only after exact grammar checks.
- Add an executable `perl -T` regression covering object identity, VG intent
  tags, VG state digests, kernel transaction identifiers, command arguments,
  option injection, and control-character injection.
- Report each per-VM thin pool's physical reservation, approximate payload, and
  reserved slack independently. The dashboard and Doctor explicitly explain
  that pool slack remains unavailable to other VM pools and is included in
  standard PVE VG utilization.
- Generate `SHA256SUMS` with a portable package basename rather than a local
  absolute build path. Two independent builds of the same source are required
  to be bit-identical before the local artifact is accepted.

## 0.9.0~rc5.3.1 (2026-09-09)

- Recognize the qualified 50% elastic early-grow threshold as healthy in
  Doctor while retaining 80% as the qualified legacy-policy value.
- Scope the dm-event health requirement to locally active pools so a healthy
  non-owner cluster node does not fail merely because it sees inactive shared
  pool metadata.
- Qualified the public package with a live elastic grow at
  approximately 400 MiB/s, exact one-event enforcement, pattern readback,
  reserve verification, and complete disposable cleanup.

## 0.9.0~rc5.3 (2026-09-09)

- Add elastic allocation: initial and subsequent physical capacity is based
  on actual use plus an absolute burst headroom, never a percentage of a
  multi-terabyte virtual disk. Small auxiliary allocations use a 1 GiB
  bootstrap and the per-allocation headroom defaults to 64 GiB.
- Preserve VG reserve admission and exact postconditions for every elastic
  create/grow operation. Existing `fixed`, `proportional`, and `full` modes
  retain their semantics.
- Advertise the exact running PVE Storage API within the explicitly tested
  range 14..15, eliminating the false older-plugin warning on API 15.
- Fail plugin registration closed on Storage API 13 and 16+ instead of
  dynamically claiming compatibility with an unknown interface.

## 0.9.0~rc5.2

- Formalized DS-16: restored paths and matching identity are necessary but
  insufficient evidence after a transport/storage recovery event.
- Added side-effect-free `sharedlvmthin recovery-check <storage-id>` with
  bounded sequential LVM/PVE probes, exact identity/path/pool/quorum checks,
  scoped D-state evidence and fail-closed `RECOVERY_REQUIRED` classification.
- The checker never repairs, rescans, activates, reloads or changes storage.
  Ambiguous D-state attribution returns `UNKNOWN` and cannot yield
  `SAFE_FOR_MUTATION=YES`.

## 0.9.0~rc5.1 (development)

- Added read-only thin-pool exhaustion-policy diagnostics using LVM's
  `lv_when_full` state and the kernel `dm_thin_pool.no_space_timeout` value.
- Report bounded `queue` behavior as a warning, immediate `error` behavior as
  explicit evidence rather than a safety guarantee, and `queue` with a zero
  kernel timeout as critical. The plugin never changes either policy.
- Classified current Proxmox, third-party storage, and upstream LVM-thin incident
  reports against RC5 invariants and recorded unsupported stacked-backend and
  manual-repair boundaries.

## 0.9.0~rc5 (development)

- Added explicit fixed/proportional/full allocation-headroom policies for
  clone, restore, import, storage-copy, and multi-disk write bursts. Existing
  fixed behavior remains the compatibility default.
- Added extent/metadata-aware reserve admission, post-allocation reserve
  verification, conservative inactive-pool accounting, and uncertain pre-grow
  postconditions with no retry, shrink, or cleanup.
- Advertise zero-initialized LVM-thin destinations through `sparseinit` without
  conflating sparse writes with physical burst-capacity guarantees.
- Diagnose that PVE raw full-copy may scan the complete virtual disk even with
  sparse destination writes; do not wrap qemu-img or alter PVE/SAN policy.
- Added a fail-closed Windows backup/Storage Move TRIM harness using only
  native PVE QGA commands, explicit disposable VG reserve, and bounded cleanup.
- Batch health-collector and Doctor LVM fields per VG instead of spawning
  per-pool `lvs` processes; health semantics and JSON fields are unchanged.
- Diagnose low/exhausted PV/VG metadata-area capacity separately from thin-pool
  metadata and VG data extents; never resize or repair PV metadata automatically.
- Added a ledger-cleaned real 100/500/1000-pool scale/latency harness; the
  100-pool disposable-node stage passed with zero artifacts.
- Qualified a real Windows 11 full clone, second-disk admission, measured
  Windows ReTrim reclamation, and four-volume snapshot/rollback lifecycle.
- Harden snapshot deletion to prove the candidate LV belongs to the expected
  owned per-VM pool and has canonical snapshot flags before `lvremove`, both
  for direct snapshot deletion and disk cleanup.
- Reject rollback sources that do not have canonical read-only thin snapshot
  and activation-skip flags before the first mutation.
- Make disabled shared-LVM autoactivation a verified allocation postcondition
  for new per-VM pools and guest/auxiliary LVs; preserve uncertain objects as
  PARTIAL instead of acknowledging unsafe success.
- Diagnose duplicate PV device bindings, raw-path preference, and legacy
  autoactivation flags without automatic LVM or multipath policy changes.
- Preserve and verify disabled autoactivation across snapshot creation, resize,
  rollback replacement, online Storage Move, VM stop/start, vmstate rollback,
  and scoped dmeventd recovery.
- Added optional fail-closed VG UUID, PV UUID, and multipath WWID safety gates.
- Applied identity preflight to activation and all mutating plugin hooks.
- Preserved RC4 authentication, sliding idle-session, and health-cache behavior.
- Removed destructive automatic cleanup from failed allocation and rollback partial states.
- Added postcondition checks for resize and snapshot create/delete.
- Enforced read-only LVM-thin and activation-skip snapshot flags.
- Added exact PVE vmstate and backup-fleecing auxiliary volume support inside positively owned per-VM pools.
- Added disposable running-VM vmstate and backup-fleecing lifecycle gates with zero-artifact verification.
- Added DS-14 allocation-confidentiality qualification and middle-snapshot deletion regression.
- Added post-Storage-Move snapshot/rollback/reverse-move SHA256 qualification and synthetic 100/500/1000-pool cache isolation coverage.
- Added exact cloud-init auxiliary-volume support and Debian 13 guest/QEMU/LVM-thin TRIM reclamation and latency qualification.
- Added adversarial clean/stale VMID reuse policy and regression coverage.
- Added physical VG reserve gates and uncertain-outcome handling to cluster-locked autogrow.
- Moved the safety helper outside PVE's custom storage-plugin discovery namespace.
- Added delayed-discovery, capacity, event-storm, VMID-reuse, migration, and rollback qualification coverage.
- Refresh active long-running PVE storage consumers during package
  install/upgrade so HA LRM does not retain a stale plugin registry; a failed
  refresh now prevents a false operational-ready result.
- Added lock coverage, data-safety, bounded no-path, fault-injection, and critical-recovery documentation.

## 0.9.0~rc4

- Make dashboard session idle timeout configurable and truly activity-based.
- Add a single background health collector with last-good cache metadata.
- Keep collector failure separate from authentication lifecycle.
- Correct aggregate path health when a configured storage is unavailable.
- Add bounded TFA challenge attempts.
- Correct standalone/non-quorate health semantics.
- Serialize resize and snapshot mutations with the PVE storage lock.
- Add pool-relation and storage-tag checks before volume deletion.
- Preserve legacy untagged pools rather than silently adopting/deleting them.
- Make rollback create a replacement before deleting the current disk.
- Add unit/mocked lifecycle, session, cache, quorum, and package tests.
- Add reproducible package build/content validation and RC4 documentation.

## 0.9.0~rc3

Known-good POC baseline preserved separately with verified release hashes.

