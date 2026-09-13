# Thick Generations tg24 handoff

## Candidate

`0.9.0~rc5.4~tg24` is one dual-mode laboratory release-candidate Debian package for Proxmox
VE 9 Storage API 14 and 15. One installed plugin exposes two explicitly chosen
storage modes:

- `thin`: the existing per-VM LVM-thin design;
- `thick-generations`: independent fully allocated generations with temporary
  persistent `dm-clone` transitions and a linear steady-state frontend.

The established RC5.x Thin behavior remains available through the explicit
`thin` mode; the package never silently converts existing volumes.

## Qualification state

Core software lifecycle, identity, ownership, quorum, locking, crash recovery,
cross-node reconstruction, migration, Thin/Thick storage move, snapshot,
rollback, resize, full-copy, native PVE backup/restore, supported-UI Veeam
HotAdd backup and cross-mode restore, incompatible-schema and exact-cleanup
gates have positive evidence. The accepted commit passed 156 Python and 236
Perl tests, syntax and ShellCheck, package content/privacy checks,
reproducible builds, and install/reinstall on two API 15 nodes and one API 14
node. The first lifecycle-overlapped four-hour run correctly classified
`FAIL` after fail-closed D-state observations; its guest-integrity evidence is
preserved as a negative qualification result. The isolated mutation-free
four-hour retest subsequently passed on both modes with all three host
collectors green. TG24 additionally passes 160 Python and 244 Perl tests,
rolling installation on all three nodes, a concurrent 150 Thin plus 150 Thick
Generations running-VM baseline, exact SAN zero-initialization A/B
qualification, 60/60 bounded parallel thick migrations, 50/50 native PVE HA
relocations, and exact restoration of 106 changed placements. These results
must not be presented as a 200--500 VM enterprise certification.

TG23 fixed a lifecycle race found by the two-disk scale test. An asynchronous
snapshot intentionally clears the VG-wide intent after handing transaction
authority to its signed non-MATERIALIZED anchor. Immediate VM stop previously
required the already-cleared intent while validating the still-running clone
frontend. The verifier now derives only the exact signed anchor-scoped
snapshot identity and revalidates every persistent object and live DM
dependency. A live two-disk snapshot, immediate stop, start during hydration,
linear pivot, stopped rollback, restart and exact cleanup all passed. Both
rollback HEAD devices were byte-identical to their immutable snapshot
generations. Cleanup restored the original one-disk VM configuration and
exactly 159161253888 free VG bytes.

TG24 separates the bounded installation preflight from the complete Doctor.
Rolling installation on API 14 and API 15 nodes completed in 75.00--79.15
seconds including service refresh, with zero preflight failures, active
storage, 2/2 paths and zero D-state tasks. The full per-volume and anchor audit
remains an explicit command and is never implied by the quick result.

The current TG24 laboratory DEB SHA-256 is:

```text
6309f058305468533c3a48f681f69069867ecbc0b18f6eaf30523eede1f1401f
```

Two isolated TG24 builds produced this byte-identical package. The same
artifact passed rolling installation on all three qualified nodes. Record the
final commit identifier only after the in-progress scale gate is frozen.

## Remaining support boundary

All required software and available laboratory integration gates are closed.
TG24 remains a pre-release rather than a universal production certification.
Every deployment must be qualified on disposable storage matching its own SAN,
multipath, fencing, firmware and workload design.

Representative physical FC qualification remains outside the tested hardware
support envelope. Virtual FCoE evidence does not qualify a physical HBA,
fabric, firmware or enterprise array, and no physical-FC support claim may be
made until that separate matrix is repeated on representative hardware.

Veeam replication was not exposed by the installed edition. HotAdd backup and
supported-console restore are qualified; replication is not claimed.

## Authoritative records

- `docs/original-cluster-qualification-plan.md` is the executable checklist.
- `docs/thick-generations-poc-status.md` is the chronological evidence log.
- `docs/thick-generations-release-gate.md` is the support decision table.
- `docs/known-issues.md` contains the current limitations.
- `docs/RELEASE-NOTES-RC5.4-TG12.md` contains candidate release wording.
