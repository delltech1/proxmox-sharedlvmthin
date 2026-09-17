# Experimental Thin Pool LeaseGuard

## Purpose

The existing TG26 node/epoch tags prevent a second managed activation and
make ambiguous state fail closed. They are durable ownership evidence, but
they are not an independently renewed runtime lease and cannot fence a host
that loses cluster communication while retaining SAN access.

LeaseGuard is a research design for adding one exclusive `sanlock` resource
lease per managed per-VM thin pool. The lease lives on a dedicated, pinned
shared lease LV. It is not stored in the thin pool whose ownership it protects.

The intended invariant is:

```text
valid cluster quorum
AND exclusive disk-backed pool lease
AND matching durable node/epoch owner
AND exact local dm-thin runtime mapping
    -> pool may serve guest I/O
```

Losing any term must never be interpreted as permission to guess or repair.

## Why this is materially different

`pmxcfs` locks and LVM tags serialize control-plane decisions. A sanlock
Paxos resource lease is arbitrated on the shared storage itself. Competing
hosts cannot both acquire the same exclusive lease. On PVE, `/dev/watchdog`
is already owned by `watchdog-mux`, so starting `wdmd` as a second watchdog
owner is not a valid integration. The qualified design runs sanlock without
direct watchdog ownership and uses a purpose-built watchdog-mux client bridge:
it refreshes its PVE watchdog client only while the lockspace, resource lease,
durable owner, quorum and storage identity are all positively healthy.

This can strengthen runtime ownership during network partitions and complete
host failure. It does **not** make dm-thin metadata cluster-coherent and does
not make ordinary PVE Thin live migration safe. Target activation must still
wait for source close, pool deactivation, lease release or expiry with proven
fencing, and a fresh target activation.

## Proposed on-disk model

- One dedicated lease LV per SharedLvmThin storage, outside all thin pools.
- One sanlock lockspace with a unique host ID per cluster node.
- One deterministic Paxos resource slot per managed pool.
- Resource name derived from the immutable storage identity, VG UUID and pool
  LV UUID, never from a reusable VM name alone.
- The current owner epoch remains in LVM tags and must match a lease-side
  version/generation recorded in the transaction evidence.

The lease LV must be pinned to the same positively verified multipath identity
as the protected VG. Raw SCSI paths are never acceptable lease paths.

## Activation protocol

```text
cluster quorum PASS
storage identity PASS
recovery gate PASS
acquire exclusive pool lease
re-read and validate lease owner/generation
under canonical cluster/VG lock, claim a new node/epoch owner
activate the pool locally
verify exact mapper UUID, table, dependencies and local owner correlation
publish success
```

Any failure runs bounded rollback. If absence of a local mapper or lease
release cannot be positively proved, the result is `RECOVERY_REQUIRED`, not a
retry on another host.

## Deactivation protocol

```text
close every child volume
prove no child mapper remains
deactivate public and hidden pool mappings
prove the exact local `-tpool` mapping is absent
clear the matching node/epoch owner
release the exact pool lease
verify the lease is no longer locally held
```

The lease must be held for the complete interval in which the local kernel can
mutate thin metadata. Releasing it before the hidden pool mapping disappears
would recreate the original corruption window.

## Failure and watchdog rules

- Lease expiry is not by itself proof that an unfenced kernel stopped I/O.
  The sanlock/watchdog-mux integration must prove the old host reset deadline
  has passed before another host activates the pool.
- The bridge may send watchdog-mux magic close (`V`) only after it positively
  stopped all protected QEMU I/O and removed the local pool mapping. If that
  cannot be proven, it stops refreshing and allows watchdog fencing.
- Once lease health becomes uncertain, the bridge is monotonic: it cannot
  resume refreshes from a later transient PASS without a new activation.
- All participating hosts must use the same validated watchdog and I/O timeout
  policy. Custom values are detected and checked; they are never overwritten.
- A missing watchdog, disabled sanlock recovery, mismatched host ID, lease I/O
  timeout, unverified multipath identity or unavailable lease LV blocks
  activation.
- SCSI persistent reservations may be additional whole-LUN defense, but cannot
  replace the per-pool lease or fresh dm-thin activation.
- The plugin never initializes an unknown lease area automatically.

## Performance properties

Sanlock renews one host lockspace lease and uses it to maintain ownership of
many resource leases. It does not need to rewrite every per-pool Paxos lease
on each renewal. Steady-state guest I/O remains the normal dm-thin path and
does not pass through a userspace lock service.

Pool activation/deactivation gains bounded lease I/O. This is acceptable for
strong ownership and avoids adding a distributed lock round-trip to every
guest write, allocation or discard.

## Qualification stages

1. Run `experiments/thin-lease/readiness-check.sh` read-only on every node.
2. Create a disposable lease LV with an explicit operator command and record
   its LV UUID, WWID, size and offsets.
3. Test raw sanlock contention with no LVM pool or VM involved.
4. Qualify the watchdog-mux bridge first with a fake UNIX socket. Then test
   watchdog expiry on an empty disposable nested PVE node with verified
   external power control, and prove the old node resets before the new lease
   holder is allowed to activate.
5. Wrap one disposable thin pool, then test clean start/stop, process crash,
   node power loss, quorum loss, SAN path loss and return.
6. Prove that target preparation during Thin live migration is refused without
   leaving a mapper, owner tag or lease behind.
7. Only after all stages pass may LeaseGuard become an opt-in experimental
   backend. It must not silently alter existing TG26 storage.

## Current result

The qualified PVE 9 nodes expose watchdog devices, use LVM built with sanlock
and DLM support, and provide a sanlock package candidate. PVE watchdog-mux is
the existing watchdog owner; `wdmd` must not be started alongside it. Sanlock
is not currently installed and no lease area has been created. Therefore the
design is feasible on the present platform, but runtime protection is not yet
claimed. The current bridge is intentionally non-arming and simulator-only.

The activation decision is implemented as a separately tested, opt-in safety
primitive. Disabled LeaseGuard preserves existing storage behavior. When
enabled by a future qualified backend, every lease-area identity, sanlock
daemon, joined lockspace, exclusive resource, durable owner, quorum, storage
identity and fresh local pool-runtime input must be explicitly positive;
missing evidence is `UNKNOWN` and blocks activation. This primitive does not
claim that the sanlock lifecycle backend is complete.
