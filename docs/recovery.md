# Recovery

Before a destructive test, record the known-good state, exact action, expected result, failure condition, recovery procedure, and post-test validation.

If a test fails: stop expansion, preserve evidence, identify the layer, restore the last known-good state, and validate both PVE nodes, quorum, LVM, storage, multipath, web, and test-object cleanup.

Rollback to RC3:

1. Preserve `/etc/pve-sharedlvmthin/web.conf` and the node-local TLS directory.
2. Verify the canonical RC3 DEB SHA256.
3. Install the same RC3 package on both nodes in a controlled maintenance window.
4. Confirm plugin syntax/API, restart only the optional dashboard if needed, and run Doctor.
5. Verify identical storage identity and active paths on both nodes.

Do not roll back while an RC4-only mutating operation is active. Do not restore node-specific private keys from a portable bundle.
