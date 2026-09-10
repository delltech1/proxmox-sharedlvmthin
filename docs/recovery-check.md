# Read-only recovery check

Use this command after an observed SAN, multipath, VG or storage disappearance
and after the underlying infrastructure appears to have returned:

```text
sharedlvmthin recovery-check <storage-id>
```

The selected storage must pin its expected VG UUID, PV UUID, multipath WWID
and minimum path count. The command checks those values, owned thin-pool flags,
bounded LVM/PVE probes, quorum and D-state evidence. For conventional Thin
mode it also compares every PVE snapshot-section reference with the exact
snapshot LV inventory in the corresponding owned per-VM pool. Missing,
unreferenced, malformed or wrong-pool snapshot objects fail closed. Disk entries
explicitly marked `snapshot=0` are excluded from the expected inventory.

A healthy result ends with:

```ini
STATE=HEALTHY
SAFE_FOR_MUTATION=YES
```

Any mismatch, failed probe, timeout, unhealthy pool, unavailable quorum,
insufficient path count, persistent relevant D-state task, or persistent
unscoped/ambiguous D-state evidence ends with:

```ini
STATE=RECOVERY_REQUIRED
SAFE_FOR_MUTATION=NO
```

The D-state check searches process command line, wait channel and kernel stack
for the exact VG, mapper path or WWID. A detected task must disappear during a
bounded two-second confirmation interval before the result can become `PASS`;
the transient observation is still reported. A task that survives confirmation
remains `FAIL` when storage-scoped or `UNKNOWN` when it cannot be positively
attributed. This is not installed as a global mutation gate.

Probes are sequential and bounded. After the first timed-out or kernel-blocked
LVM/PVE probe, no further LVM/PVE probe is started. This limits the checker to
one outstanding probe and prevents monitoring from amplifying an incident.

The command is strictly observational. It does not activate storage, rescan
SCSI, reload multipath, restart services, clear state, repair metadata or run
any LVM mutation.

An interrupted native PVE Thin snapshot can leave `lock: snapshot`, a snapshot
section in `snapstate: prepare`, and only a subset of the expected snapshot
LVs. Do not clear this state merely because the base disks remain readable.
First preserve evidence and require `recovery-check` to report the exact
missing or excess objects. For a stopped disposable guest, the qualified manual
recovery path is to verify those exact preconditions, use the supported
`qm unlock <vmid>` command, and then run
`qm delsnapshot <vmid> <snapshot> --force`. Re-run the read-only gate and verify
the source data before permitting further mutation. This is an explicit admin
recovery procedure, not automatic plugin behavior.
