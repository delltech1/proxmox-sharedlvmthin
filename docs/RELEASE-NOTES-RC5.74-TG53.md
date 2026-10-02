# SharedLvmThin RC5.74 TG53

> **EXPERIMENTAL PRE-RELEASE — disposable Proxmox VE hosts, shared storage
> and guest data only.** This candidate is not production-ready, certified,
> supported or warranted.

RC5.74 supersedes the unpublished RC5.73 candidate. It retains RC5.73's exact
runtime-tuple binding and Doctor-before-finalization ordering, and closes a
separate APT post-update/reboot replay gap found during qualification.

## Update safety correction

The APT post-update path and the direct package-profile path now construct the
same boot-bound runtime receipt. The receipt includes the exact tuple ID and
status plus SHA-256 identities for the compatibility manifest, tuple and
observed runtime. The post-update path re-observes those bindings and performs
a durable receipt CAS immediately before removing its package latch.

This prevents an apparently successful guarded APT update from producing a
receipt that would become unverifiable at the next reboot. Missing, ambiguous
or changed tuple evidence leaves the update latch in place and requires
explicit recovery; it never silently opens storage mutation admission.

## Candidate package profiles

- `pve-sharedlvmthin_0.9.0~rc5.74~tg53_all.deb` — DUAL Thin + Thick profile.
- `pve-sharedlvmthin-thick_0.9.0~rc5.74~tg53_all.deb` — Thick-only profile.

The profiles remain mutually exclusive and must not be mixed across
participating nodes. GitHub publication remains gated on full lab rollout,
regression, package privacy and reproducibility evidence.
