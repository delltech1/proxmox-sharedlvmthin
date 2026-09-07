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
- **DS-15:** New shared-storage pools and guest/auxiliary LVs must not be acknowledged as successfully allocated until generic LVM autoactivation is proven disabled. An uncertain flag preserves the object and returns PARTIAL; existing legacy flags are diagnostic-only and are never silently rewritten.

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
