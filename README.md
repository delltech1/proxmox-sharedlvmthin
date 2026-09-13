# BASTRIX SharedLVM for Proxmox VE

**Shared FC/iSCSI SAN storage for Proxmox VE 9, with Thin Pools and Thick
Generations.** This project was originally published as SharedLvmThin; the
existing package, command and storage-plugin identifiers remain compatible.

## What is it for?

BASTRIX SharedLVM addresses a specific gap in native Proxmox storage support:
an existing shared FC, FCoE or iSCSI SAN can be used through the shared LVM
backend, but that backend does not support snapshots or clones. The native
LVM-thin backend supports snapshots and clones, but Proxmox supports it only as
local storage because an LVM-thin pool cannot be shared across cluster nodes.

This project adds snapshot-capable shared LVM storage for Proxmox clusters,
with per-VM ownership domains and two selectable modes. Retaining an existing
SAN during a VMware-to-Proxmox migration is one common use case, but migration
from VMware is not a requirement:

- **Thin** — an isolated LVM-thin pool per VM with guarded autogrow.
- **Thick Generations** — fully allocated independent generations with an
  ordinary linear steady-state device and temporary `dm-clone` transitions.

Both modes integrate through the standard Proxmox Storage API and support
snapshots, rollback, resize, backup, Storage Move and live migration. The
implementation uses standard Proxmox orchestration and Linux LVM,
device-mapper and multipath components—without proprietary SAN integration,
third-party kernel modules or an external locking service.

The SAN LUN remains shared and visible to every participating node. The plugin
does not use array snapshots, move LUNs between hosts, configure the SAN,
replace fencing or quorum, or make simultaneous writable activation safe.

If you already use Ceph, NFS or a suitable vendor-native Proxmox plugin, you
probably do not need this project.

## Release status

`0.9.0~rc5.4~tg12` is the current dual-mode pre-release for Proxmox VE 9 and
is published from the `experimental/thick-generations` branch. The established
Thin-only line remains available, while RC5.4 TG12 adds Thick Generations and
cross-mode operation. Qualification covers Proxmox VE 9.2.x with Storage API
14 and 15, but is not universal certification of every SAN, HBA, array,
multipath policy, firmware or failure mode. Validate it first on disposable
storage matching your production design.

For new deployments, the recommended capacity mode is `elastic`: physical
pool size follows actual allocation plus bounded absolute burst headroom, so a
multi-terabyte virtual disk does not reserve a proportional fraction of its
logical size. See [clone/restore burst capacity](docs/clone-restore-burst-capacity.md).

## Requirements

- Proxmox VE 9 with Storage API 14 or 15. Proxmox VE 8 and earlier are not
  supported by this release candidate.
- The same existing shared LUN, multipath identity, PV and VG visible on every
  participating node.
- Working cluster quorum, fencing and storage locking.
- Administrator-managed SAN, multipath and LVM device discovery.
- A dedicated VG; do not point the plugin at a VG containing unrelated data.

Read [storage requirements](docs/storage-requirements.md),
[iSCSI/FC/FCoE setup examples](docs/transport-setup-examples.md),
[known issues](docs/known-issues.md), and
[critical recovery guidance](docs/critical-storage-recovery.md) first.

## Per-VM thin-pool trade-off

The per-VM thin-pool model deliberately gives every VM a smaller metadata,
ownership, snapshot, and failure domain. The cost is reduced space elasticity
between VMs. Once physical extents have been assigned from the shared VG to one
VM's thin pool, blocks later discarded by that VM are reusable inside that pool
by the VM and its snapshots, but are not automatically returned to the VG for a
different VM's pool to consume.

Guarded incremental growth and conservative initial sizing reduce unnecessary
allocation, but do not remove this architectural trade-off. Administrators must
therefore monitor both free space inside each per-VM pool and unallocated free
space in the shared VG. SharedLvmThin favors isolation and deterministic
lifecycle behavior over the maximum space elasticity of one large thin pool
shared by many VMs.

## Install a release package

Download the `.deb` and `SHA256SUMS` from the GitHub release, then verify it:

```bash
sha256sum --check SHA256SUMS
apt install ./pve-sharedlvmthin_0.9.0.rc5.4.tg12_all.deb
```

Install the same version on every participating PVE node, one node at a time.
The package does not create a PV, VG, filesystem, SAN session, or multipath
configuration.

Configure and validate storage using [the installation guide](docs/installation.md).
Never run `pvcreate`, `vgcreate`, `wipefs`, or repair commands against an
unknown or unavailable device.

## Upgrade or reinstall

```bash
apt install ./pve-sharedlvmthin_0.9.0-rc5.3_all.deb
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

Copyright (C) 2026 BASTRIX.

Licensed under GPL-3.0-only. Commercial redistribution is permitted by the
license, but distributors must comply with all GPLv3 obligations, including
preserving applicable notices and providing Corresponding Source when
required. See [NOTICE](NOTICE) and [LICENSE](LICENSE).

BASTRIX is a registered European Union word trade mark (EUIPO No. `019343330`), covering Nice classes 9 and 42. The official EUIPO
record controls its current status and exact scope. The GPL license applies to
the code but does not grant rights to present a third-party build, fork,
product or support service as official, certified or endorsed. Truthful
compatibility and attribution statements remain welcome. See the
[trademark policy](TRADEMARKS.md).

This project is independent community software and
is not affiliated with or endorsed by Proxmox Server Solutions GmbH.

Report vulnerabilities through GitHub Private Vulnerability Reporting. General
bugs should include sanitized diagnostics only—never credentials, PVE tickets,
private keys, guest data, or organization-specific storage identities.


