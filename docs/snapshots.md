# Snapshots

The plugin supports snapshot create, delete, and rollback for current VM disks. Linked clones are not advertised.

RC4 serializes snapshot mutations with the PVE storage lock. Rollback first creates a temporary replacement from the selected snapshot, then removes the current disk and renames the replacement. This avoids the RC3 failure mode where the current disk was removed before a replacement existed. If the final rename fails after origin removal, the replacement is deliberately preserved and named in the error for manual recovery.

Snapshot operations require adequate thin-pool metadata/data space. A failed operation must be followed by an orphan/postcondition inspection before retrying.
