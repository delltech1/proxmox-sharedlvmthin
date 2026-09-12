# Thin and Thick Generations redundancy gate

This matrix defines the destructive laboratory fault qualification required
before Thick Generations can leave the experimental branch. Every disruptive
case uses disposable data or an explicitly verified canary. Path return alone
never proves that mutation is safe: identity, device-mapper state, bounded LVM
probes, PVE storage health, quorum and data integrity must all be revalidated.

| Fault domain | Thin mode | Thick Generations | Required postcondition | State |
| --- | --- | --- | --- | --- |
| One cluster node stopped and rejoined | Existing guest and lifecycle operations | Linear HEAD and lifecycle operations | Survivor quorum, no duplicate owner, exact rejoin identity | PASS |
| Worker node lost during snapshot hydration | Partial native snapshot inventory is detected and recovered explicitly | Persistent anchor permits exact cross-node resume | Fencing before restart, no speculative cleanup, exact data canaries | PASS BOTH MODES, BOUNDED LAB |
| Quorum reduced from three nodes to two | Mutation follows native PVE quorum | Mutation follows native PVE quorum | No plugin-specific watchdog assumption | PASS |
| Quorum unavailable | Every new mutation fails closed | Every new mutation and resume fails closed | No LV, tag, mapper or config delta | PASS |
| QDevice unavailable with all three nodes online | Native quorum remains authoritative | Native quorum remains authoritative | Operations neither invent nor override votes | PASS BOTH MODES, BOUNDED LAB |
| One iSCSI path lost and returned | Active guest I/O and snapshot lifecycle | Linear and hydrating guest I/O | 2-to-1-to-2, bounded I/O, identity and hashes preserved | PASS BOTH MODES |
| All iSCSI paths lost and returned | Disposable active thin pool and guest I/O | Linear HEAD and active hydration | Bounded policy outcome; recovery gate remains closed on surviving D-state or non-materialized anchors | THIN FAIL_HOST_DM_THIN; THICK LINEAR PASS; HYDRATING FAIL_HOST_DM_CLONE; EXPLICIT POST-REBOOT RESUME PASS, PRE-FAULT SHA NOT PROVEN |
| One FCoE path lost and returned | Disposable thin data | Linear Thick Generations data | Same identity and bounded I/O after 2-to-1-to-2 | FAIL TARGET TCM_FC PATH RETURN |
| All FCoE paths lost and returned | Disposable thin data | Linear and hydrating Thick Generations data | No unsupported recovery claim; capture target and initiator state | NOT RUN: BASELINE TARGET ALREADY FAILS CLOSED |
| Snapshot callback process terminated | Exact PVE-to-LV snapshot inventory and explicit partial cleanup | C0-C9 persistent classification | One authoritative generation; exact recovery only | PASS BOTH MODES, BOUNDED LAB |
| Snapshot-delete process terminated | Exact snapshot ownership | D0-D4 delete transaction | Idempotent exact delete; HEAD unchanged | PASS |
| Host lost after clone publication | Not applicable to dm-clone | Published frontend reconstructed on survivor | Resume persisted progress and pivot to one linear dependency | PASS |
| Host lost during explicit recovery | Thin recovery remains read-only/manual | Same signed transaction resumes after a second worker-host loss | No metadata reinitialization, no duplicate mapper, final SHA and exact cleanup | PASS BOUNDED LAB |
| Host lost during rollback | Thin rollback remains ownership-checked | HYDRATING rollback reconstructed from signed snapshot and transaction | New HEAD matches snapshot; superseded HEAD removed; explicit PVE lock cleanup only after proof | PASS BOUNDED LAB |
| Host lost during storage move | Source retained until mirror commit | Destination anchor/generation remains scoped | Never delete source before committed mirror result | PASS BOUNDED LAB |
| Host lost during backup | Incomplete archive remains explicitly partial | Steady-state frontend remains ordinary linear | Source ownership unchanged; exact partial cleanup; last durable guest slot survives | PASS BOUNDED LAB; outstanding guest write not guaranteed |
| Host lost during restore | Partial destination retained for exact diagnosis | PREPARED anchor/generation pair retained and mutation gate closed | Exact reference-free recovery only; clean retry restores and deletes all destinations | PASS BOUNDED LAB |
| Reboot after recovered path state | Pool health positively revalidated | Linear or exact resumable transition reconstructed | Same WWID, PV UUID, VG UUID and guest hashes | PASS BOTH MODES |

## Execution rules

1. Capture the exact pre-fault WWID, PV UUID, VG UUID, LV UUIDs, mapper UUIDs,
   dependency graph, quorum state and guest canary hashes.
2. Start only a bounded workload with explicit write-through, flush or fsync
   operations and independent read verification.
3. Inject exactly one named fault. Do not combine node, quorum and transport
   faults until every single-fault case has passed independently.
4. Record the native PVE, QEMU, multipath, SCSI, device-mapper and LVM outcome.
   The plugin must not hide a lower-layer failure.
5. After return, run the read-only recovery gate. A matching identity and path
   count are necessary but insufficient.
6. Permit mutation only after every health dimension is positively healthy.
   Otherwise preserve evidence and classify `RECOVERY_REQUIRED`.
7. Verify guest canaries, snapshot identity, one-owner semantics and absence of
   transaction-scoped artifacts after recovery.
8. Restore the baseline and prove no unrelated storage, guest or cluster state
   changed before starting the next fault.

The Linux VN2VN/tcm_fc target has already reproduced a path-return deadlock.
That result is retained as a transport failure. Physical FC support remains
unqualified until the same matrix passes on representative HBAs, firmware,
fabric and array hardware.
