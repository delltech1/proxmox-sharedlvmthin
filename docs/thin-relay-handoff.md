# Thin Relay Handoff (research prototype)

Thin Relay Handoff is the capacity-conserving migration design for an
exclusively owned per-VM dm-thin pool.  It is not enabled in a release and no
current release claims direct Thin live migration.

## Purpose

The existing Materialized Migration Bridge is deliberately conservative, but
temporarily requires a fully allocated Thick destination.  Relay Handoff aims
to preserve the original Thin pool and move its *exclusive ownership* during
the bounded QEMU migration switchover instead of copying the disk.

```text
steady state       source QEMU -> source-only thin pool
RAM pre-copy       target sees a temporary source-owned NBD relay
switchover         quiesce -> flush -> close source -> ownership CAS
target activation  prove source absence -> activate same pool on target
steady state       target QEMU -> target-only thin pool
```

The relay is transition transport, never the steady-state storage format.

## Non-negotiable invariant

The same thin-pool metadata domain must never be active in two kernels.
Neither pmxcfs locking nor a tag is proof that the old kernel mapping has
disappeared.  The coordinator requires positive runtime absence evidence or
positive fencing before committing target ownership.

## Transaction phases

`PREPARED -> RELAY_READY -> QUIESCED -> SOURCE_CLOSED ->
OWNERSHIP_COMMITTED -> TARGET_ACTIVE -> COMPLETED`

Before `OWNERSHIP_COMMITTED`, recovery may only restore the source.  At and
after `OWNERSHIP_COMMITTED`, recovery may only finish on the target.  An
unknown journal, quorum, QEMU-open, mapper, identity, or fencing state yields
`RECOVERY_REQUIRED`; it never advances the transaction.

`PVE::SharedLvmThinRelay::evaluate_relay_handoff()` is the pure fail-closed
reference state machine.  A later coordinator must gather its evidence using
bounded PVE/QMP/device-mapper probes and must persist a signed transaction
record before performing each irreversible step.

## Why NBD is used

QEMU already uses an NBD-backed block migration path for non-shared storage.
During RAM pre-copy the relay preserves one dm-thin metadata owner.  It does
not make dm-thin multi-host capable and does not permit target activation.

## Capacity behavior

The zero-copy relay needs only bounded transaction metadata and networking;
it does not allocate a Thick disk or a second Thin data copy.  A separate
Thin-to-Thin mirror remains a possible fallback, but necessarily needs
temporary physical space for the copied allocated data.

## Required qualification before enablement

- exact QEMU/PVE 9 migration hook and QMP ordering;
- crash at every transaction boundary;
- source, target and management-network loss before and after ownership CAS;
- NBD disconnect, reconnect and timeout behavior;
- multi-disk all-or-nothing handoff;
- fencing and quorum-loss behavior;
- bounded I/O freeze and guest filesystem verification;
- package upgrade/reboot recovery from every persisted phase.

Until all gates pass, Materialized Migration Bridge remains the supported
experimental fallback and Relay Handoff remains disabled.
