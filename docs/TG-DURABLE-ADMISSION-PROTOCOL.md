# Thick Generations durable admission protocol

Status: design only; not implemented or enabled in TG53.

## Objective

Provide fair, replay-resistant mutation admission without treating a timeout,
process exit, event, lease, or loss of quorum as proof that a previous SAN
writer is dead. The protocol sits below PVE API adapters and above the existing
exact mutation engine, so future PVE hook changes affect the adapter rather
than the persistent transaction contract.

## Authority model

Three responsibilities remain separate:

1. An immutable bounded pmxcfs log records ordering, capabilities and audit.
2. A small versioned SAN descriptor records the active epoch and a
   spend-watermark `(epoch, sequence, request_hash, tx, checkpoint_hash)`.
3. The existing create-only VG intent remains the sole authority for an
   in-progress storage mutation.

There is no claimed atomic transaction between pmxcfs and LVM. A request is
spent on SAN before its VG intent is created. A crash after spend but before a
proven intent consumes the request and yields `UNKNOWN`; it is never replayed
automatically. This also prevents restoration of an older pmxcfs state from
reviving a mutation already spent on SAN.

## Records and states

An epoch pins VG/PV/WWID identity, a nonce, previous checkpoint, protocol and
format versions, alias-configuration hash, eligible writer nodes, required
capabilities and bounded resource limits. Each request pins sequence,
request-id, node and boot-id, operation, storage/volume/snapshot identity,
expected anchor identity, normalized parameters and configuration hash.

Records are immutable and hash-linked:

`ENQUEUED -> SELECTED -> SPENT -> INTENT_CONFIRMED -> SLOT_RELEASED -> SETTLED`

`CANCELLED_BEFORE_SPEND` and `UNKNOWN` are explicit terminal classifications.
`SLOT_RELEASED` means the global VG intent was safely handed to an exact signed
anchor transition; it does not claim that hydration or cleanup is complete.

## Grant invariant

Under one canonical lock derived from VG UUID, the engine verifies quorum,
runtime qualification, SAN identity, alias parity, epoch/capabilities, queue
head, exact volume preconditions and any foreign transition. It then persists
and reads back `SELECTED`, spends the sequence on SAN, reads back the exact
watermark, create-only writes the VG intent with the pinned tx, and reads back
the exact intent. Only the proven intent authorizes the first LV/DM effect.

No mutation callback is retried. Events may wake a waiter but never grant
admission; the authoritative reread remains under the canonical lock.

## Rolling activation

First deploy a compatibility-floor reader to every eligible writer. That
release must reject unknown protocol majors/features and respect
`PREPARED/ACTIVE/DRAINING/DISABLED` at every mutation entry point while still
supporting legacy mode when the protocol is disabled. Only after all writers
prove compatible package, boot, API contract and configuration identities may
an administrator publish an epoch and activate the SAN descriptor.

An old or offline writer cannot be made safe by a tag it does not understand.
Rejoining nodes must qualify before becoming eligible. Two-node constrained
mode never lowers expected votes, invents fencing or takes over an ambiguous
writer.

## Required adversarial gate

- kill after every persistent write and around the first storage effect;
- duplicate, delayed and replayed requests and acknowledgements;
- stale pmxcfs restore and SAN watermark ahead/behind;
- malformed, duplicated or unknown-major records and broken hash chains;
- quorum loss at `SELECTED`, `SPENT` and `INTENT_CONFIRMED`;
- node reboot, surviving helper/D-state and unavailable remote writer;
- ten-node contention, cancellation, bounded overflow and FIFO ordering;
- anchor handoff and blocker replacement while waiting;
- API14/API15 and incompatible mixed-version writer rejoin;
- crash during activation, draining, checkpoint/compaction and disable.

The oracle for every failure is: no second execution, no overwritten intent,
no automatic UNKNOWN recovery and no silent fallback to legacy mutation.
