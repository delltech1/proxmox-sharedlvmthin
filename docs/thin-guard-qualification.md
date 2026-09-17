# ThinGuard qualification evidence

This document records bounded evidence for the experimental ThinGuard
prototype. It does not enable ThinGuard by default and does not turn a local
LVM VG into an upstream-supported shared thin VG.

## QF-01: real PVE watchdog-mux self-fence

Date: 2026-09-17  
Node: disposable test role on the three-node PVE 9.2.x qualification cluster  
Package baseline: `0.9.0~rc5.7~tg28`

Preconditions positively verified:

- the only running qualification VM on the node was stopped;
- no dm-thin-pool mapper remained;
- its pool retained the owner schema but no owner node or epoch;
- cluster quorum was 3/3;
- PVE HA watchdog state was standby;
- the adapter was loaded from `/tmp` and was not installed or enabled as a
  service.

The adapter opened the canonical `/run/watchdog-mux.sock`, wrote the refresh
byte, accepted a negative runtime decision and closed without the magic-close
byte. The previous-boot journal recorded:

```text
added new client (...) to watch
client (...) did not stop watchdog - disable watchdog updates
exit watchdog-mux with active connections
```

The node rebooted. Kernel boot identity changed from
`669f25d6-14f8-4688-96b2-fae77c71df6b` to
`b15b05ea-91fa-4d9d-ac2a-beca1e79f255`.

Postconditions:

- node returned with `watchdog-mux.service` active;
- quorum returned 3/3;
- all relevant iSCSI maps reported two healthy paths;
- D-state task count was zero;
- no Thin mapper autoactivated;
- `recovery-check slt-tg-thin` returned `HEALTHY` and
  `SAFE_FOR_MUTATION=YES`;
- bounded Doctor returned 78 PASS, 7 expected informational policy warnings
  and 0 FAIL;
- the VM was restarted normally;
- exactly one Thin pool mapper appeared;
- a new exact owner node/epoch was committed;
- the second recovery check again returned `HEALTHY` and
  `SAFE_FOR_MUTATION=YES`.

Result: **PASS for deliberate client-loss self-fencing and clean post-reboot
reactivation with inactive protected I/O.**

## QF-02: guardian process crash

With the same no-active-I/O preconditions, the adapter armed a real aggregate
watchdog client and its process was terminated with uncatchable `SIGKILL`.
There was no destructor, explicit close or opportunity to write `V`. The shell
observed exit status 137. `watchdog-mux` recorded the failed client, disabled
updates and the node rebooted. Boot identity changed from
`b15b05ea-91fa-4d9d-ac2a-beca1e79f255` to
`4a6b42a8-947c-469d-814d-b8eed83b9ec2`.

After reboot, quorum was 3/3, D-state count was zero, recovery reported
`THIN_OWNER_STATE=PASS`, `STATE=HEALTHY` and `SAFE_FOR_MUTATION=YES`. The
qualification VM then started normally with exactly one Thin pool mapper.

Result: **PASS for guardian process-crash fencing and clean recovery with
inactive protected I/O.**

## Evidence not yet established

This test does not prove the complete runtime guard. Remaining mandatory gates
include:

- loss of quorum while the aggregate guardian is refreshing;
- delayed/stuck QEMU stop and refusal to magic-close;
- old-owner reset observed by a target before takeover;
- active-I/O canary continuity and recovery after fencing;
- multi-pool aggregate behavior and one-bad-pool fencing;
- daemon/package upgrade and restart ordering;
- hardware watchdog qualification in addition to the lab `softdog`.

Production arming remains disabled until those gates pass.
