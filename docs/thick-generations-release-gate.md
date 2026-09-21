# Thick Generations release gate

Thick Generations remains a release candidate. Existing lifecycle evidence is
recorded below, but the newly separated Thick-only package profile has open
installation and replacement gates. No source-level test or successful package
build converts those open runtime gates into a success claim. A documented
hardware boundary is likewise not converted into a success claim.

## Published TG32 versus audit candidate

The immutable `v0.9.0-rc5.11-tg32` pre-release contains the previously
qualified dual-mode TG32 package. It is not rebuilt or silently replaced by
this audit. The Thick-only profile, explicit dm-clone `rw`/`ro`/`Fail` checks,
monotonic transition timing, package transaction fences, checksum manifests
and profile-replacement tooling exist only on the unreleased audit branch until
a later candidate passes every applicable open gate below.

No known incident currently proves routine data corruption in the published
TG32 artifact. That absence is not a production-safety claim and the new
hardening must not be represented as retroactively present in its `.deb`.

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
| Thin mobility | Offline source-deactivate then target-activate handoff; overlapping live migration must fail closed | PASS; LIVE REFUSED BY DESIGN |
| Materialized Thick mobility | Offline/live migration in independent linear steady state | PASS |
| Storage conversion | Thin-to-thick/thick-to-thin storage move with exact destination and source-cleanup evidence | PASS |
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
| Thick-only clean install | Exact candidate installs/configures on every qualified PVE API version; no absent Thin helper is invoked | OPEN |
| Package profile replacement | Dual-to-Thick-only and Thick-only-to-dual replacement refuse unsafe state and preserve healthy Thick guests | OPEN |
| Package rolling update | Same-flavor upgrade one node at a time with active services, reboot and post-update guest I/O verification | OPEN |
| Package removal fence | Active ThinGuard plus managed Thin objects positively refuses removal; empty audited inventory permits cleanup | OPEN |
| Thick-only diagnostics | Package identity/version, Doctor and JSON health output match the installed Thick-only artifact on a live PVE node | OPEN |
| Recovery monitoring | Live IN_PROGRESS, RECOVERY_REQUIRED and post-resume MATERIALIZED classification; scoped dmeventd requirement | PASS |
| Regression | Python, Perl taint-mode, package content, privacy, reproducibility and profile-parity gates at accepted commit | PASS: CI RUN 199 AT `71eb487`; 211 PYTHON TESTS; 21 PERL FILES / 823 ASSERTIONS |

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
service health, and retain its checksum with the test evidence. For the
Thick-only profile, execute every package row marked `OPEN` above with both a
quiescent cluster and deliberately injected refusal conditions. A refused
unsafe transaction must leave the previously installed package and guest I/O
intact; merely returning a non-zero dpkg status is insufficient evidence.
