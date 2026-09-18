# SharedLvmThin `0.9.0~rc5.5~tg26` release notes

## Status

TG26 is a development candidate for disposable-lab qualification. It is not a
production recommendation. Cross-node refusal, offline handoff,
crash-stale-owner classification, simulated fenced-owner recovery, upgrade,
reinstall and data-canary gates have passed. A real externally fenced host-loss
exercise remains open.

## P0 dm-thin ownership correction

One LVM thin pool is one mutable kernel metadata domain. A cluster lock around
LVM commands does not make guest allocation, copy-on-write, or discard updates
cluster-coherent after QEMU has opened a thin LV. Previous successful live
migration tests therefore did not prove that overlapping cross-node activation
was safe.

TG26 establishes the following invariant:

```text
one managed Thin pool -> at most one kernel owner
```

Every newly created pool carries the `pve-slt-owner-v1` schema marker. Before
activation, the plugin serializes through the canonical cluster/VG lock and
persists an exact owner node plus a fresh 128-bit epoch. A different owner,
missing schema, torn tag pair, malformed tag, or ambiguous tag is a hard
refusal with zero activation attempt.

Ownership is removed only after all child mappings, the public pool, and the
hidden `-tpool` mapping are positively absent. PVE Thin live migration is
therefore intentionally refused because its target preparation overlaps the
source runtime. Offline stop/deactivate followed by target activation is the
supported Thin handoff.

After a host loss, the plugin never treats an offline node or timeout as proof
of fencing. `thin-recover-fenced-owner` requires the exact former owner and an
administrator assertion that external fencing has already made that kernel
unable to access the LUN.

The read-only recovery gate also correlates the persistent node/epoch record
with the exact local hidden `-tpool` mapper. A local mapper without the local
owner, or a local owner whose mapper is absent, is `RECOVERY_REQUIRED`. This
detects the crash window between a durable claim and local activation without
guessing whether activation succeeded.

## Existing pools

Legacy pools are never guessed to be safely unowned. The package pre-install
gate refuses locally active legacy pools. Once every affected pool is inactive
on every node and TG26 is installed everywhere, the administrator must perform
the documented one-time `thin-adopt-owner-model ... ALL-NODES-INACTIVE`
procedure. The command adds only the schema marker; the next activation creates
the first node/epoch owner pair.

## Thick Generations

Fully materialized Thick Generations remain independent fully allocated LVs
behind ordinary linear frontends and do not share dm-thin runtime metadata.
Their existing live-migration and recovery rules are unchanged. Transitional
or hydrating generations continue to fail closed without exact signed state.

## Current qualification evidence

- 166 Python cases and 115 Perl subtests (635 assertions) pass.
- A 77-VM concurrent Thin runtime restored exact owner records with no
  duplicate cross-node pool mapping and no owner/runtime mismatch.
- A live Thin migration was refused at target activation while the source VM
  remained running and its owner/epoch remained unchanged.
- Offline stop/deactivate then cross-node activation passed with a fresh target
  epoch; a two-disk VM retained ownership until the final mapping closed.
- An injected durable-claim/no-mapper crash window was classified
  `RECOVERY_REQUIRED`; the exact fenced-owner recovery path restored
  `SAFE_FOR_MUTATION=YES` without data or metadata repair.
- Byte-identical reinstall and compatibility checks passed on all qualified
  nodes. API 14 and API 15 both pass with zero compatibility failures.

Downgrade to a pre-TG26 package is unsupported: older code cannot interpret or
enforce the owner protocol. Debian `dpkg` may fall back to the incoming older
package's maintainer scripts when a currently installed `prerm` refuses an
upgrade, so TG26 does not claim that it can technically prevent an explicit
administrator-forced downgrade. Keep TG26 or newer on every node.

Real external host fencing has now passed on the disposable two-disk VM 992600.
The owning PVE03 virtual host was powered off from its external ESXi hypervisor.
The surviving cluster remained quorate, had no local pool mapping, and refused
target start while the durable owner still named PVE03. Only after positive
hypervisor fencing was the exact former owner cleared. PVE02 then activated
the pool with a fresh epoch, started the VM and reproduced the pre-fault 4 KiB
canary SHA-256 exactly. After PVE03 rejoined, all three nodes reported
`HEALTHY`, `SAFE_FOR_MUTATION=YES`, no relevant D-state and no test-pool map.

Forced downgrade was tested and is explicitly outside the support envelope
because pre-TG26 code cannot enforce the owner protocol. TG26 remains a
development candidate rather than a production recommendation while physical
SAN/HBA qualification and the documented platform-specific gates remain open.

An additional experimental design, `thin-pool-leaseguard.md`, investigates a
per-pool disk-backed sanlock lease coupled to watchdog fencing. It may harden
runtime ownership further, but is not enabled or claimed by TG26.
