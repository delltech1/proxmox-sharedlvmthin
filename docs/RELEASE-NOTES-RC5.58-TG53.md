# SharedLvmThin RC5.58 TG53

## Experimental pre-release — disposable labs only

Thin and Thick Generations remain experimental. Use this release only on
disposable Proxmox VE hosts, disposable shared storage and disposable guest
data. It is not production-ready, supported, certified or warranted. Use can
cause data loss, corruption, unavailability, failed migration or recovery,
and host or cluster downtime. Operators remain responsible for fencing,
tested independent backups, recovery, SAN identity, multipath policy,
capacity and every deployment decision.

## What changed since TG52

- Added three explicit update policies. Their current schema-2 names are
  `freeze`, `qualified-auto` and `warn`; older candidates used
  `qualified-only` and `manual-override`. Legacy policy files remain readable
  but are renamed only by the explicit `migrate-policy-schema` command. No
  policy is silently selected for the administrator.
- FREEZE schema 3 protects complete installed package/source families rather
  than a short non-transitive package list. Unrelated APT updates remain
  installable; watched storage, cluster, QEMU and kernel changes remain held.
- Package transitions use exact one-shot plan/context authorization and
  fail-closed settlement. Interrupted transitions remain visible and cannot
  be treated as a successful upgrade.
- Added exact runtime tuple reporting and a qualified-tuple manifest. An
  installable package or unchanged Storage API is not a compatibility claim.
- Hardened snapshot/vmstate cleanup, strict PVE property parsing, migration
  preflight, bridge topology checks, recovery reference accounting and
  package-profile replacement.
- Fixed native full clone from a named Thick RAM snapshot when the current
  HEAD mapper is intentionally inactive. Snapshot copy is admitted only when
  the exact signed snapshot and materialized HEAD have equal byte geometry.
- DUAL and Thick-only profiles continue to share the same Thick code and
  on-disk format. Thin and Thick require separate dedicated VGs; Eager and
  Lazy aliases may share one dedicated Thick VG.

## Package choices

- `pve-sharedlvmthin_0.9.0~rc5.58~tg53_all.deb`: DUAL Thin + Thick profile.
- `pve-sharedlvmthin-thick_0.9.0~rc5.58~tg53_all.deb`: Thick-only profile.

Choose exactly one profile and install the identical version on every
participating node through the documented package-profile gate. The packages
conflict intentionally and must not coexist.

## Qualification evidence

- 2,118 Python tests passed with one declared skip.
- 27 Perl files / 1,249 tests passed.
- Both package profiles built reproducibly, passed forbidden/private-content
  validation and passed shared Thick payload parity.
- The exact DUAL candidate was rolled through three disposable SAN lab nodes with
  FREEZE schema 3 settlement and all applicable storage recovery gates.
- Native named-RAM-snapshot Thick-to-Thin clone, rollback, start/stop and
  cleanup passed with distinct source/snapshot SHA-256 markers.
- Native two-disk RAW-to-Thin and standalone-QCOW2-to-Thick import passed with
  exact start/middle/end hashes, exact geometry, start/stop and cleanup.
- Earlier TG53 qualification also covered retained-snapshot Storage Move,
  `backup=0` VMA isolation, snapshot-grow-rollback geometry, migration
  boundaries and the API14 clean-install/update-policy laboratory.

The API14 node had no SAN dataplane. Newer upstream tuples retain the precise
scope and `RETEST_REQUIRED` classifications recorded in the
[compatibility matrix](compatibility.md). None of this is universal hardware,
workload or failure-order certification.

Read [installation](installation.md), [known issues](known-issues.md),
[compatibility](compatibility.md), and the
[risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md) before testing.
