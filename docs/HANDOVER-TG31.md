# TG31 handover — 2026-09-18

## Authoritative state

- Branch: `experimental/thin-stability`
- Qualified source commit before this handover note: `c80fff5`
- Package: `pve-sharedlvmthin_0.9.0~rc5.10~tg31_all.deb`
- SHA-256: `27a31d49a667088999e6d7af688b4c06c16cffaaf00457056d26c0bfa4d0f0fe`
- Unit qualification: 198 Python tests and 698 Perl assertions
- The DEB was built twice from one clean Git archive with identical SHA-256.
- The exact DEB is installed on all three lab PVE nodes; `dpkg -V` is clean,
  required PVE services are active, and the final compatibility run reported
  39 PASS lines and 0 FAIL lines.

## Live results added in this cycle

- 50-way stale dmeventd event contention: 50/50 completed under the configured
  bounded lock policy, no unintended `lvextend`, no timeout and no D-state.
- 8-TiB Thin control-plane lifecycle: create, PVE attach, +1-GiB resize and
  cleanup passed. This was not an 8-TiB physical-copy test.
- A hidden active `-tpool` mapper is now accepted as exact runtime evidence for
  guarded autogrow even when the public pool LV is reported inactive.
- The monitor alone may recover exact `out_of_data_space` capacity after all
  normal identity, quorum, topology, ownership, mapper and reserve gates pass.
- Elastic allocation now uses the larger of absolute burst headroom and the
  size required to leave known used data at or below 94%.
- Capacity pressure no longer prevents exact node-local mapper teardown; it
  still blocks capacity-consuming mutations.
- A fully written 48-GiB disk passed Thin -> Thick -> native live migration ->
  Thin. Whole-device SHA-256 matched before and after. Timings and exact digest
  are recorded in `docs/scale-qualification.md`.
- Exact 500-GiB and 8-TiB regression vectors cover the capacity-floor integer
  math and signed-range contract; they do not replace physical-copy testing.
- Bridge inspection reports validated elapsed/progress-age telemetry without
  converting freshness into a timeout, mutation decision or speculative ETA.
- An externally hard-powered Thin owner passed a simultaneous 50-VM native
  PVE HA recovery. All 50 reached `started`; exact owner/epoch evidence and
  mapper cardinality passed 50/50 while the owner was fenced and again after
  it rejoined. Planned Thin live migration remained fail-closed because an
  online source is not fencing evidence.
- Native import admission now recovers exact live Data% from a proven hidden
  `-tpool` and preserves a 94% ceiling for the complete promised burst. The
  repeated 8-GiB import completed at 80% with no low-water/out-of-data event;
  source and destination SHA-256 matched exactly.
- A two-disk bridge completed Thin -> Thick -> PVE02-to-PVE03 live migration ->
  Thin while one of two exact iSCSI sessions was removed during the non-zero
  materialization copy. Progress continued on the surviving path, the removed
  session restored to 2/2, final recovery was healthy and the non-zero 8-GiB
  disk retained its exact SHA-256.

## Final cluster state

- The disposable VMIDs 999802, 999803 and 999804 and all their owned LV families were
  removed through normal PVE lifecycle operations.
- No transaction-scoped Thick generation or anchor remains.
- The VG has 36 MiB less free space than its earlier baseline because LVM grew
  the global `lvol0_pmspare` by nine 4-MiB extents. Do not remove or shrink it.
- All nodes report zero D-state tasks.
- Final `slt-scale-thin` recovery check after exact VM999804 cleanup: 150
  pools, `STATE=HEALTHY`, `SAFE_FOR_MUTATION=YES`.
- The temporary 50-resource HA test set was removed with zero failures; the
  cluster returned to 3/3 quorum and the stopped backup proxy was restarted.
- Final PVE02 compatibility result: 39 explicit PASS records, 0 FAIL.
- A post-cleanup plan attempt against the retained bridge evidence refused with
  exit 70, unchanged VG free bytes and no recreated LV.

## Important limits

- Physical data movement is qualified here at 48 GiB, not 500 GiB--8 TiB.
- The 8-TiB result proves control-plane and integer correctness only.
- Thick Generations and the materialized migration bridge remain development
  candidates. They must be staged against the intended SAN and failure policy.
- The plugin never substitutes for multipath, fencing, quorum, SAN durability
  or dm-thin metadata repair.

## Recommended next work

Use `docs/QUALIFICATION-MATRIX-TG31.md` as the claim boundary and ordered test
backlog; do not promote a PARTIAL row from indirect evidence.

1. Repeat the physical round trip at a larger practical size when lab time and
   SAN throughput permit, retaining whole-device hashes.
2. Repeat the bounded one-path bridge gate on physical FC/SAS hardware.
3. Qualify interrupted return-to-Thin recovery with the final capacity policy.
4. Run the existing package/update/reboot matrix on the next PVE API or kernel
   update before changing the public release candidate.

Never infer success from restored path count, a process exit alone or a stale
state file. Preserve the fail-closed identity, ownership and evidence model.
