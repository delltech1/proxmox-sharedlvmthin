# BASTRIX SharedLVM 0.9.0~rc5.9~tg30 release notes

TG30 is a development candidate for disposable-lab qualification. It is not a
declaration of production readiness.

## Migration bridge hardening

- Persists bounded progress heartbeats for long PVE Storage Move operations;
  no fixed timeout is imposed on a healthy large-disk copy.
- Adds read-only `sharedlvmthin-migrate-bridge inspect <vmid>`.
- Pre-creates the return-to-Thin per-VM pool at aggregate virtual size plus
  configured burst headroom under the canonical storage lock.
- Recognizes both the public empty-pool mapper and hidden `-tpool` mapper.
- Adds evidence-scoped `resume <vmid>` for a completed return-to-Thin copy whose
  final validation/publish was interrupted. Ambiguous and earlier phases remain
  fail-closed.
- Blocks mutation of the affected Thin pool when live `Data%` is at least 95%,
  while reporting other full pools as isolated Doctor capacity warnings.

## Qualification evidence

- One running VM completed Thin → Thick → online PVE migration → Thin in both
  cluster directions.
- The corrected return pool was 3 GiB for a 2 GiB disk and completed at 66.67%
  instead of exhausting a 1 GiB elastic bootstrap pool.
- An intentionally interrupted final publish was reconstructed from the exact
  root-owned transaction state and finalized only after positive target checks.
- Current source regression result: 691 Perl assertions and 185 Python tests.

The plugin still does not replace SAN multipath policy, quorum, fencing,
watchdog configuration, kernel recovery or manual dm-thin metadata repair.
