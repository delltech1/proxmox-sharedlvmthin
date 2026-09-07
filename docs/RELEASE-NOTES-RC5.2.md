# SharedLvmThin 0.9.0~rc5.2 release notes

Build date: 2026-09-07

RC5.2 adds a conservative, side-effect-free post-incident recovery
qualification. It does not add automatic storage recovery.

## New command

```text
sharedlvmthin recovery-check <storage-id>
```

The result is `SAFE_FOR_MUTATION=YES` only after exact path and storage
identity, owned thin-pool health, bounded LVM/PVE probes, PVE storage health,
quorum and scoped D-state evidence all pass. Any failure or ambiguity returns
`STATE=RECOVERY_REQUIRED` and `SAFE_FOR_MUTATION=NO`.

The checker never activates storage, rescans SCSI, reloads multipath, restarts
services, clears recovery state, initializes devices, repairs metadata or
runs an LVM mutation.

## Qualification

- 64 public Python tests PASS; the complete qualification suite passed before
  the public source export.
- 87/87 Perl tests PASS.
- Real API14 and API15 read-only iSCSI/FCoE checks passed with redundant paths
  and exact WWID/PV/VG identity.
- Two package builds were byte-for-byte identical and passed package
  security/content validation. DEB SHA256:
  `f3d2eee2b939f2112f17a73052565c4fd78aceed58b0691ac96d8f4f712738f3`.
- Sequential upgrade from RC5 and same-version reinstall passed on all three
  qualification nodes. Each of the six `dpkg -i` runs completed successfully.
- After the three-node deployment, both configured SharedLvmThin storages
  remained active everywhere, all six per-storage recovery checks returned
  `SAFE_FOR_MUTATION=YES`, quorum remained healthy and no D-state task was
  present. Configured dashboards remained active; the intentionally
  unconfigured node remained unconfigured and inactive.

RC5.2 remains a release candidate. Hardware FC, late physical SAN discovery
and host dm-thin recovery following complete path loss remain external gates.
