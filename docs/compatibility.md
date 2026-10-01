# Compatibility

RC5.77 TG53 is intended exclusively for **Proxmox VE 9**. Its plugin hooks
retain the Storage API 14 and 15 source contract, but the **current RC5.77
artifact is qualified only on the exact API 15 tuples listed below**. API 14
has historical control-plane/package evidence through RC5.69 and an RC5.74
retest candidate; RC5.77 on API 14 is currently `RETEST_REQUIRED` and its
runtime qualification gate must fail closed. PVE 8 and earlier are
unsupported. A package being installable or compiling is not evidence that a
new PVE package tuple is compatible with storage mutations.

| PVE | Storage API | Plugin API | Status |
|---|---:|---:|---|
| 9.2.x | 14 | 14 | Hook-compatible; RC5.77 exact artifact is `RETEST_REQUIRED` |
| 9.2.x | 15 | 15 | Lifecycle, package-update, reboot and endurance qualification |

## Exact TG53 qualification tuples

The table records what was actually exercised. `Exact lab-tested` means the
listed runtime tuple was used for the stated scope; it is not certification of
every SAN, topology or failure ordering. `Contract-compatible` is a narrower
source/read-only result and must not be described as exact lab qualification.
`Retest required` blocks a compatibility claim until the listed disposable
regressions pass.

| Scope | pve-manager | libpve-storage-perl | qemu-server | pve-qemu-kvm | Kernel | libpve-common-perl | Status |
|---|---|---|---|---|---|---|---|
| Historical API 14 clean install and in-place plugin upgrade (through RC5.69; not RC5.77) | 9.2.2 | 9.1.5 | 9.1.15 | 11.0.0-3 | 7.0.2-6-pve | 9.1.12 | Historical exact control-plane/package evidence only; RC5.77 remains `RETEST_REQUIRED` and the node had no SAN dataplane |
| API 15 DUAL SAN lifecycle, rolling plugin upgrade and reboot | 9.2.18 | 9.1.10 | 9.2.7 | 11.0.3-3 | 7.0.14-16-pve | 9.2.1 | Exact lab-tested on the recorded disposable SAN scope |
| API 15 updated SAN node | 9.2.20 | 9.1.10 | 9.2.7 | 11.0.3-3 | 7.0.14-17-pve | 9.2.2 | Exact TG53 package gate, Thick RAM-snapshot clone/rollback, RAW-to-Thin and QCOW2-to-Thick two-disk import, hashes and cleanup passed; broader tuple remains targeted-retest scope |
| Current SAN candidate | 9.2.20 | 9.1.11 | 9.2.10 | 11.0.3-3 | 7.0.14-17-pve | 9.2.2 | RC5.77 package/runtime gate; Thin/Eager/materialized-Lazy lifecycle, supported migration, multi-mode resize, supported RAM snapshot/rollback/delete, exact canaries and cleanup passed; explicitly blocked configurations remain documented below |
| Current no-SAN package/profile node | 9.2.21 | 9.1.11 | 9.2.10 | 11.0.3-3 | 7.0.14-19-pve | 9.2.2 | RC5.77 DUAL/Thick-only build/profile checks and guarded package transition passed; SAN dataplane is not in scope |

`libpve-storage-perl 9.1.11` and `qemu-server 9.2.10` are a coherent
upstream pair. Storage 9.1.11 declares `Breaks: qemu-server (<< 9.2.10)` due
to the changed decompressor/VMA restore argument contract. Do not force or
qualify storage 9.1.11 with an older qemu-server.

Every future release note must include an equivalent exact table. Operators
must treat any unlisted storage, QEMU, kernel, LVM/device-mapper or cluster
tuple as `RETEST_REQUIRED` unless a reviewed contract qualification explicitly
states a narrower compatible scope.

### Operation-scoped qualification

Newer TG53 manifests additionally classify the exact runtime by operation
group.  `sharedlvmthin update-plan` reports this matrix without changing APT,
storage or guest state.  The allowed states are:

- `QUALIFIED`: the exact operation/runtime combination has named executable
  evidence;
- `RETEST_REQUIRED`: partial evidence may exist, but the complete named test
  set has not passed;
- `BLOCKED`: a known semantic boundary prevents a compatibility claim;
- `NOT_IN_SCOPE`: the qualification node cannot exercise that data path.

The matrix must cover every declared operation group exactly.  A missing group,
an evidence-free `QUALIFIED` result, or a blocked result without named tests and
a reason makes the update plan invalid.  A partial success is recorded but is
never promoted: for example, one successful materialized Thick-Lazy migration
does not qualify Thin ownership transfer, dormant Lazy handoff, every Thick
generation, or an untested mixed-version direction.

This is initially a diagnostic and test-selection layer.  Existing package and
runtime latches remain the authoritative mutation boundary; no operation is
unblocked solely because a JSON matrix says `QUALIFIED`.  Runtime enforcement
will be introduced only after exact evidence digests and rolling-edge bindings
are implemented and crash-tested.

Storage transports:

- iSCSI multipath: tested in the POC.
- iSCSI reboot qualification requires every currently managed path to map to
  one exact persistent node record with automatic startup and an enabled
  `open-iscsi.service`. Live sessions alone do not prove reboot persistence.
- Linux FCoE/LIO multipath: experimentally tested; target-side `tcm_fc` total-loss recovery remains a known infrastructure issue.
- Physical FC/enterprise SAN: architecturally supported when shared-block
  requirements are satisfied, but not hardware-qualified by this POC. Treat
  it as unqualified until validated on the intended HBA, fabric, array,
  firmware and multipath profile.

The explicit tested Storage API range is 14..15 on Proxmox VE 9. API 13 and
API 16+ remain
unsupported until their exact hook/semantic delta is audited and qualified;
the plugin advertises the exact host API only inside this qualified range and
does not claim an unknown API merely to suppress a PVE warning.

## Upstream source audit boundary

The TG53 lineage was compared with the authoritative upstream repositories on
2026-09-21. A follow-up audit on 2026-09-29 reviewed the exact package delta
to `libpve-storage-perl 9.1.11`, `qemu-server 9.2.10`, `pve-manager 9.2.20`,
`libpve-common-perl 9.2.2` and kernel `7.0.14-19-pve`.

- `pve-storage` `b519de3c117c95b508ce20bb0230f0002e61be0c`;
- `qemu-server` `7b44050a7a66451954dc53b2d38e5834b01777ab`;
- `pve-container` `3a26da6db5e949418b2ec4eb6b383450feb7064d`.

The follow-up found no Storage API or directly used hook-signature change: the
current API remains APIVER 15/APIAGE 6. It did find a wider behavioral delta:

- storage 9.1.11 and qemu-server 9.2.10 have a coupled decompressor/VMA
  contract and must not be rolled independently;
- inherited qcow2 `raw+size` export changed from `qemu-img convert` to
  `qemu-img dd`, including different progress behavior;
- qemu-img paths now terminate options explicitly with `--`;
- worker-context generic image resize may wait up to one hour, while the
  plugin's own RAW resize contract remains separately bounded;
- vmstate allocation now occurs after runtime machine/CPU/network queries;
- qemu-server 9.2.8 and newer refuse machine versions older than 5.0;
- PCI bridge discovery and live hotplug changed. A running LSI VM without
  `pci.4` cannot accept a high-slot disk because the new hotplug path refuses
  to create the missing bridge; this includes the rolling-update case and can
  also occur on QEMU 11.0 when the VM started without a high-slot disk;
- libpve-common 9.2.2 strictly rejects newline-tainted property strings.

The exact new tuple therefore remains `RETEST_REQUIRED` until stream
framing/hash, compressed VMA file/stdin restore, snapshot failure cleanup,
running/stopped resize, old-born high-slot hotplug, q35/i440fx cold start,
old-born and new-born mixed-version migration, strict-parser zero-effect
refusal, aligned/misaligned I/O and reboot regressions complete on disposable
storage.

The 2026-09-30 candidate tests additionally proved this migration boundary on
the 9.2.20/storage 9.1.11/qemu-server 9.2.10 tuples:

| Volume state | Online migration | Offline handoff |
|---|---|---|
| Thin, source owned | Direct overlap refused; use the Materialized Migration Bridge | Passed after source deactivation |
| Eager/materialized Thick | Passed k17 -> k19 -> k17 with exact data canary | Ordinary Thick contract |
| Lazy, unmaterialized and running | Refused before target activation; materialize on the active source first | Different lifecycle |
| Lazy, materialized | Passed k17 -> k19 -> k17 with exact data canary | Ordinary Thick contract |
| Lazy, stopped `LAZY_DORMANT` | Not applicable | Guarded handoff; qualify separately from live migration |

`sharedlvmthin migration-preflight VMID TARGET [--online]` is a read-only
per-volume report for this matrix. It does not patch or replace PVE migration
or HA orchestration; the storage activation ownership gate remains
authoritative if the preflight is omitted.

The empty `running-nets-host-mtu:` RAM-snapshot defect was reproduced with
both qemu-server 9.2.7 and 9.2.10 for no-NIC and e1000-only guests. It is a
latent upstream snapshot/config inconsistency, not evidence of a new 9.2.10
regression and not repaired by downgrading. Such snapshot configurations must
remain an explicit negative qualification cell. The read-only update plan now
binds each finding to its raw-config digest and exact section, but deliberately
does not repair it: no-NIC-at-snapshot and VirtIO-QMP-failure histories cannot
be distinguished safely from the current config alone, and inventing or
deleting resume metadata can change the RAM-state device contract. Native Lazy VMA restore also
remains fail-closed: PVE activates newly allocated restore volumes before the
final guest disk reference is published, and the activation hook has no
trusted restore identity. Do not weaken the Lazy discard/reference guard to
make that path appear functional.

The native snapshot-orchestration fixture additionally records the actual
`PVE::QemuConfig` effect order. The API14/qemu-server 9.1.15 reference allocates
vmstate before querying the running machine, CPU and MTU; qemu-server 9.2.10
queries all three first. Both tested versions couple `savevm-end` and vmstate
deactivation in one exception block and then poll `query-savevm` without an
explicit deadline. The integrated API15 fixture proves two distinct failure
postconditions: warning-only `savevm-end` can leave an unreferenced open
vmstate after forced config cleanup, while a thrown final `query-savevm`
retains the prepared snapshot section and `lock: snapshot` and replaces the
earlier disk error. `sharedlvmthin update-plan` fingerprints the loaded source
and reports these contract properties. They are qualification boundaries, not
authorization for the plugin to patch PVE code or automatically remove an
unreferenced state volume.

This records the reviewed source boundary; it is not a permanent assertion
about newer upstream commits. Any change to storage API/API age, lifecycle
hooks, migration/snapshot ordering, activation semantics or container
filesystem-freeze orchestration reopens compatibility qualification. Package
installation success alone never closes that gate.
