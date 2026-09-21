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

## QF-03: local quorum loss

The destructive, non-packaged
`experiments/thin-guard/quorum-loss-harness.pl` was armed only after proving
the qualification VM stopped and no thin-pool target active. It used one
aggregate real watchdog client and refreshed while `pvecm status` positively
reported `Quorate: Yes`.

Local `corosync.service` was then stopped on the guarded node. The other two
members retained 2/3 quorum. The harness observed quorum loss and entered its
irreversible no-refresh state while keeping the socket registered. The PVE
multiplexer provided the authoritative timing:

```text
client (...) watchdog is about to expire
client (...) watchdog expired - disable watchdog updates
exit watchdog-mux with active connections
```

The node reset and boot identity changed from
`4a6b42a8-947c-469d-814d-b8eed83b9ec2` to
`4ce00d0d-4933-49bb-b9e1-07db55b090c2`. After boot, corosync and watchdog-mux
were active and cluster quorum returned 3/3. No Thin mapper autoactivated,
D-state count was zero and recovery reported healthy paths, owner state,
`STATE=HEALTHY` and `SAFE_FOR_MUTATION=YES`. The qualification VM then started
normally with exactly one mapper.

Result: **PASS for detected local quorum loss, PVE-timed self-fencing and clean
post-fence recovery with inactive protected I/O.**

The harness was also invoked while the VM and its Thin mapper were active. It
refused before opening the socket (`RC=255`), and the watchdog journal client
count remained unchanged. This proves the destructive qualification helper
cannot accidentally arm over an active Thin pool.

## QF-04: active owner fencing before takeover

The active-owner harness positively verified a running QEMU VM, the exact
thin-pool mapper, one local owner-node tag, one 32-hex owner epoch and initial
quorum before opening its aggregate watchdog client. Local corosync was then
stopped. The guardian observed quorum loss and stopped refresh while QEMU and
the persistent owner epoch still belonged to the old node. PVE watchdog expiry
reset that node; boot identity changed from
`4ce00d0d-4933-49bb-b9e1-07db55b090c2` to
`26256846-4c65-4249-becc-6d9d1d3f5e75`.

During the membership-offline window, the VM configuration was transferred to
the survivor. Its first start was refused independently by the storage plugin:

```text
UNSAFE shared LVM-thin activation refused: pool ... is owned by node
'lab-node-c', not 'lab-node-b'; concurrent dm-thin activation can corrupt
metadata; live migration is unsupported
```

The exact old epoch remained intact. After the old boot was positively proven
replaced and its exact mapper absent, the explicit
`thin-recover-fenced-owner` operation removed only that fenced owner. Starting
the VM on the survivor committed a new owner node and a new epoch. The survivor
had exactly the expected mapper, the old node had no matching mapper, and
recovery returned owner/D-state PASS, `STATE=HEALTHY` and
`SAFE_FOR_MUTATION=YES`.

Result: **PASS for active-owner quorum-loss fencing, pre-recovery takeover
refusal and explicit post-fence takeover with a fresh epoch.**

## QF-05: active Thin I/O crash canary

A dedicated disposable 64 MiB Thin LV and per-VM pool were created for VMID
992799. No guest or existing volume was reused. The plugin's native activation
path committed the exact local owner epoch and mapper.

`thin-io-canary.py` alternated deterministic 4 KiB records between two slots.
Every record contained a sequence and SHA-256, was fsynced to the shared LV,
and only then advanced a separately fsynced local progress journal. The
active-owner guardian verified the exact mapper, owner and live writer PID,
then local quorum was removed. The writer continued until watchdog fencing.

After reboot, the local journal reported generation 1941. The pool was freshly
activated and both on-storage slots were independently validated. Result:

```text
CANARY_VERIFY=PASS storage=1941 journal=1941
```

No confirmed fsync generation was lost, the newest record was not torn, D-state
count was zero and the boot identity changed. The exact canary LV and its empty
per-VM pool were then deactivated and removed; inventory proved no leftovers.

Result: **PASS for active host-side Thin I/O, quorum-loss watchdog fencing,
durable canary recovery and exact cleanup.** This is block-layer evidence; a
guest-filesystem/application consistency claim still requires a guest-aware
workload and is intentionally not inferred from this test.

## QF-06: packaged runtime handshake and aggregate protection

The experimental package was reinstalled successfully on every member of the
three-node qualification cluster. The service remained static and stopped;
package installation did not enable or start the guardian and did not restart
running guests.

On an isolated shared test VG, runtime guarding was then selected explicitly
and the guardian was started on one node. Starting a stopped Thin VM completed
the full admission sequence before activation: exact inventory, storage
identity, quorum, remote mapper audit, owner epoch, watchdog registration and
only then local thin-pool activation. Guardian status reported
`STATE=PROTECTED` and `WATCHDOG=ARMED`. Stopping the VM removed the mapper and
owner epoch before release; status returned to `STATE=IDLE` and
`WATCHDOG=DISARMED`.

A second disposable per-VM pool was then added. Two VMs were active
concurrently with exact inventory reporting two pools, two active mappers and
two matching QEMU references, protected by one aggregate watchdog client.
Stopping the first VM left the guardian protected and armed while the other
pool remained active. Stopping the last VM disarmed the watchdog. Both owner
epochs and both mappers were absent afterward. The disposable VM, LV and pool
were removed exactly, and all nodes reported the isolated test mapper absent.
The original stopped VM configuration and storage policy were restored.

Result: **PASS for the packaged activation handshake, strict teardown order
and two-pool aggregate watchdog lifecycle.**

## QF-07: one bad pool fences an aggregate guardian

Two disposable VMs and independent per-VM Thin pools were activated on the
isolated test VG through the packaged runtime handshake. Both exact QEMU
references and both thin-pool mappers were present while one aggregate
watchdog client protected the node.

The test then removed only the persistent owner-epoch tag from one pool. It
did not modify guest data, Thin metadata or the second pool. At the next
bounded observation the inventory helper refused the malformed owner state.
The daemon reported that it was stopping while active epochs remained and did
not magic-close its watchdog client. PVE watchdog expiry reset the node; the
kernel boot identity changed.

After reboot, both disposable VMs were stopped, neither test mapper was
active, quorum was restored, `watchdog-mux` was active and no D-state task was
present. The deliberately malformed test tag and the surviving fenced-owner
tags were cleared only after the changed boot identity proved the previous
kernel dead. Both disposable VMs, their LVs and their pools were then removed,
and all cluster nodes reported the isolated mapper absent. The storage was
returned to disabled guard mode.

Result: **PASS. One ambiguous pool makes the aggregate node decision unsafe;
another healthy active pool cannot mask it or keep watchdog refresh alive.**

## QF-08: active-QEMU release refusal

The packaged guardian protected one disposable VM and its exact Thin mapper.
The non-destructive `active-release-refusal-harness.pl` derived the pool UUID
and owner epoch from a fresh independent inventory and deliberately submitted
a syntactically valid `RELEASE` request while both QEMU and the mapper were
still active.

The request was refused. The VM remained running, the guardian process stayed
active and the aggregate watchdog remained protected. A subsequent normal
PVE stop completed the ordered mapper/owner teardown, after which the guardian
accepted release. The disposable VM, LV and pool were removed and the service
and storage option returned to their disabled baseline.

Result: **PASS. A delayed or still-running QEMU cannot cause a clean watchdog
close merely by sending or replaying a validly shaped release request.**

## QF-09: package upgrade and guardian restart ordering

With one disposable VM protected and its exact Thin mapper active, the final
candidate package was installed over the existing package. Installation
syntax checks and the bounded Doctor completed successfully. The package did
not restart or replace the running guardian process: its PID remained exact,
the VM remained running and the mapper remained protected throughout the
transaction.

The VM was then stopped through PVE. Only after QEMU, mapper and owner epoch
were absent was the idle guardian explicitly restarted. Its PID changed and
the new process became active. The disposable VM, LV and pool were removed and
the service and storage policy returned to their disabled baseline.

Result: **PASS. Package upgrade preserves the guardian that owns active
watchdog epochs; the new executable is adopted only after an explicit drained
restart.**

## QF-10: frozen-root metadata validation and fingerprint

The candidate package was installed on every qualification node. All bounded
installation preflights passed with zero failures and the installed helper
passed its Perl syntax check. On an active exclusively owned Thin pool, the
packaged CLI helper then twice
reserved the kernel metadata snapshot, ran bounded
`thin_check --metadata-snap`, released the reservation and returned
`safe_for_mutation=true`. QEMU remained running. Pool, metadata and data UUIDs,
transaction ID, owner epoch, table hash and dependency hash produced the same
SHA-256 fingerprint at both checkpoints.

A foreign metadata snapshot was then reserved before invoking the helper. The
helper returned non-zero, did not run a false PASS and did not release the
foreign reservation. Pool status before and after the refusal was exact. The
test owner explicitly released its reservation afterward; status returned to
no reserved snapshot and the VM remained running.

Result: **PASS for bounded frozen-root validation, deterministic checkpoint
fingerprinting, exact release, foreign-reservation isolation and installed
package execution.**

## Evidence not yet established

These tests do not prove the complete runtime guard. Remaining mandatory gates
include:

- hardware watchdog qualification in addition to the lab `softdog`.

Production arming remains disabled until those gates pass.
