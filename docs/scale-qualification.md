# Scale qualification

Scale results are evidence for the tested lab, not a certification for an
arbitrary SAN, CPU, network, guest workload or cluster size.

## 2026-09-13 disposable three-node gate

- mixed PVE 9 Storage API 14 and 15 cluster;
- one pinned two-path iSCSI LUN, expanded online from 150 GiB to 450 GiB;
- WWID, PV UUID and VG UUID unchanged across the expansion;
- 150 simultaneously running thin-mode VMs and 150 independent per-VM pools;
- 72-VM node evacuation driven by 16 migration workers;
- exact source/destination configuration, local mapper, D-state, quorum,
  multipath and recovery checks after each fault or implementation change.

The gate found and fixed two real contention defects:

1. PVE calls `cluster_lock_storage()` around allocate/free with an undefined
   timeout.  On the qualified releases that can expire before a legitimate
   high-object-count allocation finishes.  The plugin now applies the
   configured bounded `slt-lock-timeout` to this outer wrapper too.
2. Thin deactivation was incorrectly serialized by the canonical shared-VG
   mutation lock.  Deactivation, dmeventd unregister and DM teardown are
   node-local runtime operations and do not update shared VG metadata.  They
   now retain exact identity, ownership and mapper postconditions without
   serializing unrelated VM migrations cluster-wide.

The direct A/B result for the second fix was:

- before: 50 successful migrations, 5 completed-with-cleanup-timeout and 17
  not attempted after worker failure; five exact stale local mapper chains;
- exact recovery: each affected VM proven running on another node, local open
  count zero, then guest LV followed by its exact pool deactivated; no force;
- after: the remaining 17 VMs completed at 16-way concurrency, 17/17 PASS,
  with an empty source node and no stale mapper.

## Operational scaling observations

- Per-VM pool creation generates several LVM metadata transactions.  Large VG
  inventories and accumulated `/etc/lvm/archive` files materially increase
  latency and can expose bounded lock timeouts.
- `slt-lock-timeout` accepts 10..600 seconds.  It must be chosen from measured
  worst-case serialized operations at the site.  Raising it increases how
  long a worker may wait; it does not create parallel shared-metadata writes,
  configure fencing or change the PVE HA watchdog.
- LVM archive retention is administrator-owned host policy.  The plugin does
  not prune archives or rewrite `lvm.conf`; Doctor/runbooks may report the
  condition but never clean it automatically.
- At 150 pools a full positive recovery check can take roughly one to two
  minutes in this lab.  Safety checks are not silently skipped to improve a
  benchmark.  Further work should cache only immutable/read-only inventory
  within one check while preserving exact postconditions.

No 200–500 LUN or enterprise-array scale claim is made by this gate.
