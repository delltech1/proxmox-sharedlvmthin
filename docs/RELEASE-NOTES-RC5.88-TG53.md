# RC5.88 TG53

RC5.88 TG53 is an experimental Proxmox VE 9 compatibility and safety release.
Use only disposable hosts, shared SAN storage and guest data. It is not
production-ready, certified, supported or warranted.

## Changes

- Adds an exact compatibility tuple for the current Proxmox VE 9 API 15 stack
  and retains conservative API 14 control-plane coverage.
- Extends immutable upstream inventory to the running kernel image, initramfs,
  required device-mapper modules, QEMU system binary and VMA tooling.
- Keeps older supported API 15 transitions visible to the guarded package
  upgrade workflow instead of closing them as an unknown plugin version.
- Fixes complete loaded-PVE-module accounting in the upstream inventory.
- Makes the hydration observer fail closed when its exact worker disappears or
  `/proc` identity becomes unreadable.
- Refuses running Storage Move to Lazy Thick, EFI/TPM-to-Lazy moves and unsafe
  discard/write-zeroes source policies before allocation.

## Exact compatibility evidence

The final DUAL artifact was installed and exercised on this exact updated SAN
canary tuple:

| Component | Version |
|---|---|
| Proxmox VE manager | 9.2.21 |
| Storage API / `libpve-storage-perl` | API 15 / 9.1.11 |
| `qemu-server` | 9.2.10 |
| `pve-qemu-kvm` | 11.0.3-4 |
| kernel | 7.0.14-20-pve |
| `libpve-common-perl` | 9.2.2 |
| LVM | 2.03.31-2+pmx1 |
| multipath-tools | 0.11.1-2 |

The guarded update first refused an unapproved watched-stack change, accepted
only its exact authorized digest, required a reboot, and left the new runtime
closed until the exact RC5.88 artifact was re-qualified. The following bounded
lab operations then passed:

- 103 compatibility checks and recovery checks for all six assigned SAN
  storage definitions;
- ten Thin/Eager snapshot create/delete cycles and post-wave recovery;
- expected fail-closed refusal for an unmaterialized Lazy snapshot;
- Thin to Eager to Lazy to materialized Lazy to Thin Storage Move with an
  unchanged SHA-256 data witness;
- native running Lazy snapshot-mode backup and Eager restore with an unchanged
  SHA-256 data witness;
- an Eager Thick online migration round trip between the updated node and an
  older API 15 node, preserving its SHA-256 data witness;
- direct Thin online migration refusal followed by a successful offline handoff
  and return, preserving its SHA-256 data witness.

API 14 was separately tested for clean package/control-plane update-policy
behavior without SAN dataplane access. It remains `RETEST_REQUIRED` for SAN
operations and must not be described as dataplane-qualified.

## Package verification

- Python: 2,517 tests passed, 1 skipped.
- Perl: 1,674 tests passed.
- Reproducible DUAL and Thick-only builds passed.
- DUAL/Thick-only profile parity and source/package privacy checks passed.

Final package SHA-256 values:

```text
c81e3bb5910c1c16a8a813c0bd2e72edbab0b59654ab684de280e6bf6fef93aa  pve-sharedlvmthin_0.9.0~rc5.88~tg53_all.deb
89acfa77356d48e856fcebf1cc6a59f3b516d408289d7312385bbaafcaacb7b0  pve-sharedlvmthin-thick_0.9.0~rc5.88~tg53_all.deb
```

Compatibility evidence is deliberately operation-scoped. Different SAN
arrays, firmware, multipath policies, package versions, kernels, cluster
topologies and failure orderings require their own qualification.
