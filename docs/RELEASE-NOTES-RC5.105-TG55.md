# BASTRIX SharedLVM RC5.105 TG55 — Thick-only experimental candidate

> **Experimental: use only disposable hosts, shared storage and guest data.**
> This software is not production-ready or warranted. Read the installation,
> compatibility and recovery documentation before testing.

This Thick-only candidate extends the read-only storage setup assistant for
Proxmox VE clusters using shared block SAN storage. The assistant helps an
administrator inspect information and prepare commands for review; it does
not configure the SAN, initialize a disk, create a PV or VG, or register a
storage definition itself.

- Guided steps cover iSCSI, FC, FCoE and shared SAS. SAS is not qualified on
  physical SAS hardware. A transport hint alone does not establish end-to-end
  connectivity, path independence or fencing.
- The registration preview checks observed identities, existing definitions
  and eligible dedicated Thick VGs, and prepares Eager and Lazy commands.
  Observations can become stale; copying a command does not execute it.
- Peer SSH checks use strict host-key verification and fresh connections.
  The guide explains fingerprint verification and recovery when trust or
  connectivity is unavailable. SSH failure is not proof of fencing.
- Thick capacity admission accounts for separately rounded allocation
  components. The administrator chooses the VG reserve; the preview defaults
  to zero and warns about capacity exhaustion. A reserve does not guarantee
  that a later snapshot or generation will fit.
- Update-policy diagnostics distinguish a runtime identity receipt from
  evidence that individual storage operations have been qualified.

## Update and compatibility boundary

The candidate's exact runtime tuple is not listed in the bundled qualification
catalogue. The existing FREEZE workflow can inspect an unlisted candidate and
can settle a prepared package transition with the runtime closed; it does not
automatically establish an operationally qualified storage runtime. A live
upgrade and post-reboot SAN operation have not been verified for this build.
Do not assume a cluster update will complete without interruption. Retain the
previous package and follow the documented recovery and compatibility steps.

## Verification scope

The candidate source passed Python and Perl regression suites, package content
and privacy checks, reproducible Thick builds and a package-preservation dry
run. An authenticated browser test exercised the candidate dashboard against
read-only inventory on a disposable cluster, covering iSCSI and FCoE guidance
and existing storage definitions. The candidate was not installed for that
browser test; the inventory socket belonged to the preceding release. These
results do not qualify every array, path failure or storage operation.

| Evidence scope | Storage API | Kernel | PVE manager | PVE storage | PVE common | QEMU server | QEMU |
|---|---:|---|---|---|---|---|---|
| Read-only inventory and package-preservation dry run | 14 | 7.0.2-6-pve | 9.2.2 | 9.1.5 | 9.1.12 | 9.1.15 | 11.0.0-3 |

The table records the test environment and scope; it is not a claim that this
build passed a live upgrade, reboot or SAN lifecycle qualification. Earlier
release evidence remains associated with its own artifact and stated scope.
