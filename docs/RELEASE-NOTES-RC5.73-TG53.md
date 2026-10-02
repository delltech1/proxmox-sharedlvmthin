# SharedLvmThin RC5.73 TG53

> [!CAUTION]
> Both Thin and Thick Generations remain experimental software for disposable
> Proxmox VE hosts, disposable shared SAN storage and disposable guest data.
> This candidate is not production-ready, certified, supported or warranted.

RC5.73 supersedes the unpublished RC5.72 candidate. It retains the same
experimental Thin, Thick-Eager and Thick-Lazy data-plane implementation and
adds a fail-closed correction to package/runtime qualification.

## Qualification-order correction

- A package profile gate can no longer release mutation admission merely
  because command/API probes pass. The exact installed plugin package,
  version, profile, PVE package tuple, Storage API and running kernel must
  match one unambiguous compatibility-catalogue entry.
- The tuple decision is bound into the durable runtime receipt by the manifest,
  tuple-record and observed-identity SHA-256 digests. Finalization repeats the
  proof before and after the long operational probe and again immediately
  before removing the pending latch.
- Completed-finalization replay and post-reboot requalification repeat the
  same exact tuple proof. Removing a version from the catalogue, changing the
  manifest or changing the installed runtime therefore cannot reuse an old
  success receipt.
- The full quick Doctor now runs before `finalize-runtime`. Doctor failure
  leaves mutation admission closed and prevents `RESULT=EXECUTE_PASS`.

This correction was prompted by a disposable rolling-upgrade test in which an
otherwise healthy older Proxmox VE 9 tuple omitted the new plugin version. The
old gate correctly withheld its final success result, but detected the
unlisted tuple only after releasing its runtime latch. RC5.73 closes that
ordering gap. No guest-data mismatch or storage corruption was observed.

## Additional regression evidence

- Snapshot/QMP orchestration fixture: 78/78.
- Integrated multi-disk/vmstate failure-prefix fixture: 47/47, including
  warning-only `savevm-end` and a second-disk exception after an ambiguous
  simulated effect.
- Thick rollback pre-stop fixture: 8/8, proving a transitional HEAD is refused
  before VM stop, lock publication or storage mutation.
- Runtime/update-policy focused suite: 60/60 before the final full-suite gate.

RAM snapshot configurations without an authoritative VirtIO MTU record remain
blocked on the tested qemu-server tuple. A successful upstream task followed
by cleanup is not interpreted as pre-effect safety.

## Packages

- `pve-sharedlvmthin_0.9.0~rc5.73~tg53_all.deb` — DUAL Thin + Thick profile.
- `pve-sharedlvmthin-thick_0.9.0~rc5.73~tg53_all.deb` — Thick-only profile.

The profiles conflict intentionally. Install the same package profile and
version on every participating node, one node at a time, through the documented
guarded package-profile workflow. Do not bypass an unlisted tuple or a failed
Doctor result.

All RC5.72 features and boundaries otherwise remain as documented in
[the preceding candidate notes](RELEASE-NOTES-RC5.72-TG53.md). Exact package
hashes and final test totals will be added only after reproducible builds and
the remaining lab gates complete.
