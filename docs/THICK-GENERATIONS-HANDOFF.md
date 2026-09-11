# Thick Generations tg12 handoff

## Candidate

`0.9.0~rc5.4~tg12` is one unpublished experimental Debian package for Proxmox
VE 9 Storage API 14 and 15. One installed plugin exposes two explicitly chosen
storage modes:

- `thin`: the existing per-VM LVM-thin design;
- `thick-generations`: independent fully allocated generations with temporary
  persistent `dm-clone` transitions and a linear steady-state frontend.

The public RC5.x branch and its thin-only behavior are unchanged.

## Qualification state

Core software lifecycle, identity, ownership, quorum, locking, crash recovery,
cross-node reconstruction, migration, Thin/Thick storage move, snapshot,
rollback, resize, full-copy, native PVE backup/restore, supported-UI Veeam
HotAdd backup, incompatible-schema and exact-cleanup gates have positive
evidence. A pre-final run passed 156 Python and 236 Perl tests, syntax and
ShellCheck, package content/privacy checks, reproducible builds, and
install/reinstall on two API 15 nodes and one API 14 node. A complete
lifecycle-overlapped four-hour run produced positive Thin/Thick guest-integrity
evidence but correctly classified `FAIL` after sticky fail-closed D-state
observations on PVE01/PVE02; its checksummed evidence is preserved. A new
mutation-free clean endurance retest is running. It and the supported-UI Veeam
cross-mode restore matrix remain open and are not represented as passes.

The current exact `af119c3` candidate DEB SHA-256 is:

```text
8a00e2f2fb66eb115797b1a35d34a30ccb1184866a8a619be2ddd18d0b7756f1
```

This is not a reusable checksum for a later terminal build. The final accepted
commit must be rebuilt and its own DEB checksum recorded after all open
software gates close.

## Open release blockers

- Complete one uninterrupted four-hour concurrent Thin/Thick endurance run on
  a healthy representative backend. The failed lab datastore is not accepted
  as a qualification platform.
- Complete the supported-UI Veeam Thin-to-Thin, Thin-to-Thick,
  Thick-to-Thick, Thick-to-Thin and mixed restore matrix with exact data and
  cleanup evidence.
- Copy the final accepted source, package, checksum and sanitized evidence to
  every required offline mirror when those mirror volumes are available.

Until those gates pass, tg12 remains an experimental candidate and must not be
published or described as production-ready.

Representative physical FC qualification remains outside the tested hardware
support envelope. Virtual FCoE evidence does not qualify a physical HBA,
fabric, firmware or enterprise array, and no physical-FC support claim may be
made until that separate matrix is repeated on representative hardware.

## Authoritative records

- `docs/original-cluster-qualification-plan.md` is the executable checklist.
- `docs/thick-generations-poc-status.md` is the chronological evidence log.
- `docs/thick-generations-release-gate.md` is the support decision table.
- `docs/known-issues.md` contains the current limitations.
- `docs/RELEASE-NOTES-RC5.4-TG12.md` contains candidate release wording.
