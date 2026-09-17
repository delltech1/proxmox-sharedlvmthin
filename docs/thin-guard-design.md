# ThinGuard: quorum-fenced Thin authority

ThinGuard is the proposed opt-in runtime safety layer for the existing per-VM
Thin mode. It is not a metadata repair engine and it does not make one dm-thin
metadata device safe for simultaneous writers. It prevents that state from
being authorized.

## Design

Each active per-VM pool has one authority record:

```text
storage identity + pool UUID + owner node + random epoch + transaction digest
```

The record lives in the quorum-backed PVE configuration domain. One guardian
per node opens one aggregate client of the already installed PVE
`watchdog-mux`. Thin I/O
may start only after the epoch is committed, every peer positively reports the
exact pool mapper absent, and watchdog protection is armed.

While active, a local guardian correlates quorum, epoch, canonical storage
identity, the exact DM table/dependencies, QEMU block references and peer
conflict evidence. Only wholly positive evidence refreshes its watchdog
client. Loss of quorum, ownership, storage identity or a detected peer writer
irreversibly latches the epoch uncertain and stops watchdog refresh. A mapper
or QEMU-reference mismatch first requests a bounded pause and exact withdrawal;
failure to prove withdrawal also falls through to fencing.

The PVE watchdog multiplexer currently has a finite 100-client table and a
60-second client expiry. ThinGuard therefore never consumes one client per VM.
The single node client is refreshed only if every locally active Thin authority
epoch is healthy. One uncertain pool deliberately fences the node rather than
allowing a partially failed guardian to protect some metadata domains but not
others. Timing values are discovered and compatibility-checked; they are not
copied into storage policy as an assumed universal constant.

Takeover is permitted only after positive fencing evidence for the previous
owner and a newly committed epoch. A planned non-live handoff contains an
intentional no-authority gap: source QEMU reference and mapper disappear before
the target arms and activates.

## Live migration

The same thin-pool metadata is never activated by source and target kernels at
the same time. Live mobility uses Thin Generation Mobility instead:

```text
source per-VM thin pool (metadata UUID A)
             -> native QEMU active-sync mirror ->
target per-VM thin pool (metadata UUID B)
```

The two independent metadata domains may safely coexist during copying. QEMU
pivots authority once. This consumes temporary Thin capacity but avoids the
fully allocated Thick bridge and avoids an unsupported shared dm-thin runtime.

## Why the layers are separate

- ThinGuard protects steady-state ownership and failover.
- Thin Generation Mobility performs live movement without metadata overlap.
- Transaction fingerprints detect unexpected topology changes.
- Capacity Stability Governor refuses operations without safe runway.
- Thick Generations remains an independent selectable storage mode.

## Enablement boundary

The current implementation is a pure fail-closed decision engine. It is not
armed by default and no production watchdog is opened by unit tests. Packaging
the privileged guardian requires all of these qualification gates:

1. fake-socket watchdog protocol and timeout tests;
2. disposable nested-node self-fence test;
3. quorum partition with old-owner reset proven before takeover;
4. daemon crash, restart and upgrade tests with active guests;
5. exact PVE version/ABI compatibility gate;
6. multi-disk VM and simultaneous evacuation qualification;
7. evidence that no magic-close is sent while protected I/O remains active.

Until those pass, `remote-audit` remains the available activation hardening
and ThinGuard is an experimental, non-arming prototype.
