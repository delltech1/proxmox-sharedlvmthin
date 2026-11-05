# RC5.101 TG53 — experimental compatibility and recovery update

> [!CAUTION]
> **Both Thin and Thick, including Eager and Lazy, are experimental. Use only
> disposable Proxmox VE hosts, shared SAN storage and guest data.** This is
> not production-ready, certified, supported or warranted. Keep independently
> tested backups and recovery procedures. Test results apply only to the
> observed operations and do not guarantee against data loss or I/O pauses.

Read the [risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md).

Debian version: `0.9.0~rc5.101~tg53`. Choose either the DUAL package or the
Thick-only package; install the same profile and version on participating
cluster nodes. Both profiles share the Thick implementation and RAW format.

## Changes

- Extend exact package and kernel identity checks for the API 14–16 bridge,
  including the newer PVE common library and kernel candidate.
- Harden package preservation, runtime settlement, recovery and health checks.
- Strengthen Thick volume identity and lifecycle handling while retaining
  existing Eager and Lazy volume semantics.
- Preserve the `freeze`, `qualified-auto` and `warn` administrator choices;
  an unqualified update is not automatically treated as compatible.

## Test scope

The tested candidate source passed **2,664 Python tests** (one skipped) on
kernel 7.0.14-26-pve and **1,706 Perl tests** across 46 files on kernel
7.0.14-28-pve. These lab results apply to the preceding candidate; subsequent
release preparation changed documentation, Git attributes and the spelling of
two timeout validation conditions. Package privacy, reproducibility and shared
Thick payload parity are checked again for the final release artifacts.

Disposable lab checks included:

- API14 DUAL in-place package upgrade. An immediately preceding candidate also
  completed a DUAL → Thick-only → DUAL profile cycle on API14 without SAN.
- DUAL upgrades on two SAN nodes while disposable guests performed repeated
  write, flush, read and SHA-256 checks: one guest used Thin, Eager and Lazy
  media, and the other used three Eager disks.
- Thick-only package replacement on a SAN node, followed by a return to DUAL.
- Kernel 7.0.14-28-pve installation and reboot on one evacuated node, iSCSI and
  multipath reconnection, peer SSH checks, quorum and runtime settlement.
- One live migration of a three-disk materialized Thick VM from kernel
  7.0.14-26-pve to 7.0.14-28-pve.
- On kernel 7.0.14-28-pve, an offline five-disk OVMF/Q35 snapshot, rollback and
  snapshot deletion; full-disk SHA-256 matched on all five disks. The disks
  were not deliberately changed between snapshot and rollback.
- An offline Thick disk expansion retained the original 256 MiB SHA-256 and
  returned zeroes across the newly added 64 MiB.

API15 hook checks used extracted PVE classes. A full API15-to-API16 rolling
transition and every possible SAN, guest configuration or failure sequence are
not established by these checks. `RETEST_REQUIRED` remains the catalogue
classification for the candidate tuples and rolling edge.

## Recorded platform versions

| Exercised scope | API | Kernel | Manager | Storage | Common | QEMU server | QEMU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| API14 control-plane package upgrade | 14 | 7.0.2-6-pve | 9.2.2 | 9.1.5 | 9.1.12 | 9.1.15 | 11.0.0-3 |
| Live plugin upgrade with mixed guest media | 16 | 7.0.14-16-pve | 9.2.18 | 9.1.12 | 9.2.3 | 9.2.10 | 11.0.3-3 |
| Live plugin upgrade and migration source | 16 | 7.0.14-26-pve | 9.2.25 | 9.1.13 | 9.2.6 | 9.2.14 | 11.1.3-1 |
| Reboot, migration target and offline lifecycle | 16 | 7.0.14-28-pve | 9.2.25 | 9.1.13 | 9.2.7 | 9.2.14 | 11.1.3-1 |

Recorded SAN dependencies include LVM `2.03.31-2+pmx1`,
thin-provisioning-tools `1.1.0-4+b1`, multipath-tools `0.11.1-2`,
Corosync `3.1.10-pve3`, pve-ha-manager `5.2.5` and libpve-cluster-perl
`9.1.8`. These records do not qualify unlisted versions or workloads.

## Installation and updates

Verify `SHA256SUMS` and follow the [installation guide](installation.md).
Thin and Thick require separate dedicated VGs; Eager and Lazy may share the
dedicated Thick VG. Read the [compatibility information](compatibility.md)
and [known issues](known-issues.md) before testing.

Choose an update policy deliberately. Deploy a compatible bridge to every
participating node before a platform transition, then evacuate and update one
node at a time. For a kernel update, boot the intended kernel, run the public
postcheck and complete runtime qualification before returning workloads.
Feedback and sanitized reproducible reports are welcome.
