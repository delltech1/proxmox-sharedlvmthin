# Lazy-zero L1--L4: disposable loop harness contract

Date: 2026-09-23. Design only: no kernel commands or experiments were run by
this review. This file does not authorize module loading or a live experiment.
Root leads implementation; this is the independent safety review input.

Verdict: **GO for implementing and reviewing an isolated harness; NO-GO for
kernel mutation until that implementation passes its own pre-live gate.**
Production lazy allocation, continuous online reload protection, ownership on
shared storage and release compatibility remain unqualified.

## Reuse boundaries

Read in full: `stage1-loop-poc.sh`, `hydration-io-fault-qualification.sh`, and
`lazy-zero-l0-inventory.sh` under `experiments/thick-generations/`.

- Reuse exact numeric table/dependency, NOFLUSH status, data comparison and
  persistent reopen assertions from the first two, not their lifecycle code.
- `stage1-loop-poc.sh` loads a module, uses PID-only names, performs best-effort
  name-only cleanup and prints PASS before cleanup. Its initially sparse zero
  destination cannot establish absence of recycled-data disclosure.
- `hydration-io-fault-qualification.sh` tracks successful creates only; an
  ambiguous create can escape tracking. Its retry/remove and backing-file
  deletion after failed cleanup are inappropriate here.
- L0 checks availability, not these semantics. Missing zero target requires a
  separately authorized module-load step and fresh L0. Neither this harness nor
  a failure trap implicitly loads modules. L0 output must be bound to this host,
  boot and exact kernel; the mutation harness independently rechecks targets.

Use a Bash orchestrator with narrowly bounded existing/stdlib helpers for exact
FD identity, direct-I/O data checks and ioctl errno. This is not a daemon or new
package. Do not stretch grep, command exit codes or shell PID variables into
proof of kernel object identity or command quiescence.

## Closed scope and ownership

The first run has fixed small geometry: 128 MiB data, 32 MiB metadata, 1 MiB
regions (2048 sectors). Different geometry is a separate recorded run. Only two
new loop-backed files are needed per fixture; source is a read-only dm-zero
target. No arguments accepting existing devices, VG/LV names or storage IDs.
No mount, filesystem creation, LVM, plugin calls, QEMU, hydration enable, pivot,
package install, reboot or network operation.

Create a fresh root-owned 0700 directory directly under the approved local
`/var/tmp/slt-lazy-loop-lab-*` namespace. Use a random run nonce, not a PID.
Reject symlink components, existing roots and a nonlocal/unapproved backing
filesystem. Set a clean environment, fixed PATH/locale and umask 077. Bound
tools, helper/script hashes, host/boot/kernel/L0 digest and explicit disposable
ACK in the run record. The ACK is not proof of object ownership.

Before each external mutation, persist its intent; capture its sole executor's
PID/start identity, exact argv, outcome and exact postcondition. Each DM role
gets a run/fixture-specific name and UUID. Record:

- backing regular file's pinned directory, device/inode, size and initial hash;
- loop devno, disk sequence/incarnation evidence where supported, exact backing
  device/inode, offset=0, size limit/size, read/write policy and holder set;
- DM UUID, numeric devno/incarnation, exact table, length, RO/suspended state,
  dependencies and expected role graph; separately bind the consumed block
  node/FD to that devno;
- publication permission, live probe identities and command state.

Record intent before create, not merely a `live=1` flag after success. A timeout
does not prove failure or terminal execution. An unaccounted command/helper,
D-state, unexpected identity or uncertain udev completion means UNKNOWN: stop
dispatch, preserve objects and evidence, do not retry or detach dependencies.
Bound waits and output capture. `timeout ... dmsetup` alone is not sufficient
proof that there is no surviving executor. Names are lookup keys, not ownership.

## Construction and publication order

1. Create exclusive regular data/metadata files. Write the entire destination
   with a deterministic nonzero pattern (for example repeated byte 0xA5), fsync,
   and verify every byte and exact length. Initialize the entire small metadata
   file once to zero and fsync. Record independent expected hashes. Failure or
   short write refuses before loop/DM creation. Do not use a sparse destination
   as the stale-data oracle.
2. Attach only these files to fresh loops. Prove backing identities and sizes.
   There must be no partition scan, unexpected holders or overlapping loop
   association. Verify the destination via its block interface too.
3. Create zero source with exact read-only table `0 262144 zero`; verify UUID,
   table, zero dependencies, length and kernel/node RO. Read it as a zero oracle.
4. Create the clone with exact numeric role devnos:
   `0 262144 clone META DEST ZERO 2048 2 no_hydration no_discard_passdown`.
   Verify exact table/UUID/dependencies, active writable frontend, geometry,
   metadata mode rw, hydrated=0 and hydrating=0 via `status --noflush`.
5. For a guarded fixture, resolve queue sysfs through the verified devno, prove
   it is this clone queue, set `discard_max_bytes` to 0 and read it back. Verify
   identity and table again. Only then issue the harness's publication token
   and allow its probes to open the frontend. A failed write/readback means no
   publication; never continue under QEMU configuration as substitute.

Publication here means permission for the cooperating harness probes. DM/udev
can already expose a device node to root; this is NOT a kernel ACL preventing
arbitrary external open, nor proof of safe production publication. No existing
plugin volume name or UUID is reused. The negative-control fixture never gets
normal publication permission; it has a separate explicit hazard-probe role.

## Data and status oracle rules

Use aligned direct-I/O full-device reads through verified block FDs, with an
independent expected byte pattern. Buffered pre-discard reads can populate cache
and mask a later change of mapping! Do not hash the attached backing regular
file as a substitute for coherent block-device reads. A small stdlib helper may
use an aligned mmap buffer with O_DIRECT/preadv; unsupported alignment/direct I/O
refuses this qualification instead of falling back silently. Record alignment,
sector sizes, exact bytes read and kernel/node identity.

Parse complete NOFLUSH status, checking target, geometry, mode, flags, hydrated
and in-flight counts; reject Fail, unknown fields/shape or unexpected progress.
Periodic clone metadata commits mean byte-identical metadata files are not a
valid live no-op oracle. Unchanged counts plus full logical data and clean reopen
are bounded evidence, not proof of every bitmap bit or metadata byte.

For discard, open a verified writable block FD (O_RDWR, kernel RO=0) and return
the exact BLKDISCARD errno. A generic nonzero exit, EINVAL, EPERM or EIO is not
PASS. Native write-zeroes, BLKZEROOUT and buffered ordinary-zero fallback are
different operations and remain L6 obligations.

## Ordered cases

| Case | Action and exact result |
| --- | --- |
| L1 zero reads | Guarded fresh fixture: full logical read equals all-zero oracle while full direct destination still equals nonzero canary. Hydrated=0, hydrating=0. |
| L2 negative control | Separate fresh metadata/destination/DM identities. Leave discard enabled intentionally. Discard one aligned full unhydrated region using BLKDISCARD, then direct-read it. Require successful ioctl, increased hydrated count, original nonzero destination canary visible and unchanged raw destination. This establishes the hazard despite no_discard_passdown; never reuse this contaminated fixture for L3. |
| L3 guarded discard | Guarded fixture: 512-byte/4 KiB partial-region, aligned whole-region, crossing/multiple-region and full-device ranges, all in-bounds with recorded logical-sector alignment. Each must return exact EOPNOTSUPP. Before/after every call: exact queue=0, same identity/table, hydrated/in-flight counts unchanged, full logical oracle unchanged and raw canary unchanged. |
| L4a suspend/resume | Stop/reap all probes, require zero opens, withdraw publication. Suspend and resume without table replacement. Record queue before/after. Restore and verify guard before allowing any new probe; rerun L3 oracle. |
| L4b exact reload | Unpublished, no probes/opens: suspend; load exact same clone table, prove full inactive table equality; resume; record queue before any reapplication. If reset, record it explicitly, restore guard and verify before probes. Do not label this continuous online protection. |
| L4c clean reopen | After L3, perform one independently specified small ordinary write+fsync; expected logical image is built from the original zero oracle plus that write, not read back as its own oracle. Stop probes, remove exact clone, prove absence, recreate using SAME metadata/data and read-only zero source, guard before probes. Full logical image and expected hydrated count must survive; rerun guarded discard. Never reinitialize metadata on reopen. |
| L4d guard failure | Inject failure/refused readback at the guard boundary using a narrow test hook, not a foreign sysfs path. Assert no publication token and no probe dispatch. A real invalid sysfs write is optional only on the exact owned queue. |
| L4e controller stop | Separate supervisor holds the controller at pre-guard/post-guard-pre-publication checkpoints, proves no probe/consumer dispatch, then kills/reaps only its exact child. No successor automatically republishes/recreates. Preserve owned kernel state for explicit reconciliation; no unattended exit cleanup. |

First run may execute L1--L3 and stopped L4a--c only. L4d should first pass mocked
behavioral tests; L4e needs its own reviewed supervisor before a physical run.
Do not silently mark all L4 covered when only clean stopped paths ran. Comparing
the queue on either side of resume cannot rule out a transient unguarded window.
If online reload is eventually required, it needs a different continuous I/O
qualification and possibly a different graph/publication mechanism.

## Cleanup is part of the result

Successful path: stop/reap exact probes and all command children; prove zero
opens/expected holders; remove exact owned clone once; prove UUID/devno absence;
remove zero source once after proving it has no holders; prove absence; detach
only loops whose complete backing/role identities still match. Prove the loop
association actually disappeared (detach can be lazy). Keep files/evidence by
default; any later file removal requires independently confirmed detach.

Never use remove-all, wildcard cleanup, `--retry`, force/deferred removal, detach
by stored path alone, or unconditional `rm -rf` in a trap. A foreign/recycled
identity is a refusal, not something to repair. Signals exit explicitly; traps
record failure and retain evidence, not automatically continue kernel mutation.
Any cleanup UNKNOWN prevents overall PASS and preserves dependent resources.
No linear pivot is a cleanup shortcut. PASS is emitted after terminal cleanup
evidence, not before an EXIT trap.

## Minimum behavioral tests before live approval

Mock kernel boundaries; never create real devices in the unit suite. Test:

1. Missing target, stale boot/L0, unsafe path, existing root and canary short
   write refuse before first loop/DM command; no implicit modprobe.
2. Failed/ambiguous create with a now-present object stays tracked as UNKNOWN;
   foreign UUID, recycled loop backing inode and wrong node devno never trigger
   remove/detach. Failed removal preserves loops and files.
3. Guard failure or wrong queue identity dispatches no probe. Guard reset on
   resume cannot publish until renewed verification; no metadata zeroing on
   reopen. Inactive table mismatch issues no resume.
4. Exact errno discrimination, writable FD requirement, direct-I/O short read,
   stale cached-read simulation, bad status/mode/geometry and unexpected progress
   all fail; a fixture with no nonzero canary cannot claim L1/L2 success.
5. Before/after logical expected image is independent of device output; L2
   fixture identities cannot be supplied as L3. Live metadata-byte drift alone
   is not treated as discard corruption or asserted absent.
6. Timeout/unreaped child, held pipe, signal and cleanup failure retain original
   failure evidence and cannot print overall PASS. L4e needs exact stopped-child
   PID/start/argv plus pidfd kill/reap and no replay/publication assertions.

Evidence records each case as PASS/FAIL/UNKNOWN/NOT_RUN, exact pre/post tables,
queue limits/status, bytes/hashes, errno, command identity and cleanup proof.
Use classifications such as LOOP_ZERO_READ_CONTROL_PASS,
LOOP_UNGUARDED_DISCARD_HAZARD_REPRODUCED,
LOOP_BLKDISCARD_GUARD_PASS and LOOP_STOPPED_REOPEN_GUARD_PASS. Every result keeps
`production_authorized=false`, `online_reload_qualified=false`,
`shared_owner_qualified=false` and `power_loss_qualified=false`.

This loop test cannot qualify physical capacity reservation, SAN durability,
guest/async discard paths, large-disk performance, cross-node exclusion, live
migration or the existing same-transaction duplicate-executor P0. Further inert
scaffolding is useful only insofar as it supports this small real semantic test;
do not turn the harness into a production admission service.

Primary source for the discard, region and persistence semantics:
[kernel dm-clone documentation](https://docs.kernel.org/admin-guide/device-mapper/dm-clone.html).
Exact PVE kernel behavior remains an experimental result, not a deduction from
the target's version string.

## Model-only guard epoch status

`lazy_zero_guard_model.py` is a pure model with no OS, sysfs, ioctl, process,
journal or storage backend. It closes a pre-live ordering gap: a bare observed
`discard_max_bytes=0` is never publication evidence. A guard receipt is bound
to the fixture, boot, configuration epoch, full graph digest, exact clone
devno/diskseq/table and the matching queue identity.

Construction, stopped reload, stopped resume and reopen each require a fresh
epoch guard. Reload/resume preserve the exact graph. Reopen preserves fixture,
boot, purpose, data/metadata loop identities and the zero source; only the
clone incarnation may change and it still requires a new guard. The negative
L2 fixture can never receive normal model publication permission.

The model validates the concrete table assembled from the four roles, fixed
geometry, writable backing loops, read-only zero source, reverse holder policy,
nonce-bound DM names/UUIDs and closed exact-typed schemas. Its maximum result is
`MODEL_GUARD_PUBLICATION_ORDER_VALID`, with all runtime, production,
shared-owner, power-loss and pivot authorizations false.

This holder/open-count policy is synthetic and is not a qualified kernel
snapshot. Equal metadata backing identity does not prove unchanged contents or
exclude reinitialization/rollback. These remain live P0 gates, not claims made
by the model.
