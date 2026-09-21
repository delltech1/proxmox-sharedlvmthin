# Changelog

## Unreleased — Thick Generations audit

- Treat kernel `dm-clone` metadata mode as mandatory transition evidence.
  `ro`, `Fail`, or a missing mode now stops hydration/recovery immediately and
  preserves the transaction instead of being misclassified as ordinary slow
  progress until the no-progress timeout.
- Add a Thick-only Debian package profile built from the same plugin core as
  the dual-mode package. It exposes only `thick-generations`, removes Thin
  operational helpers, refuses installation while Thin configuration or
  managed Thin pools remain, and conflicts with the dual package so their
  shared files cannot overwrite one another.
- Make Doctor package-aware: Thick-only installations verify their own package,
  treat absent Thin services and autogrow policy as intentional, and use the
  Thick Generations default when the fixed allocation property is omitted.
- Refuse a dual-to-Thick package replacement while a transient Thick worker is
  pending, active or failed, or while the installed read-only upgrade gate
  cannot positively prove a healthy mutation-safe state.
- Measure Thick hydration and close-wait deadlines with a monotonic clock so
  wall-clock corrections cannot shorten or extend their safety windows.
- Require PVE filesystem freeze orchestration for live LXC `rootdir` block
  snapshots in both package profiles, matching the safety contract used by
  other external block-snapshot backends.
- Apply the pre-unpack Thick transaction/recovery fence to ordinary upgrades
  and both package-profile replacement directions, not only dual-to-Thick.
- Refuse removal while an active ThinGuard still protects managed Thin
  objects; stop it only after a successful empty managed-object inventory.
- Fix Thick-only package configuration so `postinst` never syntax-checks Thin
  daemons intentionally absent from that artifact, and enforce the condition
  against the built package in the release checker.
- Report the actual Thick-only package identity and version in JSON health
  diagnostics instead of querying the mutually exclusive dual package.
- Make Thick-only installation fail closed when cluster storage configuration
  is unreadable, `lvs` is unavailable, or the authoritative LVM inventory
  fails; missing evidence is never treated as an empty Thin inventory.
- Keep binary-package documentation under the actual package namespace and
  reject cross-profile documentation leakage during artifact validation.
- Pin published TG32 persistent-format fixtures for object keys, LV/mapper
  names and every signed Thick anchor, generation, transition and VG-intent
  tag sequence. Package-profile maintenance now fails CI if it silently
  changes the shared on-disk format.

## RC5.11 TG32 — experimental timing and scale hardening

- Clarify the public migration contract without changing behavior: a running
  VM may migrate online from Thin through the Materialized Migration Bridge
  (`Thin -> Thick -> live migration -> Thin`); only direct in-place dm-thin
  overlap between two kernels is unsupported and refused.
- Make Thin peer SSH-connect and whole-evidence deadlines independently
  configurable, bounded, internally consistent and identical across same-VG
  Thin/Thick aliases. Expiry remains `UNKNOWN` and refuses activation.
- Prevent the disposable rolling-cycle helper from killing or retrying a
  potentially still-running native PVE start/stop task at a wall-clock limit.
- Replace the fixed roughly two-second Thick frontend close loop with a
  configurable monotonic observation window; expiry preserves the mapper.
- Classify every failed Thin rollback create/remove/rename from a fresh exact
  VG inventory instead of trusting the command exit. Never retry; accept an
  ambiguous rename only when the canonical completed postcondition is proven.
- Apply the same single-attempt/postcondition rule to Thin resize and snapshot
  create/delete. Snapshot creation now proves same-name absence before the
  command so a pre-existing object can never be adopted after an error.
- Refuse migration-bridge inspect/plan/resume when more than one state file
  exists for the VM unless the exact transaction is supplied. Modification
  time is no longer treated as transaction authority.
- Document the two-audit Thin latency envelope and the progress-driven,
  unbounded-total-time behavior required for large Thick disks.

## RC5.11 TG32 — additional hardening incorporated after TG31+fix2

- Add a prominent, consistent experimental/disposable-lab risk and no-support
  boundary to the README, security policy and installation guide. The new
  detailed risk document distinguishes GPL code rights, operational warnings,
  absence of support/SLA/warranty promises, and non-excludable local-law
  obligations.
- Add a dry-run-by-default disposable-lab rolling node helper. Mutations
  require an explicit VMID list, `--execute`, and exact hostname confirmation;
  host reboot remains an operator-controlled change-management step.

- Re-audit every configured peer when an existing owner epoch names the local
  node; a durable tag is no longer accepted as proof that no stale remote
  kernel mapper exists.
- Add an activation commit barrier immediately before `lvchange -ay`: repeat
  the peer mapper audit and prove the exact local owner epoch did not change.
- Identify remote thin-pool mappings by immutable kernel DM UUID across every
  enumerated `thin-pool` target. A renamed/aliased mapper can no longer evade
  the remote audit, while a canonical-name/UUID mismatch fails closed.
- At qualification time these changes were local, uncommitted, not installed,
  and not published.
  They do not enable overlapping Thin activation or change Thick Generations
  activation semantics.

## `0.9.0~rc5.10~tg31+fix2` (hotfix pre-release)

- Preserve Thin ownership during additional-disk allocation: an unowned
  stopped-VM pool is republished inactive, a locally owned running pool remains
  active for hot-add, and a foreign-owned pool is rejected before mutation.
- Qualify Thin/Thick hot-add and hot-remove in all four primary/disk-mode
  combinations, plus exact two-disk offline cleanup and VG baseline recovery.
- Apply the Thin durable-owner boundary to deletion as well as allocation:
  foreign-owner removal fails before mutation, while unowned offline removal
  and local-owner hot-remove remain supported.

- Treat `/dev/mapper/<name>` and `/dev/<VG>/<LV>` only as convenience
  namespaces when deciding whether a device-mapper object exists. Recovery,
  reconstruction, resize, rollback and cleanup gates now resolve the canonical
  DM name and query the authoritative kernel inventory even when udev has
  removed or not yet published the block-device node.
- Preserve the ordinary block-device fallback for paths outside the DM/LVM
  namespaces. Add exact tests for mapper paths, escaped LVM aliases and absent
  kernel mappings; the full Perl and Python regression suites remain green.

## `0.9.0~rc5.10~tg31+fix1` (hotfix pre-release)

- Wait outside the canonical VG lock when a decoded Thick DM_CUTOVER or
  DM_PIVOT intent temporarily blocks a same-VG Thin mutation. Revalidate
  quorum, storage identity and intent on every acquisition; use an absolute
  monotonic admission budget (`slt-mutation-admission-timeout`, default 600 s).
  Never replay an entered callback, clear an intent, or retry arbitrary errors.
- Preserve optional empty UUIDs of unrelated kernel DM mappings. Exact Thick
  frontend UUID checks and duplicate/malformed inventory rejection remain.
- Admit offline Thin rollback through the same exclusive owner/ThinGuard
  activation path before creating its replacement LV. On success, normally
  deactivate the snapshot and restored head before releasing pool ownership;
  on partial failure preserve ownership and data for explicit recovery.
- Add regression tests for wait/clear, abandoned intent timeout, transaction
  churn, lost identity/quorum, lock errors, partial callbacks and UUID-less maps.

## 0.9.0~rc5.10~tg31 (unreleased development candidate)

- Discover a Thick Generations frontend from authoritative kernel DM inventory,
  not only its udev-created `/dev/mapper` node.  Source cleanup after live
  migration now removes an exact zero-open frontend before deactivating its
  backing generation LVs, while duplicate names or a mismatched DM UUID fail
  closed.
- Use the same single authoritative kernel DM inventory when classifying Thin
  public pool, hidden `-tpool`, and child mappings.  Udev device-node absence
  is no longer interpreted as kernel-mapper absence in this ownership gate.
- Persist and authenticate an exact per-slot disk manifest for migration
  recovery instead of reconstructing intent from mutable VM configuration.
- Add a pure fail-closed recovery planner and executable recovery steps for
  interrupted materialization, Thick migration, and return to Thin.
- Correlate bridge state and pmxcfs with QEMU's live `query-block` graph;
  runtime/config divergence now blocks all automatic recovery.
- Qualify a killed online mirror through safe reconciliation and a resumed
  Thin-to-Thick-to-Thin live migration lifecycle with zero Thick leftovers.
- Prevent noninteractive SSH probes from consuming the remaining disk manifest;
  qualify interruption and exact continuation between two VM disks.
- Replace the bridge's fixed 15-minute admission wait with a configurable,
  bounded policy, persistent heartbeats, and desynchronized bounded backoff;
  qualify exact single-winner behavior with 50 concurrent contenders.
- Qualify a disposable 8-TiB Thin control-plane create, PVE attach, +1-GiB
  resize and exact cleanup cycle with byte-exact VG free-space restoration;
  retain the explicit boundary that this is not an 8-TiB physical-copy test.
- Fix dmeventd autogrow activity detection: an exact active hidden `-tpool`
  mapper is authoritative even when LVM reports the public pool LV inactive.
  This prevents valid growth events from being refused until Data%=100.
- Make dmeventd autogrow use the storage's bounded `slt-lock-timeout` policy
  and revalidate that policy under the lock instead of using a fixed 30-second
  wait; qualify 50/50 concurrent stale events without mutation or timeout.
- Permit only the autogrow monitor to classify an exact out-of-data Thin pool
  as recoverable capacity, while every ordinary mutation remains fail-closed;
  quorum, identity, ownership, mapper and protected-reserve gates still run
  before the sole permitted `lvextend` and no repair action is introduced.
- Restore native-import pre-growth when `lvs --readonly` omits Data% for an
  active hidden `-tpool`: exact runtime topology is proven first and only the
  pinned active pool is queried. Existing active-pool admission also keeps the
  complete promised burst below 94% instead of sizing to an exact 100%-full
  target and relying on asynchronous autogrow.
- Treat configured elastic burst headroom as a minimum, not a sufficient final
  target: autogrow, allocation planning and Thick-to-Thin import now also size
  the exact pool so known used data is at or below 94%. This prevents a large
  disk with small absolute headroom from completing immediately behind the
  95% mutation-safety gate.
- Allow exact node-local Thin mapper teardown under capacity pressure while
  retaining mandatory storage identity, ownership and metadata-health checks;
  a stopped VM can now release runtime ownership even when Data% blocks every
  capacity-consuming mutation.
- Qualify a fully written 48-GiB Thin disk through online Thin-to-Thick
  materialization, native Thick live migration, return to Thin and whole-disk
  SHA-256 equality; verify exact temporary Thick cleanup and corrected
  capacity-safe grow/start/stop teardown on the destination.
- Add exact 500-GiB and 8-TiB integer-boundary regression vectors proving the
  elastic capacity floor is minimal, remains at or below 94% Data%, and stays
  inside the signed arithmetic contract. This is arithmetic qualification,
  not a claim of physical copy testing at those sizes.
- Extend read-only bridge inspection with validated observation time, elapsed
  time and durable progress-update age. The 90-second freshness hint never
  kills work, declares failure, invents an ETA or authorizes recovery; malformed,
  duplicate or future timestamp evidence fails inspection closed.
- Pass 198 Python tests and 699 Perl assertions.

## 0.9.0~rc5.9~tg30 (development candidate)

- Make long materialized migrations observable without imposing a fixed copy
  timeout, and persist transaction-scoped progress evidence.
- Pre-size the target per-VM Thin pool for the complete return copy plus burst
  headroom so fast imports cannot outrun asynchronous autogrow.
- Recognize both public empty-pool and hidden `-tpool` runtime mapper forms.
- Add exact, fail-closed finalization of an interrupted completed return-to-Thin
  transaction and a per-pool 95% capacity mutation gate.
- Qualify a running Thin → Thick → online migration → Thin round trip in both
  cluster directions; current source passes 691 Perl and 185 Python tests.

## 0.9.0~rc5.8~tg29

- Add an opt-in PVE HA fenced-owner takeover which requires fresh exact HA
  assignment/fencing evidence plus mapper-absence evidence from every
  non-fenced peer before changing persistent Thin ownership.
- Make the takeover retry-safe by completing peer audit before removing the
  former owner/epoch; audit failure now performs zero ownership mutation.
- Qualify a 50-VM bidirectional offline Thin evacuation and an externally
  fenced 10-VM automatic HA recovery with no duplicate mapper or D-state.

## 0.9.0~rc5.7~tg28 (unreleased development candidate)

- Add an opt-in PVE-native Thin LeaseGuard remote kernel-mapper audit using
  exact DM UUID evidence from every configured peer; unreachable, ambiguous
  or active peers fail closed while existing storage remains unchanged.
- Publish newly allocated Thin targets fully inactive before the normal PVE
  activation hook. This closes the online Thick-to-Thin Storage Move race
  introduced by strict single-kernel ownership.
- Add the Materialized Migration Bridge prototype: native online Thin-to-Thick
  Storage Move, ordinary Thick live migration, and optional online return to
  Thin with physical-capacity admission and persistent phase evidence.
- Serialize the capacity-consuming bridge materialization phase with a
  transaction-scoped VG admission record. A competing bridge queues rather
  than colliding with another allocation intent; exact release is verified.
- Fix PVE-native LeaseGuard node-scope handling for PVE's parsed membership
  hash and qualify healthy and missing-peer-helper activation paths on three
  nodes. Missing evidence leaves the VM stopped and never claims the pool.
- Add the disabled Thin Relay Handoff research state machine and prove its
  fail-closed recovery decisions. This is not a direct Thin live-migration
  support claim.
- Qualify an online Thin-to-Thin storage-mirror round-trip between independent
  managed VGs with exact QMP target path, data-canary and cleanup evidence.
- Ensure every new migration and remote-evidence helper is executable in the
  binary package; the release test now enumerates every installed shebang.

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
- Make compatibility qualification require `dm-event.service` only when an
  exact managed Thin mapper exists locally; unreadable or contradictory
  LVM/device-mapper evidence fails closed.
- Detect public `sltp-<VMID>_meta<N>` detached metadata artifacts left by a
  metadata replacement/repair workflow. They require manual review and are
  never repaired or removed automatically.
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


