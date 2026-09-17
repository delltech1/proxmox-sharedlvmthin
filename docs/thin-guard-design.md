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

## Two-node and larger clusters

ThinGuard does not require three PVE compute nodes for normal operation. A
two-node cluster without QDevice is classified `SUPPORTED_GUARDED` while both
members are online and quorate. Thin and Thick lifecycle operations and
planned movement remain available, with a prominent `NO_THIRD_VOTE` warning.

If one member disappears, the survivor cannot distinguish a dead peer from a
network partition by itself. Automatic takeover is therefore blocked. A
manual takeover becomes eligible only after both positive external-fencing
evidence and restored PVE quorum. Merely forcing expected votes is not treated
as fencing evidence. Two nodes with a functioning QDevice follow the normal
quorate fenced path.

The classifier has no lab-sized node-count constant. Runtime watchdog use is
one aggregate client per node and local evaluation is proportional to locally
active pools. Peer absence auditing is proportional to configured cluster
members. Actual support remains bounded by the PVE/Corosync deployment and the
release qualification envelope; the project does not claim literally
unlimited nodes.

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

## Watchdog adapter prototype

`PVE::SharedLvmThinWatchdog` implements the exact current PVE multiplexer
client contract behind an explicit real-socket opt-in. Normal tests can only
provide an injected socket. Arming and a healthy refresh write one NUL byte.
The magic-close byte `V` is legal only after positive proof that every guarded
QEMU reference and mapper is absent and every owner epoch was released.

After runtime uncertainty, the adapter enters an irreversible `FENCING` state:
it sends no further refresh and rejects clean disarm even if a later sample
looks healthy. Explicit fencing closes without `V`; unexpected destruction
also never sends `V`. This matches watchdog-mux fail-closed semantics while
preventing unit tests or package installation from touching the live socket.
