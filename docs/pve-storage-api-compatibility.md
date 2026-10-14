# PVE Storage API compatibility

The current development source recognizes PVE Storage APIs **14, 15 and 16**
and advertises the exact running API inside that explicit range. It never
mirrors an unknown host API. APIs below 14 or above 16 are refused.

**Recognized API is not a qualification certificate.** A source-level adapter,
successful registration or package installation does not establish safe SAN
lifecycle behavior. Consult the [compatibility matrix](compatibility.md) and
the notes for the exact published artifact, package/kernel tuple and tested
operation. Development candidates and unlisted tuples remain
`RETEST_REQUIRED`; this document does not retroactively add API 16 support to
an older published package.

The source audit compared `libpve-storage-perl` 9.1.5 (API 14) with 9.1.10
(API 15). The only changed hook signature used by SharedLvmThin is:

```text
API 14: volume_resize($scfg, $storeid, $volname, $size, $running)
API 15: volume_resize($scfg, $storeid, $volname, $size, $running, $snapname)
```

The implementation accepts both forms. An absent API 14 argument becomes
`undef`. API 15 snapshot resize is explicitly refused because SharedLvmThin
does not implement snapshot-as-volume-chain semantics. Allocation, deletion,
activation, snapshot create/delete, and rollback signatures used by the plugin
did not change in this API transition.

The API 16 adapter preserves RAW block-volume names during native format/name
reconciliation, including vmstate names containing dots. It does not append
file-format extensions or enable qcow2/VMDK on the block backend. This is an
explicit adapter, not a replacement for snapshot, migration, resize and
backup/restore qualification against the actual upstream callers.

For a rolling platform transition, first install the same qualified bridge
plugin on every participating node while the old platform is still running.
Then update and reboot one evacuated node at a time, checking quorum, package
identity, SSH, storage paths and runtime qualification before moving workloads
to it or updating the next node. Exact mixed-platform migration coverage must
be established; compatible API numbers alone are insufficient. Plugin package
replacement and platform/kernel maintenance are separate operations. Neither
an APT guard nor a passing bounded I/O witness guarantees zero latency or
eliminates the need for a tested recovery plan.
