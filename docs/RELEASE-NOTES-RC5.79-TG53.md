# SharedLvmThin RC5.79 TG53 candidate

RC5.79 is an unpublished experimental hardening candidate for disposable
Proxmox VE laboratory clusters and disposable guest data only.  It is not a
production release, certification, support commitment or warranty.

## Delta from RC5.75/RC5.76

- Adds bounded, pre-effect-only Thick allocation admission behind an exact
  foreign `DM_CUTOVER`/`DM_PIVOT`. The canonical VG lock is released while
  waiting, every observation is revalidated, one monotonic deadline applies,
  and the mutating callback is invoked at most once. Unknown, malformed, own
  and unsupported intents still refuse without an allocation effect.
- Treats a closed `LAZY_DORMANT` object as a stable, reference-checked state
  rather than a missing materialization worker, and replays the complete
  read-only package gate after boot-bound runtime settlement.

- Serializes Thick deactivation with the exact per-volume snapshot
  materialization executor.  After the bounded wait, all pre-lock anchor and
  mapper observations are discarded and the state is read again.  This closes
  a measured stop-versus-pivot TOCTOU in which a post-pivot dm-clone table was
  compared with a stale pre-pivot anchor.
- A crashed or absent materializer is not guessed complete: an unfinished
  exact transition frontend is verified, left published and retained for
  explicit recovery.
- Experiment oracles now apply their declared dispatch deadline, distinguish
  inventory failure from proven object absence, refuse offline canary reads
  while a VM is running, and no longer label a set of mixed terminal receipts
  as successful operations.

## Evidence so far

- The RC5.75 three-mode RAM snapshot/rollback/delete lifecycle reproduced the
  deactivation race without data loss.  Rollback restored all three disk
  identities and cleanup completed; both affected VGs subsequently reported
  healthy and safe for mutation.
- The source fix has valid Perl syntax and passed 2,162 Python tests (one
  intentional skip) plus 1,278 Perl tests across 30 files on an isolated
  control-only qualification node.
- Fresh Linux builds of DUAL and Thick-only passed archive, checksum, privacy,
  identity and shared-profile parity checks and reproduced the qualified
  artifact hashes exactly.
- The three disposable SAN lab nodes passed the exact live regression plus supported Eager and
  materialized-Lazy online migration, stopped Thin handoff, three-mode resize
  with complete zero-tail verification, concurrent snapshots/deletes, exact
  canary verification and clean recovery.
- A complex q35/OVMF VM with EFI, TPM2, cloud-init, VirtIO networking, Thin
  and Eager data disks and explicit Thick vmstate passed two RAM snapshots,
  immediate stop, rollback, deletion in dependency order and exact cleanup.
- The isolated control-only qualification node passed the guarded WARN update path, negative unqualified reboot
  boundary and subsequent canonical direct-package qualification.  A future
  candidate still cannot authorize itself for `QUALIFIED_AUTO`.

The final independent P0/P1 evidence audit found no remaining blocker for an
experimental release in this precisely recorded scope.  Passing this
disposable-lab scope is not a production-readiness or universal compatibility
claim.

## Candidate artifacts

- DUAL `pve-sharedlvmthin_0.9.0~rc5.79~tg53_all.deb`:
  `eb96c7932c6e04f1dce088c58b077850fa8bfbdebcc32cee2e2228a6e38a05c6`
- Thick-only `pve-sharedlvmthin-thick_0.9.0~rc5.79~tg53_all.deb`:
  `636f3752b3db3b3c38a0b07e09b420edada2c886adb7fe44f9d9aba5f2825da0`

Both hashes reproduced in two independent clean output directories. These
artifacts must not be described as qualified until their package/live gates
pass. Exact API 15
host-package tuples and their narrower qualified scopes are recorded in
[compatibility.md](compatibility.md). API 14 retains historical and source
contract evidence, but this exact RC5.79 artifact is `RETEST_REQUIRED` there
and must be refused by runtime qualification until its clean-install, upgrade,
profile-cycle and reboot evidence is repeated. A different package hash or an
unlisted host tuple is not covered by this evidence.
