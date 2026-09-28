# PVE/upstream compatibility gate

Status: **incremental implementation; not yet an upgrade authorization**. The currently packaged
`sharedlvmthin compat-check` is a narrower read-only host check. Its
`COMPATIBILITY=PASS` must never be translated into the full `COMPATIBLE`
verdict defined here.

Every release and every proposed Proxmox upgrade must run one reproducible
compatibility gate before package replacement and again after the upgraded node
reboots. Successful package installation alone is not compatibility evidence.

The gate has three layers:

1. **Static package/source contract** — record exact installed and candidate
   versions; download/extract candidates without installing; compare PVE
   Storage APIVER/APIAGE, every plugin hook signature, helper/library symbol
   and the source semantics on which the plugin relies.
2. **Read-only host contract** — run the packaged compatibility checker,
   Doctor, recovery inventory and exact parsers against real command output.
   Capture kernel dm targets, LVM fields/values, multipath/iSCSI state, quorum,
   services and package profile without modifying storage.
3. **Disposable mutation regression** — only in the isolated lab, exercise
   allocation/free, activation/deactivation, snapshot/delete/rollback, resize,
   backup/restore, offline and live migration, interrupted operations,
   dm-clone hydration/fault recovery, Thin single-owner guards, Thick generation
   transitions and DUAL/Thick-only package lifecycle.

## Required dependency inventory

The version record must include at least:

- `libpve-storage-perl`, `pve-manager`, `qemu-server`, `pve-qemu-kvm`;
- every installed and candidate `proxmox-kernel` package;
- `pve-cluster`, `libpve-cluster-api-perl`, `corosync`, `pve-ha-manager`;
- `libpve-common-perl`, `pve-container`/LXC when `rootdir` is enabled;
- `lvm2`, libdevmapper/dmsetup/dmeventd, `thin-provisioning-tools`, udev,
  `multipath-tools`, open-iscsi, OpenSSH, util-linux, coreutils and systemd;
- SQLite/libsqlite3 for any pmxcfs durability claim, plus the exact running
  kernel and loaded module identities rather than installed kernels alone;
- the exact plugin commit, package profile and package SHA-256.

## Contract catalogue

Tests must cover each dependency instead of assuming a command still behaves
because its name exists:

- PVE Storage API 14–15 policy, hook presence, argument count/context and return
  semantics used by allocation, path, activation, lifecycle and feature probes;
- storage configuration parsing, node scope, pmxcfs quorum/read-only behavior,
  distributed locks and any future atomic admission primitive;
- PVE VM lock, migration, replication, backup/restore and QMP ownership
  assumptions used by the bridge and Thick lifecycle;
- exact `lvs/vgs/pvs` fields, JSON/text parsing, tags, activation state and
  zeroing semantics; every mutating LVM command and its expected postcondition;
- exact `dmsetup` target/status/table/event/message/suspend/resume semantics,
  inactive tables, UUID/dependency discovery and dm-clone target version;
- multipath identity/path evidence, iSCSI sessions, udev convergence and block
  sizes; no single-path fallback may be silently reclassified safe;
- systemd unit/cgroup lifecycle, invocation identity, D-state reporting and
  package maintainer-script ordering;
- RAW disk compatibility with QEMU, backup/restore and cross-mode transitions;
- both DUAL and Thick-only payloads, clean install, profile replacement,
  upgrade, failed-upgrade, removal refusal, reboot and Web UI registration.

## Verdicts

- `COMPATIBLE`: an existing complete baseline manifest matches the exact plugin
  and package hashes, relevant configuration and complete dependency tuple;
  exact static/read-only contracts pass and all required disposable regressions
  for changed plugin code, dependencies and dependency combinations pass.
- `RETEST_REQUIRED`: no contradiction is found, but a relevant version,
  signature, kernel target or semantic dependency changed and its mutation
  regression has not completed.
- `BLOCKED`: missing/changed contract, ambiguous evidence, failed regression,
  unsupported API/kernel/target, unsafe package lifecycle or unreconciled
  runtime state.

Unknown, timeout and incomplete node coverage are never `COMPATIBLE`. The gate
must emit a machine-readable result with exact versions, checks, evidence paths
and required follow-up. A release manifest records which exact gate revision
qualified which exact package hashes.
Missing baseline, an uncovered plugin change or an unqualified combination is
at least `RETEST_REQUIRED`; a contradicted safety contract is `BLOCKED`.

## Upgrade sequence

1. Refresh metadata only and save installed/candidate inventory.
2. Extract candidate packages and run static comparison without installation.
3. Classify changed contracts and run the matching disposable lab matrix.
4. Refuse production/lab rolling upgrade unless the verdict permits it.
5. Qualify the exact old/new mixed-version pair first. Upgrade one disposable
   node, reboot it, rerun read-only and mutation gates, then validate migration
   and recovery before authorizing the next rolling step.
6. Preserve all evidence and never auto-publish or auto-install from the gate.

The intended implementation will version and test the catalogue: adding a new
PVE, LVM, dmsetup, systemd or kernel-dependent code path must eventually add
its contract and regression before CI can pass. The present string-level source
test protects this specification from disappearing; it is not that mechanism.

The versioned machine-readable catalogue is now shipped as
`pve-compatibility-contracts.json`. `sharedlvmthin contract-check` rejects
duplicate/malformed entries, empty evidence classes and missing referenced
source tests. The upstream inventory records the exact catalogue digest and
contract IDs. A reviewed qualification must contain that exact digest and the
complete set of passed IDs; a free-form `lab_matrix_id` alone cannot qualify a
node. The catalogue includes explicit mixed-version rolling-upgrade and
large-cluster bounded-fanout contracts. The dm-clone fault and Thick generation
contracts also require independent `suspended-pivot-recovery` and
`suspended-resize-recovery` evidence. Their production-inert C10/R0 boundaries
prevent a green upgrade result based only on the earlier pre-suspend C0-C9
matrix. Both affected contract revisions are `2`, so consumers that track
per-contract revisions cannot mistake the expanded obligation set for the
earlier revision even before comparing the full catalogue digest.

This establishes coverage accounting, not physical proof. Entries in
`lab_tests` remain obligations until result manifests with exact node/boot,
package/configuration fingerprints, parameters and evidence digests are
implemented and collected. Until then the overall result remains
`RETEST_REQUIRED`; installation success never promotes it.

`sharedlvmthin compatibility-gate --phase pre-upgrade|post-reboot
--collection-plan PLAN.json --output-dir DIR` now creates the first atomic
per-node evidence bundle. It
runs the catalogue validator, complete upstream inventory, legacy compatibility
and recovery-aware upgrade checks, plus optional static candidate inspection.
Every subprocess has a deadline, isolated process group, fixed environment and
bounded retained stdout/stderr. Timeout, truncation, missing output or a failed
required step is `BLOCKED`. A completely clean local bundle is still only
`RETEST_REQUIRED` and always reports `upgrade_authorized=false`; cluster-wide
aggregation and physical mutation/mixed-version results remain mandatory.

`sharedlvmthin compatibility-aggregate` consumes only explicitly named bundles
from that exact collection plan. The plan fixes cluster/run/upgrade identities,
a bounded validity interval, collector digest, candidate artifacts and every
eligible node's expected storage-configuration scope. The aggregator recomputes
evidence hashes, tool/exit/terminal semantics, canonical contract fingerprints,
start/end boot and configuration continuity, exact node/storage coverage,
candidate agreement and cross-node collection spread. It performs no SSH or
implicit report discovery, which keeps fanout orchestration separate and
bounded for large clusters. Its observed contract classes are not qualified
classes; its maximum verdict remains `RETEST_REQUIRED`.

`sharedlvmthin lab-evidence-check` derives exact class and directed-transition
obligations from the versioned catalogue and collection plan. It opens every
explicitly referenced bounded regular evidence file, verifies size and SHA-256,
rejects duplicate JSON keys, and validates fixture, participant, running
software, fault-confirmation, independent-oracle, forbidden-action and exact
reconciliation evidence. Unknown, missing, failed or ambiguous executions
block the evaluation. Even complete coverage yields only
`LAB_EVIDENCE_COMPLETE` with `upgrade_authorized=false`; a later evaluator must
still establish freshness, sequencing and current all-node readiness. Evidence
hashing provides integrity, not authentication of the reporting host/operator,
and the manifest cannot by itself prove that an attempt was omitted.

## Implemented first layer

The candidate command `sharedlvmthin upstream-inventory` now implements the
local read-only software/configuration tuple. It records installed and cached
candidate package versions with architecture/status, installed kernels and the
running kernel, PVE Storage API/hook presence, dm target versions, exact hashes
of required command binaries and critical loaded source paths, package profile
and a redacted PVE-parser-derived storage configuration contract.

The emitted document is `kind=inventory`. Comparison accepts only a separately
reviewed `kind=qualification` document with the same schema/catalogue and exact
contract fingerprint. An inventory cannot qualify itself. Missing or changed
qualification returns `RETEST_REQUIRED`; mandatory collection failure returns
`BLOCKED`. Even an exact reviewed match is node-scoped and emits
`cluster_upgrade_authorized=false`.

This is deliberately not the complete gate yet. Runtime compat/recovery marker
validation, all-node aggregation, candidate-package extraction/source diff and
the disposable mutation matrix remain separate required layers. Therefore the
command is useful evidence collection, not authorization for rolling upgrade.

The second candidate command,
`sharedlvmthin candidate-inspect --current-inventory REPORT PACKAGE.deb...`,
is also static-only. It refuses insecure APT policy, verifies each exact
package identity and bytes against APT SHA-256/size metadata, reads the data tar
as a bounded stream and hashes only allowlisted regular source members. It does
not extract an overlay or load candidate Perl. It records changed relevant
sources and dependency relationships, but explicitly leaves dependency closure
incomplete and can return only `RETEST_REQUIRED` or `BLOCKED`.
