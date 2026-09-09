# Clone/restore burst-capacity qualification

## Failure class

PVE calls `alloc_image()` with a name and virtual size, then performs a full
clone with a separate `qemu-img convert -n`. The hook receives no reliable
clone/restore/bulk-write context. A fixed 4 GiB per-VM pool can therefore fill
faster than asynchronous dmeventd autogrow completes. The first Windows clone
reproduced this safely: qemu-img received ENOSPC, PVE removed the incomplete
target, and the source config/LVs were unchanged.

This is not a TRIM defect. `sparseinit` and physical burst headroom solve
different problems. RC5 advertises the zero-initialized semantics of a new
thin LV to avoid materializing zero extents, but it does not claim that this
alone guarantees capacity.

For a raw LVM block source, PVE's supported `qemu-img convert` path can still
scan the complete virtual size. A lab `qemu-img map` of the stopped 64 GiB
Windows source reported one `data=true` extent covering the entire device, so
`-S` cannot skip source reads; it only suppresses sufficiently long zero runs
on the destination. RC5 therefore does not wrap `qemu-img`, patch PVE core, or
inject `-m`, `-W`, `-C`, cache, or multipath tuning. Those controls are outside
the PVE storage-plugin API and can change ordering, offload, or durability
semantics. Doctor reports the scan behavior as an operational diagnostic.

## Allocation-headroom policies

### Recommended production policy: elastic absolute headroom

RC5.3 adds a virtual-size-independent policy:

```ini
slt-initial-pool-mode elastic
slt-burst-headroom-gib 64
slt-vg-reserve-percent 5
```

The first allocation targets the smaller of its virtual size and the absolute
headroom (with a 1 GiB technical bootstrap for EFI/TPM-sized allocations).
Later allocations and dmeventd events target live physical use plus the same
absolute headroom, rounded upward to a GiB. A 30 TiB virtual disk therefore
does not reserve 15 TiB: with 10 GiB physically used and 64 GiB configured
headroom, the target is 74 GiB. The ceiling applies to headroom, never to the
total lifetime size of the pool.

Elastic growth remains subject to the cluster storage lock, quorum, exact
storage identity, ownership, thin-pool health, extent rounding and protected
VG reserve. It never shrinks a pool and does not claim that any finite
headroom can absorb an unlimited write rate. New installations use an early
50% LVM event threshold; administrators with an existing LVM policy retain
their configuration and Doctor reports it for qualification.

The compatibility mode remains available for existing deployments:

```ini
slt-initial-pool-mode fixed
slt-initial-pool-size 4
```

`fixed` preserves earlier behavior and provides no bulk-write guarantee.
Doctor reports this as a diagnostic warning and never changes the policy.

New deployments can select:

```ini
slt-initial-pool-mode proportional
slt-initial-pool-size 8
slt-initial-pool-percent 50
slt-initial-pool-max 128
```

For every new guest LV, proportional mode targets live used bytes plus the
larger of the fixed minimum or the configured percentage of the new virtual
disk. The optional maximum is an explicit bounded guarantee.

For strict admission:

```ini
slt-initial-pool-mode full
```

Full mode targets live used bytes plus the complete newly requested disk. If a
small EFI/TPM-style volume creates the pool first, the fixed minimum still
applies. A configured maximum that is smaller than the required full target
causes a fail-closed allocation; it never silently weakens `full` semantics.

An inactive thin pool can omit `Data%`. RC5 does not activate shared storage to
improve an estimate. It conservatively treats the current pool size as fully
used, which can over-reserve but cannot under-provision the next allocation.

## Mutation boundary

Under the PVE storage lock, immediately before creation/growth, RC5 revalidates
quorum, identity, ownership, pool health, live VG capacity and reserve. The
admission calculation includes extent rounding and a conservative thin
metadata/pmspare allowance. It also verifies the actual reserve after LVM has
completed.

If preflight fails, zero `lvcreate`/`lvextend` commands run. If a pre-grow has
an uncertain result, RC5 reads the size postcondition exactly once and never
blindly retries or shrinks the pool. A larger owned pool with no new guest LV
is an acceptable PARTIAL state; automatic cleanup remains prohibited.

## Qualification evidence (2026-09-02)

- 85 Perl and 39 Python automated tests passed.
- Disposable loop/VG proportional multi-disk gate: 8 → 18 GiB and burst write
  PASS, zero artifacts.
- Disposable loop/VG full multi-disk gate: 4 → 10 GiB and burst write PASS,
  zero artifacts.
- Real `pvesm alloc` property/lock gate: 4 → 10 GiB on the second disk, PASS,
  zero artifacts.
- Windows 11 full clone: EFI created the 4 GiB minimum; the 64 GiB system disk
  pre-grew the pool to 68 GiB; clone completed without ENOSPC; TPM completed;
  source config/inventory remained unchanged.
- A second 8 GiB Windows disk pre-grew the existing pool to 76 GiB.
- Windows non-zero 4 GiB allocation changed pool Data% 41.75 → 47.01;
  Windows ReTrim reported 7.95 GiB trimmed and host Data% returned to 41.75.
- Four-volume Windows snapshot/rollback (OS, second disk, EFI, TPM) removed a
  post-snapshot marker, preserved activation flags, and snapshot deletion plus
  VM cleanup left zero target artifacts.

The bulk clone exposed a separate performance observation: healthy two-path
iSCSI experienced long burst/pause latency. It caused no ENOSPC or new kernel
I/O error and is not hidden by the capacity policy.

LVM grew the VG-global metadata spare while creating the 76 GiB disposable
pool and intentionally retained it after pool deletion. RC5 never shrinks or
removes pmspare automatically.
