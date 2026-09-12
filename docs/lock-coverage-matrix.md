# Mutating-hook lock and safety coverage

Audited against PVE 9.2.11 / `libpve-storage-perl` 9.1.9, Storage API 15.
The source inspected was `/usr/share/perl5/PVE/Storage.pm` and
`PVE/Storage/Plugin.pm` on the known-good POC.

| Path | Mutation | PVE wrapper lock | Plugin lock | Quorum gate | Identity gate | Ownership gate | Postcondition | RC5 status |
|---|---|---|---|---|---|---|---|---|
| `vdisk_alloc` → `alloc_image` | pool/LV create, tag | Yes: `PVE::Storage::vdisk_alloc` | No; avoids double-lock | Yes | Yes | Existing pool only | Created object re-read; ambiguous partial state preserved | MOCK + DISPOSABLE INTEGRATION PASS |
| `vdisk_free` → `free_image` | snapshot/LV/pool delete | Yes: `PVE::Storage::vdisk_free` | No; avoids double-lock | Yes | Yes | Yes, positive tag and relationship | Refresh before pool cleanup | MOCK + DISPOSABLE INTEGRATION PASS |
| `volume_resize` | `lvextend` | No | Yes | Yes | Yes | Yes | Requested size re-read | MOCK + LINUX/WINDOWS INTEGRATION PASS |
| snapshot create | `lvcreate -s` | No | Yes | Yes | Yes | Yes | Snapshot/pool relationship re-read | MOCK + API-14/API-15 INTEGRATION PASS |
| snapshot delete | `lvremove` | No | Yes | Yes | Yes | Yes, including candidate snapshot preflight | Absence re-read | MOCK + disposable integration PASS |
| snapshot rollback | create replacement, remove origin, rename | No | Yes | Yes | Yes | Yes plus rollback relationship checks | Replacement and final relationship checked | MOCK + LINUX/WINDOWS INTEGRATION PASS |
| `activate_volume` | local LV activation state | No | No | No | No | No | LVM exit status | REVIEW: not shared metadata; must never create/repair |
| `deactivate_volume` | local LV activation state | No | No | No | No | No | LVM exit status | REVIEW: not shared metadata; must never delete/repair |
| empty-pool cleanup | `lvremove pool` inside `free_image` | Inherited `vdisk_free` lock | No | Yes | Yes | Positive pool tag plus zero references of any name | Refreshed LVM inventory | MOCK + DISPOSABLE INTEGRATION PASS |
| dmeventd autogrow | `lvextend --use-policies` | Not through PVE wrapper | Yes: canonical VG lock | Yes | Yes | Positive storage tag revalidated under lock | Size re-read after uncertain result | UNIT + SAME-VG LOCK INTEGRATION PASS; DAEMON RECOVERY IS MANUAL |

## PVE wrapper evidence

- `PVE::Storage::vdisk_alloc` activates storage and invokes `alloc_image` inside
  `cluster_lock_storage`.
- `PVE::Storage::vdisk_free` invokes reference checks and `free_image` inside
  `cluster_lock_storage`.
- `PVE::Storage::volume_resize`, `volume_snapshot`,
  `volume_snapshot_delete`, and `volume_snapshot_rollback` dispatch directly to
  the plugin. RC5 therefore owns those locks.
- For shared storage, `PVE::Storage::Plugin::cluster_lock_storage` delegates to
  `PVE::Cluster::cfs_lock_storage`; for non-shared storage it uses a local lock.

## Advertised and indirect paths

The plugin does not advertise a custom clone/template mutation hook. Generic
copy/restore paths ultimately allocate through `alloc_image` and delete through
`free_image`, so they inherit the PVE wrapper locks but still require lifecycle
fault qualification. Activation/deactivation only changes local LV activation
state and must never contain creation, deletion, initialization, or repair.

## Remaining qualification boundary

The lifecycle and same-VG contention items from the original audit have live
disposable evidence. Automatic dmeventd recovery after daemon loss remains
deliberately unsupported: an administrator must revalidate and re-enrol exact
owned pools. Zero unsafe fallback or speculative cleanup remains a permanent
regression requirement for lock, exception, postcondition, VG-loss and
quorum-loss failures.
