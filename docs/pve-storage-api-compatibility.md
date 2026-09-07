# PVE Storage API compatibility

SharedLvmThin declares plugin API 14 and explicitly qualifies running PVE
Storage APIs 14 and 15. It does not dynamically mirror an unknown host API.
Every mutating hook revalidates that the runtime API is inside `14..15` and
fails closed before LVM mutation otherwise.

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

API 13 and API 16+ remain unsupported until separately audited and qualified.
