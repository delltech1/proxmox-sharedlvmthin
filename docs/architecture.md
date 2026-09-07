# Architecture

The data path is `QEMU -> device mapper/LVM -> multipath (when used) -> shared block storage`. SharedLvmThin participates in lifecycle operations, not each guest I/O.

Each VM owns one thin pool named `sltp-<VMID>`. Its disks and snapshots reside in that pool. New pools carry a storage-specific `pve-slt-sid-<storage-id>` tag. RC4 refuses to mutate pools tagged for another storage. Untagged RC3-era pools are treated as legacy: their explicitly requested disks remain manageable, but the pool is preserved rather than silently adopted or automatically deleted.

PVE's cluster storage lock serializes allocation and deletion. RC4 also takes the same lock for resize and snapshot mutations, because the low-level PVE snapshot/resize dispatch does not add it. The dmeventd monitor uses the same lock before policy-based autogrow. There is no private quorum mechanism.

The HTTPS dashboard and health collector are optional read-only monitoring components. Stopping them does not stop the storage plugin.
