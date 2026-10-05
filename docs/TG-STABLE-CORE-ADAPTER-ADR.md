# TG stable core and PVE adapter boundary

Status: accepted design. The read-only exact-family resolver and compatibility-
gate binding are implemented; storage algorithms and on-disk formats remain
unchanged. A matched adapter still returns `RETEST_REQUIRED`, never automatic
qualification.

## 2026-09-30 independent design review

Two independent Astra reviews converged on this ADR and rejected a second
broker, a journal LV, automatic PVE patching, and version-number-only
compatibility.  The durable target is **operation-scoped qualification**:

```
ADMIT(operation) =
    supported adapter contract
    AND qualified operation/runtime evidence
    AND current ownership/admission epoch
    AND no unresolved package or storage transaction
```

A capability probe proves only that an interface is present and parseable.  It
does not prove failure ordering, cleanup, ownership transfer, snapshot
atomicity, or migration semantics.  Exact installed, loaded-worker and
boot/kernel identities therefore remain mandatory evidence.  An unknown major
release may remain `BLOCKED`; the system promises safe detection and refusal,
not automatic compatibility with arbitrary future PVE behaviour.

The package manager hook remains a bounded plan parser and verifier.  It must
not start nested APT/dpkg, perform SAN drains, restart services, or run a long
qualification while package-manager locks are held.  APT and direct package
replacement use the same durable transaction envelope; raw dpkg or root can
bypass an APT hook, so runtime drift detection remains an independent storage
admission condition.

The existing catalogue is the seed of the adapter contract.  Its next schema
must bind each operation group to exact source/runtime evidence, executable
semantic scenarios, and allowed mixed-version edges.  A changed dependency
selects the affected tests; it does not automatically block unrelated,
independently qualified operation groups.

## Decision

SharedLVM will evolve as three deliberately separate layers:

1. A stable storage-safety core owns identity, canonical VG locking, admission,
   create-only intent, anchors, ownership, recovery, and exact LVM/device-mapper
   effects.
2. Thin versioned PVE adapters translate public storage hooks and narrowly
   qualified PVE/QEMU lifecycle contracts into normalized core requests.
3. An upgrade coordinator evaluates exact package plans and proves installed,
   loaded, and boot/runtime identities before releasing mutation admission.

The core interface is operation-scoped.  Its conceptual boundary is:

```
check(operation, normalized_request, runtime_contract)
admit(operation, expected_identity, deadline)
execute_once(admission)
observe(transaction)
```

`admit` is not a transferable grant.  The final identity check and create-only
intent write remain in the same canonical VG critical section.

## Compatibility evidence

The compatibility catalogue distinguishes three different facts:

- adapter contract: API, hook, parser, command, and lifecycle capabilities;
- qualified tuple: exact packages, architectures, kernel, profile, core and
  adapter identities, plus executable scenario evidence;
- rolling edge: an explicitly qualified old/new pair and the operations and
  on-disk formats allowed while both are present.

An API loader accepting a plugin is not proof that snapshot, migration,
vmstate, restore, resize, or cleanup semantics remain correct.  Unknown API or
unknown required capability has no fallback to a nearest adapter.

Installed identity, loaded worker identity, and running boot/kernel identity
are separate evidence.  A new package on disk does not prove that a long-lived
PVE process has loaded it.

## Administrator update policies

- `FREEZE`: reject a watched-stack change except through the exact supported
  package settlement workflow.
- `WARN`: permit an explicitly selected unqualified plan only after a durable
  pending record and warning.  Affected new storage mutations remain blocked
  until runtime qualification; WARN is not fail-open admission.
- `QUALIFIED_AUTO`: admit only an exact qualified transition and release new
  mutations only after post-install runtime settlement.  It never starts APT.

Legacy `MANUAL_OVERRIDE` and `QUALIFIED_ONLY` policy files are not silently
reinterpreted. The explicit schema migration records their exact predecessor
and maps them to `WARN` and `QUALIFIED_AUTO` before effects.
An APT plan containing both unrelated updates and a refused watched update is
refused as one plan; the administrator may split it.

## Durable upgrade state machine

```
PREPARED
  -> PACKAGE_PENDING
  -> INSTALLED_UNVERIFIED
  -> RUNTIME_VERIFIED
  -> RELEASED
```

The latch is durable before the first package effect.  Abort, crash, raw dpkg,
old loaded workers, wrong running kernel, or failed postconditions never imply
RELEASED.  Recovery either proves the exact next transition or refuses.  A
reboot invalidates old-boot runtime receipts and requires fresh evidence.

## Rolling upgrade and protocol floor

Before a new persistent protocol can be activated, every potential SAN writer
must first run a compatibility-floor reader at every relevant mutation entry
point.  Only after that bootstrap may a coordinated barrier raise the floor.
An offline or rejoining legacy node is not assumed safe: it must prove a
compatible loaded runtime or be operationally excluded from SAN writes.

Control-plane admission cannot fence existing QEMU I/O, a surviving LVM child,
or a kernel writer.  No timeout, lease expiry, process exit, or quorum loss
authorizes conflicting takeover.  A downgrade below an active floor is
refused until operations are drained, all transactions are settled, and the
new protocol is explicitly disabled.

## Durable admission protocol relationship

The proposed pmxcfs request log plus SAN anti-replay watermark remains a
separate future protocol for ordering and audit.  It is not required for minor
PVE compatibility and is not fencing.

If implemented, its SAN spend is consumed before effect.  A crash after spend
but before proven intent is `UNKNOWN` and is never replayed automatically.
Global VG intent and per-volume anchor authority must both be considered: the
absence of a VG intent does not prove that no transitioned volume remains.

No broker daemon, journal LV, lease stealing, event-only admission, automatic
PVE source patching, or per-package-version adapter is introduced now.

## Required executable gates

1. Unknown API, hook, parser, or command contract causes zero storage effects.
2. FREEZE, WARN, QUALIFIED-AUTO, and one-shot override have distinct, tested
   authorization semantics; no receipt can be replayed.
3. Kill after every durable package phase, including dpkg abort branches,
   leaves a pending state and never a false RELEASED state.
4. New files with an old loaded worker, and a reboot into an old kernel, block
   affected mutations.
5. Mixed-version operations run only over a qualified rolling edge; rejoining
   legacy writers cannot create a new-format effect.
6. Snapshot, rollback, resize, migration, vmstate, backup/restore, and cleanup
   failure injection preserve exact ownership and recovery oracles.
7. An unresolved intent or anchor blocks downgrade and protocol disablement.
8. Two-node constrained mode remains fail-closed and gains no automatic
   quorum adjustment or ownership takeover.

## Immediate implementation order

1. Preserve the qualified RC5.69 transaction/runtime baseline: one durable
   package latch, read-only bootstrap qualification, separate installed,
   loaded and boot identities, and explicit FREEZE/WARN/QUALIFIED_AUTO policy.
2. Evolve the compatibility catalogue to operation groups without changing
   storage algorithms or on-disk formats.  Start with activation,
   allocation/removal, snapshot/vmstate, resize, move/import, and migration.
3. Bind every group to source-surface probes and executable semantic scenarios;
   store explicit `QUALIFIED`, `RETEST_REQUIRED`, or `BLOCKED` reasons.
4. Add rolling old/new edges and a dormant protocol-floor reader.  Do not raise
   an on-disk floor until every potential SAN writer has a qualified reader or
   has been operationally excluded.
5. Add crash injection at every durable package phase, stale-worker and PID
   reuse tests, WARN-with-blocked-runtime tests, and concurrent dpkg/hold drift
   tests before allowing the coordinator schema to advance.
6. Consider a durable FIFO/anti-replay protocol only after the above gates are
   qualified and a demonstrated fairness requirement exists.

## Implemented lifecycle-family resolver

`sharedlvmthin lifecycle-adapter-check` resolves the current Storage API and
the exact versions of the PVE manager, storage, QEMU server, QEMU binary and
common library stack against `pve-lifecycle-adapters.json`. It has no nearest-
version fallback. No match or more than one match is `BLOCKED`; exactly one
match is only `RETEST_REQUIRED` and reports the operation-specific contract,
preflights and regression tests still required.

The node compatibility gate runs this resolver before QEMU destroy semantics,
inventory comparison, upgrade checks or candidate inspection. This is a
compatibility firewall, not a claim that a package tuple is safe: exact tuple,
loaded-worker, boot/kernel and executable lab evidence remain separate gates.
