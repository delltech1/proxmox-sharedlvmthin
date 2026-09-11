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

## Result

No new automatic repair or destructive fallback is justified by these
incidents. The remaining open qualification items are environmental or
explicitly listed in the release gate: uninterrupted endurance on healthy
infrastructure, representative physical FC hardware, and optional Veeam PVE
backup/restore validation. These boundaries must not be described as passed
until direct evidence exists.
