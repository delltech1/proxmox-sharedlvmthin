# Changelog

## Unreleased — Thick Generations audit

- Declare the non-Essential runtime used by the candidate pre-unpack recovery
  checker as Debian `Pre-Depends` in both package profiles.  The safety fence
  can no longer rely on ordinary `Depends`, whose packages are not guaranteed
  to be available when the candidate `preinst` runs.  Depend directly on the
  packages that own `pvecm` and `pvesm`, rather than pre-depending on the whole
  `pve-manager` package and unnecessarily constraining PVE upgrade ordering.
- Scope every LVM probe in the read-only recovery/upgrade checker to the pinned
  `/dev/mapper/<WWID>`. VG, intent, PV and LV evidence can no longer be sourced
  from an unrelated same-name local or stale VG before the identity comparison.
- Give explicitly disabled storage a usable but equally strict recovery-check
  path: only the PVE `active` status probe is not applicable; pinned identity,
  paths, quorum, D-state, VG intent, LVM flags and Thick anchors remain
  mandatory. Invalid disable syntax fails before any external probe.
- Re-audit every locally scoped SharedLvmThin storage with the candidate
  recovery rules before unpacking. Neither an enabled nor disabled
  configuration can hide an OPEN intent or incomplete Thick anchor from
  ordinary upgrades or package-profile replacement; unavailable or
  recovery-required state refuses before files are
  replaced. The candidate `preinst` performs this check with the exact new
  read-only checker shipped in its control archive, because dpkg has not yet
  unpacked the payload and published TG32's installed checker lacks the signed
  intent and disabled-storage semantics. Storage scoped exclusively to other
  nodes remains not applicable. Release validation proves the control-archive
  checker is executable, syntax-valid and byte-identical to the payload copy in
  both package profiles. The candidate no longer double-probes through the old
  installed helper; Debian's `upgrade` argument and the installed flavor marker
  select the audit, and duplicate storage identifiers fail before probing. A
  first local install also runs the candidate audit whenever cluster
  `storage.cfg` already contains SharedLvmThin storage, covering a new node
  joining an existing data-bearing cluster. Its explicit `--preinstall` mode
  skips only the PVE active-status probe that cannot work before the local
  plugin is unpacked; every storage and recovery invariant remains mandatory.
- Fail closed when asynchronous materialization scheduling is not positively
  confirmed. A `systemd-run` client/transport error may occur after the exact
  transaction worker was queued, so the snapshot callback no longer starts a
  competing synchronous owner; it preserves recovery evidence and requires an
  explicit `thick-resume` after inspecting the transaction service and timer.
  The transient worker also pins `Restart=no`, preventing systemd policy from
  replaying a failed storage mutation without fresh persistent-state checks.
- Separate historical cluster evidence from the current audit delta. The
  release gate now requires explicit physical tests for package profiles,
  PREPARE cleanup, VG-wide concurrency admission, device-scoped capacity,
  frontend-removal postconditions, health telemetry and the post-reboot
  dm-clone kernel target before an audit artifact can be published.
- Extend the read-only compatibility gate with a dry-run proof that the
  running kernel can load `dm-clone` after reboot. If the target is already
  registered, require the supported v1 interface and fail on unavailable or
  ambiguous target inventory.
- Strengthen the explicit disposable-node package gate after installation: it
  now proves the opposite profile is absent, the installed flavor marker
  matches package identity, and the recovery CLI contains
  `thick-recover-prepare` before declaring the node candidate valid.
- Require positive frontend absence after a successful stable `dmsetup remove`
  before deactivating the signed anchor and HEAD LVs. A misleading command
  success can no longer let teardown proceed against a surviving frontend.
- Scope the Thick capacity probe to the pinned multipath WWID with LVM
  `--devices`; a same-name local or stale VG can no longer supply admission
  figures after storage identity was verified.
- Expose the active/limit/available Thick materialization admission state in
  JSON health output so operators can distinguish a healthy saturated VG from
  a lost worker or recovery-required transaction.
- Bound aggregate dm-clone pressure per VG with
  `slt-tg-max-active-materializations` (default 4). Admission counts exact
  signed non-MATERIALIZED anchors under the canonical VG lock and refuses a
  new PREPARE before writing an intent or LV when the ceiling is reached.
- Add explicit idempotent cleanup for a snapshot/rollback PREPARE that never
  reached the signed anchor. `thick-recover-prepare` accepts only the exact
  OPEN intent, unchanged MATERIALIZED anchor/frontend and correctly signed
  destination/metadata remainder; ambiguity preserves every object and intent.
- Eliminate the unowned transition-LV crash window. Snapshot/rollback
  destination and dm-clone metadata LVs now receive their complete signed
  ownership tags plus `autoactivation=n` in the atomic `lvcreate` command,
  matching the already-hardened initial-allocation path.
- Add explicit, reference-gated recovery for interrupted whole-volume deletes.
  It derives authority from the exact signed `OPEN REMOVE` intent and canonical
  anchor, safely continues with both objects or an anchor left after HEAD
  removal, and clears an already-completed delete without replaying mutation.
- Make partial Thick allocation cleanup idempotent across its own two delete
  boundaries. Recovery now accepts an exact signed generation left before
  anchor creation or an exact signed anchor left after generation removal,
  while continuing to reject foreign objects, runtime frontends and PVE refs.
- Bind every dm-clone status observation to the signed transition geometry.
  The target must start at sector zero and report the exact frontend length,
  region size and derived region count throughout hydration and final pivot;
  substituted, truncated or internally impossible metadata/hydration counters
  fail closed.
- Before any direct zero/metadata write through an LVM pathname, compare the
  exact device-scoped VG/LV UUID pair with the active kernel DM UUID. A stale
  mapper or duplicate-name collision now fails before it can redirect a raw
  write to an unrelated device.
- Route every Thick LV activation through one activate-and-prove primitive.
  Each requested LV receives an individual post-activation kernel UUID check;
  direct unverified `lvchange -ay` call sites are prohibited by regression
  tests, including read-only snapshot and runtime-reconstruction paths.
- Route every Thick LV deactivation through a symmetric prove-and-deactivate
  primitive. Any pre-existing mapper must match the scoped LVM UUID, and the
  exact kernel mapper must be absent after `lvchange -an`; already-inactive
  objects remain an idempotent success case.
- Require that proof immediately before deleting detached dm-clone metadata,
  a superseded rollback HEAD, or a signed snapshot. Snapshot deletion no longer
  treats a missing udev pathname as evidence that the kernel mapper is absent.
- Add idempotent online Thick resize recovery without changing persistent tag
  formats. It derives the old published size from the exact verified linear
  frontend and the target from the signed HEAD LV, repeats and flushes the full
  unpublished zero tail, atomically republishes it, and clears only the exact
  `OPEN EXTEND` intent. Missing or contradictory runtime evidence fails closed.
- Make the read-only recovery checker inspect and cryptographically validate
  the VG mutation-intent tags. Any OPEN or malformed intent now prevents a
  healthy/safe-for-mutation result; `OPEN EXTEND` reports the exact explicit
  resize-recovery command instead of being hidden by a materialized anchor.
- Apply the same device-scoped, digest-validating VG-intent gate to JSON health
  and therefore the web/Doctor view. Monitoring can no longer report PASS when
  the mutation admission path is fenced by an OPEN or malformed intent.
- Require exactly one explicit `VG_INTENT_CLEAR=PASS` record in the package
  upgrade gate. A checker regression that omits, duplicates or weakens this
  evidence cannot authorize Dual or Thick-only unpacking.
- Exercise five-TiB allocation zeroing and a four-to-five-TiB interrupted
  online resize in CI, asserting exact byte counts, sector counts and tail
  offsets so large-volume paths cannot silently regress to 32-bit arithmetic.
- Cover every resize-recovery publication boundary: an already-published map
  clears only the exact intent without repeating I/O, a missing frontend fails
  before mutation, and an already-suspended frontend resumes without issuing a
  second suspend.
- Require every authoritative and inactive Thick linear table to be exactly one
  full segment with source offset zero and no trailing target arguments. UUID,
  size and dependency identity can no longer mask a shifted map on the correct
  backing LV.
- Fully zero every new snapshot/rollback destination before a dm-clone
  frontend can expose it. This makes dm-clone's unhydrated-region DISCARD
  semantics deterministic and prevents old free-extent contents from becoming
  readable when discard passdown is disabled. Interrupted `PREPARED` recovery
  repeats the entire exact zero range before advancing.
- Classify an exact, complete clone left suspended at the final pivot boundary
  as explicitly recoverable `PIVOT_READY` evidence. It still blocks unrelated
  mutation and may proceed only after the resume path revalidates the signed
  source, complete hydration, writable metadata and inactive linear table.
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
- Prevent a later purge of the replaced profile's residual Debian
  `config-files` entry from deleting configuration, recovery state or LVM
  policy owned by the currently installed opposite package profile.
- Remove the dual package's precisely delimited managed Thin autogrow fragment
  when configuring Thick-only, preventing a stale `thin_command` reference to
  a monitor intentionally absent from that artifact while preserving all
  administrator/vendor LVM policy.
- Make JSON health monitoring fail, rather than merely report a null version,
  when the package-flavor marker is missing/invalid, contradicts the package
  identity or the installed package version cannot be read.
- Validate the positive artifact manifest as well as Thin exclusions: both
  package profiles must contain the shared Thick schema/plugin, materializer,
  recovery, upgrade, compatibility and health entry points with executable
  modes where required.
- Compare the built package payloads directly: every file retained by
  Thick-only must match the dual artifact byte-for-byte and mode-for-mode,
  excluding only the explicit flavor marker and mapped package documentation.
- Add a dry-run-by-default disposable-node package profile gate with exact
  artifact checksum and hostname confirmation, bounded recovery checks,
  downgrade/profile-version refusal and explicit post-install verification.
  It never downloads dependencies, reboots a host or advances another node.
- Keep a refused package removal operationally side-effect free by evaluating
  the active ThinGuard/managed-object fence before stopping or disabling the
  diagnostics service.
- Generate and validate a complete deterministic Debian data-file checksum
  manifest for both package profiles, and reject post-install verification
  output instead of treating an empty or unchecked `dpkg --verify` run as
  evidence of package integrity.
- Prove build reproducibility rather than inferring it: CI rebuilds both Dual
  and Thick-only profiles in independent staging directories and requires
  byte-identical `.deb` artifacts and checksum files.
- Make the post-hydration linear pivot a separately resumable transaction
  boundary. A crash after the authoritative frontend points at the new HEAD
  can now finish exact source-mapper, metadata and superseded rollback-HEAD
  cleanup without replaying the pivot or requiring already removed temporary
  objects to reappear.
- Explicitly deactivate the exact signed Thick HEAD and anchor before deletion
  even when the stable frontend is already absent, instead of delegating that
  decision to `lvremove -f`.
- Make explicit `thick-resume` cover every persisted transition phase from
  `PREPARED` through `LINEAR_PIVOTED`, and reconstruct a reboot-lost frontend
  after the pivot as a canonical linear map to the signed new HEAD only.
- Scope the rollback source read-only probe to the configured pinned multipath
  device, preventing an unscoped duplicate-VG lookup from influencing the
  transition decision.
- Make the final clone-to-linear cutover resumable when the frontend was
  already suspended by an interrupted attempt, and revalidate complete `rw`
  dm-clone status after I/O drains but before publishing the linear table.

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


