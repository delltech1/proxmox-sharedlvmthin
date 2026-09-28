# Timing and scale safety

This document describes the timing model of the experimental Thin and Thick
Generations modes. A timeout is never fencing, never proof that a remote
mutation did not happen, and never permission to retry or delete state.

## Safety rules

1. A bounded control-plane observation either returns exact evidence or the
   operation fails closed as `UNKNOWN`.
2. An ambiguous mutating command is not automatically repeated. Persistent
   intent and signed objects are preserved for exact inspection or recovery.
3. Long data-plane work has no arbitrary total wall-clock deadline. It may run
   for as long as verified progress continues.
4. Cleanup requires exact identity, ownership, topology and postcondition
   evidence. Age, elapsed time and a missing client process are not evidence.
5. PVE HA fencing remains external. SSH reachability, a lease and a timer do
   not prove that a previous kernel can no longer write.

## Thin: peer audit timing

`slt-thin-peer-connect-timeout` bounds SSH connection establishment for one
peer (default 5 seconds, allowed 1..120). `slt-thin-peer-probe-timeout` bounds
the complete evidence command for one peer (default 15 seconds, allowed
2..600) and must be greater than the connection timeout.

Every guarded activation performs two peer audits: the admission audit and the
commit-barrier audit immediately before local activation. Peers are checked
sequentially while the canonical VG lock is held. Therefore a conservative
upper observation bound is approximately:

    2 * configured peer count * slt-thin-peer-probe-timeout

Normal positive evidence should complete far below that bound. A timeout,
malformed output or unreachable peer refuses activation. It does not mark the
peer fenced. Many simultaneous VM starts serialize on the canonical VG lock;
increase the lock acquisition budget from measured loaded-cluster latency,
not from disk size, and do not disable either audit to improve throughput.

Both aliases over the same VG must use identical peer timing. This prevents
the Thin and Thick views of one failure domain from applying different safety
policy after configuration changes.

Runtime-guard PREPARE uses the same two parameters for its independent peer
proof. Its outer helper/client bounds are deliberately longer than two maximum
peer probes in the qualified three-node topology. Expiry is still a refusal;
it never arms the runtime guard or authorizes activation. Sites with more than
three configured PVE nodes must qualify this envelope explicitly before using
the experimental runtime-guard mode.

## Thick: large disks and slow storage

Thick allocation creates and signs the exact objects under the VG lock, then
performs full zero initialization outside that lock. A 100-GB or multi-TB
device can therefore take as long as the backing storage requires without
blocking unrelated metadata work for the duration. Publication occurs only
after a flush, deactivation, exact object re-read and persistent intent check.
An interruption preserves the `OPEN ALLOC` intent and prepared objects; it
does not retry allocation or guess whether zeroing completed.

Full-range initialization and unpublished resize-tail initialization are data
plane operations. `blkdiscard --zeroout`, the direct-write `dd` fallback and
`blockdev --flushbufs` are intentionally not wrapped in
`slt-tg-command-deadline-sec`. That setting bounds short control-plane commands;
it is not a maximum disk-copy duration. A timeout can end a userspace wait
without proving that kernel I/O stopped. In particular, a process or descendant
in uninterruptible `D` state may outlive signals until the storage path returns.
Starting a replacement writer from that ambiguous state could overlap the
original I/O and is forbidden.

The zeroout fallback is sequential, not speculative: the complete direct-write
fallback starts only after the synchronous BLKZEROOUT process has returned an
error. It rewrites the complete range because a failed zeroout may have changed
an unknown prefix. Allocation and snapshot publication remain after zero and
flush. Online resize exposes only the old frontend size until its complete new
tail has been zeroed and flushed; explicit recovery may therefore rewrite that
entire still-unpublished tail after proving that no previous worker remains.
Slow completion is allowed. Ambiguous termination is `RECOVERY_REQUIRED`, not
permission to retry, publish, clear intent or start a second worker.

Snapshot materialization uses a dm-clone progress watchdog.
`slt-tg-hydration-timeout` is a **no-progress** interval, not a total copy
deadline. Each verified increase of the hydrated-region counter renews the
window. It retains its existing property name for configuration compatibility;
it is not a userspace command timeout. A DM event-number change merely requests
an immediate re-read, and a nonzero active-hydration count is diagnostic only.
Neither resets the watchdog. Event waits use slices bounded by both the
remaining no-progress interval and the independent command deadline to permit
revalidation. Completion
requires exact geometry, a fully hydrated counter and zero active hydration
requests. Counter regression, geometry change, immediate wait failure or a
full interval without progress is recovery-required and fails closed.
The interval is measured with a monotonic clock, so NTP corrections, manual
wall-clock changes and daylight-saving transitions cannot shorten or extend
the watchdog window. An immediate failed event wait is an error. An immediate
successful wakeup with no verified counter progress is treated as a stale or
racing observation and receives a bounded 250-ms yield before status is read
again, preventing a slow or overloaded target from causing a CPU busy loop.
No storage command is retried by that yield.

Region geometry is deterministic, never performance-measured at runtime.
`slt-tg-region-size-kib` accepts power-of-two minimum candidates from 64 KiB
through 4 MiB for new Thick transitions; only extreme capacities grow the
region further to retain the global bound. The current candidate is 1 MiB.
The signed anchor region is authoritative for recovery, so a legacy 4/8 KiB
transition is never reinterpreted using a newer default. With no explicit
threshold/batch override, new geometry targets roughly 1 MiB copy requests and
4 MiB concurrently hydrating data; legacy geometry keeps historical 32/32.

On a full no-progress expiry the worker sends `disable_hydration` only to the
exact transaction mapper, reads kernel status once and requires an explicit
`no_hydration` feature before reporting that further background scheduling is
disabled. It preserves the anchor, intent, metadata, source and destination
for explicit recovery. It does not claim that an already in-flight failed
region was cancelled; such I/O may remain in the kernel until the path
recovers. A failed command or missing confirmation becomes an unknown
background state; the message is never retried because a client failure does
not prove that the kernel rejected it.

The dm-clone status and event observations, hydration enable/disable messages,
and both flush-capable snapshot table-boundary suspends have an independent
userspace deadline configured by
`slt-tg-command-deadline-sec` (default 30 seconds, range 5--600). The default
is a bounded control-plane responsiveness value, not a claim about acceptable
storage latency and not a safety boundary. Operators may raise it from measured
control-plane latency without changing the independently qualified no-progress
interval. Expiry means the command outcome is unknown; it
does not confirm a state transition and does not authorize a retry. This is a
control-plane limit, not a kernel fencing primitive: a process blocked in
uninterruptible `D` state can outlive `SIGKILL` and the deadline until its
underlying I/O returns. Health therefore reports an exactly matched scheduled
or resume worker in that state as `BLOCKED_DSTATE`, keeps the transition
recovery-required and forbids starting a second worker.

The suspend deadline does not relax the flush requirement. Both
linear-to-clone publication and the completed clone-to-linear pivot invoke
ordinary `dmsetup suspend` without `--noflush`; the inactive table is published
only after the frontend is positively observed suspended. If underlying I/O
enters uninterruptible `D` state, the deadline cannot make that kernel I/O
complete or safely start a replacement worker. Persistent anchor/intent state
therefore remains authoritative until storage recovery or external fencing.
Every Thick suspend-state observation uses the same deadline, including
unpublished-prepare recovery and online-resize recovery. A suspend command
return is not treated as proof by itself; the separately bounded `dmsetup info`
must report exactly one `Suspended` state before publication continues.

Observed hydration rate, estimated completion time and historical maximum
progress gaps may be exported as telemetry in the future. They must not turn an
unknown or stalled transition into a safe one, and the implementation does not
use an adaptive rate estimate for safety decisions.

The kernel may serve a read of an unhydrated region from the immutable source.
Linux documents that repeated reads can therefore inherit source latency, and
that background hydration I/O failures may continue retrying inside dm-clone
until the underlying I/O succeeds. Confirmed `no_hydration` stops background
copying, but foreground guest access to an unhydrated region can still require
the failed source or destination and can stall. The plugin does not cancel,
repair or reinterpret that kernel I/O. Use synchronous online
materialization when post-snapshot guest latency must not depend on the source,
and qualify source/destination path loss separately from ordinary slow-but-
progressing hydration.

Health JSON preserves `worker_state=FAILED` and `BLOCKED_DSTATE` and reports
them separately from an absent or unobservable worker. All remain
`RECOVERY_REQUIRED`. A failed exact unit directs the operator to retain its
journal together with the signed anchor/intent evidence before explicit
resume. A D-state worker must not be duplicated; path recovery or external
host fencing remains authoritative. Monitoring never clears the unit or starts
recovery automatically.

The asynchronous worker has `TimeoutStartSec=infinity`; systemd does not kill
a legitimate long hydration. Restart/resume reconstructs only the exact
persisted transaction and verifies all dependencies before continuing.
Admission waits never clear or steal an existing transition.

`slt-tg-max-active-materializations` bounds aggregate dm-clone pressure across
the entire VG (default 4, range 1..64). Admission counts signed anchors in a
transition phase while holding the canonical VG lock. At the ceiling a new
snapshot or rollback fails before its intent or LVs are created; running
guests and existing materialization workers are not interrupted. Choose the
limit from measured SAN throughput and latency rather than VM count alone.
Concurrency means independent transitions with exactly one userspace owner
each; it never means multiple owners or competing pivot/cleanup workers for one
transaction. Qualification must also rule out starvation by recording each
transition's proven-progress gaps and foreground tail latency, not only total
aggregate throughput.

Health JSON exposes the backward-compatible boolean `available` together with
the machine-readable `state` (`AVAILABLE`, `SATURATED`, or `UNKNOWN`) and
`available_slots`. A limit reduced below the number of existing transitions is
reported as `SATURATED` with zero, never a negative slot count; it does not
interrupt existing workers. Invalid policy is `UNKNOWN` and fails health
closed rather than inventing capacity.

Thick deactivation uses `slt-tg-close-timeout` (default 30 seconds, allowed
1..300) only to observe the exact frontend open count reaching zero. This
absorbs qmeventd/host-load close latency without assuming closure. Expiry
refuses mapper removal and leaves the verified frontend and dependencies
intact. It never forces a busy mapper closed.

## VM count and operational sizing

VM count affects queueing, not the evidence required for a single operation.
Disk size affects full zeroing, copying and hydration duration, not Thin peer
proof. Before choosing timeouts, measure under the intended worst useful load:

- SSH connection and complete evidence latency to every peer;
- canonical VG-lock queue time during a start/stop storm;
- LVM metadata command latency with the expected LV count;
- the longest legitimate interval between hydration progress increments;
- multipath recovery latency and storage-controller congestion.

Use values above the observed loaded maximum with operational margin. A larger
timeout changes availability and diagnosis latency only; it does not add
fencing. Roll out one node at a time and verify quorum, active PVE tasks,
mapper uniqueness, persistent intents and failed systemd units between nodes.

Package preflight may use `sharedlvmthin upgrade-check --fail-fast`. The first
storage that is not positively `HEALTHY` and `SAFE_FOR_MUTATION=YES` already
proves that package mutation is forbidden, so continuing through hundreds of
additional bounded PVE probes cannot make that transaction safe. Fail-fast
therefore reports `UPGRADE_SCAN_COMPLETE=NO` and exits unsafe after that first
failure. It is a refusal-latency optimization, not a weaker success path: a
successful run still checks every in-scope storage and reports
`UPGRADE_SCAN_COMPLETE=YES`. The default diagnostic command, post-install gate
and post-reboot gate retain a complete scan so their success evidence covers
the full node.

## Idempotent automation

Automation may skip a start or stop only after re-reading that the requested
state already exists. It waits for the native PVE task rather than killing the
client at an arbitrary deadline. After any interrupted or uncertain mutation,
stop automation and inspect the persistent state; never blindly issue the same
mutation again. Recovery commands are deliberately explicit and accept only
one exact signed topology.
