# SharedLvmThin RC5.4 TG12 release notes

RC5.4 TG12 is the first public dual-mode release candidate. One Debian package
supports explicit `thin` and `thick-generations` storage definitions over an
existing dedicated shared LVM VG. It is intended exclusively for Proxmox VE 9
with Storage API 14 or 15.

## Allocation modes

- **Thin** retains the established one-LVM-thin-pool-per-VM model and guarded
  autogrow behavior.
- **Thick Generations** uses fully allocated independent generation LVs. Its
  steady-state guest device is linear; snapshot and rollback materialization
  use a temporary persistent `dm-clone` transition.

Two PVE storage IDs can expose both modes over the same pinned VG. They report
the same physical capacity and must not be added together. An ordinary PVE
Storage Move performs an explicit Thin-to-Thick or Thick-to-Thin conversion.

## Qualification evidence

The accepted candidate passed:

- 156 Python tests and 236 Perl tests, Bash syntax, ShellCheck and Python
  compilation;
- two independent byte-identical package builds;
- installation and same-version reinstallation on a three-node PVE 9.2.x
  cluster containing Storage API 14 and 15 nodes;
- two-node and three-node quorum, QDevice and fail-closed mutation gates;
- Thin and Thick allocation, snapshot, rollback, resize, deletion, cross-node
  reconstruction, live migration and exact cleanup;
- Thin-to-Thick and Thick-to-Thin Storage Move with block-hash verification;
- native PVE backup and cross-mode restore;
- Veeam HotAdd backup of Thin, Thick and mixed guests and supported-console
  restore to Thin, Thick and mixed single-target layouts, with eight restored
  destination hashes and four unchanged source hashes verified;
- a clean four-hour dual-mode endurance run with zero host probe, path,
  quorum or persistent D-state failures.

Veeam replication was not available in the installed edition and is not
claimed.

## Safety boundaries

All mutating operations require positive storage identity, ownership, quorum,
cluster lock, capacity reserve and recovery-health evidence. Missing transport
is `UNAVAILABLE`, never empty storage. Ambiguous, foreign, legacy or partial
objects are preserved; the plugin never initializes an unknown device or
automatically repairs storage metadata.

The plugin does not configure SAN sessions, multipath, fencing, quorum or
automatic recovery. Existing guest I/O during a complete path loss follows
the QEMU, device-mapper, multipath and SAN policies below the plugin.

## Tested hardware envelope

Multipathed iSCSI and a virtual Linux FCoE laboratory target were exercised.
Representative physical enterprise FC hardware was not available. This
release therefore does not claim physical-FC hardware qualification; validate
the intended HBA, fabrics, array, firmware and multipath policy on disposable
storage before production use.

## Install

Verify `SHA256SUMS`, install the same package on every participating node, and
follow `docs/installation.md`. Existing PV/VG creation is an explicit one-time
administrator provisioning action and must never be repeated as recovery.

This remains a pre-release. Preserve verified backups and qualify it against
the intended production topology and failure policy before carrying production
data.
