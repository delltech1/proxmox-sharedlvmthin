# Mutating-hook lock and safety coverage

Audited against PVE 9.2.11 / `libpve-storage-perl` 9.1.9, Storage API 15.
The source inspected was `/usr/share/perl5/PVE/Storage.pm` and
`PVE/Storage/Plugin.pm` on the known-good POC.

| Path | Mutation | PVE wrapper lock | Plugin lock | Quorum gate | Identity gate | Ownership gate | Postcondition | RC5 status |
|---|---|---|---|---|---|---|---|---|
| `vdisk_alloc` → `alloc_image` | pool/LV create, tag | Yes: `PVE::Storage::vdisk_alloc` | No; avoids double-lock | Yes | Yes | Existing pool only | Created object re-read; ambiguous partial state preserved | MOCK PASS; integration pending |
| `vdisk_free` → `free_image` | snapshot/LV/pool delete | Yes: `PVE::Storage::vdisk_free` | No; avoids double-lock | Yes | Yes | Yes, positive tag and relationship | Refresh before pool cleanup | MOCK PASS; integration pending |
| `volume_resize` | `lvextend` | No | Yes | Yes | Yes | Yes | Requested size re-read | MOCK PASS; integration pending |
| snapshot create | `lvcreate -s` | No | Yes | Yes | Yes | Yes | Snapshot/pool relationship re-read | MOCK PASS; integration pending |
| snapshot delete | `lvremove` | No | Yes | Yes | Yes | Yes, including candidate snapshot preflight | Absence re-read | MOCK + disposable integration PASS |
| snapshot rollback | create replacement, remove origin, rename | No | Yes | Yes | Yes | Yes plus rollback relationship checks | Replacement and final relationship checked | MOCK PASS across phase boundaries; integration pending |
| `activate_volume` | local LV activation state | No | No | No | No | No | LVM exit status | REVIEW: not shared metadata; must never create/repair |
| `deactivate_volume` | local LV activation state | No | No | No | No | No | LVM exit status | REVIEW: not shared metadata; must never delete/repair |
| empty-pool cleanup | `lvremove pool` inside `free_image` | Inherited `vdisk_free` lock | No | Yes | Yes | Positive pool tag plus zero references of any name | Refreshed LVM inventory | MOCK PASS; integration pending |
| dmeventd autogrow | `lvextend --use-policies` | Not through PVE wrapper | Yes: explicit `cfs_lock_storage` | Yes | Yes | Positive storage tag revalidated under lock | Size re-read after uncertain result | MOCK PARTIAL: core gates/event storm/reserve pass; cross-node and migration integration pending |

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

## Release blockers from this audit

1. Run real lock-contention qualification for hook-vs-hook and hook-vs-monitor paths.
2. Prove cross-node monitor serialization and post-migration monitor ownership.
3. Run disposable integration tests for the mock-qualified lifecycle boundaries.
4. Preserve zero unsafe fallback/cleanup as a regression requirement for lock,
   exception, postcondition, VG-loss, and quorum-loss failures.
