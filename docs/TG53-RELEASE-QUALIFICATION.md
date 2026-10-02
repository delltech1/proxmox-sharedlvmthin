# TG53 release qualification matrix

This is an execution checklist, not a compatibility claim. A row becomes
`PASS` only when the exact PVE task completes successfully, the expected
storage/configuration postcondition is proved, and all written data markers
retain their recorded SHA-256 digest. Unit tests alone do not satisfy a live
row.

## Profiles and allocation modes

Run applicable rows for both mutually exclusive package profiles:

- DUAL: Thin, Thick Eager and Thick Lazy on physically separate Thin/Thick VGs.
- Thick-only: Thick Eager and Thick Lazy on a dedicated Thick VG.

Run cross-mode operations in both directions where PVE exposes a supported
Storage Move path. Do not describe these as VMware vMotion or Storage vMotion;
the qualified interfaces are Proxmox VE migration and Storage Move.

## Single-disk lifecycle

For each supported allocation mode:

1. Create a disposable VM and one RAW guest disk.
2. Write non-zero canaries at the beginning, middle and end; record SHA-256.
3. Create snapshot A, mutate all canaries, and prove current-state SHA-256.
4. Roll back to A and prove the original SHA-256.
5. Delete A and prove the VM starts and data remains unchanged.
6. Grow the disk, prove old ranges unchanged, write/read the new tail.
7. Create snapshot B, mutate data, delete B without rollback, and prove the
   current-state SHA-256 is unchanged.
8. Stop/start, deactivate/reactivate and reboot the owner node at qualified
   boundaries; prove config, ownership and SHA-256 after every reopen.
9. Destroy the VM and prove that the config, frontend, private metadata,
   generations, anchor and allocation objects are absent.

Shrinking a virtual disk is not a supported positive test. It must be refused
without mutation.

## Multi-disk lifecycle

Exercise two, four and high-slot disk layouts, including mixed controller
types supported by PVE:

- independent canaries and SHA-256 per disk;
- one VM snapshot covering all disks;
- two successive snapshots followed by rollback of the older and newer state;
- snapshot deletion in both orders where PVE permits it;
- grow one disk while proving every neighbour disk unchanged;
- online add, detach to `unusedN`, reattach and cold reopen;
- destroy with one and multiple retained Thick snapshot generations;
- EFI, TPM state, cloud-init and RAM/vmstate volumes where applicable.

The test oracle must identify every QEMU block device through QMP. Initial
process command-line inspection is insufficient for dynamically hot-plugged
devices.

## Backup and restore

For every mode and representative multi-disk layout:

- stopped VMA backup and restore to the same mode;
- running backup where supported by PVE;
- restore to Thin, Eager and Lazy destinations through ordinary PVE APIs;
- prove all disk SHA-256 values, VM configuration, EFI/TPM state ownership and
  absence of orphaned source/target objects;
- repeat after a source snapshot and after a disk resize.

Veeam is deliberately outside the automated release gate. It is the final
manual acceptance test after the native PVE matrix passes.

## Migration and Storage Move

Test stopped and live VM migration across participating PVE nodes for:

- one disk and multiple disks;
- Thin, Eager and Lazy source disks;
- RAM/vmstate snapshots, EFI, TPM and cloud-init variants;
- simultaneous/burst migration of multiple disposable VMs;
- source and destination nodes using every qualified PVE package tuple.

Test ordinary PVE Storage Move in the supported direction matrix:

| Source | Thin | Thick Eager | Thick Lazy |
|---|---:|---:|---:|
| Thin | same-mode | convert | convert |
| Thick Eager | convert | same-mode | convert |
| Thick Lazy | convert | convert | same-mode |

Each cell requires source and destination capacity admission, exact task
status, per-disk SHA-256, correct source cleanup and no orphaned target after a
failed or cancelled move. A same-VG Thin/Thick layout is not used.

## Adversarial boundaries

Inject one failure at a time at qualified disposable boundaries:

- task cancellation and command deadline;
- node reboot before dispatch, during a transition and after committed state;
- storage path loss, partial multipath degradation and recovery;
- ENOSPC/capacity refusal at destination;
- quorum loss and constrained two-node mode;
- stale frontend/open writer, surviving worker and D-state classification;
- package upgrade/profile replacement with active, stopped and snapshotted VMs;
- repeated operation after an ambiguous result (must not blindly retry);
- concurrent operations against the same volume and against neighbour volumes.

Expected safety result is either a proved commit or a fail-closed,
recovery-required state with preserved data. Automatic cleanup is allowed only
for exact, signed, unreferenced and settled objects.

## Evidence required for release

For every live case retain:

- exact installed PVE/plugin package tuple and kernel;
- VM configuration and storage IDs with lab-specific secrets removed;
- PVE UPID and terminal task status;
- before/after SHA-256 values for every disk marker;
- anchor/generation/runtime inventory and recovery-check result;
- cleanup proof;
- a classification of `PASS`, `FAIL`, `BLOCKED` or `NOT SUPPORTED`.

The public release notes must list only completed PASS coverage and must link
known failures or unsupported operations to the corresponding limitation.
