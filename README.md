# BASTRIX SharedLVM — Snapshot-Capable Shared FC/iSCSI SAN Storage for Proxmox VE 9 Clusters

BASTRIX SharedLVM is an open-source storage plugin for Proxmox VE 9 clusters.
It provides snapshot-capable Thin and Thick allocation modes on an existing
shared LVM volume group backed by FC, FCoE or iSCSI SAN storage. This project
was originally published as SharedLvmThin; the existing package, command and
storage-plugin identifiers remain compatible.

> [!CAUTION]
> **BOTH THIN AND THICK GENERATIONS ARE EXPERIMENTAL SOFTWARE FOR DISPOSABLE
> LABORATORY SYSTEMS AND DISPOSABLE DATA ONLY.** Neither allocation mode is
> production-ready, supported, certified, or warranted. Do not use
> this release candidate with production workloads, irreplaceable data, or as
> the only copy of any data. A defect, operator error, incomplete fencing,
> storage latency or failure, host failure, update, or incompatible platform
> change can cause loss, corruption, unavailability, or an unrecoverable
> cluster state. Maintain independently tested backups and recovery procedures.
> Installation or use means that the operator accepts these risks to the
> maximum extent permitted by applicable law. The software is provided under
> GPLv3, including its sections 15–17 warranty disclaimer and limitation of
> liability. This operational warning explains the tested scope; it does not
> add a restriction to the GPL or override rights and liabilities that cannot
> legally be excluded.

Read the full [experimental risk and support boundary](docs/RISK-AND-SUPPORT-BOUNDARY.md)
before installing or testing the plugin.

## Project status — testing only

This repository publishes an **experimental community test version**. Use it
only with disposable hosts, disposable shared storage, and disposable guest
data in a laboratory environment. This applies equally to **Thin** and
**Thick Generations**. A successful test, long-running test, or previous
release does not make either mode production-ready or certify it for another
environment.

Every good-faith report is welcome: successes, failures, suspected corruption,
timeouts, performance problems, unusual SAN behavior, upgrade regressions and
reproducible edge cases all help define the real safety envelope. Reports must
be sanitized and must never contain credentials, private keys, guest data,
PVE tickets, private addresses, hostnames, WWIDs or other infrastructure
secrets. Acknowledging, discussing or acting on a report is voluntary and does
not create an entitlement to support, a fix, a response time or continued
maintenance.

This is a community project. No support, maintenance response time, service
level, update schedule, compatibility commitment, warranty, or duty to fix is
offered or promised. A voluntary reply, issue review, patch, or release does
not create a support relationship or future obligation.

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
snapshots, rollback, resize, backup and Storage Move. Materialized Thick
Generations support normal shared-storage live migration. Thin mode requires
an offline deactivate-then-activate handoff; overlapping Thin live migration
is intentionally refused because dm-thin metadata is not cluster-coherent. The
implementation uses standard Proxmox orchestration and Linux LVM,
device-mapper and multipath components—without proprietary SAN integration,
third-party kernel modules or an external locking service.

The SAN LUN remains shared and visible to every participating node. The plugin
does not use array snapshots, move LUNs between hosts, configure the SAN,
replace fencing or quorum, or make simultaneous writable activation safe.

If you already use Ceph, NFS or a suitable vendor-native Proxmox plugin, you
probably do not need this project.

## Release status

`RC5.11 TG32` (`0.9.0~rc5.11~tg32`) is the current experimental pre-release,
intended exclusively for disposable Proxmox VE 9 laboratories. Both Thin and
Thick Generations remain experimental. TG32 incorporates the previously
published TG31+fix2 behavior plus additional timing, scale, ambiguity and
postcondition hardening; it is not a new storage format. The inherited hotfix
baseline passed 198 Python tests and 799 Perl tests plus targeted concurrent
snapshot/rollback, restored-disk writes, resize and exact-cleanup lab checks.
The final package was reinstalled one node at a time across the three-node lab,
then exercised with the Thin/Thick add/remove matrix, exact offline cleanup and
four concurrent 100 GiB Thin VMs on two owner nodes. This hotfix did not repeat
the earlier long-duration endurance run.

TG24 introduced the Thin/Thick Generations architecture;
TG25 hardened package-update and reboot compatibility; TG26 added the
fail-closed single-kernel Thin ownership protocol. TG29 added an opt-in,
PVE-HA-fenced Thin takeover while direct overlapping Thin activation remains
prohibited. Earlier qualification includes fault injection, two-node and
three-node cluster checks, API 14/15 installation and reinstallation, and a
four-hour dual-mode endurance run on the Proxmox VE 9.2.x release line.
Historical results do not imply every scenario was rerun for this hotfix.

The TG24 disposable multi-node qualification ran 150 Thin and 150 Thick VMs
concurrently and covered bounded parallel Thick migration, native HA relocation,
snapshot/rollback, Thin-to-Thick and Thick-to-Thin storage moves, backup and
restore, rolling API 14/15 installation, exact cleanup and endurance testing.
TG25 additionally qualifies a full PVE package upgrade and reboot followed by
automatic transport recovery, storage revalidation and Thin/Thick lifecycle
operations. This is a laboratory release candidate, not validation of
200--500 VM enterprise scale or universal certification of every SAN, HBA,
array, multipath policy, firmware or failure mode. Evaluate it only on
disposable storage matching the prospective design.

TG30 hardens the online Thin migration bridge with observable progress,
pre-sized return-to-Thin pools, a per-pool 95% mutation gate, and exact
evidence-based finalization after an interrupted final health check.
TG31 adds authenticated per-disk manifests, a deterministic recovery planner,
and mandatory QMP live-path correlation so pmxcfs/runtime divergence after an
interrupted block job fails closed instead of selecting a copy by inference.

See the [RC5.11 TG32 release notes](docs/RELEASE-NOTES-RC5.11-TG32.md),
the historical [TG31+fix2 hotfix notes](docs/RELEASE-NOTES-RC5.10-TG31-FIX2.md),
[TG31+fix1 hotfix notes](docs/RELEASE-NOTES-RC5.10-TG31-FIX1.md),
[installation guide](docs/installation.md), [TG31 development notes](docs/RELEASE-NOTES-RC5.10-TG31.md), the
[TG30 release notes](docs/RELEASE-NOTES-RC5.9-TG30.md), the
[TG28 release notes](docs/RELEASE-NOTES-RC5.7-TG28.md), the
[TG26 release notes](docs/RELEASE-NOTES-RC5.5-TG26.md), the
[TG25 hotfix notes](docs/RELEASE-NOTES-RC5.4.1-TG25.md) and the original
[TG24 milestone notes](docs/RELEASE-NOTES-RC5.4-TG24.md) for the exact tested
envelope, package identity and remaining support boundaries.

The previously published RC5.2/RC5.3 Thin behavior remains available through
the explicit `thin` allocation mode. Thick Generations is newer and should be
treated as a release-candidate technology until it has broader independent
hardware and workload coverage.

### TG24 dual-mode milestone

[`RC5.4 TG24`](https://github.com/delltech1/proxmox-sharedlvmthin/releases/tag/v0.9.0-rc5.4-tg24)
introduced a second, explicitly selected **Thick Generations** allocation mode next
to the existing per-VM Thin mode. Thick snapshots become independent fully
allocated linear LVs after a temporary persistent `dm-clone` transition, so
there is no permanent snapshot chain in the steady-state guest I/O path.
TG25 is a compatible hardening update to that milestone, not a new storage
format. See the [TG24 milestone notes](docs/RELEASE-NOTES-RC5.4-TG24.md).

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
[critical recovery guidance](docs/critical-storage-recovery.md), and the
[allocation-mode guide](docs/allocation-modes.md) first. Measured concurrency
limits and the 300-VM dual-mode lab evidence are recorded in
[scale qualification](docs/scale-qualification.md). The stricter
[TG31 objective matrix](docs/QUALIFICATION-MATRIX-TG31.md) separates direct
PASS evidence from partial and still-open enterprise claims.

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

## Thin single-owner boundary

Per-VM pools contain the failure domain but do not make dm-thin metadata safe
for concurrent activation by two kernels. Cluster locks protect administrative
LVM mutations; guest allocation, COW and discard updates occur inside the
kernel after QEMU opens the disk.

Every managed Thin pool therefore has a versioned persistent owner consisting
of one PVE node plus a fresh activation epoch. A second node cannot activate it
until the first node has closed every child and the public and hidden pool
mappings are positively absent. Thin live migration is
unsupported and fails closed. Offline movement is supported through an exact
deactivate-then-activate handoff. A stale owner after host loss requires proven
external fencing and the explicit recovery procedure in
[migration.md](docs/migration.md).

Pools created by an older release do not have this ownership schema and are
never guessed to be unowned. Upgrade requires an explicit all-nodes-inactive,
one-time adoption step. The package refuses to install over a locally active
legacy pool. See the migration guide before upgrading an existing Thin setup.

Materialized Thick Generations are different: their steady-state frontend is
an ordinary linear mapping to a fully allocated independent LV, so this
dm-thin ownership restriction does not apply.

## Install a release package

Download the `.deb` and `SHA256SUMS` from the GitHub release, then verify it:

```bash
sha256sum --check SHA256SUMS
apt install './pve-sharedlvmthin_0.9.0.rc5.5.tg26_all.deb'
```

Install the same version on every participating PVE node, one node at a time.
The package does not create a PV, VG, filesystem, SAN session, or multipath
configuration.

Configure and validate storage using [the installation guide](docs/installation.md).
Never run `pvcreate`, `vgcreate`, `wipefs`, or repair commands against an
unknown or unavailable device.

## Upgrade or reinstall

```bash
apt install './pve-sharedlvmthin_0.9.0.rc5.4.1.tg25_all.deb'
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

## Qualification summary

The TG24 milestone and TG25 maintenance candidate exercised both modes through allocation,
online and offline lifecycle operations, snapshot/rollback and resize. TG26 is
the fail-closed single-kernel Thin-ownership development pre-release. Its
disposable-lab gate, including real external owner-host fencing, survivor
refusal, explicit recovery, fresh epoch and exact data canary, has passed. It
is not universal production certification; representative physical SAN/HBA
qualification and local acceptance remain required. Earlier
Thin live-migration success is retained only as historical evidence and is not
a safety claim; current Thin activation fails closed before cross-node overlap.
The qualification also covered materialized Thick live migration, cross-node
reconstruction, Thin-to-Thick and Thick-to-Thin Storage Move, native PVE
backup/restore, and exact cleanup. Veeam qualification covered
HotAdd backup of Thin, Thick and mixed guests plus supported-console restores
to Thin, Thick and mixed single-target layouts with block-hash verification.
Veeam replication was not available in the installed edition and is not
claimed.

Multipathed iSCSI and virtual Linux FCoE were exercised. Representative
physical enterprise FC hardware was not available; physical FC therefore
remains outside the tested hardware envelope even though the plugin itself is
transport-agnostic. See [compatibility](docs/compatibility.md) and the detailed
[qualification record](docs/thick-generations-poc-status.md).

## Support the project

If SharedLvmThin helped you, consider giving the repository a GitHub Star.
Feedback, reproducible issue reports, and anonymized compatibility results for
Proxmox VE, SAN arrays, HBAs, multipath configurations, and firmware versions
are also appreciated. Never include credentials, private addresses, WWIDs, or
other sensitive infrastructure identifiers in a public report.

## Community and feedback

- [Proxmox Support Forum project thread](https://forum.proxmox.com/threads/project-sharedlvmthin-for-proxmox-ve-9-%E2%80%94-shared-fc-iscsi-san-storage-with-lvm-thin-snapshots-multipath-safety.186250/)
- [Reddit r/Proxmox community showcase](https://www.reddit.com/r/Proxmox/comments/1weg33h/community_showcase_bastrix_sharedlvm_for_proxmox/)
- [GitHub Discussions](https://github.com/delltech1/proxmox-sharedlvmthin/discussions)
- [Bug reports and feature requests](https://github.com/delltech1/proxmox-sharedlvmthin/issues)

Please use Discussions for design questions and general usage. Use Issues for
reproducible defects and include sanitized diagnostics only.

## License and support

Copyright (C) 2026 BASTRIX Project Contributors.

Licensed under GPL-3.0-only. Commercial redistribution is permitted by the
license, but distributors must comply with all GPLv3 obligations, including
preserving applicable notices and providing Corresponding Source when
required. See [NOTICE](NOTICE) and [LICENSE](LICENSE).

BASTRIX is a registered European Union word trade mark
(EUIPO No. `019343330`), covering Nice classes 9 and 42. The official EUIPO
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


