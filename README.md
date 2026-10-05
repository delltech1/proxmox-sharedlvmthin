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

New Thick Generations transitions currently use a deterministic **1 MiB
dm-clone region candidate** by default; it is not selected dynamically from
transient storage speed. This candidate still requires representative physical
first-write and foreground-tail qualification. Existing and interrupted
transitions always recover the exact signed region geometry with which they
were created, including legacy 4/8 KiB objects—an upgrade never rewrites that
geometry. A 4 KiB write mentioned by a latency test is simulated guest I/O,
not the new dm-clone region default.

Both modes integrate through the standard Proxmox Storage API and support
snapshots, rollback, resize, backup and Storage Move. A running VM whose disks
start in Thin can migrate online through the Materialized Migration Bridge:
the disks are moved online to independent Thick Generations, normal PVE
shared-storage live migration runs, and the disks can then be moved online to
new target-owned Thin pools. Only direct in-place shared dm-thin migration is
refused, because PVE's source/target activation overlap would expose the same
non-cluster-coherent dm-thin metadata to two kernels. An offline
deactivate-then-activate Thin handoff remains available. The
implementation uses standard Proxmox orchestration and Linux LVM,
device-mapper and multipath components—without proprietary SAN integration,
third-party kernel modules or an external locking service.

Lazy Thick has an additional migration boundary. An unmaterialized running
Lazy disk has persistent dm-clone metadata owned by one node/boot epoch and
cannot overlap activation on a second kernel. Before online migration, run the
read-only check and follow every reported materialization command:

```text
sharedlvmthin migration-preflight <vmid> <target-node> --online
sharedlvmthin storage-move-preflight <vmid> <disk> <target-storage>
sharedlvmthin volume-operation-preflight <resize|attach> <vmid> <disk-key|volume-id>
sharedlvmthin qmdestroy-contract-check
sharedlvmthin thick-lazy-materialize <storage-id> <volume>
sharedlvmthin migration-preflight <vmid> <target-node> --online
qm migrate <vmid> <target-node> --online 1
```

Materialization runs on the active source while the VM remains running. A
materialized Lazy disk then uses the ordinary Thick linear/live-migration
contract. A stopped `LAZY_DORMANT` disk has a separately guarded offline
handoff. The plugin cannot safely inject automatic source materialization into
the current PVE shared-volume migration lifecycle, so target activation remains
the authoritative fail-closed backstop if the preflight is skipped.

The SAN LUN remains shared and visible to every participating node. The plugin
does not use array snapshots, move LUNs between hosts, configure the SAN,
replace fencing or quorum, or make simultaneous writable activation safe.

If you already use Ceph, NFS or a suitable vendor-native Proxmox plugin, you
probably do not need this project.

## Release status

`RC5.89 TG53` (`0.9.0~rc5.89~tg53`) is the current experimental pre-release.
It is intended exclusively for disposable Proxmox VE 9 labs with
disposable storage and guest data. Both Thin and Thick Generations remain
experimental, unsupported and without warranty.

This is a major hardening candidate. Existing materialized Thick v5 objects
remain compatible; Lazy introduces persistent v6 transition state that requires
a Lazy-aware runtime and recovery path and must not be downgraded blindly. It
adds a separately installable Thick-only package,
strict separation of Thin and Thick VGs, durable fail-closed mutation and
package-replacement gates, and expanded upgrade/reboot compatibility checks.
Eager and Lazy aliases may share one dedicated Thick VG. Thin must use a
different dedicated VG.

The final source candidate passed 2,526 Python tests (53 environment-dependent
skips) and 1,674 Perl tests. Both package
profiles were built reproducibly, inspected for forbidden/private content,
and compared for shared-payload parity. The same executable lineage previously
passed a rolling DUAL-package upgrade and controlled reboot with Eager, Lazy
and Thin guest lifecycle checks. The exact TG53 DUAL candidate was installed
through the supported profile gate on three SAN nodes; named-RAM-snapshot
clone and two-format/two-disk import checks then passed. These results apply
only to the recorded disposable lab
envelope; they are not production certification or proof for every SAN,
kernel, firmware, topology, workload or failure sequence.

The exact PVE package tuples and the scope tested on each are recorded in the
[compatibility matrix](docs/compatibility.md). Do not infer compatibility from
`PVE 9.2.x`, APIVER alone, successful package installation or a version range.
Minor Proxmox updates can change storage, QEMU, restore or kernel semantics.
Unlisted relevant tuples require a reviewed contract check and, when the
changed path can mutate or carry guest data, targeted disposable-lab
regression before cluster rollout.

See the [RC5.89 TG53 release notes](docs/RELEASE-NOTES-RC5.89-TG53.md),
the previous [RC5.88 TG53 release notes](docs/RELEASE-NOTES-RC5.88-TG53.md),
the previous [RC5.87 TG53 release notes](docs/RELEASE-NOTES-RC5.87-TG53.md),
the previous [RC5.79 TG53 release notes](docs/RELEASE-NOTES-RC5.79-TG53.md),
the previous [RC5.75 TG53 release notes](docs/RELEASE-NOTES-RC5.75-TG53.md),
the previous [RC5.74 TG53 release notes](docs/RELEASE-NOTES-RC5.74-TG53.md),
the previous [RC5.31 TG52 release notes](docs/RELEASE-NOTES-RC5.31-TG52.md),
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

- Proxmox VE 9. RC5.89 retains the API 14/15 source contract. Its exact DUAL
  artifact passed the documented API 15 package/update/reboot and targeted SAN
  lifecycle gates on the exact tuple in the compatibility matrix. This does
  not qualify unlisted tuples or operations. API 14 still requires
  its documented current-artifact package/profile/reboot and SAN replay and
  therefore remains `RETEST_REQUIRED` for RC5.89 dataplane use.
  Proxmox VE 8 and earlier are unsupported.
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
mappings are positively absent. Direct in-place shared dm-thin live migration
is unsupported and fails closed. Online migration of a running VM whose disks
start and finish in Thin is provided by the experimental Materialized
Migration Bridge (`Thin -> Thick -> live migration -> Thin`), without
overlapping Thin ownership. Offline movement is also supported through an exact
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
# DUAL profile: experimental Thin + Thick Generations
DEB="$PWD/pve-sharedlvmthin_0.9.0.rc5.89.tg53_all.deb"

# OR Thick-only profile: experimental Thick Generations only
DEB="$PWD/pve-sharedlvmthin-thick_0.9.0.rc5.89.tg53_all.deb"

HASH="$(sha256sum "$DEB" | awk '{print $1}')"
sudo experiments/thick-generations/package-profile-gate.sh \
  --package "$DEB" --sha256 "$HASH" \
  --expect-host "$(hostname)" --expect-current none \
  --select-update-policy freeze --execute
```

Install the same version on every participating PVE node, one node at a time.
The package does not create a PV, VG, filesystem, SAN session, or multipath
configuration.

Configure and validate storage using [the installation guide](docs/installation.md).
Never run `pvcreate`, `vgcreate`, `wipefs`, or repair commands against an
unknown or unavailable device.

## Upgrade or reinstall

Run the read-only gate first, then replace one node at a time:

```bash
sharedlvmthin upgrade-check
DEB="$PWD/pve-sharedlvmthin_0.9.0.rc5.89.tg53_all.deb"
HASH="$(sha256sum "$DEB" | awk '{print $1}')"
sudo experiments/thick-generations/package-profile-gate.sh \
  --package "$DEB" --sha256 "$HASH" \
  --expect-host "$(hostname)" --expect-current dual \
  --select-update-policy freeze --execute
sharedlvmthin doctor
sharedlvmthin recovery-check <storage-id>
```

Use the Thick-only filename instead only for a qualified Thick-only node.
The profiles conflict and cannot coexist. Profile replacement has additional
fail-closed requirements documented in the installation guide; uninstalling a
profile is never a storage migration. Existing dashboard configuration,
certificates, and administrator-owned LVM policy are preserved when their
ownership can be proved.

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
prove -Itests/unit/lib -v tests/unit/*.t
sh scripts/check-reproducible-packages.sh
sh scripts/build.sh dist/dual dual
sh scripts/build.sh dist/thick-only thick-only
sh scripts/compare-package-profiles.sh dist/dual/*.deb dist/thick-only/*.deb
```

The default dual-mode package is written to `dist/`. The Thick-only command
uses a separate output directory so neither artifact nor checksum manifest can
overwrite the other. Both are validated for forbidden content and common
credential leaks.

The separately installable `pve-sharedlvmthin-thick` profile exposes only
experimental Thick Generations. It shares the same Thick implementation and
on-disk format with the dual-mode package; it is not a second storage driver.
The packages conflict intentionally and must never be installed together. See
the [Thick-only package boundary](docs/thick-only-package.md). A CI artifact is
only a build candidate, not a published or qualified release.

Use separate VGs for Thin and Thick storage. Eager and Lazy Thick aliases may
share the Thick VG because both reserve their full capacity. TG53 rejects a
same-VG Thin+Thick topology in both package profiles. The parser retains the
retired `mixed` token only so a rolling cluster can read old remote config;
preinstall and every runtime operation reject it. The Thick-only package
additionally never exposes a Thin allocation mode.

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
Direct in-place Thin live-migration success is retained only as historical
evidence and is not a safety claim; current Thin activation fails closed before
cross-node overlap. Online migration from Thin uses the materialized bridge.
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
transport-agnostic. See [compatibility](docs/compatibility.md) and the
[sanitized qualification summary](docs/thick-generations-poc-status.md).

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



