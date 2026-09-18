# TG31 objective qualification matrix

This matrix prevents a narrow successful test from being presented as proof of
the complete orchestration objective. `PASS` means the named scope has direct
recorded evidence. `PARTIAL` means an important sub-scope is proven but the
full target claim is not. `OPEN` means no current evidence supports the claim.

| Requirement | Status | Authoritative evidence | Remaining acceptance gate |
|---|---|---|---|
| 50+ VM Thin evacuation | PASS | `scale-qualification.md`, bounded 50-VM Thin evacuation; both directions, ownership handoff, zero D-state | Repeat after a future PVE API/locking change |
| High mixed-mode concurrency | PASS in lab envelope | 150 Thin plus 150 Thick running concurrently; rolling package deployment and exact inventory checks | Not a 200–500 LUN enterprise-array certification |
| Canonical lock backlog | PASS | 50 simultaneous contenders and 50-way dmeventd event storm; exact one-winner/admission behavior and 50/50 bounded completion | Re-run when lock implementation changes |
| Timeout behavior | PASS for configured bounded policy | Storage lock and bridge admission waits are bounded/configurable; timeout is never interpreted as proof of failure | Site must qualify values against its measured P99/max latency |
| Reboot/rejoin | PASS | Controlled node reboot, updated package reboot and runtime-guard reboot evidence in `thin-guard-qualification.md` and Thick status ledger | Repeat for new kernel/LVM/device-mapper releases |
| Hard-host failover without duplicate Thin activation | PASS for 50-VM fenced gate | Externally hard-powered owner, surviving quorum, native PVE HA fencing, 50/50 recovery, exact owner audit and one mapper on exactly one node before and after old-owner rejoin | Does not certify an untested fencing implementation or array |
| Split-brain refusal | PASS for tested quorum/fencing cases | Owner tags, exact epoch, remote mapper audit, no-quorum mutation refusal and fenced-owner ordering | Does not certify an untested fencing implementation |
| Progress-driven long operations | PASS at current physical envelope | Persistent schema-v3 state, monotonic QMP progress, validated elapsed/update-age telemetry, transaction manifest, no arbitrary copy deadline, exact resume planner | Physical long-copy duration above 48 GiB remains unmeasured |
| Interrupted bridge recovery | PASS for tested materialization/return cases | Killed mirror reconciliation, exact runtime/config correlation, resume actions and stale post-cleanup evidence refusal | Repeat with a larger physical payload and one-path loss |
| Thin capacity recovery | PASS | Hidden `-tpool` runtime detection, guarded out-of-data grow, 50-way stale event test, 49-to-52-GiB live correction | Kernel-level completion of already outstanding writes remains outside plugin scope |
| 500-GiB disk suitability | PARTIAL | Exact integer/capacity-floor regression and generic control-plane paths | Fully written 500-GiB Thin--Thick--Thin copy with whole-device hashes |
| 8-TiB disk suitability | PARTIAL | Exact 8-TiB create/attach/resize/cleanup plus integer/capacity-floor regression | Fully written 8-TiB movement, interruption and duration evidence |
| Thin--Thick--Thin lifecycle | PASS at 48 GiB | Fully written 48-GiB round trip, native Thick live migration and identical whole-device SHA-256 | Larger physical payload and path-loss transition |
| Single-path operation | PASS for bounded 8-GiB bridge materialization gate | One of two exact iSCSI sessions was removed during active Thin-to-Thick copy; progress continued on the surviving path and the exact session restored to 2/2 | Physical FC and larger-payload repetition remain open |
| Total path loss | fail-closed plugin behavior PASS; data-path recovery is external | Recovery gate refuses ambiguous/D-state state and never treats restored path count as health | Kernel D-state, SAN cache, multipath timer and guest-write completion are infrastructure responsibilities |
| Reproducible source/package/evidence | PASS | Clean Git history, reproducible DEB SHA, full tests, handover archive and release-boundary documentation | Regenerate for every published commit |

## Current non-negotiable boundaries

- No claim of physical 500-GiB--8-TiB copy qualification exists yet.
- The qualified simultaneous hard-failover batch is 50 small disposable VMs.
  This is an orchestration/ownership result, not a large-disk throughput claim.
- Restored paths, a process exit code, or a stale transaction file alone never
  authorize mutation.
- The plugin does not repair dm-thin metadata, configure SAN sessions, replace
  multipath/fencing/quorum, or guarantee completion of outstanding guest I/O.

## Next highest-value bounded gates

1. Fully written 500-GiB round trip with pre/post hashes and an interrupted
   return-to-Thin resume point.
2. Repeat a physical bridge copy while one of two paths is unavailable, then
   restore the path and prove exact identity/dependency cleanup.
3. Repeat the 50-VM fenced gate after material PVE HA, watchdog, LVM or kernel
   changes; do not infer other fencing implementations from this lab result.
4. Treat an 8-TiB physical test as a separately scheduled endurance run; never
   infer it from the control-plane or arithmetic results.
