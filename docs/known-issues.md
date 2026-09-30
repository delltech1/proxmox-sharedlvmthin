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
11. Native offline VMA restore directly to an unpublished Lazy volume is not
    supported. PVE allocates and activates restore volumes before publishing
    the final guest disk reference, while the storage activation hook receives
    no trusted restore transaction identity. Lazy therefore refuses this path
    rather than bypassing its discard and reference guards. Restore to Eager or
    Thin first and use a separately qualified storage move. A future dedicated
    restore receipt protocol must be crash-tested before this boundary changes.
12. RAM snapshots of no-NIC and e1000-only guests can leave an empty
    `running-nets-host-mtu:` snapshot property which strict PVE config parsing
    subsequently rejects. This was reproduced on qemu-server 9.2.7 and 9.2.10;
    it is not fixed by reverting only the newer package. Disk-only snapshots
    and the separately tested VirtIO-network RAM snapshot path are distinct
    qualification cells. `sharedlvmthin update-plan` reports the VM, node,
    exact section/line, occurrence count and raw-config SHA256 without changing
    the config. TG53 does not automatically delete or synthesize the property:
    an empty line can mean either that no relevant VirtIO-net device existed or
    that PVE failed to obtain resume-critical MTU metadata. Current VM config
    does not prove which historical case produced an existing RAM snapshot.
    Automatic repair, PVE module patching and background normalization are
    therefore intentionally blocked; preserve the VM, vmstate and raw config
    and treat the finding as recovery requiring upstream-aware review.
13. qemu-server 9.2.8 and newer refuse VM machine versions older than 5.0.
    Run `sharedlvmthin update-plan` and resolve every
    `LEGACY_MACHINE_LT_5` finding before updating; plugin storage health cannot
    make an unsupported VM machine type bootable.
14. The qemu-server 9.2.8 PCI/hotplug rewrite refuses an LSI high-slot hotplug
    when the running QEMU process has no required `pci.4` bridge. This is most
    visible across rolling updates, but with QEMU 11.0 it can also affect a VM
    started without a high-slot disk because that bridge is not pre-created.
    The tested `scsi14` operation allocated the exact Thick disk, refused the
    live attach, and retained it as pending configuration; it did not silently
    attach or discard the disk. `sharedlvmthin update-plan` reports running LSI
    VMs without `pci.4`. Do not treat allocation followed by hotplug refusal as
    a successful attach, and reconcile config, pending, unused-volume, QMP and
    storage state explicitly.
15. On tested qemu-server 9.2.7 and 9.2.10, `qm disk resize` can return process
    status zero after its exact asynchronous resize task terminated `ERROR`.
    The plugin exception is propagated into the PVE task, but the `qm` command
    definition does not attach the existing UPID exit-status callback. Test and
    automation code must correlate the newly created exact resize UPID and
    require terminal `OK`, then verify the requested config and block-size
    postconditions. Never replay a relative resize merely because the client
    result is ambiguous.
16. `pvesm free` can likewise print a plugin removal refusal while returning
    process status zero. TG53 safely refuses removal of an open Lazy frontend,
    preserves the exact volume and permits normal stop/deactivate followed by
    delete; however, the CLI status alone is not proof that deletion occurred.
    Automation must require exact object absence after an intended delete and
    exact object preservation after an expected refusal. A zero-open,
    locally-owned Lazy graph left active by a previously refused PVE operation
    is closed through the normal verified lifecycle before TG53 deletes it;
    foreign, open, transitional or ambiguous state remains fail-closed.
17. An unmaterialized running Lazy Thick disk cannot be live-migrated
    directly. Its persistent dm-clone metadata and frontend are owned by one
    exact node/boot epoch; PVE starts the migration target before the source
    writer is closed, so target activation fails closed instead of activating
    the same clone metadata in two kernels. Run `sharedlvmthin
    migration-preflight VMID TARGET --online`, materialize every reported Lazy
    disk on the active source, verify the preflight again, and only then retry
    `qm migrate`. Materialized Lazy disks use the ordinary Thick linear
    migration contract. A stopped `LAZY_DORMANT` disk is a separate guarded
    offline-handoff case. Skipping preflight cannot bypass the target-side
    ownership refusal.
18. Snapshot vmstate orchestration has two qualified upstream cleanup
    boundaries which are not storage-plugin hooks. On the API14 reference tuple
    (`qemu-server` 9.1.15), vmstate allocation precedes the live machine, CPU
    and network-MTU queries; a query failure can therefore leave an allocated
    volume without a configuration reference. The tested 9.2.10 tuple moved
    those queries before allocation. On both tuples, `savevm-end` and vmstate
    deactivation occupy one `eval`, so a `savevm-end` exception skips the
    deactivation call, and the following QMP finalization loop has no explicit
    wall deadline. Exact integrated API15 testing additionally proves that a
    warning-only `savevm-end` failure can be followed by forced snapshot
    cleanup: deletion of the still-open vmstate is refused, while upstream
    nevertheless removes the snapshot config section and lock. The preserved
    vmstate is then an unreferenced orphan requiring explicit reconciliation.
    If the subsequent `query-savevm` throws, that finalizer exception replaces
    the original disk-snapshot error and cleanup is not entered; the prepared
    section and `lock: snapshot` remain. SharedLVM health inventory reports
    both unreferenced managed
    volumes and refuses ambiguous cleanup; it cannot safely rewrite the PVE
    snapshot state machine. Treat a timed-out or warning-only snapshot as
    unresolved until the exact UPID, QMP state, config reference, LV identity,
    mapper opens and worker inventory are reconciled. Never infer cleanup from
    a successful client exit code or delete an unreferenced vmstate LV merely
    from its name.
19. Guest-agent filesystem freeze is not a RAM-snapshot consistency oracle.
    In both tested qemu-server tuples, RAM snapshots skip the agent freeze
    path; running disk-only snapshots use it only when the agent reports the
    operation applicable. Freeze and thaw exceptions are emitted as warnings
    and do not themselves fail the snapshot caller. Require explicit guest
    application-consistency evidence where that property matters, and never
    reinterpret an otherwise successful PVE task as proof of a completed
    freeze/thaw cycle.
20. A Proxmox snapshot rollback stops a running guest only after asking every
    storage plugin whether the exact target is ready. Older SharedLVM
    candidates treated this preflight as a no-op, so a Lazy/Thick transition
    could be rejected later, after the VM had already stopped. TG53 candidates
    now refuse non-materialized or ambiguous Thick targets in the public
    pre-stop hook and repeat all checks in the mutating hook. This reduces an
    avoidable outage but cannot eliminate a state change between preflight and
    mutation; a later ambiguity still fails closed.
21. Snapshot rollback of a mixed-storage VM can have a replication-side
    prefix before SharedLVM's pre-stop hook. PVE correctly excludes shared SAN
    volumes from `get_replicatable_volumes()`, but a local replicatable disk in
    the same VM may enter legacy replication cleanup first. If that local
    plugin throws without populating the blockers array, upstream can remove
    all of its local replication snapshots before checking the VM lock and
    before a later SharedLVM preflight refusal. SharedLVM still prevents SAN
    rollback and VM stop in the tested refusal path, but cannot roll back an
    earlier effect owned by PVE replication orchestration. Inspect replication
    state before retrying; do not describe a failed mixed-storage rollback as
    globally side-effect-free.
