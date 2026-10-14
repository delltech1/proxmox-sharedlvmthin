# RC5.97 TG53 — experimental API14–16 bridge

> [!CAUTION]
> **Both Thin and Thick (Eager and Lazy), in both package profiles, remain
> experimental. Use disposable Proxmox VE hosts, shared SAN storage and guest
> data only.** Not production-ready, certified, supported or warranted.
> Keep independently tested backups and recovery procedures. Test results
> describe exercised workloads, not a guarantee against loss or I/O pauses.

Read the [risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md).

Debian version: `0.9.0~rc5.97~tg53`. Both packages retain the same Thick
implementation and on-disk format; install one profile consistently across
participating Proxmox VE cluster nodes.

## Changes

- Storage API 14, 15 and 16 lifecycle adapters, including API16 RAW format
  contracts and exact package-family checks.
- Plugin-first bridge workflow: replace the plugin before intentionally
  updating the Proxmox storage stack, then settle each maintenance transaction.
- Stronger package preservation and executor fencing, including canonical
  volume, mapper, process and runtime identity checks for both profiles.
- Boot-bound qualification and exact-predecessor replay of completed reboot
  settlement without repeating the original mutation.
- Recovery/reference checks and read-only health/inventory improvements.
- Reusable native task and guest-protocol regression tooling, and immutable
  publication checks that refuse replacement of an existing release.

## Test scope

The RC97 source suite completed **2,647 Python tests: 2,594 passed and 53
declared platform/CI skips**, plus **1,691 Perl tests across 46 files**.
Shell syntax, ShellCheck, reproducible builds, DUAL/Thick-only payload parity,
package identity, source privacy and both DEB content/security checks passed.

Current-build lab checks included:

- API14 clean DUAL and Thick-only installation, upgrade and profile replacement
  on an isolated control system.
- Live DUAL plugin replacement with one three-media guest and four additional
  three-disk guests; ordered write/read/SHA witnesses and existing guest
  process identities continued across package replacement.
- API15 plugin-first installation followed by the exact common/storage/QEMU
  server package transition to API16, with APT refusal, explicit one-shot
  admission, package verification and runtime settlement.
- Signed kernel update, one reboot, automatic iSCSI reconnection, multipath
  identity/path checks, strict peer SSH, quorum and post-boot qualification.
- Current kernel three-disk RAM snapshot, RAM A/B/A rollback verification,
  original guest boot identity, snapshot deletion and stopped final state.

The existing regression campaign also exercised Thin, Eager and Lazy storage,
whole-byte SHA checks, multi-disk snapshots and rollback, growth/zero-tail
checks, native backup/restore, migrations and profile replacement. Evidence
remains version- and operation-bound: historical tests are not silently
relabeled as fresh tests of another artifact.

## Recorded platform versions

| Exercised scope | API | Kernel | Manager | Storage | Common | QEMU server | QEMU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Clean package/profile control | 14 | 7.0.2-6-pve | 9.2.2 | 9.1.5 | 9.1.12 | 9.1.15 | 11.0.0-3 |
| Plugin-first package transition source | 15 | 7.0.14-20-pve | 9.2.21 | 9.1.11 | 9.2.2 | 9.2.10 | 11.0.3-4 |
| API16 package settlement | 16 | 7.0.14-20-pve | 9.2.21 | 9.1.13 | 9.2.3 | 9.2.11 | 11.0.3-4 |
| Reboot/reconnection and three-disk RAM lifecycle | 16 | 7.0.14-23-pve | 9.2.21 | 9.1.13 | 9.2.3 | 9.2.11 | 11.0.3-4 |

Recorded SAN-stack dependencies: LVM `2.03.31-2+pmx1`,
thin-provisioning-tools `1.1.0-4+b1`, multipath-tools `0.11.1-2`,
Corosync `3.1.10-pve3`, pve-ha-manager `5.2.5`, and
libpve-cluster-perl `9.1.6`. Consult the compatibility catalogue and runtime
checks for the exact operation and package tuple rather than inferring
universal compatibility from an API number.

## Installation and updates

Packages: `pve-sharedlvmthin` (DUAL) or `pve-sharedlvmthin-thick` (Thick-only).
They are mutually exclusive. Verify `SHA256SUMS` before installation and follow
the [installation guide](installation.md). Thin and Thick require physically
separate dedicated VGs; Eager and Lazy may share a dedicated Thick VG.

Select `freeze`, `qualified-auto` or `warn` deliberately. Catalogue entries
marked `RETEST_REQUIRED` are not automatic upgrade authorization; an exact
administrator-authorized plan still requires its public settlement workflow.
For a concrete kernel update the order is install, actual new boot,
`sharedlvmthin update-policy postcheck`, then runtime qualification. Do not
run a pre-boot postcheck or restart guest disk mappings to install the plugin.

Release assets contain both DEBs and `SHA256SUMS`. Lab identities, credentials,
private addresses, hostnames, device identifiers, logs and Python cache files
are excluded. Sanitized feedback and reproducible failure reports are welcome.
