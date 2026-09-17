# BASTRIX SharedLVM 0.9.0~rc5.7~tg28 release notes

## Status

TG28 is a development candidate for disposable-lab qualification. It is not
a production support claim. Direct overlapping activation of one dm-thin pool
on multiple hosts remains prohibited.

## Opt-in PVE-native Thin activation witness

`slt-thin-leaseguard remote-audit` requires every configured online peer to
prove absence of the exact hidden thin-pool mapper and expected DM UUID before
an unowned pool can be claimed. It uses PVE cluster membership and PVE-managed
SSH trust and installs no additional daemon or package. Missing, malformed,
unreachable or `PRESENT` peer evidence fails closed.

The default is `disabled`; upgrades do not change existing storage behavior.
This witness is not fencing and does not authorize takeover from an
unreachable host that may retain SAN access.

## Migration work

The Materialized Migration Bridge remains the conservative supported
experimental path:

```text
Thin -> online Storage Move to independent Thick
     -> ordinary shared-storage live migration
     -> optional online Storage Move to target-owned Thin
```

TG28 serializes its materialization phase cluster-wide and preserves explicit
phase evidence. Real concurrent bridge qualification showed bounded queuing,
successful VM movement and exact return to Thin without residual bridge tags.

An online Thin-to-Thin QEMU storage-mirror round-trip between two independent
managed VGs also passed with an exact data canary and healthy cleanup. That
proves the copy primitive without a Thick intermediate; it does not make a
shared Thin pool directly live-migratable.

The package contains a disabled fail-closed state machine for a future
capacity-conserving Thin Relay Handoff. It cannot be enabled until PVE/QMP
switchover integration and the complete crash/fencing matrix are qualified.

## Packaging correction

The DEB build explicitly installs the bridge admission, remote Thin evidence
and migration bridge programs as executable files. A source regression now
enumerates every installed shebang so a new helper cannot silently be shipped
as mode `0644`.

## Installation gate

Install the byte-identical package on every participating PVE node, then run:

```text
sharedlvmthin compat-check
sharedlvmthin upgrade-check
sharedlvmthin doctor --quick
sharedlvmthin recovery-check <storage-id>
```

Do not enable `remote-audit` until the identical TG28 package is installed on
every configured node and the full healthy-path start/stop test passes on
disposable storage.
