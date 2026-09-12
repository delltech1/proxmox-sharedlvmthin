# Allocation modes

The same SharedLvmThin package supports two explicit storage definitions over
one dedicated shared VG. The administrator chooses a mode by choosing the PVE
storage ID when creating, restoring, importing, or moving a disk.

## Thin

`slt-allocation-mode thin` creates one LVM-thin pool per VM. It provides thin
allocation, snapshots, guarded autogrow, and a deliberately small ownership
and metadata domain. Physical extents already assigned to one VM pool remain
inside that pool when the guest later discards blocks; another VM cannot use
those extents until the exact owned pool is safely removed.

Use Thin when capacity efficiency and discard reclamation inside each VM pool
matter more than reserving the complete virtual capacity in advance.

## Thick Generations

`slt-allocation-mode thick-generations` creates fully allocated independent
generation LVs. The normal guest path is an ordinary linear device. Snapshot
and rollback transitions temporarily use persistent `dm-clone` metadata to
materialize a new independent generation and then return the frontend to a
linear destination-only mapping.

Use Thick Generations when deterministic physical reservation and independent
materialized generations are more important than thin allocation efficiency.
It requires enough free VG space for the requested disk or transition and can
therefore consume capacity substantially faster than Thin.

## Coexistence and conversion

Both definitions may point to the same pinned VG, but they are aliases of one
physical allocation domain. Their displayed capacities must never be added
together. Keep the identity, reserve, path, shared, and node-scope properties
identical between the pair.

There is no implicit conversion and no global mode switch. Existing disks keep
their allocation model. To convert a disk, use the normal PVE Storage Move and
select the other storage ID. The qualified matrix includes both Thin-to-Thick
and Thick-to-Thin moves with block-hash verification.

## Choosing a mode

| Requirement | Thin | Thick Generations |
|---|---|---|
| Allocate physical capacity on demand | Yes | No |
| Independent per-VM metadata domain | Yes | Yes |
| Guest discard reusable inside the same VM allocation domain | Yes | Not applicable to capacity reservation |
| Full physical reservation before use | No | Yes |
| Ordinary linear steady-state guest path | No; LVM-thin target | Yes |
| Snapshot transition needs temporary additional capacity | Pool-dependent | Yes |

Neither mode replaces SAN redundancy, multipath, fencing, quorum, backups, or
capacity monitoring. All mutations remain subject to the same identity,
ownership, quorum, cluster-lock, reserve, and recovery-health gates.
