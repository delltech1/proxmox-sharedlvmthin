# SharedLvmThin RC5.75 TG53 candidate

RC5.75 is an unpublished experimental hardening candidate. It is not a
production release and must be used only on disposable Proxmox VE hosts,
shared storage and guest data.

## Changes from RC5.74

- Replaces the plugin's two direct calls to the upstream global
  `LVMPlugin::lvm_vgs()` parser with one bounded, read-only JSON inventory
  probe scoped to the configured VG and pinned multipath device.
- Requires an exact VG name and, where configured, the expected VG UUID and
  WWID. Empty, duplicate, malformed, overflowing or impossible capacity
  reports fail closed; missing values are never converted to zero.
- Adds exact task-receipt test helpers so a successful `pvesh` dispatcher exit
  cannot be mistaken for a successful remote storage worker.
- Adds the five-way Thin/Eager/Lazy snapshot and move contention regression
  with per-volume SHA-256 postconditions and exact cleanup receipts.

This change reduces exposure to an upstream `LVMPlugin.pm` parser warning in
our own activation/status hooks. It does not patch other Proxmox storage
plugins or claim that global PVE inventory can never emit that upstream
warning.

## Current evidence

- Complete isolated source suite: 2,156 Python tests passed with one
  intentional skip; 1,278 Perl tests passed.
- Exact live read-only probe passed for the disposable lab Thin, Eager and Lazy storage
  definitions and correctly distinguished a nonexistent VG.
- Five-way contention produced two successful snapshots and three safe
  fail-closed move refusals; every source backing and canary remained exact.
- Sequential Eager-to-Lazy and materialized-Lazy-to-Thin moves preserved data
  hashes and removed sources only after complete copies.
- All five disposable VM namespaces were removed and both involved VGs ended
  healthy and safe for mutation.

RC5.75 remains unpublished until guarded cluster rollout, post-install live
inventory, mixed-mode migration and final profile/package gates pass.
