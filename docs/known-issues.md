# Known issues

1. Linux LIO/`tcm_fc` software FCoE targets may exhibit blocked recovery after
   complete communication loss. That target stack is not certified by this
   project. This boundary is independently visible in current upstream Linux:
   the [`ft_recv_seq()` error path](https://github.com/torvalds/linux/blob/master/drivers/target/tcm_fc/tfc_cmd.c)
   still records that a queued command must be found, marks the command aborted,
   and returns. The [file history](https://github.com/torvalds/linux/commits/master/drivers/target/tcm_fc/tfc_cmd.c)
   does not show a later path-specific fix. This is supporting evidence, not a
   claim that the source comment alone proves every observed target stall.
2. Indefinite `queue_if_no_path` can block LVM/PVE management during total path loss. Multipath policy remains an administrator-owned infrastructure setting; Doctor reports risk but does not change it.
3. Legacy untagged per-VM pools are preserved and are not silently adopted, tagged, grown, or deleted.
4. Thick Generations remain release-candidate technology. The upstream Linux
   kernel still marks `DM_CLONE` experimental. Validate the complete lifecycle on
   disposable storage matching the intended production topology before use.
5. Web sessions are in memory; a dashboard service restart requires login again.
6. Physical FC HBA/fabric behavior requires qualification with the intended production hardware, firmware, array and multipath policy.
7. Active dm-thin metadata I/O and active dm-clone hydration did not recover
   transparently in every laboratory all-path-loss experiment. The plugin
   correctly blocked new mutations, but it cannot repair or guarantee recovery
   of an already blocked kernel/device-mapper transaction. See the release gate
   before claiming complete-path-loss availability.
8. Veeam HotAdd backup and supported-console cross-mode restore were tested.
   Veeam replication was not available in the installed edition and is not
   part of the qualification claim.
9. Upstream dm-clone redirects reads for not-yet-hydrated regions to the
   immutable source, so repeated reads can inherit source latency. Upstream
   also documents that background hydration I/O failures are logged but may be
   retried indefinitely by the kernel until they succeed. SharedLvmThin's
   monotonic no-progress watchdog requests that background hydration stop,
   requires kernel `no_hydration` confirmation, reports recovery-required and
   leaves the signed transition intact. This prevents scheduling additional
   background regions but cannot cancel a failed region already in flight or
   prevent guest I/O to an unhydrated region from encountering the same
   source/destination failure. It does not claim to repair the kernel
   target, SAN or multipath layer. An unconfirmed stop is treated as unknown
   state and is not retried automatically. Latency-sensitive workloads should
   qualify synchronous materialization, and source/destination fault recovery
   requires representative disposable hardware testing. See the
   [upstream dm-clone known issues](https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html#known-issues).
10. Region geometry is a latency/throughput tradeoff, not a universal speed
    switch. Upstream recommends a filesystem-block-sized region and specifies
    that a partial write to an unhydrated region waits for that region to be
    hydrated. Thick Generations currently uses 1 MiB only as the deterministic
    candidate for new transitions to bound region cardinality and improve
    sequential materialization; consequently, a small first write may wait for
    up to 1 MiB of source-to-destination copying. The release gate must qualify
    first-write and concurrent foreground p50/p95/p99/max latency on the actual
    storage. Existing signed 4/8 KiB transitions retain their exact geometry;
    they are compatibility state, not the new-transition default. See the
    [upstream region semantics](https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html#regions).
