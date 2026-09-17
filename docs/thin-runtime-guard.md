# Thin Runtime Guard

`runtime-guard` is an opt-in extension of the exclusive per-VM Thin owner
model.  It uses only components already present on Proxmox VE: pmxcfs quorum,
SSH peer evidence, LVM/device-mapper, `watchdog-mux`, systemd and the storage
plugin itself.

It does not make one dm-thin metadata device safe for simultaneous activation
in two kernels.  It enforces the opposite invariant: exactly one admitted
kernel, protected by a watchdog-backed owner epoch for the complete mapper
lifetime.

## Prerequisites

- install the same qualified package on every node in the storage node scope;
- configure and test PVE fencing/watchdog policy;
- pin WWID, PV UUID and VG UUID in the storage configuration;
- ensure every managed pool uses the `pve-slt-owner-v1` schema;
- stop/deactivate every volume on the storage before first enrollment;
- verify `sharedlvmthin recovery-check <storage-id>` on every node.

## Enrollment

Run on every node included in the storage node scope:

```bash
systemctl enable --now pve-sharedlvmthin-thin-guard.service
systemctl is-active pve-sharedlvmthin-thin-guard.service
```

Only after every node reports the daemon active, change the storage:

```bash
pvesm set <storage-id> --slt-thin-leaseguard runtime-guard
```

The package installation never enables or starts the daemon and never changes
an existing storage to this mode.

## Activation transaction

```text
PVE storage lock
  -> pinned storage identity
  -> exact peer mapper absence
  -> persistent owner node + epoch
  -> independent guardian identity and peer audit
  -> watchdog-mux arm
  -> ACK_PREPARED
  -> lvchange -ay
  -> exact mapper + attached QEMU reference
  -> PROTECTED
```

If the daemon, socket, quorum, peer audit, watchdog, identity or exact response
is unavailable, activation is refused before `lvchange -ay`.  A transport
retry reuses the same request ID and the daemon replays the recorded result;
it never executes a second admission transaction.

## Teardown

The daemon releases its aggregate watchdog client only after the plugin and
its independent inventory prove all of the following for the exact epoch:

- no attached QEMU reference;
- no exact thin-pool mapper;
- persistent owner node and epoch tags removed.

When other protected pools remain, the aggregate watchdog continues to be
refreshed.

## Two-node clusters without a QDevice

Two-node clusters are supported for normal and planned operations while both
nodes are online and the cluster is quorate.  The Doctor reports
`NO_THIRD_VOTE` because automatic single-survivor takeover cannot be inferred
safely.  After losing one node, mutation and takeover remain blocked until
quorum is restored and the former owner is positively fenced.  Manually
forcing expected votes is not fencing evidence.

The same guard works for larger clusters and uses one watchdog-mux client per
node, not one client per VM.  There is no plugin-side small-cluster or per-VM
watchdog-client ceiling; normal Proxmox and infrastructure limits still apply.

## Boundaries

- Thin live migration with overlapping source/target activation remains
  unsupported.  Use stopped handoff or the documented materialized migration
  bridge.
- The guard does not configure SAN sessions, multipath, quorum or fencing.
- Starting a fresh daemon while a protected mapper already exists is refused;
  a new process may not retroactively bless an unknown runtime.
- Stopping or crashing an armed daemon closes watchdog-mux without magic `V`
  and deliberately leaves host fencing armed.
