# FIX2 Thin activation hardening qualification

Status: **LOCAL HARDENING UNRELEASED / UNCOMMITTED / LAB-DEPLOYED / NOT PUBLISHED**  
Base commit: `ed03f88c1b8e79a85fbbed4fd3f70f84d3eda531`  
Target: Proxmox VE 9 three-node disposable lab  
Date: 2026-09-18

## Release and deployment lineage

FIX2 itself is already public. GitHub publishes the pre-release
`v0.9.0-rc5.10-tg31-fix2` from release commit `ede2ec0`.

The three lab hosts currently run the later laboratory FIX2 build
`0.9.0~rc5.10~tg31+fix2lab3ed03f88`. On 2026-09-18, the installed plugin,
QMP path helper, and remote Thin evidence helper had identical SHA-256 hashes
on anonymized `lab-node-a`, `lab-node-b`, and `lab-node-c`. Those hashes match the
clean local `ed03f88` source byte-for-byte:

| File | SHA-256 at `ed03f88` and on all three hosts |
|---|---|
| `SharedLvmThinPlugin.pm` | `b02b9ec38fccbcc1954c203ac90b40ed9a2181958729ec42abdebd1b68efe16f` |
| `sharedlvmthin-qmp-path-check` | `303ba24d733b06a4af6ddb7a5893f5613334c86426f9f180731e2fffb97a078c` |
| `sharedlvmthin-remote-thin-evidence` | `c66692301b34ce0fff605b4314a5de0f7dbb10496334219ae9f54c13650eec18` |

H1–H3 in this document are additional hardening on top of that exact deployed
FIX2 baseline. On 2026-09-18 the local candidate was installed one node at a
time on all three disposable-lab hosts and each host completed a controlled
reboot/rejoin. It remains uncommitted and unpublished.

The local package candidate is named
`0.9.0~rc5.10~tg31+fix2lab4harden1`. This version is intentionally newer than
the installed `+fix2lab3ed03f88` build while remaining visibly separate from a
public release.

## Purpose

This candidate strengthens the already fail-closed FIX2 Thin activation path.
It does not claim that dm-thin is cluster-aware and does not enable native
overlapping Thin live migration. The invariant remains:

> One managed thin pool may be loaded by at most one kernel device-mapper
> instance.

The changes deliberately remain inside the storage plugin and its read-only
peer helper. They do not patch PVE/QEMU, require a private migration callback,
or add a package, daemon, lock manager, or kernel module.

## Candidate changes

### H1 — existing local owner is not an audit bypass

A durable owner node and epoch are recovery evidence, not proof of current
kernel exclusivity. Every guarded activation re-audits all configured peers
even when the owner already names the local node. Missing, malformed,
unreachable, or conflicting peer evidence blocks activation without LVM
mutation.

### H2 — activation commit barrier

Immediately before `lvchange -ay`, activation repeats the exact peer audit and
re-reads the durable owner state. The local node and admitted epoch must still
match exactly. A last-moment peer conflict or epoch change blocks activation
without attempting `lvchange`.

### H3 — UUID-based alias detection

The remote helper enumerates kernel `thin-pool` targets and reads the DM UUID
of every enumerated name. Presence is decided by the expected immutable LVM DM
UUID, not only by the canonical mapper name. Therefore the same pool loaded
under an alternate name is still a conflict. A canonical name with a different
UUID is ambiguous and fails closed.

## Mode-interaction assessment

- `activate_volume()` dispatches `thick-generations` directly to
  `_thick_activate_volume()` before entering the Thin path. H1–H3 do not alter
  Thick frontend activation, linear generation mapping, hydration, cutover, or
  cleanup.
- Thin-to-Thick materialization keeps the source Thin pool on its current
  owner. Any real Thin activation passes H1–H3; Thick generation operations do
  not.
- Cross-node movement in the materialized bridge occurs only after all disks
  are independent Thick linear volumes.
- The optional return to Thin creates a new target-owned pool and must pass the
  same peer/epoch gates before target Thin activation.
- Thin rollback intentionally calls the common Thin activation routine before
  `lvcreate` can implicitly activate pool metadata, so it inherits H1–H3.

## Qualification record

| Gate | Expected result | Current result |
|---|---|---|
| Perl/shell syntax | plugin and helper parse | PASS after H3 |
| Full Perl unit suite | all assertions pass | 801 PASS after H3 |
| Full Python unit suite | all tests pass | 203 PASS after H3 |
| Existing-local-owner remote conflict | refuse, zero LVM mutation | PASS |
| Final peer conflict | refuse before `lvchange` | PASS |
| Owner epoch drift | refuse before `lvchange` | PASS |
| Canonical mapper absent | strict `ABSENT` record | PASS, private mount namespace |
| Canonical mapper present | strict `PRESENT` record | PASS, private mount namespace |
| Expected UUID under alternate name | strict `PRESENT` record | PASS, private mount namespace |
| Canonical name with wrong UUID | exit 73/UNKNOWN | PASS, private mount namespace |
| Thick activation regression | Thin owner/barrier not entered | PASS: explicit unit dispatch trap plus installed 1 GiB start/stop; Thin maps 29→29 |
| Thin-to-Thick-to-Thin bridge | no new refusal or residue | PASS: installed 1 GiB VM 992700, node 2→3, returned running on `slt-tg-thin`; Doctor 0 FAIL |
| Native Thin live migration | second kernel remains blocked | PASS: VM 990110 refused on destination, source remained running on node 2, destination mapper absent |

## Installed-candidate rolling evidence

- Candidate DEB SHA-256:
  `5ad654b6dd08b0e1fa4008cf5317f995ef6fbacca6dd59075fe91c28c252e0af`.
- Installed version on all three nodes:
  `0.9.0~rc5.10~tg31+fix2lab4harden1`.
- Installed plugin SHA-256 on all three nodes:
  `eb2611211eea983be633848e3708890ec3573d58b7d869c06dc169a7a7401c76`.
- Installed remote-evidence helper SHA-256 on all three nodes:
  `23a76767ea935f938afda1ace0ec26b21bf7a9eab5b151997eabd65ab6b29cb7`.
- Node 1, node 3 and node 2 completed controlled reboot/rejoin with quorum,
  required services and configured storage healthy. Post-reboot Doctor results
  contained zero failures.
- Node 3 restored the exact pre-reboot 29-VM set and 29 Thin maps. Its 29
  minimalist no-OS lab VMs required native PVE 180-second shutdown timeouts;
  this was guest shutdown behavior, not a Thin activation wait.
- Node 2 restored the exact pre-reboot 30-VM set (29 Thin plus one non-Thin
  VM). Serial restoration of the 29 Thin workloads completed in 191 seconds
  with zero failures. The measurement includes canonical VG lock acquisition,
  remote audits, LVM metadata operations and a backend already warning about
  roughly 2.8 GiB/~6,000 LVM archive files; it is not an isolated H1–H3
  benchmark.
- Final cross-node kernel inventory contained 58 active Thin pool UUIDs and
  zero UUID duplicates.
- The local `lab/fix2-rolling-node-cycle.sh` helper records and verifies exact
  running sets. It defaults to dry-run, requires an explicit disposable VMID
  list, and requires both `--execute` and exact hostname confirmation before a
  mutation. It deliberately does not issue host reboot itself.

Expected warnings remained limited to documented capacity aliasing/headroom
and excessive administrator-owned LVM archive retention. Transient pmxcfs
startup messages before Corosync became available were followed by healthy
three-node quorum and are not persistent service failures.

## Publication gate

Do not publish, tag, release, or upload this candidate until all pending gates
above pass on the three-node lab and the exact source/package hashes are
recorded. Unit success alone is not a production claim. No micro-release is
planned; accepted changes are accumulated into one qualified stable candidate.

The H3 functional gate uses
`lab/fix2-h3-remote-evidence-alias-gate.sh`. It bind-mounts a fake `dmsetup`
only inside a private mount namespace and performs no real device-mapper, LVM,
PVE, or storage mutation.

## Explicit non-claims

- This does not make stock shared LVM-thin safe.
- This does not permit two kernels to open the same thin-pool metadata.
- SSH success, owner tags, quorum, `thin_check`, or a canonical mapper name
  alone are not proof of safe activation.
- Root can still bypass any userspace policy manually; out-of-band mutation is
  detected where observable and is never silently blessed.
