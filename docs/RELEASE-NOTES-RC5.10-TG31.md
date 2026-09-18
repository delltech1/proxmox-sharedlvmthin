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
- Remote probes use stdin-null SSH sessions so a command executed inside a
  manifest loop cannot consume later disk records. The sole streaming SSH
  session is the explicit QMP expectation pipeline.
- Migration admission waiting is site-configurable, bounded, observable, and
  uses desynchronized backoff instead of a fixed 15-minute busy-poll window.

## Qualification completed

An online Thin-to-Thick mirror was deliberately terminated. QMP proved that
the guest still wrote Thin while pmxcfs named Thick. Automatic recovery was
blocked, both copies were preserved, and orphan cleanup refused while the
destination was open or referenced. After stopped-guest reconciliation, the
same transaction resumed through materialization, live migration, and return
to Thin. Final QMP/config topology matched, admission was clear, and no Thick
transaction LV remained.

A two-disk VM was then interrupted during the second Thin-to-Thick mirror.
The first slot remained Thick, the second remained Thin, state became
`MOVE_FAILED`, and the planner selected `CONTINUE_MATERIALIZE`. Recovery copied
only the remaining slot, completed the native live migration, and returned
both slots to Thin. This test also exposed and fixed SSH consuming the second
manifest line during remote return recovery.

The current source passes 197 Python tests and 691 Perl assertions. Broader
multi-disk, repeated crash-point, large-disk, and concurrent recovery
qualification remains required before a public release.

A real VG admission race with 50 concurrent contenders was also qualified.
While an exact holder existed, 0/50 contenders acquired admission. After its
exact release, a simultaneous 50-way race produced exactly one winner; the
other 49 failed closed against the winner's durable transaction tag. Exact
winner release restored an empty admission state.
