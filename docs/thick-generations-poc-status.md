# Thick Generations qualification summary

This is a sanitized public summary, not a raw laboratory log. It contains no
hostnames, addresses, storage identities, transaction identifiers, guest data
or operator paths. Detailed evidence is retained privately and is not part of
the public source or release assets.

Thick Generations remains experimental software for disposable laboratory
hosts, storage and guest data. Recorded tests reduce uncertainty only for the
exact tested envelope; they do not certify other arrays, firmware, kernels,
multipath policies, topologies, scale, workloads or failure orderings.

Publicly claimable coverage includes:

- Eager and Lazy allocation, materialization and verified linear pivot;
- snapshot, rollback, resize, delete and exact-cleanup lifecycle paths;
- online materialized-Thick migration and cross-mode Storage Move;
- controlled package install, rolling upgrade, reboot and recovery gates;
- PVE Storage API 14 and 15 compatibility checks;
- two-node warned operation and multi-node quorum-aware operation;
- identity, ambiguity, path-loss, timeout and interrupted-operation refusals;
- reproducible DUAL and Thick-only packages with shared-payload parity.

Historical 4 KiB transition geometry remains a compatibility state and is not
the default for new transitions:

```text
LEGACY_FOREGROUND_REGION_SIZE_4_KIB=PASS
```

The current candidate uses a 1 MiB region default. Legacy objects retain their
signed original geometry; an upgrade does not rewrite it. Timing results from
one environment are not portable performance guarantees.

See the current release notes, compatibility guide, known issues, installation
guide and risk/support boundary for the exact public claim and limitations.
