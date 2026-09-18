# BASTRIX SharedLVM TG31+fix1

Package version: `0.9.0~rc5.10~tg31+fix1`. Hotfix **pre-release** for Proxmox VE 9
(Storage API 14/15), not universal production certification.

## Fixes

- Concurrent Thin operations can wait for an existing Thick DM transition on
  the same VG instead of immediately failing. Waiting occurs outside the VG
  lock; quorum, identity and intent are rechecked. The absolute admission budget
  defaults to 600 seconds. No entered mutation is replayed and no intent is
  automatically deleted or repaired.
- Offline Thin rollback establishes normal exclusive ownership and ThinGuard
  admission before replacement creation, then performs normal snapshot/head
  teardown. Partial failures preserve ownership and data for explicit recovery.
- An unrelated device-mapper mapping with no UUID no longer rejects the whole
  inventory. Exact managed Thick UUID checks remain mandatory.

## Verification and limits

- 198 Python tests and 794 Perl tests passed locally.
- Targeted concurrent Thin/Thick snapshot, overwrite, rollback, hash verification
  and cleanup passed; Thick returned to a linear single-dependency frontend.
- Repeated Thin rollback, writable restored head, resize and snapshot deletion
  passed. Test objects were removed and VG free space returned to its baseline.
- Two reproducible Debian builds were byte-identical.

Live retests loaded the candidate code through an isolated CLI module path.
They did **not** install this hotfix across a cluster, refresh every long-lived
daemon, repeat a rolling host upgrade/reboot, or establish a fresh long-duration
endurance/large-disk/HA qualification. Earlier TG24–TG31 results remain historical
evidence, not new test claims for this binary. Direct overlapping writable
dm-thin activation remains prohibited; this update does not remove fencing,
quorum or capacity requirements.

## Download and update

Download the DEB and `SHA256SUMS` from this release, then verify:

```sh
sha256sum --check SHA256SUMS
```

DEB SHA256: `d83450b7cdfcb6fb6868239b81290e1722f7586d2fa2a1608b453c4995f8fda1`.

Follow the [installation and rolling-update guide](https://github.com/delltech1/proxmox-sharedlvmthin/blob/main/docs/installation.md)
on every participating node. The version sorts after TG31 and before TG32.
See [admission policy](https://github.com/delltech1/proxmox-sharedlvmthin/blob/main/docs/mutation-admission.md)
for the new bounded waiting option. Validate first on disposable storage matching
your configuration. No private lab handover or infrastructure inventory is included.
