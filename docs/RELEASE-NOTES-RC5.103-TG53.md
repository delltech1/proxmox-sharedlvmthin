# RC5.103 TG53 — experimental guided storage helper

> [!CAUTION]
> Thin and Thick, including Eager and Lazy, are experimental. Use only disposable
> Proxmox VE hosts, shared storage and guest data. This release is not
> production-ready, certified, supported or warranted.

## Guided storage connection wizard

The existing web dashboard now includes an English/German guided helper for
administrators preparing shared storage. It explains the steps for iSCSI and
Fibre Channel/FCoE and offers guidance on discovery and checks.

**This is a guide, not a storage configurator.** It does not automatically
configure the storage array, create LUNs or volume groups, or establish cluster
safety. The administrator remains responsible for completing and verifying each
step. Instructions can vary with the storage vendor and Proxmox VE version.

## Upgrade and recovery improvements

- Fixed loading of the package-executor verification helper in the isolated
  Python environment used during candidate package checks.
- Preserved the verified package-executor fence context when switching package
  profiles, so the package operation can retain its intended lock protection.
- Improved recovery after an interrupted prepared package transition: the
  recovery procedure can safely replay the tested steps while keeping runtime
  admission closed until separately qualified.
- Improved checks that the package control helper matches the packaged payload.

These improvements were tested in the release candidate. Automatic recovery of a closed, unqualified runtime whose receipt belongs to
a previous boot is not implemented.

Both Thin and Thick Generations remain experimental. Use only disposable hosts,
shared storage and guest data. Consult the project documentation and release
compatibility information before testing.

## Validation

The candidate source passed selected API14, API15 and API16 interface checks for
both package profiles. On an API16 lab host, a DUAL package upgrade and subsequent
DUAL-to-Thick-only replacement completed through the package gate. A workload on
another cluster node continued recording verified writes during those operations.
A DUAL post-reboot check passed after the test iSCSI paths were reconnected.

These results describe the tested candidate and bounded scenarios; the final
release build has a distinct package identity. Clean Thick-only installation, its post-reboot SAN
lifecycle and the API15-to-API16 rolling transition have not been qualified for
this candidate. Tests do not guarantee against data loss or I/O pauses.

## Recorded platform versions

| Evidence scope | Storage API | Kernel | PVE manager | PVE storage | PVE common | QEMU server | QEMU |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Candidate DUAL upgrade and DUAL-to-Thick-only replacement | 16 | 7.0.14-28-pve | 9.2.25 | 9.1.13 | 9.2.7 | 9.2.14 | 11.1.3-1 |

The recorded host used LVM `2.03.31-2+pmx1`, thin-provisioning-tools
`1.1.0-4+b1`, multipath-tools `0.11.1-2`, Corosync `3.1.10-pve3`,
pve-ha-manager `5.2.5` and libpve-cluster-perl `9.1.8`.
API14 and API15 interface checks used separate installed or extracted libraries;
refer to the prior release's platform table for its historical test evidence.
Do not represent prior release results as tests of this candidate.

## Installation and updates

Choose either the DUAL or Thick-only package and install the same profile and
version on participating Proxmox VE cluster nodes. Verify release checksums and
follow the installation guide. Thin and Thick require separate dedicated shared
VGs; Eager and Lazy may share a dedicated Thick VG. Select the update policy
deliberately and review compatibility information before changing PVE packages.

Feedback, code review and sanitized reproducible reports are welcome.
