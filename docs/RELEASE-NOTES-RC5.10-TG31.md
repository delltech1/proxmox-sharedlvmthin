# BASTRIX SharedLVM 0.9.0~rc5.10~tg31 development notes

TG31 is an unreleased development candidate for disposable-lab qualification.
It is not yet the recommended public package.

## Recovery hardening

- Bridge state schema v3 commits a root-owned mode-0600 disk manifest and its
  SHA-256 digest before the first storage move.
- A pure planner classifies only evidence-complete recovery states and refuses
  foreign admission, missing disks, mixed unsupported topology, and impossible
  phase/owner combinations.
- A bounded read-only QMP probe correlates every configured disk with QEMU's
  live canonical `/dev` path. pmxcfs and process arguments are never treated
  as sufficient runtime proof.
- Resume actions cover materialization, native Thick live migration, return to
  Thin, and exact finalization. Long copies publish progress without imposing
  a fixed timeout on large healthy disks.

## Qualification completed

An online Thin-to-Thick mirror was deliberately terminated. QMP proved that
the guest still wrote Thin while pmxcfs named Thick. Automatic recovery was
blocked, both copies were preserved, and orphan cleanup refused while the
destination was open or referenced. After stopped-guest reconciliation, the
same transaction resumed through materialization, live migration, and return
to Thin. Final QMP/config topology matched, admission was clear, and no Thick
transaction LV remained.

The current source passes 197 Python tests and 691 Perl assertions. Broader
multi-disk, repeated crash-point, large-disk, and concurrent recovery
qualification remains required before a public release.
