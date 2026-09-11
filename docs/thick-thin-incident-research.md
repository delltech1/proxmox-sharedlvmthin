# Thick and Thin incident research matrix

This matrix records externally observed failure classes that are applicable to
SharedLvmThin. Forum reports are incident inputs, not authoritative recovery
instructions. Destructive commands suggested in third-party threads are never
copied into automatic plugin behaviour.

## PVE 9 rollback capability dispatch

A StarWind user report describes rollback being rejected because the plugin
inherited semantics intended for PVE 9 volume chains. The relevant failure
class is an incorrect or missing `volume_rollback_is_possible` implementation,
not a data-format failure.

- Source: <https://forums.starwindsoftware.com/viewtopic.php?start=15&t=7268>
- Applicability: yes, because SharedLvmThin implements native snapshot and
  rollback hooks on PVE Storage API 14 and 15.
- Mitigation: the plugin implements `volume_rollback_is_possible`, rejects
  linked clones explicitly, and dispatches rollback through its own Thin or
  Thick transaction model.
- Evidence: API 14/15 hook regressions plus live snapshot/rollback and
  post-rollback SHA-256 qualification in both modes.

## Missing or stale snapshot objects

Proxmox forum reports show both sides of partial lifecycle failure: a PVE
snapshot reference can survive a failed LV removal, or an LV/VM-state object
can survive after its PVE reference has disappeared.

- Sources:
  <https://forum.proxmox.com/threads/remove-missing-snapshot-thin-lvm.44134/>,
  <https://forum.proxmox.com/threads/lvm-thin-storage-issue-removing-old-state-files.87045/>
- Applicability: yes; process termination, timeout or node loss can interrupt
  any multi-step lifecycle.
- Mitigation: automatic cleanup is forbidden after an uncertain outcome.
  Normal deletion and explicit orphan recovery prove exact ownership, exact
  PVE references, quorum, the canonical storage lock and per-object
  postconditions. Unknown, legacy and foreign objects remain read-only.
- Evidence: snapshot-delete D0-D4 recovery tests, orphan-tree and Thin-orphan
  regressions, VMID reuse regressions, live crash injection and exact-cleanup
  checks.

## Thin metadata transaction mismatch or corruption

Forum incidents contain thin-pool `transaction_id` mismatches after disk
corruption or block-level cloning. Suggested manual repairs vary and can be
destructive when applied to the wrong VG.

- Sources:
  <https://forum.proxmox.com/threads/restore-thin-pool-metadata.136320/>,
  <https://forum.proxmox.com/threads/storage-pool-unavailable-after-clonezilla-clone-how-to-clean-up.170095/>
- Applicability: yes as an underlying dm-thin failure, but it is below the
  plugin's repair boundary.
- Mitigation: non-writable, `needs_check`, failed, unknown or unreadable pool
  state blocks mutation. Doctor and recovery checks diagnose only. The plugin
  never invokes `thin_repair`, `vgcfgrestore`, device initialization or an
  automatic pool rebuild.
- Evidence: health-semantics regressions, fail-closed mutation tests and the
  data-safety invariants DS-06, DS-07, DS-08, DS-12 and DS-16.

## dm-clone hydration I/O failure

The upstream kernel documentation states that background hydration currently
continues indefinitely after a source-read or destination-write failure until
the operation succeeds. It also documents that ordinary guest I/O can pause
background hydration, so elapsed time or unchanged percentage alone cannot
prove a stall.

- Source: <https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html>
- Applicability: yes, but only while a Thick generation is in its temporary
  materialization transition. Steady-state Thick disks use a linear mapping.
- Mitigation: bounded worker execution, exact source/destination/metadata and
  DM-table identity, one-live-worker/probe invariants, persistent transaction
  state and explicit recovery. A timeout preserves both generations and
  returns `RECOVERY_REQUIRED`; it never retries or removes an object blindly.
- Evidence: C0-C9 crash matrix, timeout and worker-loss regressions,
  cross-node reconstruction, explicit post-reboot resume and total-path-loss
  qualification.

## Active raw-volume size discovery

The Veeam Proxmox backup workflow queries PVE's per-volume content endpoint
before starting each disk transfer. During a live HotAdd backup, the generic
PVE file-oriented size probe can return a size without a format while QEMU
holds the raw block device. PVE then rejects the endpoint response with
`volume_size_info ... failed - no format`; Veeam retries and eventually falls
back to enumerating the complete storage inventory.

- Source: directly reproduced with Veeam Backup & Replication 13.1 and the PVE
  content API during the Thin-only, Thick-only, and mixed qualification run.
- Applicability: yes for both allocation modes because both expose raw block
  devices.
- Mitigation: the plugin implements a read-only exact-path
  `blockdev --getsize64` probe and returns the canonical `raw` format without
  attempting content detection or opening the device for writing.
- Evidence: list/scalar API regression, active Thin/Thick live probe, and a
  second Veeam run required on the tg12 package to prove zero endpoint retries.

## Multi-disk restore placement

Current Veeam Proxmox entire-VM restore supports selecting a destination
storage and disk type, but not independently for every disk in one VM. Native
PVE/PBS full restore likewise commonly applies one target storage to all disks.

- Sources:
  <https://helpcenter.veeam.com/docs/vbr/userguide/pve_restore_entire_vm_storage.html>,
  <https://forum.proxmox.com/threads/best-way-to-migrate-a-multi-disk-vm-from-lvm-to-different-zfs-pools-for-storage-replication.183778/>.
- Applicability: yes to a mixed Thin/Thick VM restore, but it is a caller/UI
  placement constraint rather than a storage-plugin data-path defect.
- Mitigation: expose Thin and Thick as clear separate PVE storage choices;
  qualify single-disk cross-mode restores directly; use a subsequent native
  PVE Storage Move when individual restored disks need different modes.
- Evidence: Thin-to-Thick and Thick-to-Thin Veeam restore cells remain open;
  per-disk mixed selection is explicitly `NOT_AVAILABLE_BY_PRODUCT` and must
  never be simulated through an unsupported internal API.

## Veeam Proxmox replication placement

Veeam Backup & Replication 13.1 documents Proxmox VE replication as a desktop
console-only operation. Unlike entire-VM restore, its destination wizard can
select storage granularly for individual virtual disks.

- Sources:
  <https://helpcenter.veeam.com/docs/vbr/userguide/pve_limitations.html>,
  <https://helpcenter.veeam.com/docs/vbr/userguide/pve_replication_job_create_destination.html>.
- Applicability: yes. Thin-to-Thick, Thick-to-Thin and mixed per-disk replica
  placement are valid product-supported qualification cases.
- Boundary: Veeam's public PowerShell module in the tested 13.1 installation
  exposes no Proxmox replica-job creation cmdlet. Tests must use the supported
  desktop wizard and must not call undocumented internal services.
- Required evidence: exact destination mode and SHA-256, source immutability,
  no proxy/temporary attachment, clean supported removal, healthy recovery
  gates and zero final VG free-space delta. A license refusal is
  `NOT_AVAILABLE_BY_LICENSE`, not a plugin failure.

## Result

No new automatic repair or destructive fallback is justified by these
incidents. Veeam Thin-only, Thick-only, and mixed HotAdd backup now has direct
positive evidence; cross-mode Veeam restore and replica qualification remain
open. The other remaining qualification items are explicitly listed in the
release gate: uninterrupted endurance on healthy infrastructure and
representative physical FC hardware. These boundaries must not be described
as passed until direct evidence exists.
