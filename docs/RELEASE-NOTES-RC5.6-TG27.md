# SharedLvmThin `0.9.0~rc5.6~tg27` release notes

## Status

TG27 is an unreleased development candidate. It is not a production support
claim. It preserves the TG26 single-kernel owner protocol and adds a second
gate against generic LVM autoactivation outside the plugin lifecycle.

## Thin P0 correction

A legacy Thin pool can already contain the TG26 owner-schema tag while still
having generic LVM autoactivation enabled. During a host boot, LVM/dmeventd may
then reconstruct the hidden `-tpool` mapper before the plugin claims a durable
owner. A later owner claim must not reinterpret that pre-existing kernel
metadata instance as safe.

TG27 therefore requires all of the following before a fresh Thin claim:

```text
exact storage identity PASS
pool and requested LV autoactivation disabled
owner schema present and no foreign owner
no exact local pool mapper
no exact local child mapper
then claim node + epoch
then activate the requested LV
```

The explicit offline command now both adopts and hardens an exact pool family:

```text
sharedlvmthin thin-adopt-owner-model \
  <storage-id> <volume> ALL-NODES-INACTIVE
```

The assertion must be true on every participating node. The command refuses
an owner, any local pool/child mapper, missing membership, ambiguous evidence
or failed postcondition. It does not scan, stop or repair another node.

## New research guards

TG27 source also contains independently tested primitives for:

- exact dm-thin transaction fingerprints;
- bounded read-only metadata validation outcomes;
- predictive data/metadata runway evaluation;
- a non-arming PVE watchdog-mux bridge simulator.

These primitives are not presented as active LeaseGuard or automatic repair.
The watchdog model cannot open the real watchdog-mux socket and sanlock is not
installed or enabled by the package.

Compatibility qualification now correlates `dm-event.service` with exact
local managed Thin mapper evidence. Detached public
`sltp-<VMID>_meta<N>` recovery metadata is reported as
`RECOVERY_REQUIRED`; TG27 deliberately performs no automatic repair or
deletion of that evidence.

## Thick Generations

No Thick Generations runtime format, anchor transition, hydration or linear
pivot behavior is changed by this Thin correction. Full Thick unit and
integration regression remains a release gate.
