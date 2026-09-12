# Original cluster qualification plan

This document defines the remaining qualification work for the local
`0.9.0~rc5.4~tg12` Thick Generations candidate. It is a test plan, not a support
claim. Record exact package checksums, PVE versions, Storage API versions,
storage identities and results before changing any state.

The ESXi datastore that produced SATA command failures, controller resets and
VMFS heartbeat timeouts is excluded from all further load and fault testing.
Its interrupted run is infrastructure-failure evidence, not a plugin failure
and not an endurance pass.

## 1. Admission gate

Complete this section before installing or mutating storage.

- [x] Use disposable LUNs and test VMs; preserve production data untouched.
- [x] Confirm all nodes are Proxmox VE 9 and record Storage API 14 or 15.
- [x] Verify the tg12 DEB SHA256 on every node before installation.
- [x] Confirm identical WWID, PV UUID and VG UUID on every participating node.
- [x] Confirm multipath policy, usable path count and expected iSCSI or FC
      sessions independently of the plugin.
- [x] Confirm native cluster quorum and fencing policy.
- [x] Capture `pvesm status`, `pvecm status`, multipath state, LVM inventory,
      thin-pool flags, Thick anchor inventory and relevant D-state baseline.
- [x] Run read-only Doctor and recovery checks. Mutation requires an explicit
      healthy result; unknown or ambiguous evidence fails closed.
- [x] Verify the disposable VG has enough free extents for the complete plan.

## 2. Package and rolling-upgrade gate

Run one node at a time while storage remains available through the other
nodes. Package installation must not deactivate storage or change SAN,
multipath, PV, VG, LV or guest state.

- [x] Install tg12 over the previously qualified candidate on API 15 node 1.
- [x] Verify package files, plugin registration, services, Doctor, recovery
      checks, storage listing and running guest I/O.
- [x] Repeat on API 15 node 2.
- [x] Repeat on the API 14 node.
- [x] Reinstall the exact candidate package once on every API version.
- [x] Verify no old-API warning on API 15 and correct compatibility on API 14.
- [x] During a deliberately mixed-version interval, prove upgrade preflight
      blocks incompatible Thick mutations while existing open guest disks keep
      their normal lower-layer I/O path.
- [x] Finish with the identical package version and checksum on every node.
- [x] Reboot one node, then revalidate identity, inventory, services and both
      storage modes without speculative repair.

## 3. Thin-mode regression

- [x] Allocate, start, stop and delete a disposable Thin VM disk.
- [x] Resize and verify guest-visible capacity and retained data.
- [x] Snapshot, rollback and delete snapshots in supported order.
- [x] Full clone and linked lifecycle where supported by the Thin model.
- [x] Snapshot-mode backup and restore to a new VMID.
- [x] Offline and online migration between nodes.
- [x] Storage move Thin to Thin and exact source cleanup.
- [x] Verify capacity reporting: VG reservation, per-VM payload and reserved
      slack must be visible and arithmetically consistent.
- [x] Verify duplicate volume allocation in one storage namespace and
      conflicting ownership attempts are rejected without changing the existing
      object. The same PVE volume name in distinct Thin and Thick aliases is a
      deliberate, independently owned namespace used during storage moves.

## 4. Thick Generations lifecycle

- [x] Allocate a Thick disk and prove the steady-state frontend is linear.
- [x] Start, stop, resize and delete with exact LV and anchor postconditions.
- [x] Create a snapshot, observe persistent dm-clone transition state,
      materialize it and prove the final frontend depends only on destination.
- [x] Roll back by creating a new authoritative generation; preserve and then
      remove superseded objects only when ownership is proven.
- [x] Delete snapshots in supported and intentionally invalid orders.
- [x] Full clone, backup and restore to new VMIDs with guest hash validation.
- [x] Offline and online node migration in steady-state linear mode.
- [x] Interrupt and explicitly resume materialization; never auto-repair.
- [x] Confirm transaction-scoped cleanup leaves no mapper, tag, LV, lock or
      temporary evidence artefact.

## 5. Thin and Thick coexistence and conversion

- [x] Operate Thin and Thick disks concurrently in the same qualified VG.
- [x] Run concurrent writes, flushes and independent read/hash verification.
- [x] Move Thin to Thick and verify destination data before source deletion.
- [x] Move Thick to Thin and verify destination data before source deletion.
- [x] Repeat conversion with snapshots present only for combinations explicitly
      supported by the plugin; unsupported dependency graphs must fail closed.
- [x] Restore the same backup independently into Thin and Thick destinations.
- [x] Exercise multi-disk VMs containing both modes.
- [x] Verify capacity admission and reporting remain correct after every move,
      resize, restore and cleanup.

## 6. Cluster topology

- [x] Three-node baseline: stop and rejoin one node in both modes.
- [x] Three-node quorum loss: every new mutation and recovery resume fails
      closed without LV, tag, mapper or configuration changes.
- [x] Two-node cluster with QDevice: qualify normal operation, QDevice loss,
      one-node survival according to native quorum and rejoin.
- [x] Two-node cluster without QDevice: document the availability limitation,
      prove loss of quorum blocks mutation and verify no private vote override.
- [x] Repeat cross-node recovery of an interrupted Thick transition only after
      the old worker is demonstrably fenced or stopped.

The plugin must consume the native Proxmox quorum result. It must not configure
votes, change watchdog settings, run `pvecm expected`, or infer fencing.

## 7. Transport fault matrix

Use one named fault at a time and collect pre-fault identity and guest hashes.

- [x] iSCSI 2-to-1-to-2 with active Thin and steady-state Thick I/O.
- [x] iSCSI 2-to-1-to-2 during Thick materialization.
- [x] Bounded iSCSI 2-to-0-to-2 for steady-state Thick, followed by read-only
      recovery validation before any mutation.
- [x] Active Thin total-path loss: retain the native dm-thin outcome as a host
      storage-stack boundary; do not claim plugin recovery.
- [x] Thick materialization total-path loss: preserve persistent transaction
      evidence and require explicit post-reboot recovery when necessary.
- [ ] Repeat equivalent single-path tests on physical FC when representative
      HBA, firmware, fabric and array hardware is available.

The existing Linux VN2VN/tcm_fc path-return failure remains a documented target
limitation and cannot qualify physical FC behavior.

## 8. Uninterrupted endurance release blocker

- [ ] Run at least four uninterrupted hours of concurrent Thin and Thick guest
      writes, explicit flush/fsync operations and independent direct reads.
- [ ] Keep bounded host-side monitoring of quorum, paths, identities, thin-pool
      flags, Thick transactions, relevant D-state and recovery classification.
- [ ] Exercise scheduled snapshots, materialization, resize, backup, migration
      and Thin/Thick storage moves without overlapping unsupported mutations on
      the same disk.
- [ ] Require zero unexpected host reboot, storage disappearance, guest hash
      mismatch, leaked artefact, ambiguous anchor or unsafe mutation.
- [x] Perform final backup/restore validation and exact cleanup.

Any infrastructure reset, transport ambiguity or interrupted observation makes
the run invalid. It must be repeated from a clean baseline; partial elapsed
time is not accumulated into a pass.

## 9. Final acceptance

- [x] Full Python and Perl taint-mode regressions pass from a clean checkout.
- [x] Two independent candidate builds are byte-identical.
- [x] Package content, syntax, privacy and secret scans pass.
- [x] The exact tested DEB is installed successfully on every qualified API
      version and has one recorded SHA256.
- [ ] C, Y and H project/evidence mirrors contain the exact accepted commit,
      package checksum and sanitized evidence.
- [x] Release gate and known limitations match the actual evidence.
- [x] No internal address, hostname, credential, personal identity or private
      laboratory detail is present in publishable files or the package.
- [x] Do not publish Thick Generations until every required software gate is
      positive. Physical PB and physical FC remain explicit untested hardware
      boundaries until representative equipment is qualified.
