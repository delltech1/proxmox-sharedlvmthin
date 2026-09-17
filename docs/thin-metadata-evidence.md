# Thin metadata evidence

`sharedlvmthin thin-metadata-check <storage-id> <volume>` is an explicit,
bounded read-only validation command for an active, exclusively owned Thin
pool. It uses only the dm-thin kernel interface, `thin_check`, LVM and PVE
locking already supplied by Proxmox VE.

The command:

1. acquires the canonical cluster/VG lock;
2. verifies quorum, pinned storage identity, volume ownership and pool health;
3. requires the exact local thin-pool mapper and persistent owner epoch;
4. records pool, metadata and data LV UUIDs, dm-thin transaction ID, metadata
   mode, exact table hash and dependency hash;
5. asks the kernel to `reserve_metadata_snap`;
6. runs a 60-second bounded `thin_check --metadata-snap --quiet` against the
   exact metadata mapper;
7. always attempts `release_metadata_snap` if this invocation reserved it;
8. returns canonical JSON with a SHA-256 transaction fingerprint and
   `safe_for_mutation`.

Any missing evidence, timeout, invalid metadata or unproven snapshot release
returns non-zero and `safe_for_mutation=false`. The helper never invokes
`thin_repair`, `--auto-repair`, `--clear-needs-check-flag`, activation, SCSI
rescan, multipath restart or cleanup.

If another process already owns the kernel metadata snapshot reservation, the
command refuses it and does not release that foreign reservation. It is a
diagnostic/release gate, not a periodic I/O-path operation.

The fingerprint is evidence for comparing two explicitly selected lifecycle
checkpoints. It is not a distributed lock and it is expected to change after
legitimate guest writes advance the dm-thin transaction ID.
