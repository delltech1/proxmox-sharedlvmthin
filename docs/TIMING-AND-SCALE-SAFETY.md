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

Snapshot materialization uses a dm-clone progress watchdog.
`slt-tg-hydration-timeout` is a **no-progress** interval, not a total copy
deadline. Each verified increase of the hydrated-region counter renews the
window. Event waits use bounded slices to permit revalidation. Completion
requires exact geometry, a fully hydrated counter and zero active hydration
requests. Counter regression, geometry change, immediate wait failure or a
full interval without progress is recovery-required and fails closed.
The interval is measured with a monotonic clock, so NTP corrections, manual
wall-clock changes and daylight-saving transitions cannot shorten or extend
the watchdog window.

The asynchronous worker has `TimeoutStartSec=infinity`; systemd does not kill
a legitimate long hydration. Restart/resume reconstructs only the exact
persisted transaction and verifies all dependencies before continuing.
Admission waits never clear or steal an existing transition.

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

## Idempotent automation

Automation may skip a start or stop only after re-reading that the requested
state already exists. It waits for the native PVE task rather than killing the
client at an arbitrary deadline. After any interrupted or uncertain mutation,
stop automation and inspect the persistent state; never blindly issue the same
mutation again. Recovery commands are deliberately explicit and accept only
one exact signed topology.
