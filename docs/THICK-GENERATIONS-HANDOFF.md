# Thick Generations tg12 handoff

## Candidate

`0.9.0~rc5.4~tg12` is one dual-mode release-candidate Debian package for Proxmox
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
collectors green.

The accepted TG12 DEB SHA-256 is:

```text
9bcc85efbec07fa3ad099b3bdd553b74fa99946e4e295cbedd18e8968464f2b7
```

Two isolated builds of accepted commit `8622dab` produced this byte-identical
package. The same artifact passed consecutive installation/reinstallation on
all three qualified nodes.

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
