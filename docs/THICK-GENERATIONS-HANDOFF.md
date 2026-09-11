# Thick Generations tg11 handoff

## Candidate

`0.9.0~rc5.4~tg11` is one unpublished experimental Debian package for Proxmox
VE 9 Storage API 14 and 15. One installed plugin exposes two explicitly chosen
storage modes:

- `thin`: the existing per-VM LVM-thin design;
- `thick-generations`: independent fully allocated generations with temporary
  persistent `dm-clone` transitions and a linear steady-state frontend.

The public RC5.x branch and its thin-only behavior are unchanged.

## Qualification state

All software lifecycle, identity, ownership, quorum, locking, crash recovery,
cross-node reconstruction, migration, Thin/Thick storage move, snapshot,
rollback, resize, full-copy, backup/restore, incompatible-schema and exact
cleanup gates listed in the qualification plan have positive evidence. The
candidate passed 155 Python and 235 Perl tests, syntax and ShellCheck, package
content/privacy checks, reproducible builds, and install/reinstall on two API
15 nodes and one API 14 node.

The accepted DEB SHA-256 is:

```text
465451b55976e08e000369a7c95f0a478057037b9e6f0f97c1c452ebe11c222c
```

## Open release blockers

- Repeat single-path behavior on representative physical FC HBA, firmware,
  fabric, array and production multipath policy.
- Complete one uninterrupted four-hour concurrent Thin/Thick endurance run on
  a healthy representative backend. The failed lab datastore is not accepted
  as a qualification platform.
- Copy the final accepted source, package, checksum and sanitized evidence to
  every required offline mirror when those mirror volumes are available.

Until those gates pass, tg11 remains an experimental candidate and must not be
published or described as production-ready.

## Authoritative records

- `docs/original-cluster-qualification-plan.md` is the executable checklist.
- `docs/thick-generations-poc-status.md` is the chronological evidence log.
- `docs/thick-generations-release-gate.md` is the support decision table.
- `docs/known-issues.md` contains the current limitations.
- `docs/RELEASE-NOTES-RC5.4-TG11.md` contains candidate release wording.
