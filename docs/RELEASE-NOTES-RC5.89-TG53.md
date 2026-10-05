# RC5.89 TG53

RC5.89 TG53 is an experimental Proxmox VE 9 compatibility-hardening release.
Use only disposable hosts, shared SAN storage and guest data. It is not
production-ready, certified, supported or warranted.

## Changes

- Adds a fail-closed lifecycle compatibility resolver for activation,
  allocation, snapshot/vmstate, resize, move/restore, migration and
  backup/restore operations.
- Selects an adapter only from an exact Storage API, API age and critical
  package-version tuple. Unknown and ambiguous tuples are blocked; there is no
  nearest-version fallback.
- Maps each operation family to its required contract, preflight and regression
  evidence, and integrates the resolver into the compatibility gate and CLI.
- Identity matching returns `RETEST_REQUIRED`; it never authorizes an upgrade
  or storage mutation by itself.

## Exact read-only runtime checks

The resolver selected one exact family on four disposable lab nodes:

| Storage API | API age | pve-manager | libpve-storage-perl | qemu-server | pve-qemu-kvm | libpve-common-perl | Result |
|---:|---:|---|---|---|---|---|---|
| 14 | 5 | 9.2.2 | 9.1.5 | 9.1.15 | 11.0.0-3 | 9.1.12 | `RETEST_REQUIRED` |
| 15 | 6 | 9.2.18 | 9.1.10 | 9.2.7 | 11.0.3-3 | 9.2.1 | `RETEST_REQUIRED` |
| 15 | 6 | 9.2.20 | 9.1.11 | 9.2.10 | 11.0.3-3 | 9.2.2 | `RETEST_REQUIRED` |
| 15 | 6 | 9.2.21 | 9.1.11 | 9.2.10 | 11.0.3-4 | 9.2.2 | `RETEST_REQUIRED` |

These are bounded identity and contract-selection checks, not new SAN
dataplane certification. Existing RC5.88 operation-scoped evidence remains
applicable only where the relevant runtime and storage-path semantics are
unchanged.

## Verification

- Python: 2,526 tests passed; 53 environment-dependent tests skipped.
- Perl: 1,674 tests passed.
- Public-source and package privacy checks passed.
- Reproducible DUAL and Thick-only builds passed.
- DUAL/Thick-only shared-payload parity passed.

Final package SHA-256 values are published with the GitHub pre-release after
the tagged release workflow rebuilds and validates both artifacts.
