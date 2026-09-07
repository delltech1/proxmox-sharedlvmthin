# Critical storage recovery runbook

When SharedLvmThin storage is CRITICAL or unavailable, preserve evidence and identify the failing layer before attempting repair.

## Do not run

Do not run these commands until storage identity and the failure layer have been positively established:

```text
pvcreate
vgcreate
vgremove
pvremove
wipefs
lvremove
thin_repair
vgcfgrestore
```

Do not recreate a missing map, PV, VG, thin pool, or VM disk merely because it is temporarily absent from one node.

## Read-only evidence first

1. Record quorum and cluster membership.
2. Save the affected PVE storage definition.
3. Record maps, paths, WWIDs, and kernel transport errors.
4. Record `pvs`, `vgs`, and `lvs` UUIDs and relationships with bounded read-only commands.
5. Compare VG UUID, PV UUID, WWID, and ownership tags with known-good data.
6. Save current LVM metadata backups without restoring them.
7. Determine whether the failure is transport, device-mapper, visibility, thin metadata, locking/quorum, or plugin lifecycle.

Escalate ambiguous metadata to manual recovery. SharedLvmThin must not attempt automatic repair or initialization.

