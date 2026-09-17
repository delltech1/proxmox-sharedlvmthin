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
