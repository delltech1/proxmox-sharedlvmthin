# TG31 handover — 2026-09-18

## Authoritative state

- Branch: `experimental/thin-stability`
- Qualified source commit before this handover note: `95658c5`
- Package: `pve-sharedlvmthin_0.9.0~rc5.10~tg31_all.deb`
- SHA-256: `2c4f0477a013f269d153034b8c6d743ab1c830ae34eaae3a4280f920858ed9d2`
- Unit qualification: 197 Python tests and 697 Perl assertions
- The DEB was built twice from one clean Git archive with identical SHA-256.
- The exact DEB is installed on all three lab PVE nodes; `dpkg -V` is clean.

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

## Final cluster state

- The disposable VMIDs 999802 and 999803 and all their owned LV families were
  removed through normal PVE lifecycle operations.
- No transaction-scoped Thick generation or anchor remains.
- The VG has 36 MiB less free space than its earlier baseline because LVM grew
  the global `lvol0_pmspare` by nine 4-MiB extents. Do not remove or shrink it.
- All nodes report zero D-state tasks.
- Final `slt-scale-thin` recovery check: 150 pools, `STATE=HEALTHY`,
  `SAFE_FOR_MUTATION=YES`.
- Final PVE02 compatibility result: 42 PASS, 0 FAIL.
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

1. Repeat the physical round trip at a larger practical size when lab time and
   SAN throughput permit, retaining whole-device hashes.
2. Repeat under one-path loss at bounded transition points.
3. Qualify interrupted return-to-Thin recovery with the final capacity policy.
4. Run the existing package/update/reboot matrix on the next PVE API or kernel
   update before changing the public release candidate.

Never infer success from restored path count, a process exit alone or a stale
state file. Preserve the fail-closed identity, ownership and evidence model.
