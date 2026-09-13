# Scale qualification

Scale results are evidence for the tested lab, not a certification for an
arbitrary SAN, CPU, network, guest workload or cluster size.

## 2026-09-13 disposable three-node gate

- mixed PVE 9 Storage API 14 and 15 cluster;
- one pinned two-path iSCSI LUN, expanded online from 150 GiB to 450 GiB;
- WWID, PV UUID and VG UUID unchanged across the expansion;
- 150 simultaneously running thin-mode VMs and 150 independent per-VM pools;
- 150 simultaneously running thick-mode 1 GiB VMs, distributed across all
  three nodes, after rolling TG21 installation;
- the thick baseline retained identical package/plugin hashes, quorum, 2/2
  paths per node, exact WWID/PV/VG identity and zero D-state processes;
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

The thick allocation path was also reduced from separate post-create tag and
autoactivation mutations to atomic creation attributes. In an isolated 1 GiB
allocation this reduced the archive-producing metadata commits from nine to
five and completed in 48.3 seconds. Exact tag and autoactivation read-back,
PREPARED/MATERIALIZED phase verification, VG intent and capacity gates remain
in place.

For TG22, an exact 1 GiB SAN A/B used full-device SHA256 readback before and
after each operation. `BLKZEROOUT` completed in 0.50--0.81 seconds and the
direct synchronous zero fallback in 28.78--30.40 seconds. Both produced the
same all-zero SHA256, removed only their transaction-scoped test LV, and
returned the VG to exactly 159161253888 free bytes. The optimization never
uses discard/UNMAP and falls back to a complete direct rewrite after any
zeroout error. Results are specific to this target and are not a throughput
claim for other SAN implementations.

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
- `slt-lock-timeout` accepts 10..86400 seconds. It is a bounded admission wait,
  not a watchdog value or universal enterprise default. It must be chosen from
  measured worst-case serialized operations at the site. Raising it increases
  how long a worker may wait; it does not create parallel shared-metadata
  writes, configure fencing or change the PVE HA watchdog.
- `slt-lock-yield-ms` (default 1000, range 0..5000) introduces a cooperative
  delay only after an outer PVE mutation releases the pmxcfs storage lock. It
  gives already-waiting nodes an opportunity to acquire a lock which is
  bounded but not FIFO. It never sleeps inside the critical section, retries a
  callback, or treats a timeout as proof that a mutation did not occur.
- Size the admission timeout from observed P95/P99/max operation latency, the
  largest qualified disk-zeroing time and the maximum qualified number of
  contenders. A higher timeout alone does not establish fairness or scale;
  the measured scale gate remains authoritative.
- LVM archive retention is administrator-owned host policy.  The plugin does
  not prune archives or rewrite `lvm.conf`; Doctor/runbooks may report the
  condition but never clean it automatically.
- VG text-metadata capacity is a separate hard scale limit. At 526 LVs during
  this gate, the deliberately provisioned 8 MiB metadata area had about
  3.65 MiB free. `pv_mda_free` must therefore be recorded alongside VG data
  capacity: more timeout, RAM or SAN throughput cannot recover an exhausted
  circular metadata area. Size `pvmetadatasize` before creating a production
  PV and use multiple independently pinned VG/storage domains when one VG
  cannot retain safe metadata and failure-domain headroom at the intended
  object count. The plugin diagnoses this state but never rewrites PV/VG
  metadata layout automatically.
- At 150 pools a full positive recovery check can take roughly one to two
  minutes in this lab.  Safety checks are not silently skipped to improve a
  benchmark.  Further work should cache only immutable/read-only inventory
  within one check while preserving exact postconditions.

No 200–500 LUN or enterprise-array scale claim is made by this gate.
