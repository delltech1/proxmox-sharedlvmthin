# Read-only recovery check

Use this command after an observed SAN, multipath, VG or storage disappearance
and after the underlying infrastructure appears to have returned:

```text
sharedlvmthin recovery-check <storage-id>
```

The selected storage must pin its expected VG UUID, PV UUID, multipath WWID
and minimum path count. The command checks those values, owned thin-pool flags,
bounded LVM/PVE probes, quorum and D-state evidence.

A healthy result ends with:

```ini
STATE=HEALTHY
SAFE_FOR_MUTATION=YES
```

Any mismatch, failed probe, timeout, unhealthy pool, unavailable quorum,
insufficient path count, relevant D-state task, or unscoped/ambiguous D-state
evidence ends with:

```ini
STATE=RECOVERY_REQUIRED
SAFE_FOR_MUTATION=NO
```

The D-state check searches process command line, wait channel and kernel stack
for the exact VG, mapper path or WWID. If a D-state task exists but cannot be
positively attributed, the result is `UNKNOWN`; it is not silently treated as
healthy and it is not installed as a global mutation gate.

Probes are sequential and bounded. After the first timed-out or kernel-blocked
LVM/PVE probe, no further LVM/PVE probe is started. This limits the checker to
one outstanding probe and prevents monitoring from amplifying an incident.

The command is strictly observational. It does not activate storage, rescan
SCSI, reload multipath, restart services, clear state, repair metadata or run
any LVM mutation.
