# SharedLvmThin RC5.77 TG53 candidate

RC5.77 is an unpublished experimental hardening candidate for disposable
Proxmox VE laboratory clusters and disposable guest data only.  It is not a
production release, certification, support commitment or warranty.

## Delta from RC5.75/RC5.76

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

## Qualified artifacts

- DUAL `pve-sharedlvmthin_0.9.0~rc5.77~tg53_all.deb`:
  `7f07cb23fdfa859ea156f293096f3b223904f1d9bf85f227a8e7635813c4b8be`
- Thick-only `pve-sharedlvmthin-thick_0.9.0~rc5.77~tg53_all.deb`:
  `07aaf02be273e6faf688ebb46f2164570013b94c2e121899389aadcc7d33acb5`

Both hashes were reproduced by an independent fresh Linux build. Exact API 15
host-package tuples and their narrower qualified scopes are recorded in
[compatibility.md](compatibility.md). API 14 retains historical and source
contract evidence, but this exact RC5.77 artifact is `RETEST_REQUIRED` there
and must be refused by runtime qualification until its clean-install, upgrade,
profile-cycle and reboot evidence is repeated. A different package hash or an
unlisted host tuple is not covered by this evidence.
