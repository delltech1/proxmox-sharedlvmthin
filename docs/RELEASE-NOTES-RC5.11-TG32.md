# BASTRIX SharedLVM RC5.11 TG32

Package version: `0.9.0~rc5.11~tg32`.

> [!CAUTION]
> **BOTH THIN AND THICK GENERATIONS ARE EXPERIMENTAL.** This release candidate
> is intended only for disposable Proxmox VE 9 laboratory hosts, disposable
> storage, and disposable guest data. Neither mode is production-ready,
> supported, certified, or warranted. It can cause data loss, corruption,
> unavailability, failed migration or recovery, and host or cluster downtime.
> Maintain independently tested backups and recovery procedures. Read
> [the complete risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md).

TG32 retains the TG31 storage format and the published TG31+fix2 behavior. It
adds timing, scale, ambiguity, retry and postcondition hardening. It is a new
release-candidate identifier, not `fix3` or `fix4`.

## Safety changes

- Thin peer SSH-connect and whole-evidence deadlines are independently bounded
  and consistent across same-VG Thin/Thick aliases. Timeout or incomplete
  evidence remains `UNKNOWN` and fails closed.
- Thick frontend close waits use a configurable monotonic observation window;
  timeout preserves the mapping instead of guessing that cleanup is safe.
- Thin rollback, resize and snapshot mutations use a single command attempt
  followed by fresh authoritative inventory and exact postcondition checks.
- Migration-bridge recovery refuses ambiguous multiple transaction-state files
  unless the exact transaction is selected.
- Disposable rolling-cycle automation does not kill or retry a potentially
  still-running native PVE task merely because a local wall-clock limit passed.

## Evidence boundary

The underlying TG31+fix2 baseline passed 198 Python tests and 799 Perl tests,
targeted concurrent lifecycle checks, rolling three-node reinstall checks, and
the recorded Thin/Thick matrix. Earlier milestones include larger disposable
scale and endurance evidence. Historical tests are evidence only for their
recorded versions, topology, hardware, workload and failure injections. They
do not prove TG32 correct for every SAN, firmware, kernel, LVM/device-mapper
version, multipath policy, latency profile, scale, update or failure sequence.

TG32 must be rebuilt and the complete CI-safe suite rerun before publication.
No production-readiness claim is made.
