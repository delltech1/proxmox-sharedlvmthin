# Data safety invariants

These invariants are RC5 P0 release gates. Availability loss is preferable to an ambiguous or destructive recovery action.

- **DS-01:** Never mutate a VG whose configured identity does not match.
- **DS-02:** Never delete an LV without positive SharedLvmThin ownership proof.
- **DS-03:** Never delete a per-VM pool while any owned or referenced volume remains.
- **DS-04:** Never mutate shared LVM metadata without the required cluster lock.
- **DS-05:** Never mutate shared metadata without the required quorum.
- **DS-06:** Never automatically repair ambiguous or corrupt LVM metadata.
- **DS-07:** Never automatically initialize an unknown block device.
- **DS-08:** Never run `pvcreate`, `vgcreate`, `wipefs`, or related initialization as implicit recovery.
- **DS-09:** A partial failure must preserve more data, not less.
- **DS-10:** Automated recovery, where supported, must be idempotent.
- **DS-11:** Foreign, legacy, and unknown objects are read-only until ownership is proven.
- **DS-12:** A transport failure must never trigger destructive storage cleanup.
- **DS-13:** Configuration that changes persistent volume layout, ownership, or snapshot semantics is immutable while managed or legacy objects exist. A configuration change never retroactively grants new semantics or ownership to an old object.
- **DS-14:** A newly allocated volume must not expose data from a previously deleted volume or VM. Allocation confidentiality is a release gate, not an assumption inferred from thin-pool defaults.
- **DS-15:** New shared-storage pools and guest/auxiliary LVs must not be acknowledged as successfully allocated until generic LVM autoactivation is proven disabled. Thin activation also re-verifies the exact pool and requested LV and refuses an unowned pool with any surviving local mapper. Existing pools are changed only by the explicit `ALL-NODES-INACTIVE` adoption/hardening transaction, which disables autoactivation for the exact pool family before publishing safety.

For DS-02, ownership of a snapshot origin is not ownership proof for an LV
that merely has the expected snapshot name. Snapshot deletion must revalidate
the candidate snapshot's existence, expected per-VM pool relationship, thin
type, read-only state, and activation-skip flag before `lvremove`.

Before rollback removes or replaces the current LV, the target snapshot must be
readable, canonical read-only thin snapshot flags proven, its pool relationship proven, replacement feasibility and capacity
validated, storage identity and ownership proven, quorum present, and the
cluster storage lock held. A failed precondition permits no destructive step.

An absent VG during SAN failure means **UNAVAILABLE**, never EMPTY. Absence of visible LVs is not proof that an LV or pool should be removed.

Identity failures are operation-blocking. Capacity, metadata, chunk geometry, and transport-policy findings are health diagnostics unless a separately specified and tested operation gate exists.

Thin-pool `needs_check`, read-only metadata, an explicit non-healthy
`lv_health_status`, or unknown/unexpected thin-pool attributes are separately
specified P0 operation gates. They disable mutations and require manual
recovery; neither the plugin nor Doctor runs `thin_check`, `thin_repair`, or
`lvconvert --repair` automatically.
## DS-16 — Recovery health before mutation

After any observed transport or storage disappearance/recovery event,
restored path count and matching storage identity are necessary but
insufficient conditions for mutation.

Mutation remains blocked operationally until relevant dm-thin/device-mapper
state, bounded LVM/PVE probes, thin-pool health, quorum and storage health are
positively verified. `sharedlvmthin recovery-check <storage-id>` provides this
read-only evidence and reports `SAFE_FOR_MUTATION=YES` only when every required
check passes.

An unscoped D-state task is never guessed to belong to the selected storage.
It is reported as `UNKNOWN`, making the recovery check conservative without
adding a global D-state gate to ordinary plugin mutations.

The checker never performs activation, SCSI rescan, multipath/dmeventd/PVE
restart, dm-thin reset, cleanup, initialization or metadata repair.

The explicit `thin-metadata-check` command provides the stronger frozen-root
metadata gate when the pool is active and exclusively owned. It reserves the
kernel metadata snapshot under the canonical lock, runs bounded read-only
`thin_check --metadata-snap`, releases only its own reservation, and emits an
exact transaction fingerprint. It is never run implicitly in the guest I/O
path and never repairs metadata.

## DS-17 — Single-kernel Thin-pool ownership

A managed read-write Thin pool must never be active in more than one kernel.
Quorum, VM ownership, disabled autoactivation, and cluster serialization of
LVM commands do not make dm-thin runtime metadata cluster-coherent.

Before activating any Thin volume, the plugin requires a persistent owner
schema and claims a per-pool `node + epoch` pair under the canonical cluster/VG
lock. A pool without that schema is a legacy/unknown state and requires an
explicit all-nodes-inactive adoption procedure. An owner belonging to another
node is a hard refusal; it is never stolen, timed out, or inferred stale. The
exact node and epoch are released only after the last local child, public pool
and hidden `-tpool` mapper are positively absent. Consequently, normal PVE Thin live migration,
which prepares storage on the target before the source closes it, must fail
closed. Offline deactivate-then-activate handoff remains the supported
mobility model.

After a host failure, an owner may be cleared only through the explicit
`thin-recover-fenced-owner` command after external fencing has positively made
the named former owner unable to access the LUN. The plugin never guesses or
automatically clears a stale owner.

## DS-18 — Canonical same-VG mutation lock

Every metadata mutation against the same pinned VG UUID uses the same
canonical cluster lock, independent of storage ID or thin/Thick Generations
allocation mode. Unpinned legacy storage aliases are not qualified for
same-VG mixed-mode operation. A pinned thin mutation also proves that no
global Thick Generations VG intent is open while holding that lock; an
unresolved intent blocks the thin callback before its first mutation.
The same VG must not be exposed concurrently through a native PVE `lvm` or
`lvmthin` storage definition that cannot participate in this lock domain.
The canonical thin/thick pair must also use the same PVE node scope; a
half-visible pair is rejected before activation or mutation.

## DS-19 — Thick lifecycle LVM commands are device-scoped

Every Thick Generations LVM inventory and mutation used by allocation,
activation, deactivation, resize, snapshot transition, snapshot deletion, and
recovery is restricted to the exact pinned multipath mapper with per-command
`--devices`. Failure to establish that scope is a hard refusal. The plugin does
not change the host-wide LVM devices file or scanning configuration.

## DS-20 — Same-VG capacity is never fabricated

When the canonical thin and Thick Generations aliases expose one physical VG,
each storage status reports that same physical VG truthfully. Administrators
must not sum both alias values as independent capacity. Doctor detects the
canonical pair and emits this warning; the plugin does not divide, hide, or
otherwise fabricate capacity to compensate for a presentation-layer aggregate.
