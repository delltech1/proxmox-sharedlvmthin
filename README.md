# SharedLvmThin for Proxmox VE

SharedLvmThin is a safety-focused Proxmox VE storage plugin for an existing
shared LVM volume group. It provides one LVM-thin pool per VM, snapshots,
rollback, migration support, cluster locking, guarded autogrow, storage
identity checks, and an optional read-only HTTPS health dashboard.

The plugin manages storage objects. It is not in the guest I/O path after QEMU
opens an LV and does not provide SAN connectivity, multipath configuration,
replication, fencing, quorum, or automatic metadata repair.

## Release status

`0.9.0~rc5.2` is a release candidate intended exclusively for Proxmox VE 9.
It passed extensive unit, fault-injection and three-node integration
qualification on the Proxmox VE 9.2.x release line with Storage API 14 and 15,
but this is not universal certification of every SAN, HBA,
array, multipath policy, firmware, or failure mode. Validate it on disposable
storage matching your production design before carrying production data.

## Requirements

- Proxmox VE 9 with Storage API 14 or 15. Proxmox VE 8 and earlier are not
  supported by this release candidate.
- The same existing shared LUN, multipath identity, PV and VG visible on every
  participating node.
- Working cluster quorum, fencing and storage locking.
- Administrator-managed SAN, multipath and LVM device discovery.
- A dedicated VG; do not point the plugin at a VG containing unrelated data.

Read [storage requirements](docs/storage-requirements.md),
[known issues](docs/known-issues.md), and
[critical recovery guidance](docs/critical-storage-recovery.md) first.

## Install a release package

Download the `.deb` and `SHA256SUMS` from the GitHub release, then verify it:

```bash
sha256sum --check SHA256SUMS
apt install ./pve-sharedlvmthin_0.9.0-rc5.2_all.deb
```

Install the same version on every participating PVE node, one node at a time.
The package does not create a PV, VG, filesystem, SAN session, or multipath
configuration.

Configure and validate storage using [the installation guide](docs/installation.md).
Never run `pvcreate`, `vgcreate`, `wipefs`, or repair commands against an
unknown or unavailable device.

## Upgrade or reinstall

```bash
apt install ./pve-sharedlvmthin_0.9.0-rc5.2_all.deb
sharedlvmthin doctor
sharedlvmthin recovery-check <storage-id>
```

The same command safely handles a same-version reinstall. Existing dashboard
configuration, certificates, and administrator-owned LVM autogrow policy are
preserved.

## Optional HTTPS dashboard

Run the local configuration helper after package installation:

```bash
sharedlvmthin-web-configure
systemctl status pve-sharedlvmthin-web.service
```

The dashboard delegates login to the local PVE API and exposes monitoring only.
It does not expose storage mutation endpoints. See the
[HTTPS dashboard guide](docs/web-dashboard.md).

## Build and test

```bash
python3 -m unittest discover -s tests/unit -v
prove -v tests/unit/plugin_lifecycle.t
sh scripts/build.sh
```

The reproducible package is written to `dist/` and validated for forbidden
content and common credential leaks.

## Safety model

- Identity mismatch or ambiguity fails closed before mutation.
- Shared metadata mutation requires quorum and the PVE cluster storage lock.
- Missing transport means unavailable storage, never empty storage.
- Foreign, legacy, or unknown objects are never adopted or deleted by name.
- Partial failures preserve objects rather than guessing that cleanup is safe.
- Recovery checks are read-only and never initialize or repair a device.

See [data-safety invariants](docs/data-safety-invariants.md) and
[recovery-check](docs/recovery-check.md).

## Support the project

If SharedLvmThin helped you, consider giving the repository a GitHub Star.
Feedback, reproducible issue reports, and anonymized compatibility results for
Proxmox VE, SAN arrays, HBAs, multipath configurations, and firmware versions
are also appreciated. Never include credentials, private addresses, WWIDs, or
other sensitive infrastructure identifiers in a public report.

## License and support

Copyright (C) 2026 Stanislav Baran.

Licensed under GPL-3.0-only. Commercial redistribution is permitted by the
license, but distributors must comply with all GPLv3 obligations, including
preserving applicable notices and providing Corresponding Source when
required. See [NOTICE](NOTICE) and [LICENSE](LICENSE).

This project is independent community software and
is not affiliated with or endorsed by Proxmox Server Solutions GmbH.

Report vulnerabilities through GitHub Private Vulnerability Reporting. General
bugs should include sanitized diagnostics only—never credentials, PVE tickets,
private keys, guest data, or organization-specific storage identities.
