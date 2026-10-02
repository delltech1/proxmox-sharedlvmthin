# TG53 adversarial stability plan

This plan extends the completed release qualification. It does not repeat
long hydration or already-passed single-stream cells unless their
implementation changes. Every object is disposable and bounded by an explicit
capacity/time budget.

## Stage 1 — VM configuration and RAM snapshot matrix

Exercise one variable at a time: no NIC, e1000-only, one VirtIO, mixed
VirtIO/e1000, two VirtIO with one link-down, and VirtIO VLAN/firewall/rate.
For each case retain task status, raw config hash, strict snapshot parser
result, exact vmstate identity and serialized MTU inventory. A successful PVE
task that leaves an unparsable snapshot is a failed test. QMP partial-failure
prefixes are fixture-only; live QMP sabotage is not required.

Then qualify suspend-to-disk with Eager data plus Thin vmstate and one
two-disk RAM snapshot with Thin plus MATERIALIZED Lazy. Verify both backing
paths, distinct markers, rollback and exact cleanup.

## Stage 2 — bounded concurrency

Create ten small VMs split across Thin, Eager and MATERIALIZED Lazy. Run ten
concurrent snapshots, settle and delete them, then run ten concurrent stopped
Storage Moves across the supported direction matrix. Run one separate
same-VM snapshot-versus-move conflict.

Safe serialization or explicit bounded refusal is acceptable. Every request
requires its own receipt. Client timeout is UNKNOWN, never proof of backend
completion and never authorization to retry. Stop on crossed identity,
double-writer evidence, wrong `unusedN`, unexplained worker survival or data
hash mismatch.

## Stage 3 — bounded I/O and helper ambiguity

Use a short rate-limited workload with 4 KiB random writes, writes crossing a
1 MiB boundary, sequential writes and fsync. Snapshot once under load and
verify all canaries. Test client interruption separately from one exact helper
timeout/crash fixture. Do not kill a publication worker or inject total SAN
path loss on the shared lab VG.

## Stage 4 — evacuation and controlled reboot

Use the supported live path for Eager and MATERIALIZED Lazy. Handle Thin and
unmaterialized Lazy only through their explicit bridge/materialization or
offline policy. Reboot one evacuated node only after proving no local writer
or open operation and persistent transport readiness. Revalidate boot ID,
sessions, WWID/PV, owners, helpers, gates and markers before one return move.

## Stage 5 — update-policy matrix on a recoverable host

- FREEZE: unrelated update allowed; watched change held; administrator-owned
  pre-existing holds preserved.
- QUALIFIED_AUTO: exact current/target/rolling edge admitted; missing tuple,
  scope or edge refused; incomplete post-gate never publishes success.
- WARN: exact one-shot plan/context only; changed payload, expiry
  and replay refused; result remains unqualified.

For every mode test interrupted settlement and idempotent re-entry. No update
policy may bypass storage identity, ownership or OPEN-intent gates.

## Global stop conditions

Stop new mutations and preserve evidence on any data/identity mismatch,
double writer, unexpected deletion, false HEALTHY/COMPLETE, quorum/path
degradation, capacity limit or unexplained timeout. Automatic cleanup is not a
valid response to an ambiguous result.
