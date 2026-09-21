# Thick-only package profile

The Thick-only package is an experimental packaging profile of the same
SharedLvmThin source and Thick Generations implementation. It is not a fork,
does not introduce a second on-disk format and must not receive independent
storage logic. A correctness fix in shared Thick code therefore reaches both
the dual-mode and Thick-only packages.

The profiles are mutually exclusive:

- `pve-sharedlvmthin` exposes experimental Thin and Thick Generations modes;
- `pve-sharedlvmthin-thick` exposes only experimental Thick Generations;
- both use the `sharedlvmthin` PVE storage type and the same signed Thick
  anchor/generation schemas;
- Debian `Conflicts` and `Replaces` prevent both packages from owning the
  shared plugin files simultaneously.

The Thick-only profile removes Thin runtime services, helpers and migration
bridge from the binary package. Its plugin schema defaults to
`thick-generations`, does not advertise Thin-only properties, and rejects an
explicit Thin allocation mode. The public CLI and the internal recovery worker
also reject Thin operations so a direct helper invocation cannot bypass the
package boundary.

## Switching from the dual-mode package

Switching is permitted only after every `sharedlvmthin` storage is explicitly
configured as `thick-generations` and no managed Thin pool remains in any
backing VG. The pre-install script checks both conditions before files are
unpacked and refuses an ambiguous or unsafe switch. A Thin pool must be
migrated or deliberately removed with the dual-mode package; uninstalling its
runtime is never a migration procedure.

Run `sharedlvmthin upgrade-check` immediately before every rolling package
change. Every Thick anchor must be positively verified as `MATERIALIZED`; do
not replace plugin code while a hydration, rollback, deletion or recovery
transaction is in progress. Upgrade one cluster node at a time and verify the
installed package, PVE services, storage health and guest I/O before advancing.

## Release boundary

Building both profiles in CI proves that their package contents and common
source remain internally consistent. It does not make either profile
production-ready. The Thick-only artifact must remain unpublished until its
own install, upgrade, replacement, reboot and destructive disposable-storage
qualification gates pass. Both profiles remain experimental, unsupported and
limited to disposable lab hardware, storage and guest data.
