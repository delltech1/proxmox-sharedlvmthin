# Same-VG mutation admission hotfix

Status: `0.9.0~rc5.10~tg31+fix1`, development hotfix candidate. This does not
change the Thin single-owner model or enable concurrent dm-thin activation.

## Problem and behavior

A Thick snapshot/rollback can leave a valid persistent VG intent while it
materializes outside a critical section. Previously a concurrent Thin
deactivation could acquire the VG lock and fail immediately on that intent.
The fix waits **before** starting the Thin mutation, releasing the canonical
VG lock between observations so the Thick transaction can finish.

Only successfully decoded OPEN DM_CUTOVER/DM_PIVOT intents are waitable.
Malformed intent, failed identity/quorum, lock acquisition errors, and other
intent types fail immediately. An abandoned waitable intent expires without
automatic repair. No callback is replayed after it starts, even on error.

`slt-mutation-admission-timeout` controls admission (10..86400 seconds,
default 600). It is separate from `slt-lock-timeout`, which bounds each lock
acquisition. The remaining admission budget also caps acquisition; an expiry
observed after acquiring the lock prevents the mutation from starting.
External command delays and an already-started mutation are not preempted by
this budget. Guest I/O, fencing and HA watchdog timeouts are not changed.

Polling backs off from 250 ms to 2 s outside the lock. A changing transaction
does not reset the absolute monotonic deadline. The waiter logs each new
transaction identity. This is bounded admission, not proof that a transaction
is healthy and not a hydration-progress watchdog. Very long transitions can
still cause a safe admission failure; sizing a timeout does not make arbitrary
large-disk HA operations qualified.

## Upgrade

The hotfix Debian version sorts after TG31. Follow the existing installation
and rolling-update procedure on every participating node; do not install an
older-version package over it to perform a future upgrade. No new daemon or
external package dependency is introduced (Time::HiRes is part of Perl).
Keep the default admission setting unless measured lifecycle durations require
a different bound. The option can be set per storage alias; it is a caller
waiting policy, not an ownership or fencing policy.

## Device-mapper inventory correction

The dmsetup UUID is optional for unrelated devices; see the
[dmsetup manual](https://manpages.debian.org/trixie/dmsetup/dmsetup.8.en.html).
Inventory preserves those devices instead of rejecting the entire host.
A matching managed Thick frontend still requires its exact SLT UUID. An empty
UUID never makes a present mapping absent or authorizes a takeover.

## Qualification boundaries

The same hotfix also corrects offline Thin rollback ownership: LVM may activate
the replacement while creating it. Rollback now uses the normal exclusive-owner
and ThinGuard activation path first, and normal snapshot/head teardown after
success. A partial failure preserves ownership and objects rather than leaving
an unowned active pool or performing automatic destructive cleanup.

Unit regression tests accompany both changes. See the private lab handover
for actual integration results; a passing unit suite alone is not production
qualification. In particular this change does not establish arbitrary
500 GiB–8 TiB migration/HA or storage-path-failure coverage.
