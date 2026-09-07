# Cluster and quorum

SharedLvmThin does not calculate or change quorum. Proxmox/Corosync remains the
authority for every shared-storage mutation, independent of cluster size.

- Standalone, non-shared storage does not require cluster quorum.
- A quorate N-node cluster may mutate only after the normal identity, ownership,
  lock and capacity gates also pass.
- A configured but non-quorate cluster remains visible to read-only Doctor and
  inventory, while every mutation fails closed.
- A healthy two-node 2/2 cluster without qdevice is operational but receives a
  resilience warning because either node loss removes quorum.
- A legitimate qdevice-backed survivor follows the native Proxmox quorum result.
- A clearly detected multi-node `expected_votes=1` forced-quorum state is
  classified CRITICAL and SharedLvmThin mutations are refused by default.

The plugin never runs `pvecm expected`, changes votes, configures a qdevice, or
treats a degraded cluster as standalone. Existing guest I/O is not in the
plugin data path and is not stopped by these admission checks.
