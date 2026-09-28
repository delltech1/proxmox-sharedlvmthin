# TG35 PVE data-plane qualification gate

Status: package/API14 lane passed; SAN data-plane not executed for TG35.

This gate qualifies the exact DUAL and Thick-only artifacts on disposable PVE
objects.  It deliberately excludes Veeam and repeated long-duration hydration
endurance.  A TG32/TG33 result is baseline evidence only and must not be called
a TG35 pass.

The exact `0.9.0~rc5.14~tg35` DUAL and Thick-only packages passed native
source tests, package/privacy validation, profile parity, reproducibility and
the clean API14 install/profile-switch/reboot lane.  Those results do not
qualify the SAN data plane.  The current three-node SAN lab remains on TG33
ZERO2 with legacy same-VG Thin+Thick definitions and cannot be partially
upgraded or hidden from the candidate preinstall gate.

Run this matrix only on an isolated disposable PVE cluster with its own
pmxcfs/corosync identity and exclusively assigned test LUNs/VGs.  Merely adding
another VG to the current cluster, upgrading one SAN node, bypassing preinst,
or loading TG35 helpers over TG33 is not an exact TG35 qualification.

## Preconditions

- Every participating node runs the exact candidate artifact and reports the
  expected package hash, plugin hash, Storage API/APIVER and APIAGE.
- The all-node layout migration/maintenance protocol is complete.  No legacy
  mixed layout, package fence, maintenance hold, unknown worker or recovery
  receipt is open.
- Only an allow-listed disposable VMID range, test storage IDs and test LVs may
  be mutated.  Existing non-test VMs are excluded even if powered off.
- Record storage configuration, VG/LV/DM identities, multipath state, quorum,
  node boot IDs and test-object hashes before the first mutation.
- Each disruptive action has a separately verified recovery command and a
  stop condition.  Never stack a second fault on an UNKNOWN result.

## Workload set

Use 18 small disposable VMs: six Thin, six Thick Eager and six Thick Lazy.
Distribute two of each mode per node.  Give every VM a small boot disk and a
second data disk carrying deterministic canaries plus a continuously updated
sequence/hash journal.  Add four separate 100 GiB data disks and one 500 GiB
data disk for capacity and transfer-boundary tests; these need not be boot
volumes.  Permit at most two copy operations concurrently; larger waves test
bounded queueing and admission rather than bypassing those limits.

## Required test cells

1. Baseline lifecycle: create, start, stop, delete, full clone, linked operation
   where supported, snapshot, rollback, backup/restore through native PVE,
   grow resize and explicit refusal of unsupported reduction.  Current
   unmaterialized Lazy snapshot/rollback/resize hooks are expected to refuse
   with zero mutation; that is a PASS, not a missing test.
2. Mode/storage moves: offline and online moves for Thin to Eager, Thin to
   Lazy, Eager to Thin, Lazy to Thin, Eager to Lazy and Lazy to Eager.  Direct
   Thin live migration is not supported: its online test must use the explicit
   Thin to Eager bridge, PVE live migration and creation of the new Thin
   destination.  Unmaterialized Lazy online movement must either refuse with
   zero mutation or first follow the explicit materialization contract.  Test
   both a quiet guest and a guest writing its journal.  Destination identity
   and source cleanup must be exact; ambiguous cleanup is failure.
3. Compute-only migration: migrate every running VM around all three nodes
   while its shared disk remains on the same storage.  Prove one active writer,
   continuous journal sequence and no unexpected frontend on the old node.
4. Burst migration: submit six independent migrations as one wave, then a
   bounded twelve-VM wave, while allowing at most two copy operations to run.
   Queueing is acceptable; bypassing the canonical lock, duplicate workers,
   overlapping ownership or an unbounded retry is not.
5. Mass evacuation: evacuate 6–8 running disposable VMs from one node to the
   other two, first with shared storage unchanged and then with a balanced mix
   of storage moves.  Repeat from each node.  Record task ordering and elapsed
   time; safety must not depend on a fixed 30-second completion assumption.
6. Scale transfers: move four 100 GiB disks in parallel and the 500 GiB disk
   alone.  Slow completion is not failure.  Lack of proven progress, deadline
   ambiguity or blocked I/O must transition to the documented fail-closed
   state without starting a second worker.
7. Mutation faults: terminate the initiating PVE task before dispatch, during
   copy/materialization, after destination publication and during source
   cleanup.  Each phase must either resume idempotently from exact evidence or
   remain blocked for explicit recovery; it must never guess success.
8. Host faults: reboot the source and destination in separately selected test
   cells.  Simulate a single SAN path loss first; total path loss is allowed
   only on disposable objects with an out-of-band recovery route.  A second
   node must not activate or mutate while ownership/outcome is UNKNOWN.
9. Capacity boundaries: test reserve threshold minus/at/plus one extent,
   exhausted destination capacity and Thin metadata/data warning boundaries.
   Admission must refuse before destructive source changes.
10. Final recovery/cleanup: complete or explicitly recover every transaction,
    materialize and verify Lazy objects chosen for pivot, prove exact linear
    pivot where expected, remove all disposable objects, and compare final
    VG/LV/DM/multipath inventory with the recorded baseline.

## Pass invariants

- Guest journal has no missing acknowledged sequence and every retained test
  disk matches its expected content/hash.
- Whole-disk hashes are compared only with the workload stopped or consistently
  frozen; the externally retained fsync-confirmed journal prefix is the crash
  oracle for a running workload.
- At most one writable frontend/owner exists for each volume at every sampled
  transition; absence checks alone never manufacture ownership.
- No duplicate worker, blind retry, leaked reservation, orphan mapper, partial
  LV, stale PVE config or unexplained source/destination pair remains.
- Every ambiguous interruption is classified UNKNOWN/RECOVERY_REQUIRED and
  blocks conflicting mutation until exact recovery evidence closes it.
- Package and plugin hashes stay unchanged during a run; cluster remains
  quorate except in an explicitly isolated quorum test.

## Automation boundary

Creation, journal/hash checking, ordinary migration waves, task collection and
read-only inventory comparison may be automated.  Host reboot and path loss
must be armed per test with exact node/path identity.  Total path loss, quorum
loss, forced task termination after publication and recovery-finalize remain
one-at-a-time supervised operations.  Automation must stop on the first
UNKNOWN, invariant violation or unexpected object identity.
