# TG Lazy Zero Stability Qualification

Status: local design and lab qualification plan; not a production claim.

This plan deliberately separates the current disposable device-mapper block
prototype from a future PVE-managed Lazy volume lifecycle. A passing raw block
test is not evidence for VM migration, HA, backup, restore, snapshot or resize.

## Common admission and evidence

Before every physical test, the previous transaction must have a terminal
classification and exact cleanup, or remain retained for reconciliation. There
must be no unknown executor, D-state descendant, claim, hold or intent. Quorum,
multipath, VG/PV/WWID identity and capacity reserve must be healthy.

Every run uses new disposable objects and records the commit, executable
hashes, package/kernel versions, boot IDs, transaction and object identities,
geometry, deadlines, commands, exit status, status timeline, writer journal,
oracles and cleanup audit. Fault injection requires a separately reviewed
procedure. Evidence needed after a fault must be copied off the affected host.

The data oracle is generated independently from a sealed plan using seed,
member UUID, block index and generation. A durable ledger identifies writes
confirmed after flush/fsync. The full range, region boundaries and final sector
must be checked. A metadata hash does not replace a data oracle.

Classifications:

- PASS: every declared invariant, data oracle and terminal cleanup passes.
- FAIL: canary leakage, confirmed-data loss, corruption, dual ownership, wrong
  object mutation or false publication is observed.
- UNKNOWN-retain: executor/outcome/identity/evidence cannot be proved.

FAIL and UNKNOWN stop the dependent matrix. They do not permit blind retry,
force cleanup, a second worker or reinterpretation as SAFE.

## A. Current block prototype

### A1 — 100-GiB scale happy path

Require full poisoned-raw zero-visibility proof, concurrent foreground writes
with hydration, 100 percent hydrated with zero inflight regions, metadata `rw`,
full expected/frontend/raw/pivot SHA equality, exact cleanup and three-node
fixture absence. Process success or hydration completion alone is insufficient.

### A2 — 500-GiB scale happy path

Run serially, without VM, migration or faults. Use an explicit budget and prove
whole-capacity zero reads before writes, partial writes, cross-region writes,
last-region and last-4-KiB writes, raw canary isolation, full oracle equality,
stopped linear pivot, and metadata used/total/mode at the beginning, during the
run and before pivot. This qualifies block size behavior, not guest format or
PVE move behavior.

### A3 — dispersed writes, flush and reopen

On a new 8-GiB fixture with background hydration disabled, perform sealed,
dispersed and repeated-region writes with fsync after each committed step.
Cleanly close and reopen the original metadata, verify the full logical oracle,
then hydrate, pivot and verify raw SHA. Record peak metadata use and every mode
change. Do not manufacture an ENOSPC test by shrinking metadata.

This must be one continuous shared-LVM qualification; results from the loop
reopen, shared happy path and controlled-reboot tests cannot be combined into
an A3 PASS. Implement it as a separate opt-in mode without changing default or
fault branches. Its sealed plan must include two different last-write-wins
overwrites of an already hydrated region. With hydration still disabled, the
terminal writer journal, zero in-flight count, writable metadata and exact
hydrated-region union derived from that plan are required before reopen.

At zero opens, remove only the exact clone mapper once, prove absence and
create it once over unchanged data, metadata, delay and zero identities with
`no_hydration`. Record the old and new clone incarnations, reapply the discard
guard before the first read and prove the exact table, dependencies, persisted
region count, zero in-flight count, writable metadata and complete frontend
oracle. Only then enable hydration and reuse the qualified completion, full
frontend/raw comparison, stopped linear pivot, post-pivot comparison and exact
cleanup path. Premature hydration, an unexpected region count, backing identity
drift, remove/create ambiguity or guard failure is UNKNOWN-retain; it never
permits a second create or cleanup retry. Metadata samples establish only the
maximum observed use, not a continuously proven peak.

### A4 — discard and write-zeroes

Test full- and partial-region discard plus write-zeroes/unmap before and after
reload/reopen over poisoned raw regions. Unsupported operations must refuse
before effect, or return qualified logical zeros without canary leakage. Never
remove the guard merely to make a tool continue. This does not yet qualify the
QEMU/guest path.

### A5 — controlled crash and reboot

Use one new fixture per boundary: the existing controller-SIGKILL after a
fsynced partial state; qualified graceful reboot/reopen; reviewed active-
hydration fault; and pivot faults while suspended before load, after an exact
inactive linear table before resume, and after resume before final publication.
The latter boundaries require a reviewed bounded harness, not random sleeps or
manual kills. Confirmed writes must survive and no poisoned bytes may leak.
Graceful reboot and controller SIGKILL are not power-loss qualification.

### A6 — dependency and I/O faults

First use an isolated disposable graph and one fault at a time: source-read,
destination-write and metadata I/O error, then temporary delay/recovery. Never
publish hydrated/materialized/PASS after an ambiguous effect. Budget expiry or
indefinite kernel retry is UNKNOWN-retain, not authority to start another
worker. Test one physical path loss only when the other path is healthy; test
all-path loss only on an explicitly isolated and authorized LUN.

### A7 — multi-node refusal

With A owning a small lazy graph, B attempts activation. Repeat with A stopped
before dispatch, A in an unclosed operation, a persistent peer hold, an
unavailable peer and changed peer boot identity. B must have no mutating effect.
Timeout, a new lock or mapper absence cannot prove the original writer dead.

### A8 — same-VG four-member batch prerequisite

Do not bypass the single OPEN VG intent. A 4x100-GiB same-VG test requires one
sealed batch plan and hash, one group authority and four exact members/workers.
Admission reserves all data, metadata, extent rounding and policy headroom
before the first create. A partial create/remove sequence remains explicitly
owned and retained; cleanup may clear the group intent only after all exact
members are proved absent. Concurrent PASS requires a measured common interval
in which all four workers make fresh progress. One member failure prevents
group PASS and intent release.

## B. Tests gated on genuine PVE Lazy lifecycle integration

Directly attaching a manually created mapper to QEMU does not satisfy this
section. The plugin must own creation, activation, recovery, feature gates and
cleanup, and must prevent old code from interpreting Lazy as materialized.

### B1 — one small VM disk

Prove allocation/reservation, guarded frontend-only QEMU access, guest partial,
cross-region and end writes, flush/fsync, discard/fstrim/write-zeroes, stop/start,
supported reboot recovery and second-owner refusal. Use a guest fsync journal
and deterministic file/block corpus; hash a quiescent image.

### B2 — operations on a Lazy disk

Backup, snapshot, rollback, grow and move must either refuse before effect or
materialize first. They must never read/export the raw destination directly.
Restore goes to a new disposable disk and verifies the corpus. Snapshot/change/
rollback must restore the exact snapshot oracle. Grow preserves the old range
and exposes a zero new range; unsupported shrink refuses before effect.

### B3 — one 100-GiB VM migration gate

Qualify materialize-first offline move before online move. Verify content,
config, source and destination ownership, and delete the source only after the
destination result is proved. This does not qualify migration of live Lazy
metadata between two kernels.

### B4 — four 100-GiB VMs

After B1-B3 and the reviewed batch admission: four concurrent workloads with
no fault; two parallel offline moves; four parallel offline moves; two online
moves; then four online moves. Every disk has distinct identity, transaction
and oracle. Record capacity, worker slots, progress and metadata. A dormant Lazy
disk must not consume a hydration worker slot or be misclassified as stalled.

### B5 — one 500-GiB VM

Create through the plugin, format while observing discard behavior, write a
sequential corpus plus random/boundary records, fsync and seal its manifest,
then qualify offline move and only later online move. A mount, filesystem check
or successful PVE task is not a content proof.

### B6 — Lazy, Eager and Thin transitions

Start with Lazy -> fully materialized Eager -> Thin copy/move, then Thin ->
Eager. Eager/Thin -> a new Lazy target requires a qualified import through the
frontend. Never reset a bitmap over a used destination with a zero source,
because that hides valid data. Retain the source through the commit point and
verify the complete logical oracle after every transition.

### B7 — HA/failover

Qualify one VM and one declared failure phase first. Only then inject one fault
while three other VMs act as controls. Successful takeover requires proof that
the old writer cannot continue, exactly one new owner and preserved committed
data. SSH failure, timeout, lock acquisition or an absent mapper is not fencing.
Without the required ownership/fencing proof, the only safe expected result is
fail-closed refusal.

## Required reruns after a fix

1. Add and pass the exact negative reproducer.
2. Pass affected unit and contract tests.
3. Pass the full Python and Perl regressions.
4. Use a new fixture to repeat the failed physical gate and its nearest happy
   path.
5. If identity, ownership, discard, durability or pivot logic changed, repeat
   the relevant smaller crash/reopen regression.

The original failure and evidence remain part of the record; a later PASS does
not replace or erase them.
