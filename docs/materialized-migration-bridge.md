# Materialized Migration Bridge

## Purpose

Native shared-storage live migration opens a VM disk on the destination before
the source has completely released it. That overlap is safe for an ordinary
linear LV, but it is not a safe ownership transfer for a per-VM dm-thin pool:
two kernels can load independent in-memory views of the same mutable thin
metadata.

The bridge preserves an online VM while ensuring that cross-node movement is
performed only with independent Thick Generations linear volumes:

```text
running Thin VM
  -> native PVE online Storage Move to Thick Generations
  -> verify every managed disk is Thick and storage health is positive
  -> native PVE shared-storage live migration
  -> optionally native PVE online Storage Move to a new Thin pool
```

It uses only software shipped with Proxmox VE plus this plugin. It does not
patch QEMU or PVE, install a lock daemon, share dm-thin metadata between
kernels, or call private QMP commands.

## Invocation

The first implementation is deliberately explicit:

```bash
sharedlvmthin-migrate-bridge preflight \
  <vmid> <target-node> <thin-storage> <thick-storage>

sharedlvmthin-migrate-bridge run \
  <vmid> <target-node> <thin-storage> <thick-storage>
```

Add `--keep-thick` to omit the final Thick-to-Thin move. The default returns
the disks to a newly allocated Thin pool on the destination.

## Safety contract

- The VM must be running and every non-CD disk must be on the selected Thin
  storage. Mixed layouts are refused by the first implementation.
- Thin and Thick aliases must refer to the same qualified VG.
- Both storage recovery checks, quorum, target membership, exact disk sizes
  and physical VG capacity must pass before the first mutation.
- Full Thick capacity for every disk plus the configured fixed VG reserve is
  admitted up front. A failed capacity proof is not treated as overcommit.
- Every disk move uses PVE's supported online Storage Move path and deletes
  its source only after PVE reports the block mirror and pivot completed.
- Cross-node live migration starts only after every managed disk is Thick.
- A transaction-scoped VG admission tag serializes the materialization phase
  across the cluster. This prevents independent evacuation workers from
  overlapping long-lived Thick allocation intents. Waiting is bounded;
  timeout or a crashed holder leaves explicit evidence and fails closed.
- Once materialization and its health postcondition complete, admission is
  released. Independent Thick live migrations may then execute concurrently.
- Multi-disk conversion is resumable, not falsely atomic. If a later disk
  fails, already converted disks and the VM configuration are preserved as a
  functional mixed state for explicit recovery; no speculative rollback is
  attempted.
- A transaction state file records monotonic phases and the exact pmxcfs VM
  configuration digest. Ambiguous outcomes preserve all evidence.

## Scope

This makes safe live mobility available to a Thin-backed workload by moving
the cross-node handoff into a bounded linear-volume interval. It does not turn
dm-thin itself into cluster-aware storage and does not enable overlapping
activation of one thin pool on two hosts.

The current bridge is a development candidate. Qualification must cover
multi-disk guests, concurrent evacuation, interrupted Storage Move, target
loss, capacity exhaustion and host loss at every recorded phase before it is
presented as production-ready.
## TG30 progress, capacity and recovery hardening

TG30 remains a development candidate: validate it on disposable storage before
production use.

The bridge temporarily materializes every per-VM Thin disk as an independent
Thick Generations LV, performs the ordinary PVE online migration, and optionally
copies the disks back into a per-VM Thin pool on the target. It never activates
the same dm-thin metadata on two kernels.

Before the return copy, TG30 creates the exact target pool under the canonical
VG lock. Its reservation is the aggregate virtual disk size plus configured
burst headroom (one GiB by default). This prevents a fast full-copy from
outrunning asynchronous autogrow.

Long copies have no arbitrary wall-clock timeout. The root-owned transaction
state records the active disk, monotonic PVE mirror progress, byte count and
timestamps. Read it without taking the mutation lock:

```bash
sharedlvmthin-migrate-bridge inspect <vmid>
```

New transactions use state schema v3. Beside the state file, the bridge writes
a mode-0600 immutable disk manifest containing every slot, original volume ID
and exact virtual byte size. Its SHA-256 digest is committed into the state
record. Recovery refuses a missing, duplicated, modified or count-mismatched
manifest before contacting another node.

If the copy back to Thin completed but the final health check or state publish
was interrupted, TG30 can finalize only after proving the saved transaction,
source/target identity, online target, running VM, exact all-Thin disk topology,
completed disk count and a positive target recovery check:

```bash
sharedlvmthin-migrate-bridge resume <vmid>
```

All earlier or mixed phases are deliberately refused. `resume` never guesses,
deletes storage, retries an ambiguous copy, or rewrites VM configuration.
The bridge-admission helper also exposes a read-only exact state inspection;
finalization requires the VG-wide materialization admission to be absent and
both Thin and Thick recovery checks on the target to pass.

Thin pool capacity is isolated per VM. A reported `Data% >= 95` blocks
mutations of that pool in the plugin, but does not block unrelated VM pools in
the same VG. Doctor reports it as a scoped capacity warning.
