# BASTRIX SharedLVM 0.9.0~rc5.10~tg31 development notes

TG31 is an unreleased development candidate for disposable-lab qualification.
It is not yet the recommended public package.

## Recovery hardening

- Bridge state schema v3 commits a root-owned mode-0600 disk manifest and its
  SHA-256 digest before the first storage move.
- A pure planner classifies only evidence-complete recovery states and refuses
  foreign admission, missing disks, mixed unsupported topology, and impossible
  phase/owner combinations.
- A bounded read-only QMP probe correlates every configured disk with QEMU's
  live canonical `/dev` path. pmxcfs and process arguments are never treated
  as sufficient runtime proof.
- Resume actions cover materialization, native Thick live migration, return to
  Thin, and exact finalization. Long copies publish progress without imposing
  a fixed timeout on large healthy disks.
- Remote probes use stdin-null SSH sessions so a command executed inside a
  manifest loop cannot consume later disk records. The sole streaming SSH
  session is the explicit QMP expectation pipeline.
- Migration admission waiting is site-configurable, bounded, observable, and
  uses desynchronized backoff instead of a fixed 15-minute busy-poll window.

## Qualification completed

An online Thin-to-Thick mirror was deliberately terminated. QMP proved that
the guest still wrote Thin while pmxcfs named Thick. Automatic recovery was
blocked, both copies were preserved, and orphan cleanup refused while the
destination was open or referenced. After stopped-guest reconciliation, the
same transaction resumed through materialization, live migration, and return
to Thin. Final QMP/config topology matched, admission was clear, and no Thick
transaction LV remained.

A two-disk VM was then interrupted during the second Thin-to-Thick mirror.
The first slot remained Thick, the second remained Thin, state became
`MOVE_FAILED`, and the planner selected `CONTINUE_MATERIALIZE`. Recovery copied
only the remaining slot, completed the native live migration, and returned
both slots to Thin. This test also exposed and fixed SSH consuming the second
manifest line during remote return recovery.

The current source passes 197 Python tests and 694 Perl assertions. Broader
multi-disk, repeated crash-point, large-disk, and concurrent recovery
qualification remains required before a public release.

A real VG admission race with 50 concurrent contenders was also qualified.
While an exact holder existed, 0/50 contenders acquired admission. After its
exact release, a simultaneous 50-way race produced exactly one winner; the
other 49 failed closed against the winner's durable transaction tag. Exact
winner release restored an empty admission state.

An additional disposable control-plane gate created an 8-TiB Thin disk,
attached it to an exact stopped VM configuration, resized it by 1 GiB and
removed the VM, disk and per-VM pool through the normal PVE lifecycle. VG free
space returned exactly to baseline. This validates large-size arithmetic and
lifecycle wiring only; it is not an 8-TiB data-movement qualification.

Live qualification also exposed an autogrow defect: dmeventd observed the
active hidden `-tpool` target while `lvs` reported the public pool LV inactive.
The monitor previously trusted only the public LV activity attribute and
refused a valid event. TG31 now reuses the plugin's exact public/hidden mapper
inventory; no exact mapper still fails closed, while the authoritative hidden
target permits the cluster-locked, identity-checked growth path.

The live reproducer started with a 1-GiB pool at Data%=100. The corrected
monitor extended only that exact pool to 2 GiB, reducing Data% to 50.00. The
postcondition was a fully healthy recovery check over 150 owned pools with
`SAFE_FOR_MUTATION=YES`; all three nodes had zero D-state tasks and clean
package verification.

A 50-way live stale-event storm then exposed the monitor's separate fixed
30-second lock wait: 42 workers completed and 8 failed safely with lock request
timeouts; no pool size changed. The monitor now uses the same validated
`slt-lock-timeout` configured for the storage and revalidates that policy after
acquiring the lock. Repeating the identical 16-way-concurrency workload with
the configured 600-second policy completed 50/50 events, produced 50/50
stale/coalesced decisions, changed zero pool sizes, and left recovery healthy
with zero D-state tasks.

## Fifty-VM fenced Thin failover

Fifty small disposable Thin VMs were HA-managed on one owner when its complete
virtual PVE host was externally hard-powered off. The two survivors retained
quorum, PVE HA moved all resources through `fence` and recovery, and all 50
reached `started`. A read-only exact audit proved matching owner/epoch tags and
one thin-pool mapper on exactly the assigned node for 50/50 VMs. After the old
owner rejoined, the same three-node audit again passed 50/50, proving that the
rebooted former owner did not autoactivate a duplicate mapper.

An attempted planned HA live migration before the destructive test was
refused safely: during PVE's `migrate` state the source remained online and was
still the authoritative service node. That is not fencing evidence and must
not authorize a second dm-thin activation. TG31 therefore qualifies native
PVE fenced restart, not direct shared-Thin live migration.

A fast full-allocation workload also demonstrated that a 1-GiB elastic pool
can cross from the first dmeventd threshold to kernel `out_of_data_space`
before its serialized grow completes. Ordinary plugin mutations continue to
classify that state as recovery-required. The narrowly scoped monitor may now
classify only the exact `D` capacity flag as recoverable, after proving quorum,
storage identity, pool ownership and exact mapper topology. It still applies
the protected VG reserve before issuing one `lvextend`; metadata read-only,
check-needed, foreign, ambiguous and reserve-conflict states remain blocked and
no repair/reset action exists.

A native 8-GiB import exposed a separate admission race: `lvs --readonly`
omitted Data% for the active hidden thin-pool mapper, so allocation did not
pre-grow and the copy briefly reached `out_of_data`. TG31 now proves the exact
hidden mapper before issuing a normal device-scoped status read, and sizes the
complete admitted burst below 94%. Repeating the same import completed with
the pool at 80%, no low-water/out-of-data kernel event, and byte-identical
source/destination SHA-256.

The same two-disk VM then entered the materialized migration bridge. During
the non-zero 8-GiB Thin-to-Thick mirror, one exact iSCSI session was logged
out. Multipath retained one healthy path, durable bridge progress continued,
and the exact session was restored to 2/2 before completion. This qualifies a
bounded virtual-iSCSI one-path transition; it is not physical-FC evidence.
