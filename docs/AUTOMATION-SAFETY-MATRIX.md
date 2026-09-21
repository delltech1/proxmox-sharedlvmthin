# Automation safety matrix

The project automates only decisions that can be derived from exact,
machine-verifiable evidence. Convenience is not evidence and elapsed time is
never ownership proof.

| Area | Safe to automate | Automatic refusal / preservation | Explicit operator action only |
|---|---|---|---|
| Configuration | Schema ranges, same-VG alias identity, node scope and timing equality | Missing or conflicting policy blocks mutation | Choosing site timeouts and fencing architecture |
| Thin activation | Quorum/identity checks, exact owner claim, two peer mapper audits, runtime-guard arm | Any unreachable peer, malformed proof, epoch drift or mapper presence | Legacy owner-model adoption and non-HA takeover |
| Thin deactivation | Re-read child/pool mapper state; release owner only after exact absence | Active/ambiguous mapper preserves owner | Investigation of D-state or an unresponsive fenced host |
| Thin allocation/grow | Capacity gate, exact postcondition after ambiguous `lvextend` | No retry or shrink when outcome cannot be proven | Repair of partial pool creation or metadata damage |
| Thin resize/snapshot | Prove snapshot-name absence before create; classify create/delete/resize errors only from exact postconditions | Never retry an ambiguous LVM mutation or delete an uncertain object | Resolve state when VG or object identity cannot be re-read |
| Thin rollback | Create replacement first; re-inventory names and pool ownership after every command error; accept completed rename only from exact final topology | Preserve origin/replacement and perform no retry or cleanup in every non-final topology | Resolve a prepared `slt-rb-*` object after interrupted remove/rename |
| Thick allocation | Signed intent/object creation, full zeroing, flush and exact publication | Preserve `OPEN ALLOC` plus every object that reached disk on interruption | Explicit exact partial-allocation recovery is repeatable with the signed HEAD only, the complete pair, or the signed anchor left after the first cleanup deletion |
| Thick whole-volume delete | Remove verified idle frontend, persist `OPEN REMOVE`, then delete exact signed HEAD and anchor | Preserve the intent and every remaining exact object; never retry a failed command in-line | `thick-recover-volume-delete` requires no PVE references or frontend, revalidates the signed remainder, and is idempotent after either delete boundary |
| Thick snapshot/rollback prepare | Persist exact VG intent, then atomically create each LV with complete signed ownership and `autoactivation=n` | Intent-only, one signed object, both signed objects, or a signed PREPARED anchor; never an unowned transition LV created by this version | Read-only classifier distinguishes incomplete/unrecorded/PREPARED state; no object is adopted by name alone |
| Thick activation | Verify signed anchor, mapper UUID, table and dependency graph | Missing or altered frontend requires recovery | Reconstruction of an unproven topology |
| Thick deactivation | Observe open count to zero for a configured monotonic window | Timeout leaves mapper and dependencies intact | Diagnose a permanently open consumer |
| Thick snapshot/hydration | Persistent transaction, event-numbered progress observation, resume from exact state, linear-pivot verification | No-progress, geometry drift or missing dependency preserves transaction | Recovery when the persisted state and kernel graph disagree |
| Thick resize/delete | Capacity/identity gates, persistent intent and exact reread after mutation | Ambiguous or partial result preserves intent and objects | Classification when identity cannot be revalidated |
| Migration bridge | Journaled admission, manifests, capacity checks, progress heartbeat and explicit resume of exactly one state file | Expiry never steals admission or deletes source/destination; multiple state files are ambiguous | Deciding which transaction/side is authoritative when evidence conflicts |
| Rolling maintenance | Explicit VM manifest, idempotent state skips, native PVE task completion and post-reboot verification | No wall-clock kill and no automatic retry of an uncertain PVE task | Reboot command and change-control approval |
| Health/recovery | Read-only inventory, warnings, exact recovery plans | Diagnosis never mutates storage automatically | Fencing, repair tools, forced cleanup and destructive recovery |

## Mandatory invariants for new automation

Any future automatic action must satisfy all of these conditions:

1. It runs under the canonical storage/VG serialization boundary where a
   shared metadata mutation is possible.
2. It pins and revalidates VG UUID, PV UUID and stable WWID where supported.
3. Its precondition names the exact transaction, object, owner and expected
   topology; absence of evidence is a refusal.
4. A mutating command executes at most once. A client timeout or error is
   followed by a read-only postcondition, never an immediate retry.
5. Cleanup targets only positively owned objects and runs only after exact
   reference, open-count and dependency checks.
6. Success is declared from the persisted/kernel postcondition, not merely
   exit status.
7. Interruption leaves enough durable evidence for deterministic resume or a
   clear manual recovery boundary.
8. The failure path is tested at every boundary before, during and after the
   irreversible command.

If any condition cannot be met, automate diagnosis and refusal, not repair.
