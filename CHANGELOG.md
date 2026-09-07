# Changelog

## 0.9.0~rc5.2 (development)

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
- Classified current Proxmox, StarWind/StarLVM, and upstream LVM-thin incident
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
