# SharedLvmThin RC5.72 TG53

## Experimental pre-release — disposable labs only

Thin and Thick Generations remain experimental. Use this release only on
disposable Proxmox VE hosts, disposable shared storage and disposable guest
data. It is not production-ready, supported, certified or warranted. Use can
cause data loss, corruption, unavailability, failed migration or recovery,
and host or cluster downtime. Operators remain responsible for fencing,
tested independent backups, recovery, SAN identity, multipath policy,
capacity and every deployment decision.

## Main changes since TG52

- Added explicit `FREEZE`, `QUALIFIED_AUTO` and `WARN` update policies. No
  policy is silently chosen for the administrator.
- Added durable, exact package-plan and runtime settlement. Installed files,
  loaded workers and the running boot/kernel are treated as separate facts.
- Added operation-scoped compatibility reporting. It is advisory only and
  cannot bypass the authoritative package, runtime or storage admission gates.
- Hardened snapshot/vmstate reference accounting, PVE property parsing,
  migration preflight, bridge topology checks, profile replacement and
  Thick-Lazy export/move diagnostics.
- DUAL and Thick-only packages share the same Thick implementation and on-disk
  format. Thin and Thick require separate dedicated VGs; Eager and Lazy may
  share one dedicated Thick VG.
- Constrained two-node operation remains an explicit warned mode. It does not
  provide fencing, automatic quorum adjustment or unsafe ownership takeover.

## Package choices

- `pve-sharedlvmthin_0.9.0~rc5.72~tg53_all.deb`: DUAL Thin + Thick profile.
- `pve-sharedlvmthin-thick_0.9.0~rc5.72~tg53_all.deb`: Thick-only profile.

Choose exactly one profile and install the identical version on every
participating node through the documented package/profile gate. The packages
conflict intentionally and must not coexist.

## Qualification evidence

- 2,146 Python tests passed with one declared skip.
- 30 Perl files / 1,277 tests passed.
- RC5.70, the immediately preceding executable candidate, passed guarded DUAL
  installation on a SAN node and DUAL/Thick-only/DUAL replacement plus reboot
  qualification on a no-SAN node.
- Disposable live checks covered two-disk Thin and Eager snapshot/rollback/
  resize, Lazy resize refusal and materialized grow, Thin/Eager/Lazy Storage
  Move, supported Q35+EFI+TPM+VirtIO RAM snapshot rollback, native Thin VMA
  backup/restore with EFI/TPM/cloud-init, and materialized Lazy live migration.
- Every live case used exact task results, data SHA-256 oracles and object
  cleanup checks. The detailed evidence is retained in the development
  worklog; it is not universal certification.

RAM snapshots without a usable VirtIO MTU record, including the observed
no-NIC and e1000-only upstream edge, remain unqualified and fail closed in the
plugin's guarded paths. Unlisted PVE/QEMU/kernel tuples and untested operation
combinations remain `RETEST_REQUIRED`.

Read [installation](installation.md), [known issues](known-issues.md),
[compatibility](compatibility.md), and the
[risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md) before testing.
