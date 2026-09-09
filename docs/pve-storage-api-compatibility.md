# PVE Storage API compatibility

SharedLvmThin advertises the exact running PVE Storage API when it is inside
the explicitly qualified range 14..15. An API 14 host therefore sees plugin
API 14 and an API 15 host sees plugin API 15, without a misleading older-API
warning. It never mirrors an unknown host API: registration and every mutating
hook fail closed outside `14..15`.

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
