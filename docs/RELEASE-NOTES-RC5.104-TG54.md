# BASTRIX SharedLVM RC5.104 TG54 — Thick-only experimental candidate

> **Experimental: use only disposable hosts, shared storage and guest data.**
> This software is not production-ready or warranted. Review the installation
> guide, compatibility information and recovery requirements before testing.

This Thick-only candidate adds read-only storage setup assistance for Proxmox
VE clusters using an existing shared block SAN. The dashboard helps observe
cluster storage and prepare commands for an administrator to review and run;
it does not initialize disks, configure the SAN or register storage itself.

- Guided setup covers iSCSI, Fibre Channel, FCoE and shared SAS. SAS has not
  been qualified on physical SAS hardware. A transport hint alone does not
  prove shared access, path independence or fencing.
- The registration preview gathers available VG, PV and multipath identities
  across nodes and offers separate Eager and Lazy commands. Observations are
  point-in-time information and can become stale; copied commands have not
  been executed or verified by the dashboard.
- Peer checks retain strict SSH host-key verification. The guides explain
  independently verifying fingerprints and relevant Proxmox cluster recovery.
- Thick VG reserve guidance explains the configurable reserve and capacity
  requirements; zero remains an administrator choice, not a safety guarantee.
- FCoE transport detection accepts both `fc` and `fcoe` host reports; LVM
  inventory avoids relying on potentially stale hints. A scan can still be
  limited by LVM filters, devices files, missing paths and timeouts.

## Important update limitation

The exact API 14 control-plane tuple below is listed as `RETEST_REQUIRED`;
the listing permits candidate admission subject to the existing runtime gates
and does not establish SAN qualification. No API 15 or API 16 tuple is listed
for this candidate; their preflight is expected to refuse or remain closed
depending on update policy. A live upgrade from RC5.103
and post-reboot storage operation have not been verified for this candidate.
Do not assume an existing cluster can be upgraded without interruption.
Retain the previous package and follow the documented recovery and
compatibility procedures.

## Verification scope

Automated registration and wider Python regression tests, source privacy and
package-content checks, and reproducible Thick package builds were run. An
authenticated read-only inventory was exercised against a disposable
three-node FCoE lab and an existing iSCSI-backed configuration. These results
do not qualify other hardware, storage failure modes or live upgrades.
Platform history for the prior release is described in the preceding release
notes; it is not evidence that the new artifact repeated those tests.

The API 14 inventory test used PVE manager 9.2.2, storage 9.1.5, common
9.1.12, QEMU server 9.1.15, QEMU 11.0.0-3 and kernel 7.0.2-6-pve. This
records the inventory environment, not a qualification of every operation.
