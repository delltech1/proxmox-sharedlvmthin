# SharedLvmThin 0.9.0~rc5.4~tg12 experimental release notes

tg12 adds a read-only raw block-device size probe for the PVE content API.
This avoids transient `no format` responses while QEMU holds an active volume
and removes a ten-retry inventory fallback observed during Veeam HotAdd backup.
The probe never writes to or auto-detects the format of the guest device.

This is an unpublished Thick Generations qualification candidate for Proxmox
VE 9 Storage API 14 and 15. It retains the conventional per-VM Thin mode and
adds an explicitly selected Thick Generations mode on the same pinned shared
VG. Thick snapshots use temporary persistent `dm-clone` transitions and return
to independent linear LVs after materialization.

The candidate includes fail-closed identity, quorum, cluster-lock, ownership,
capacity, recovery and incompatible-anchor-schema gates. Qualification covers
Thin and Thick allocation, resize, snapshots, rollback, full copy, backup and
restore, cross-node migration, bidirectional Thin/Thick storage moves, host
loss, bounded iSCSI path loss, interrupted materialization and exact cleanup.
Linked clones remain explicitly unsupported.

Physical FC and the final four-hour concurrent endurance window remain release
gates. This candidate must not be published or used for production until those
requirements and the final accepted-build checks are complete.
