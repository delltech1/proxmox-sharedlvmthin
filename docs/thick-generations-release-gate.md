# Thick Generations release gate

Thick Generations remains a release candidate. Every required software and
available live integration gate below has positive evidence; a documented
hardware boundary is not converted into a success claim.

Disruptive cluster and transport coverage is tracked separately in the
[Thin and Thick Generations redundancy gate](thick-generations-redundancy-gate.md).
The executable order and remaining original-cluster work are defined in the
[original cluster qualification plan](original-cluster-qualification-plan.md).

| Area | Required evidence | Current state |
| --- | --- | --- |
| Persistent model | Signed anchor, immutable generations, exact identity and deterministic state classification | PASS |
| Transition primitive | Persistent dm-clone reopen, hydration, linear pivot and destination-only dependency | PASS |
| Crash recovery | C0-C9 classification and exact forward or cleanup recovery | PASS |
| Snapshot deletion | D0-D4 classification, stale-lock refusal and idempotent recovery | PASS |
| Cross-node recovery | Runtime reconstruction from persistent state without source-host mapper state | PASS |
| Same-VG coexistence | Canonical lock, inventory isolation, thin and thick lifecycle, exact cleanup | PASS |
| PVE lifecycle | Allocate, start, stop, resize, snapshot, rollback, delete and full clone | PASS |
| Mobility | Offline/live migration, storage move and thin-to-thick/thick-to-thin conversion | PASS |
| Native PVE data protection | Snapshot-mode backup, restore, sparse archive and restored-guest validation | PASS |
| Veeam HotAdd backup | Supported-UI Thin, Thick and mixed-VM backup with exact proxy cleanup | PASS |
| Veeam cross-mode restore | Supported-UI Thin-to-Thin, Thin-to-Thick, Thick-to-Thick, Thick-to-Thin and mixed restore with data verification | PASS |
| HA | Controlled relocation and fenced worker-host loss during materialization | PASS |
| iSCSI multipath | 2-to-1-to-2 and bounded 2-to-0-to-2 recovery with identity and data verification | SINGLE PATH PASS; THICK LINEAR TOTAL LOSS PASS; ACTIVE THIN FAIL_HOST_DM_THIN; ACTIVE HYDRATION FAIL_HOST_DM_CLONE; EXPLICIT POST-REBOOT RESUME PASS, PRE-FAULT SHA NOT PROVEN |
| FCoE lab transport | Ordinary I/O and 2-to-1 failover | READ/THICK WRITE PASS; THIN ZEROING BASELINE FAIL_TARGET_TCM_FC |
| FCoE path return | Linux VN2VN/tcm_fc target recovery without target restart | FAIL - transport limitation |
| Windows workload | Long write-through, flush and hash-verification soak | PASS |
| Windows interruption | Materialization interruption, explicit resume, pivot and guest data verification | PASS |
| Cross-node non-interference | Repeated bounded recovery checks on every qualified API node | PASS |
| Dual-mode endurance | Uninterrupted Thin and Thick guest write, flush and direct-read verification with host-side recovery monitoring | NEGATIVE RUN ARCHIVED; CLEAN RETEST PASS |
| Large-capacity arithmetic | 1, 16 and 128 PiB geometry, overflow refusal and exact health counters | PASS |
| Physical petabyte storage | Representative array qualification | NOT TESTED |
| Physical FC fabric | Representative HBA, firmware, fabric and array qualification | NOT TESTED |
| Web and installer | Dual-mode configuration, authentication, monitoring and live rendering | PASS |
| Recovery monitoring | Live IN_PROGRESS, RECOVERY_REQUIRED and post-resume MATERIALIZED classification; scoped dmeventd requirement | PASS |
| Regression | Python, Perl taint-mode, package content and privacy gates at accepted commit | PASS: 156 PYTHON, 236 PERL |

`PASS` means the evidence is recorded in
[`thick-generations-poc-status.md`](thick-generations-poc-status.md). A current
`RUNNING` or `OPEN` entry in this table would be a release blocker; historical
intermediate states remain unchanged in the chronological evidence log. A
physical PB or FC claim remains outside the qualified support envelope until
representative hardware is available. The
known Linux VN2VN/tcm_fc path-return failure is below the plugin and must remain
prominent in release documentation; it must never be described as recovered by
Thick Generations.

Before publishing a package, repeat the full regression and package privacy
checks against the exact commit used to build the release artifact. Install
that identical artifact on every qualified PVE Storage API version, verify
service health, and retain its checksum with the test evidence.
