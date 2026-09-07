# Thick volumes: future design note

RC4 is thin-only. Thick support would require a separately reviewed storage mode using classic fully allocated linear LVs, different snapshot/rollback semantics, capability reporting, free-space accounting, migration/storage-move tests, and mixed-mode ownership rules. It must not be added as a per-disk shortcut that weakens the current per-VM thin-pool lifecycle.
