# Migration

## Thin mode

Thin mode requires exclusive single-kernel ownership of each per-VM pool.
Normal PVE shared-storage live migration activates storage on the target while
the source QEMU process can still hold and modify the same pool. That overlap
is unsafe for dm-thin even when PVE later runs only one VM instance.

The plugin therefore persists a versioned owner record containing the exact
node and a fresh activation epoch. It refuses target activation while a
different node owns the pool. **Direct in-place shared dm-thin live migration**
is unsupported and must fail closed; the guard must never permit two kernels
to own the same thin pool merely to satisfy PVE's source/target overlap.

This does **not** mean that a running VM whose disks start in Thin cannot be
migrated online. The experimental Materialized Migration Bridge performs the
supported workflow `Thin -> Thick -> normal PVE live migration -> Thin` while
the VM remains online. Cross-node migration begins only after every managed VM
disk is an independent Thick Generation, and the optional return creates new
target-owned Thin pools. See
[materialized-migration-bridge.md](materialized-migration-bridge.md).

A successful direct Thin migration task from an older release is not evidence
of metadata safety.

Direct Thin-to-Thin cross-node movement without materialization is an offline
handoff:

1. stop and flush QEMU on the source;
2. deactivate the final Thin LV and pool;
3. positively verify that the public pool, hidden `-tpool`, and all child
   mappings are absent;
4. release the source owner under the canonical cluster/VG lock;
5. claim ownership and activate the pool freshly on the target.

After a hard host failure, do not clear ownership merely because the node is
offline. First prove external fencing/STONITH. Then an administrator may run:

```bash
sharedlvmthin thin-recover-fenced-owner <storage-id> <volume> <fenced-node>
```

The command validates the exact pool, owner, identity, quorum, lock, and local
absence of dm-thin mappings. The `<fenced-node>` argument is an explicit
operator assertion; the plugin does not perform or infer fencing.

### Opt-in PVE HA fenced-owner takeover

TG29 adds an opt-in PVE HA integration for the same fenced-owner transition:

```text
slt-thin-leaseguard remote-audit
slt-thin-ha-takeover pve-ha
```

It is disabled by default. It does not infer fencing from ping, SSH failure,
elapsed time or Corosync membership alone. Under the canonical VG lock it
requires fresh PVE HA manager evidence that the exact service was assigned to
the local online node and that the former owner is in PVE's fenced/offline
state. Every other configured peer must then positively prove absence of the
exact mapper. Only after those checks pass is the exact former owner/epoch
removed and a fresh local epoch claimed.

Peer audit deliberately occurs before the old owner tag is removed. A failed
or interrupted audit therefore leaves the durable recovery context intact and
the operation safely retryable. This mode requires functional PVE HA fencing,
quorum and identical plugin code on every participating node. It cannot make a
non-quorate two-node cluster safe; use an external QDevice/fencing design or
the explicit manual recovery procedure after positive fencing.

### Upgrading an existing Thin pool

Pools created before the exclusive-owner format have no trustworthy initial
owner record. They are never interpreted as safely unowned. Package installation
is refused if such a pool is active locally, and activation/allocation remains
blocked until the administrator completes this one-time offline conversion:

1. stop and deactivate every guest using the per-VM pool;
2. verify the pool, hidden `-tpool`, and its children are absent on **every**
   cluster node;
3. install the new package on every participating node;
4. adopt the exact pool through one canonical guest volume:

```bash
sharedlvmthin thin-adopt-owner-model <storage-id> <volume> ALL-NODES-INACTIVE
```

The same command is idempotent for an already-adopted, unowned pool and
hardens older releases by disabling generic LVM autoactivation on the exact
pool and all of its thin volumes. It refuses while any local mapper exists;
the operator assertion must be true on every participating node.

The confirmation is an explicit operator assertion about all nodes. The
command independently verifies identity, quorum, cluster locking, ownership,
and local runtime absence; it adds only the persistent schema marker. The next
activation creates a fresh owner node plus epoch pair.

## Materialized Thick Generations

A materialized Thick Generation is an independent fully allocated LV behind
an ordinary linear frontend. It does not share mutable dm-thin metadata and
remains independently eligible for normal PVE shared-storage live migration.
Transitional/hydrating generations retain their existing fail-closed rules and
cannot be reconstructed on another node without exact recovery evidence.

### Interrupted online disk moves

An interrupted QEMU block job can briefly leave three different views of a
disk: the durable bridge transaction, the pmxcfs VM configuration, and QEMU's
live block graph. Process command-line arguments are not sufficient evidence,
and pmxcfs can name the destination while QEMU is still writing the source.

Recovery planning therefore performs a bounded, read-only QMP `query-block`
probe on the authoritative running node. Every manifest slot must resolve to
the exact canonical `/dev` path named by the current PVE configuration. Any
missing disk, foreign path, stale destination, malformed response, unavailable
QMP socket, or config/runtime mismatch returns
`RUNTIME_CONFIG_DIVERGENCE` and blocks automatic mutation. An operator must
then stop the guest and reconcile the two views from positive evidence; the
bridge never guesses which copy is newer.

