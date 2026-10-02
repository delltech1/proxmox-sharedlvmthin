# SharedLvmThin RC5.87 TG53

## Experimental pre-release — disposable labs only

Both Thin and Thick Generations remain experimental. Use this release only on
disposable Proxmox VE hosts, disposable shared FC/FCoE/iSCSI storage and
disposable guest data. It is not production-ready, supported, certified or
warranted. Defects, operator error, incomplete fencing, storage or path
failure, incompatible updates and unqualified recovery can cause data loss,
corruption, unavailability or cluster downtime. Independently tested backups,
correct fencing, quorum, multipath and recovery remain operator
responsibilities.

## What changed since RC5.31 TG52

- Added policy-driven Proxmox update qualification. `WARN`, `FREEZE` and
  `QUALIFIED_AUTO` modes distinguish an advisory plan, an explicit hold of the
  guarded storage stack, and a strictly evidence-bound automatic path. An
  unlisted package tuple or incomplete evidence fails closed; a candidate can
  never qualify itself merely by being installed.
- Added durable, crash-recoverable VM-destroy planning and settlement around
  the qualified Proxmox destroy boundary. Unknown task outcomes remain pending
  and are not blindly redispatched or cleaned up.
- Tightened Lazy Thick lifecycle admission for resize, reattachment, Storage
  Move, restore and migration. Active transitions must first satisfy their
  explicit materialization or dormant-handoff contract.
- Added RAM-snapshot preflight for the affected upstream network/MTU query
  boundary. Empty or ambiguous network results are refused before dispatch,
  rather than permitting an invalid snapshot state.
- Expanded exact API 14 and API 15 contract inventory, package-profile parity,
  post-reboot qualification and recovery-state checks for both DUAL and
  Thick-only packages.
- Hardened disposable test runners so bounded observation does not kill or
  deactivate a slow storage worker, destructive tests require explicit object
  identity, and concurrent waves use exact task receipts and cleanup gates.

The storage topology remains unchanged: Thin and Thick require physically
separate dedicated shared VGs. Eager and Lazy Thick may share a dedicated
Thick VG. The DUAL and Thick-only packages are mutually exclusive, and every
participating cluster node must run the same package profile and version.

## Qualification envelope

The final source candidate passed 2,515 Python tests with one intentional skip
and 1,674 Perl tests. On API 14, the DUAL package passed a TG32-to-RC5.87
upgrade, reboot and clean-state restoration on a control-only node without a
SAN data plane. On API 15, the exact pre-sanitization DUAL lab artifact passed
a three-node rolling upgrade and reboot plus Thin, Eager and Lazy allocation;
single- and multi-disk snapshots; rollback and deletion; resize with zero-tail
verification; offline moves; supported online migration after Lazy
materialization; q35/OVMF, EFI, TPM2 and cloud-init configuration; native VMA
backup/verify/restore; concurrent snapshot waves; and final recovery checks.
The Thick-only profile has source, package, reproducibility and shared-payload
parity evidence in this round, not a new live SAN profile-cycle claim.

The public packages are rebuilt from the sanitized tagged source by GitHub
Actions. Sanitizing the public manifest changes the package bytes, so the
published artifacts must not be described as the exact artifacts installed in
the live lab. The release workflow repeats source privacy scanning, Python and Perl
tests, shell/static checks, two clean package builds, reproducibility checks,
archive validation and DUAL/Thick-only shared-payload comparison before it
publishes either artifact. Until their exact-byte live replay is performed,
the public artifacts have only these repeated offline package gates plus the
source-equivalent lab lineage described above. A changed hash, different
host-package tuple or different environment is outside the recorded evidence.

This is bounded disposable-lab evidence, not universal certification. It does
not establish safety for every array, firmware, kernel, LVM/device-mapper
version, multipath policy, topology, scale, workload or failure ordering.
Veeam testing is not part of this release qualification.

## Installation and upgrade

Choose exactly one package profile, verify `SHA256SUMS`, and install the
identical version on every participating node one node at a time. Before each
step run `sharedlvmthin upgrade-check`; after it, verify Doctor, recovery
checks, storage visibility and guest I/O before advancing. Do not bypass a
refusal simply because the package manager can resolve dependencies.

Read the [installation guide](installation.md),
[compatibility matrix](compatibility.md), [known issues](known-issues.md),
[local hold and release protocol](local-hold-release-protocol.md),
[Thick-only package boundary](thick-only-package.md), and
[risk and support boundary](RISK-AND-SUPPORT-BOUNDARY.md) before testing.
