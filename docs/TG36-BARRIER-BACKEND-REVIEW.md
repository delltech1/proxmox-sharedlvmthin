# TG36 maintenance barrier backend review

`experiments/thick-generations/layout-migration-barrier-backend.py` adds an
unpackaged, dependency-injected backend for the existing barrier controller.
It has no command-line entry point and is **not yet a live barrier runner**.
The existing create-only, fsync-backed coordinator journal records each
intent and attempt before transport dispatch. An uncertain command result
poisons the backend and node agent; a reconstructed controller refuses any
partial history. A matching historical attempt alone cannot grant a new
backend its single-use dispatch token.

The only node operations are `OBSERVE`, `ENTRY_BLOCK` and `SERVICE_DRAIN`.
The only effect commands constructed by the node agent are absolute
`systemctl mask -- <fixed units>` and `systemctl stop -- <fixed units>`.
There is no generic command field, shell string, guest stop, start, release,
reboot, package operation or storage mutation. The node and coordinator both
check the bound plan identity and deadline. A mask that succeeds just before
expiry does not authorize the following stop.

The four-node order remains: block entry on all four members, reobserve all
four members, drain the exact required services on all four members, then
audit all four members. The control-only member participates in this control
plane barrier but has no SAN or package action here.

## Workload scope

The normalized model field `running_guests` means **running managed
SharedLVM consumers**. The backend derives it; a collector cannot supply
that field directly. It validates the exact storage bytes and workload
snapshot hash, re-parses each guest's disk configuration, checks exact
managed consumer coverage/config hashes, and rejects new, moved or changed
managed consumers. Unrelated running guests are permitted and never stopped.
The injected collector must prove complete configuration and QEMU/LXC process
coverage; an `inventory_complete` flag is an assertion by that trusted
collector, not a proof created by this backend.

Managed mapper and open-LV inventories must both be empty. HA idle/disarmed,
tasks, workers and pending jobs retain their strict existing requirements.
Unknown inventory is refusal. The positive injected test includes a running
unrelated guest on the second node throughout the complete barrier.

## Service observations

Every watched unit must have an unambiguous raw state. Inactive/dead requires
zero main and control PIDs, an empty cgroup and no systemd job. Masks must be
persistent links under `/etc/systemd/system`, not runtime masks. The collector
must inspect those link identities rather than infer them from enabled state.
`pve-guests.service` is never stopped. Its normal active/exited oneshot state
is accepted only with zero PIDs and an empty cgroup. Corosync and pve-cluster
must remain active on every node; multipathd must remain active on SAN nodes.
The control-only member is allowed to have inactive multipathd.

## Remaining live integration work

Before this backend can be used against real services, implement and review:

- A pinned, authenticated, at-most-once transport and node-local durable
  request latch. The in-memory NodeAgent latch alone is not crash persistence.
- A real local collector covering guest configs and actual guest processes,
  managed DM/LV consumers, systemd cgroups/jobs/persistent mask identities,
  exact cluster/boot/config identity, plugin workers and HA watchdog state.
  Local observation must still work after PVE daemons have been drained;
  it cannot depend on a stopped API daemon returning cached resource state.
- Exact raw evidence retention and linkage to the coordinator journal so
  post-crash inspection can establish which service effects actually occurred.
- A separate reviewed restoration/release procedure. This backend cannot
  restart services or silently roll back a partial barrier.

Do not convert `MODEL_HELD` into live rollout authorization. The injected
controller's `mutation_performed=false` is a model result; it cannot describe
real service changes if a future production transport is supplied.

## Tests

Run `python3 -m unittest discover -s tests/unit -p 'test_layout_migration_barrier*.py'`.
The suite covers a full four-node transport with a real local journal,
effect-then-error/no retry, expired execution between commands, foreign
receipts, success without effects, swallowed reentry, scope/config ambiguity,
running managed guests, unrelated guest survival, hidden managed mappings,
surviving cgroup processes, incomplete inventories and forbidden operations.

All executions for this change use injected service effects. No real service
was masked/stopped and no package, storage or GitHub mutation was performed.
On 2026-09-26 the focused Linux suite passed **36/36** tests (14 backend,
14 existing model and eight journal tests) in an isolated `/tmp` test tree.
