# Recovery

Before a destructive test, record the known-good state, exact action, expected result, failure condition, recovery procedure, and post-test validation.

If a test fails: stop expansion, preserve evidence, identify the layer, restore the last known-good state, and validate both PVE nodes, quorum, LVM, storage, multipath, web, and test-object cleanup.

Package rollback:

1. Preserve `/etc/pve-sharedlvmthin/web.conf` and the node-local TLS directory.
2. Confirm that no Thick Generations transaction or Thin metadata mutation is
   active and that the target package supports every on-disk schema in use.
3. Verify the canonical target DEB against its published `SHA256SUMS`.
4. Install the identical package on every participating node, one node at a
   time, in a controlled maintenance window.
5. Run `sharedlvmthin compat-check`, Doctor and every applicable
   `recovery-check`; require exact storage identity and healthy paths before
   allowing mutations.

Never downgrade across an unsupported anchor or storage-format boundary. If
compatibility cannot be positively proven, stop and retain the current package.
Do not restore node-specific private keys from a portable bundle.
