# Thick-only package profile

The Thick-only package is an experimental packaging profile of the same
SharedLvmThin source and Thick Generations implementation. It is not a fork,
does not introduce a second on-disk format and must not receive independent
storage logic. A correctness fix in shared Thick code therefore reaches both
the dual-mode and Thick-only packages.

CI also evaluates fixed fixtures for the published TG32 object-key derivation,
LV and mapper names, ordered anchor/generation/transition/VG-intent tags and
their signed digests. Changing one of those fixtures is a persistent-format
change and requires an explicit migration and mixed-version qualification; it
cannot be accepted as ordinary package-profile maintenance.

The profiles are mutually exclusive:

- `pve-sharedlvmthin` exposes experimental Thin and Thick Generations modes;
- `pve-sharedlvmthin-thick` exposes only experimental Thick Generations;
- both use the `sharedlvmthin` PVE storage type and the same signed Thick
  anchor/generation schemas;
- Debian `Conflicts` and `Replaces` prevent both packages from owning the
  shared plugin files simultaneously.

The Thick-only profile removes Thin runtime services, helpers and migration
bridge from the binary package. It also omits bridge-only helpers and the
daemon-only ThinGuard/mobility Perl modules, rather than shipping dormant
operational components. Its plugin schema defaults to
`thick-generations`, does not advertise Thin-only properties, and rejects an
explicit Thin allocation mode. The public CLI and the internal recovery worker
also reject Thin operations so a direct helper invocation cannot bypass the
package boundary. Thick-only CLI help advertises only common diagnostics and
Thick recovery commands; Thin command names remain visible only in the Dual
profile.

The package also enforces a storage-topology boundary. Its `slt-vg-layout`
schema accepts only `isolated`, and its runtime rejects both Thin allocation
mode and a `mixed` layout even if an invalid configuration bypasses normal PVE
schema validation. A Thick-only VG may expose Eager and Lazy Thick aliases,
but it must not share its VG with a managed Thin failure domain.

The Dual package enforces the same boundary: Thin and Thick VGs must be
physically separate and only `slt-vg-layout isolated` is operational. The
retired `mixed` token remains parser-compatible for remote rolling-upgrade
configuration but is refused before every relevant operation. There is no
mixed compatibility opt-in in TG53. Eager and Lazy aliases may share one
dedicated Thick VG; Thin storage must resolve to another pinned VG.

After a clean install or profile replacement, `postinst` independently checks
that the excluded Thin/bridge executables are absent and that systemd reports
ThinGuard as `inactive` or `failed`. Stale payload, an active guardian or
unavailable runtime state refuses package configuration instead of declaring a
Thick-only node ready.

## Switching from the dual-mode package

Switching is permitted only after every `sharedlvmthin` storage is explicitly
configured as `thick-generations` and no managed Thin pool remains in any
backing VG. The pre-install script checks both conditions before files are
unpacked and refuses an ambiguous or unsafe switch. It also refuses every
partial or malformed VG inventory: a successful but incomplete LVM scan is
not proof that managed Thin objects are absent. A Thin pool must be
migrated or deliberately removed with the dual-mode package; uninstalling its
runtime is never a migration procedure.

When an older SharedLvmThin package is present, the pre-install script also
runs its bounded read-only upgrade gate and refuses any pending, active or
failed transient Thick materialization unit. A package replacement therefore
cannot remove entry points while a known asynchronous worker is unfinished.
The gate never resumes, repairs or deletes a transaction. Missing `systemctl`
or an unreadable unit inventory is a refusal, not evidence that no worker is
running.

The same pre-unpack transaction and recovery fence applies to ordinary
upgrades and to a Thick-only-to-dual replacement. During removal of the dual
profile, dpkg also stops and disables ThinGuard so no orphaned in-memory
guardian can remain after its executable and unit are removed. This service
cleanup is not a storage mutation and does not deactivate guest volumes. The
old Dual package refuses removal whenever any managed Thin object exists even
if ThinGuard is already stopped or failed, and a partial VG inventory is never
treated as an empty inventory. An unavailable or ambiguous systemd state also
refuses removal; an active guardian must reach a positively re-read stopped
state before its unit and executable can be removed.

An ordinary uninstall of either profile additionally refuses while any
`sharedlvmthin` storage definition, signed Thick LV, Thick transition object,
VG mutation intent, managed Thin object, partial VG or unreadable inventory
remains. This prevents removal of the plugin and recovery CLI from stranding
managed data. Maintainer-script lifecycle arguments do not authorize an
exception: real dpkg conflict replacement can invoke the displaced package as
plain `remove`, while an `in-favour` argument alone does not bind the exact
candidate artifact or completed preflight.

A Dual↔Thick-only replacement may retain Thick objects only through the
one-shot profile-replacement session created by `package-profile-gate.sh`.
The currently installed profile must already be the same newly versioned,
protocol-aware release as the incoming profile. The session pins a private
copy of the exact candidate and binds its SHA-256, both package names,
identical Debian version, host, boot identity and one helper-owned `dpkg -i`
PID/starttime/executable/argv. The old `prerm` durably consumes the session
after every read-only storage refusal check and before effects. Missing, expired,
replayed, symlinked or process-unbound evidence refuses both plain `remove`
and `in-favour`; every Thick VG mutation-intent check remains mandatory.
Consumed intent does not expire. A durable `COMPLETE` record precedes cleanup;
an exact transaction ID makes normal or post-reboot settlement idempotent.
Interrupted evidence is preserved for explicit recovery and never grants a
second package mutation.

The Thick-only post-install step removes only the dual package's explicitly
delimited, project-managed `lvmlocal.conf` autogrow fragment because that
fragment points to a Thin monitor intentionally absent from the Thick-only
artifact. It saves a pre-change copy in the private state directory and never
removes or rewrites administrator/vendor LVM policy outside the markers.

If the replaced package remains as a Debian `config-files` entry, subsequently
purging that residual entry detects the currently installed package-flavor
marker and preserves the active profile's shared configuration, recovery state
and LVM policy. An invalid marker also preserves state rather than guessing
ownership. Purging the actually active/removed profile retains the normal
scoped cleanup behavior.

The dpkg installed-state database is checked independently of the marker. If
the opposite profile is installed, its state is preserved even when the marker
is missing, unreadable or contradictory; contradictory evidence is reported
for repair but never resolved by deleting shared state.

Run `sharedlvmthin upgrade-check` immediately before every rolling package
change. Every Thick anchor must be positively verified as `MATERIALIZED`; do
not replace plugin code while a hydration, rollback, deletion or recovery
transaction is in progress. Upgrade one cluster node at a time and verify the
installed package, PVE services, storage health and guest I/O before advancing.

For the disposable qualification cluster, use
`experiments/thick-generations/package-profile-gate.sh`. It is dry-run by
default and requires an absolute non-symlink package path, exact SHA-256 and
exact hostname confirmation. The operator must also declare the expected
current state as `none`, `dual` or `thick-only`; a clean-install, same-profile
upgrade and profile-replacement run therefore cannot be confused with one
another. Mutation additionally requires `--execute` and root. It uses `dpkg`
on that exact local artifact, never downloads dependencies and never reboots
or advances another node. Its dry-run invokes the candidate's explicit
`preflight` action, which executes admission checks but cannot record a
maintenance receipt. A profile replacement is accepted only at the identical
package version and only after that version's same-profile package has taught
the installed `prerm` the one-shot protocol; ordinary same-profile downgrades
are refused. `--settle-recovery --transaction-id ID` never invokes `dpkg`; it
repeats all postconditions and can only close the exact already-consumed
transaction.
After installation, the same gate proves the shared Thick recovery surface and
then applies a flavor-specific boundary: Dual must retain its Thin adoption,
migration bridge and ThinGuard payload, while Thick-only must expose none of
them. A merely successful `dpkg` transaction is therefore insufficient.

Before the transaction, record `cat /proc/sys/kernel/random/boot_id`. After
the controlled reboot, run
`experiments/thick-generations/package-post-reboot-gate.sh` with that value
and the exact expected hostname, profile and Debian version. This read-only,
repeatable gate refuses an unchanged boot identity, a wrong or duplicate
installed profile, version drift, unfinished dpkg state, package-file drift,
leftover cross-profile payload, transient Thick work, or a failed
compatibility/Doctor/upgrade check. It also requires a valid `PASS` health JSON
whose package identity, version and profile exactly match dpkg; Thick-only JSON
may contain only `thick-generations` storage. Guest-I/O remains
a separate recorded check because only the operator knows the qualified
workload and expected data hash.

## Release boundary

Building both profiles in CI proves that their package contents and common
source remain internally consistent. It does not make either profile
production-ready. The Thick-only artifact must remain unpublished until its
own install, upgrade, replacement, reboot and destructive disposable-storage
qualification gates pass. Both profiles remain experimental, unsupported and
limited to disposable lab hardware, storage and guest data.
