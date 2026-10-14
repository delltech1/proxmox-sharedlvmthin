# API14–16 bridge: development qualification checkpoint

Experimental development evidence, **not a released compatibility guarantee**.
Use disposable hosts, SAN storage and guest data only. A completed row below
does not qualify another package profile, software tuple or failure ordering.

Checkpoint: 2026-10-09. Immutable candidate package version:
`0.9.0~rc5.95~tg53`. Later source corrections have not replaced those DEBs.
The next development candidate is `0.9.0~rc5.96~tg53`; its package deployment
and qualification remain separate, incomplete gates.
The manifest still records these candidate tuples as `RETEST_REQUIRED`.

## Exact platform tuples involved

| API | Kernel | pve-manager | libpve-storage-perl | libpve-common-perl | qemu-server | pve-qemu-kvm |
|---|---|---|---|---|---|---|
| 14 | 7.0.2-6-pve | 9.2.2 | 9.1.5 | 9.1.12 | 9.1.15 | 11.0.0-3 |
| 15, bridge source | 7.0.14-17-pve | 9.2.20 | 9.1.11 | 9.2.2 | 9.2.10 | 11.0.3-3 |
| 16, rolling target | 7.0.14-17-pve | 9.2.20 | 9.1.12 | 9.2.3 | 9.2.10 | 11.0.3-3 |
| 16, additional peer | 7.0.14-16-pve | 9.2.18 | 9.1.12 | 9.2.3 | 9.2.10 | 11.0.3-3 |
| 16, SAN canary | 7.0.14-20-pve | 9.2.21 | 9.1.12 | 9.2.3 | 9.2.10 | 11.0.3-4 |

The API14 control had no SAN workload. Its control/package results cannot be
used as an API14 SAN data-plane result. A release must also publish its exact
tested LVM, device-mapper/kernel, multipath and cluster dependency versions.

Fresh API16 SAN-canary observations used LVM `2.03.31-2+pmx1`,
thin-provisioning-tools `1.1.0-4+b1`, multipath-tools `0.11.1-2`,
Corosync `3.1.10-pve3`, pve-ha-manager `5.2.5` and
libpve-cluster-perl `9.1.6`. These supplement the canary row above;
they must not be substituted for the other rows' historical dependency tuples.

## Completed candidate evidence

- API14 clean package installation for DUAL and Thick-only; DUAL/Thick-only/DUAL
  replacement cycle; DUAL reboot and repeated public postboot settlement.
- DUAL plugin upgrade on API15 with four running, three-disk guest witnesses:
  process/config/mapper identities preserved and fresh ordered write/read/SHA
  results continued. This is bounded workload evidence, not universal zero latency.
- Exact two-package API15→16 APT transition: first refusal before DPKG, explicit
  one-shot authorization, evacuated-node update, reboot, runtime settlement,
  repeat settlement and strict peer SSH checks. This used WARN/exact proposal
  authorization, **not manifest-prequalified `QUALIFIED_AUTO` admission**.
- Four concurrent native migrations in both mixed-API directions before updating
  the last source; post-update return migrations with fresh per-disk SHA proof.
  These migration witnesses had materialized Thick disks: the mixed witness's
  Thin disk was moved to Eager and its Lazy disk materialized before evacuation.
  This is not evidence of live shared-Thin or active-Lazy migration.
- A separate three-disk Thin fixture refused direct live migration preflight
  while running, with unchanged process/configuration identity. After stopping,
  native offline handoff and return passed exact task and 21-data-check proofs.
  The running refusal fixture was an empty BIOS VM, not a guest OS workload.
- Twelve stopped-VM snapshots over four three-disk VMs, deliberate data mutation,
  rollback and snapshot deletion with independent per-disk data checks.
- Small Thin/Eager/materialized-Lazy growth, new-tail/neighbor data checks and
  cold reopen. Native shrink refusal verified through its exact task result and
  unchanged disk sizes/data, not the CLI return code alone.
- Native stopped backup followed by file restores to Eager and Thin and raw-VMA
  stdin restore to Eager; 21 distinct data checks per restored VM and source.
  Restore objects remain retained; their cleanup has not yet been qualified.

Snapshot/resize/backup rows above use DUAL. They do not substitute for independent
Thick-only shared-SAN lifecycle qualification. Materialized Lazy evidence does
not qualify an active Lazy frontend or an unsupported restore path.

## Still open before a broader qualification claim

- Independent Thick-only/API16 shared-SAN peers, lifecycle, migration and reboot.
- The executable VM configuration matrix: firmware/controller/NIC variants,
  true guest boot, RAM-state oracle and special-state volumes. The pairwise
  configuration plan is not evidence that those configurations ran.
- Exact-artifact admission/manifest decisions, final regression/package parity,
  reproducibility/privacy checks and protected CI/publication gates.
- Isolated extended failure scenarios not actually exercised on this artifact.

## Reusable functional gate tooling

[Runner schema and safety boundary](../experiments/thick-generations/release-functional-runner.md)
describe the current orchestration foundation. Its isolated Linux suite,
including the read-only guest protocol function fixtures, passed 29 tests.
These fixtures are not live guest boot, RAM rollback or storage qualification.

Ordinary poll intervals never mark a slow tracked worker dead. An explicitly
configured command deadline or ambiguous identity yields `UNKNOWN`, preserves
evidence and prevents automatic retry or further dispatch.

An async native PVE command may return CLI exit0 while its worker fails. The
exact native task identity, terminal result and independent storage/data
postconditions are required; successful process launch is not operation success.

## Additional actual guest evidence

A disposable boot fixture built from existing stock components booted through
SeaBIOS and OVMF, without a QEMU direct-kernel override. Fresh challenged guest
responses confirmed the expected EFI presence and RAM-only nonce retention.
This does not qualify Secure Boot or multiple guest operating systems.

One native PVE q35/SeaBIOS/VirtIO Eager case additionally completed RAM
A -> snapshot -> B -> rollback -> A with the same guest boot ID. Three separate
data disks were changed only while the guest was stopped; their original data
oracles returned after rollback. An independent verifier checked all seven
exact native tasks, the full unchanged boot image, snapshot absence and deletion
of the known vmstate LV/mapper namespace.

This is one configuration, not the full release matrix. The disk oracles in this
case are host-side; guest/QMP disk binding and the independent Thick-only SAN
qualification remain separate requirements. Test harness failures were retained
and reconciled explicitly, never treated as successful native operations.

A second native q35/OVMF/VirtIO Eager case, with 1 GiB guest RAM, three
64 MiB data disks and a dedicated 4 MiB EFI-state volume (pre-enrolled keys
disabled), completed the same RAM A -> snapshot -> B -> rollback -> A sequence.
Independent verification confirmed all seven native tasks, the original guest
boot ID, restored RAM nonce, entire boot-image SHA, three host data oracles and
absence of the deleted snapshot's vmstate LV and mapper. The stopped VM and
original data volumes were retained separately from snapshot cleanup.

The initial 256 MiB EFI fixture did not reach the guest protocol. An isolated
output-only GRUB diagnostic captured an EFI kernel-decompression failure;
those failed attempts remain unqualified. Receive-only guest readiness is now
required before any protocol request. This is a test-fixture correction, not a
new minimum-memory requirement for the storage plugin.

The updated isolated Linux tooling suite passed 46 tests. This does not mean
46 live SAN configurations passed. Native EFI-variable rollback, Secure Boot,
guest/QMP disk identity, the full configuration matrix and Thick-only SAN
qualification remain outside these two native RAM cases.

An additional native q35/OVMF/VirtIO Thin case on a separate dedicated Thin VG
completed RAM A -> snapshot -> B -> rollback -> A with 1 GiB guest RAM and three
64 MiB data disks. Independent verification checked seven exact native tasks,
the original guest boot ID, RAM nonce, entire boot-image SHA and three host data
oracles. The exact vmstate and disk snapshot LVs were absent after deletion.
The original Thin pool and disks remained present; owner tags were released,
all pool/child LVs were inactive and no case-related device-mapper mappings or
rollback temporary LVs remained. This does not qualify Thin live migration,
guest/QMP disk binding, EFI-variable recovery or the full configuration matrix.

The generic matrix harness also completed an i440fx/SeaBIOS Thin case with
VirtIO-SCSI-single, two 64 MiB data disks and a separate boot disk. It checked
the expected whole-disk count and sizes in the guest, RAM A/B/A, seven native
tasks, whole boot-image SHA, both host data oracles and retained/inactive Thin
pool cleanup with released ownership. The native i440fx machine alias was
verified as `pc`; the disk driver was built into the stock test kernel.
Guest enumeration is not a guest-to-QMP identity or guest disk-I/O proof.
This one additional configuration does not qualify the remaining matrix rows.

## Additional bounded allocation-mode conversion evidence

The immutable DUAL candidate completed six serial native Storage Moves on
one stopped disposable three-disk VM:

- Thin -> Eager;
- Eager -> Lazy;
- materialized Lazy -> Thin;
- Thin -> Lazy;
- materialized Lazy -> Eager;
- Eager -> Thin.

Every move retained its source volume. Exact native task results, configuration
and inventory bindings, the complete boot-image SHA and six original data
canaries were checked before and after each move. Neighbor disk options and
existing detached source references were preserved. These are sampled host-side
data checks, not whole-data-disk or running guest I/O qualification.

Both newly created Lazy targets also completed explicit materialization,
original moved-data canary verification, deactivation and an independent
two-sample inactive linear-backing proof, including absence of its original
clone metadata and custom mapper identities. Each target was qualified using
its own original transaction and metadata UUID; identities from the first
target were not reused to qualify the second.

Test-harness refusals were preserved and reconciled separately. Corrections
covered EFI extent rounding, undefined linear-LV `lv_check_needed` reporting,
the active hidden thin-pool mapper despite an inactive public pool attribute,
and legitimate elastic pool growth. The growth was matched to the installed
allocation policy and exact physical-extent geometry, not accepted merely
because the pool became larger. No native move was retried to repair a failed
postcheck.

An independent read-only host audit then rechecked all six exact native task
results, the full configuration chain, current retained-volume inventories and
the independently specified original data oracle. Both original failed harness
units remained failed; separate successful reconciliation receipts were used.
No new I/O was performed by this audit, and historical SHA evidence was labeled
as such rather than presented as a fresh data read.

Active-Lazy failure coverage and independent Thick-only SAN qualification remain
open. No broader compatibility or release readiness claim follows from this
stopped-VM conversion cycle.

## Additional actual Eager/e1000 RAM refusal boundary

A Q35/OVMF Eager fixture with VirtIO-SCSI-single, two 64 MiB data disks,
a separate boot disk and EFI state completed native create/start, initial host
data canaries and challenged guest boot/EFI/RAM observations. Guest PCI/driver
enumeration positively identified e1000. Networking remained isolated/link-down;
this is not a network-I/O test.

Its e1000-only running configuration correctly refused RAM snapshot preflight
because upstream PVE would produce an empty `running-nets-host-mtu` value.
All four attached Eager volumes passed the materialized snapshot-capability
checks. No RAM snapshot was dispatched.

Separate read-only reconciliation verified the same QEMU process identity,
guest boot ID and RAM nonce, unchanged VM configuration, and absence of snapshot
sections, the known vmstate LV/mapper and matching snapshot tasks in the retained
filtered native task journal. The original harness units remained failed;
the independent result is **EXPECTED_REFUSAL**, not RAM snapshot PASS.
This does not prove that task logs can never be externally removed.

A separate disk-only continuation subsequently passed native snapshot, stop,
rollback with a fresh guest boot, stop and snapshot deletion. Original canaries
on both data disks were checked, deliberately changed while stopped, and restored
after rollback and after deletion. The whole boot-image SHA remained unchanged.
The VM and original disks were retained, stopped.

An independent read-only audit rechecked all five exact task outcomes, current
VM configuration/stopped state and snapshot absence, and independently
recomputed the recorded original/changed/restored data expectations. Its data
SHA evidence is historical; the audit itself did not perform new disk I/O.
The earlier failed RAM harness units were preserved, not promoted to PASS.

Adding a working VirtIO NIC may address the upstream RAM-snapshot precondition,
but that changed configuration must be tested separately rather than counted
as this e1000-only case. EFI-variable content rollback and guest-to-QMP disk
identity remain open. This bounded case does not qualify Thick-only SAN or
active-Lazy lifecycle behavior.

## Additional actual Eager/SATA disk-only coverage

An i440fx/SeaBIOS Eager fixture with a SATA boot disk, three 64 MiB SATA data
disks and an isolated rtl8139 NIC completed native creation and boot. Guest
enumeration confirmed all four disk sizes and the expected NIC/driver. The
running configuration correctly refused RAM snapshot preflight for the same
upstream empty host-MTU boundary; all attached Eager disks passed capability
checks. No RAM snapshot was dispatched.

The disk-only continuation completed snapshot, stop, deliberate changes to all
three host-side data canaries, rollback with a fresh guest boot, stop and snapshot
deletion. Each original canary was restored after rollback and remained correct
after deletion; the complete boot-image SHA remained unchanged. The original
VM and disks were retained, stopped.

A separate read-only audit independently rechecked all seven native task
outcomes, exact disk-only snapshot arguments, current configuration, stopped
state and snapshot absence. It recomputed the historical original, changed and
restored canary expectations. This audit performed no fresh disk I/O. RAM
snapshot support remains **EXPECTED_REFUSAL**, not PASS; guest enumeration does
not prove guest-to-QMP identity or guest disk I/O. This bounded DUAL case does
not qualify active-Lazy operations or the independent Thick-only SAN profile.

## Observed unqualified Lazy image-import boundary

On the tested API16 tuple, native VM creation with `import-from` targeting Lazy
storage failed when PVE activated the newly allocated disk before publishing its
VM configuration reference. The candidate permits this unreferenced activation
only for its qualified Storage Move and restore contexts; a plain external-image
import does not receive that capability. The reported move-capability error does
not establish that a legitimate move token expired.

The failed native create task remained failed. A separate read-only audit confirmed
its exact error and the absence of the target VM configuration, process, storage
contents, owned/tagged or hashed LVs and custom mappers after native cleanup.
This is a refusal boundary, **not successful Lazy import qualification**. No
unreferenced-activation guard was bypassed. An Eager boot disk with separate fresh
Lazy data disks is a different supported design to test, not proof that the failed
all-Lazy image-import case works. The source candidate now refuses the exact
unqualified native-import context before starting a Lazy allocation transaction;
ten isolated Perl regressions passed. This change is not yet deployed or qualified
as a new package, and does not add native Lazy image-import support.

## Actual mixed Eager-boot / active-Lazy RAM lifecycle

A Q35/SeaBIOS fixture on the installed API16 DUAL candidate used an Eager boot
disk, three fresh 64 MiB Lazy data disks and an isolated VirtIO NIC. Initial
host-side reads through the Lazy frontends returned the expected zero canaries.
RAM snapshot preflight explicitly refused the three active, unmaterialized Lazy
disks while positively verifying the VirtIO host-MTU prerequisite.

Public per-volume materialization then completed for all three Lazy disks.
The test checked the same QEMU process, VM configuration, guest boot and RAM
marker throughout; anchor/data identities and frontend incarnations were retained,
and the original clone, zero-source and metadata maps were absent after completion.
RAM snapshot preflight then passed.

The native RAM snapshot, stop, rollback with RAM restore, stop and snapshot
deletion completed. Deliberately changed host-side canaries on all three data
disks returned to their original zeros after rollback and remained zero after
deletion. RAM returned to its original marker and guest boot identity; the whole
boot-image SHA remained unchanged. The VM and disks were retained, stopped.

A separate read-only audit joined the original two successful create/start tasks
with the five successful continuation tasks and rechecked their exact native
outcomes, current configuration, stopped state and snapshot/state-object absence.
Earlier failed test-harness executions remained failed and their evidence was
preserved. The audit replayed recorded RAM, canary and canonical-graph evidence;
it did not perform fresh disk I/O or independently reconstruct unretained raw LVM
segment responses.

Queue checks are phase-specific: active Lazy clone frontends require the proven
zero discard/write-zeroes guards. A canonically materialized disk requires an
exact direct linear HEAD/backing graph and stable, typed queue observations;
ordinary materialized linear queues need not be zero. Snapshot and rollback
create new generations and must not be compared to the original allocation HEAD.

This bounded result covers the installed candidate, not the subsequently edited
source as a newly deployed package. It does not qualify plain all-Lazy image
import, guest disk-write workloads, migration, fault recovery, the whole
configuration matrix or the independent Thick-only SAN profile.
