# Thick Generations tg22 handoff

## Candidate

`0.9.0~rc5.4~tg22` is one dual-mode laboratory release-candidate Debian package for Proxmox
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
collectors green. TG22 additionally passes 158 Python and 243 Perl tests,
rolling installation on all three nodes, a 150/150 running thick-VM baseline,
and exact SAN zero-initialization A/B qualification. Bounded parallel
migration/HA qualification of the 150-VM population remains in progress and
must not be inferred from the baseline count.

The current TG22 laboratory DEB SHA-256 is:

```text
e7f731b1747270e5085ce71f7fe0bbeb575a474d414892c7014058322d3d95b0
```

Two isolated TG22 builds produced this byte-identical package. The same
artifact passed rolling installation on all three qualified nodes. Record the
final commit identifier only after the in-progress scale gate is frozen.

## Remaining support boundary

All required software and available laboratory integration gates are closed.
TG12 remains a pre-release rather than a universal production certification.
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
