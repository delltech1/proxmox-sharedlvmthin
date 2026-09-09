# SharedLvmThin 0.9.0~rc5.3 release notes

RC5.3 removes the misleading older-storage-API warning on qualified PVE 9
hosts. The plugin now advertises API 14 on a Storage API 14 host and API 15 on
a Storage API 15 host.

This is a bounded compatibility decision, not unrestricted dynamic API
mirroring. Storage API 13 and API 16 or newer fail closed until their hook and
semantic changes are separately audited and qualified. Mutating hooks retain
their independent runtime API preflight.

RC5.3 also introduces the recommended `elastic` capacity policy. It sizes
physical per-VM pools from live allocated data plus an absolute configurable
burst headroom (64 GiB by default), never as an unbounded percentage of a
multi-terabyte virtual disk. The cluster-locked dmeventd helper maintains this
absolute headroom while preserving identity, ownership, quorum, thin-pool
health and protected VG-reserve gates. EFI/TPM-first allocations use a 1 GiB
bootstrap. No automatic pool shrink is performed.

New installations use the earliest LVM-supported automatic-extension event
threshold of 50%. Existing administrator-owned or already managed LVM policy
is preserved and must be reviewed explicitly during upgrade.

All RC5.2 data-safety, identity, locking, recovery-check, transport and
operational limitations remain in force.
