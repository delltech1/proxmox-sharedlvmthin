# TG lazy-zero RAW research — deferred next phase

Status: **research only; no implementation, installation or release**.

Read-only research, design and isolated test-harness construction may proceed
in parallel with the Thick stabilization work. Any kernel/storage mutation or
plugin wiring starts only after its own reviewed disposable-fixture gate and
must not alter or invalidate the DUAL/Thick-only baseline. Existing complete
initialization remains the production fallback.

## Fixed product requirements

- Guest images remain RAW and the final state remains the existing
  `dm-linear -> ordinary thick LV` state.
- Existing Thick generations, Thin/Thick moves, backup/restore and Veeam paths
  require no format conversion.
- No qcow2 or permanently different COW format is introduced.
- Full virtual capacity is allocated from the VG before publication.
- No byte from reused VG extents is observable until replaced by zero or by a
  write belonging to the current generation.
- Unsupported kernels, ambiguity or failed qualification fall back to eager
  complete initialization; they never publish the uninitialized raw LV.

## Preferred stock-PVE research candidate

```
dm-zero -> dm-clone -> fully allocated RAW destination LV
                    -> stable TG frontend -> QEMU

complete hydration -> verified atomic pivot -> dm-linear RAW destination LV
```

This preserves the desired final format. Upstream dm-clone provides the needed
partial-write behavior: a partial first write waits for complete region
hydration, so a dm-zero source supplies zeros for its unwritten remainder.

## P0 blocker: discard skips hydration

Upstream dm-clone treats discard of an unhydrated region as permission to skip
source copying and mark that region hydrated. `no_discard_passdown` only blocks
the underlying UNMAP; it does not block this metadata transition. A reused
nonzero destination may consequently become readable after discard.

The first no-module research path is to suppress discard at the queue of the
guest-visible DM frontend for the complete guarded phase:

- QEMU raw node uses `discard=ignore` and never `detect-zeroes=unmap`;
- the frontend queue advertises no discard capability;
- direct `BLKDISCARD` against the frontend must return `EOPNOTSUPP` and leave
  clone metadata and visible data unchanged;
- native WRITE ZEROES must be either positively qualified or rejected; on the
  current upstream clone target it is expected not to be advertised, while
  BLKZEROOUT and QEMU may fall back to ordinary writes and need separate tests;
- activation/reconstruction reapplies and verifies the restriction before any
  consumer can open the device;
- reload, suspend/resume, migration activation and reboot create no interval
  in which discard is accepted.

This is only a hypothesis. A sysfs value or QEMU setting is not safety proof;
the exact PVE 9 kernel and complete lifecycle must demonstrate the behavior.

## Alternatives reviewed

| Candidate | Same final RAW LV | Extra kernel module | Disposition |
| --- | --- | --- | --- |
| dm-clone + dm-zero + proven queue discard suppression | yes | no | preferred next-phase research; proof open |
| dm-clone + dm-zero without suppression | yes | no | rejected: stale-data disclosure possible |
| persistent dm-snapshot + dm-zero | no direct linear pivot | no | rejected for TG format requirement |
| qcow2/PVE volume chain on an LV | no | no | comparator only; excluded from TG |
| BLKZEROOUT/native WRITE ZEROES | yes | no | separate behavior gates; potential acceleration, not universal lazy initialization |
| UNMAP/read-zero | yes | no | array-specific only after complete qualification |
| patched dm-clone/custom DM target | yes | yes | last resort; conflicts with update maintainability |
| dm-thin, VDO or filesystem sparse image | no | varies | outside fully allocated RAW-per-volume design |

## Mandatory next-phase qualification

1. Fill every region of a disposable backing LV with deterministic nonzero
   canaries, reallocate it, and never expose it directly.
2. Establish zero source, clone metadata, guarded frontend and queue restriction
   in a fail-closed order; then prove a complete frontend read is zero.
3. Test 4 KiB first writes at region start/middle/end and across boundaries;
   exact write data and zero neighbors are required.
4. Submit aligned, partial, full-region and whole-device BLKDISCARD directly to
   the frontend. Every request must be rejected without counter/data changes.
5. Repeat discard/zero paths through guest, QEMU, storage move, native restore
   and Veeam backup/restore.
6. Separately exercise native WRITE ZEROES, BLKZEROOUT, QEMU fallback ordinary
   zero writes, FLUSH/FUA and mixed I/O during background hydration.
7. Crash/reconstruct around every allocation, guard, table, write, progress,
   pivot and cleanup boundary. UNKNOWN never authorizes mutation or exposure.
8. Prove exact DM UUID/table/dependencies, LV UUID, signed anchor and stored
   geometry on every node.
9. After hydration, pivot atomically to linear and prove the source/metadata
   dependencies are gone and the destination is byte-exact.
10. Complete reboot, migration and rolling-kernel-update gates before release.

## Additional protocol boundaries captured for the future implementation

- Eager complete initialization remains supported and is the default. Lazy
  zeroing is a new explicit on-disk/runtime state, never an inferred property
  of an ordinary generation and never silently accepted by older code.
- During the lazy phase the RAW destination must be reachable only through the
  exact guarded clone frontend. Direct reads, exports, backup helpers and
  generic recovery access to the destination LV are forbidden until a durable
  MATERIALIZED transition and verified linear pivot have completed.
- Persistent clone metadata and its frontend have one cluster-wide runtime
  owner. A second node may not activate the lazy disk merely because the first
  userspace worker, service or PID is absent. Node/boot/transaction identity,
  exact kernel target identity and fencing evidence remain separate proofs.
- Snapshot, rollback, resize, migration, backup/restore and storage move must
  each declare one of two behaviors: operate safely through the guarded lazy
  frontend, or require verified materialization first. No callback may bypass
  that decision by opening the destination LV directly.
- `no_hydration` is the steady lazy state, not evidence of a stalled worker.
  A disk intentionally left lazy consumes no active-materialization slot and
  no no-progress watchdog budget. Once an explicit materialization begins, the
  normal single-worker, proven-progress, command-deadline and recovery rules
  apply.
- Availability of stock `dm-clone` and `dm-zero` targets must be proven on the
  exact supported PVE 9 kernel set. Absence or semantic drift disables lazy
  allocation and falls back to eager initialization; it never changes how an
  existing lazy object is interpreted.
- The future prototype must use only newly allocated disposable lab objects.
  It must not share metadata, destination, source mapper or admission state
  with the current endurance transaction.

These are design requirements only. No lazy-zero implementation or kernel
qualification exists in the current RC candidate.

Primary references:

- <https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html>
- <https://docs.kernel.org/admin-guide/device-mapper/zero.html>
- <https://docs.kernel.org/admin-guide/blockdev/queue-sysfs.html>
- <https://www.qemu.org/docs/master/system/qemu-manpage.html>
