# Original cluster qualification plan

This document defines the remaining qualification work for the local
`0.9.0~rc5.4~tg7` Thick Generations candidate. It is a test plan, not a support
claim. Record exact package checksums, PVE versions, Storage API versions,
storage identities and results before changing any state.

The ESXi datastore that produced SATA command failures, controller resets and
VMFS heartbeat timeouts is excluded from all further load and fault testing.
Its interrupted run is infrastructure-failure evidence, not a plugin failure
and not an endurance pass.

## 1. Admission gate

Complete this section before installing or mutating storage.

- [ ] Use disposable LUNs and test VMs; preserve production data untouched.
- [ ] Confirm all nodes are Proxmox VE 9 and record Storage API 14 or 15.
- [ ] Verify the tg7 DEB SHA256 on every node before installation.
- [ ] Confirm identical WWID, PV UUID and VG UUID on every participating node.
- [ ] Confirm multipath policy, usable path count and expected iSCSI or FC
      sessions independently of the plugin.
- [ ] Confirm native cluster quorum and fencing policy.
- [ ] Capture `pvesm status`, `pvecm status`, multipath state, LVM inventory,
      thin-pool flags, Thick anchor inventory and relevant D-state baseline.
- [ ] Run read-only Doctor and recovery checks. Mutation requires an explicit
      healthy result; unknown or ambiguous evidence fails closed.
- [ ] Verify the disposable VG has enough free extents for the complete plan.

## 2. Package and rolling-upgrade gate

Run one node at a time while storage remains available through the other
nodes. Package installation must not deactivate storage or change SAN,
multipath, PV, VG, LV or guest state.

- [ ] Install tg7 over the currently installed candidate on API 15 node 1.
- [ ] Verify package files, plugin registration, services, Doctor, recovery
      checks, storage listing and running guest I/O.
- [ ] Repeat on API 15 node 2.
- [ ] Repeat on the API 14 node.
- [ ] Reinstall the exact tg7 package once on every API version.
- [ ] Verify no old-API warning on API 15 and correct compatibility on API 14.
- [ ] During a deliberately mixed-version interval, prove upgrade preflight
      blocks incompatible Thick mutations while existing open guest disks keep
      their normal lower-layer I/O path.
- [ ] Finish with the identical package version and checksum on every node.
- [ ] Reboot one node, then revalidate identity, inventory, services and both
      storage modes without speculative repair.

## 3. Thin-mode regression

- [ ] Allocate, start, stop and delete a disposable Thin VM disk.
- [ ] Resize and verify guest-visible capacity and retained data.
- [ ] Snapshot, rollback and delete snapshots in supported order.
- [ ] Full clone and linked lifecycle where supported by the Thin model.
- [ ] Snapshot-mode backup and restore to a new VMID.
- [ ] Offline and online migration between nodes.
- [ ] Storage move Thin to Thin and exact source cleanup.
- [ ] Verify capacity reporting: VG reservation, per-VM payload and reserved
      slack must be visible and arithmetically consistent.
- [ ] Verify duplicate VMID, volume name and conflicting ownership attempts are
      rejected without changing existing objects.

## 4. Thick Generations lifecycle

- [ ] Allocate a Thick disk and prove the steady-state frontend is linear.
- [ ] Start, stop, resize and delete with exact LV and anchor postconditions.
- [ ] Create a snapshot, observe persistent dm-clone transition state,
      materialize it and prove the final frontend depends only on destination.
- [ ] Roll back by creating a new authoritative generation; preserve and then
      remove superseded objects only when ownership is proven.
- [ ] Delete snapshots in supported and intentionally invalid orders.
- [ ] Full clone, backup and restore to new VMIDs with guest hash validation.
- [ ] Offline and online node migration in steady-state linear mode.
- [ ] Interrupt and explicitly resume materialization; never auto-repair.
- [ ] Confirm transaction-scoped cleanup leaves no mapper, tag, LV, lock or
      temporary evidence artefact.

## 5. Thin and Thick coexistence and conversion

- [ ] Operate Thin and Thick disks concurrently in the same qualified VG.
- [ ] Run concurrent writes, flushes and independent read/hash verification.
- [ ] Move Thin to Thick and verify destination data before source deletion.
- [ ] Move Thick to Thin and verify destination data before source deletion.
- [ ] Repeat conversion with snapshots present only for combinations explicitly
      supported by the plugin; unsupported dependency graphs must fail closed.
- [ ] Restore the same backup independently into Thin and Thick destinations.
- [ ] Exercise multi-disk VMs containing both modes.
- [ ] Verify capacity admission and reporting remain correct after every move,
      resize, restore and cleanup.

## 6. Cluster topology

- [ ] Three-node baseline: stop and rejoin one node in both modes.
- [ ] Three-node quorum loss: every new mutation and recovery resume fails
      closed without LV, tag, mapper or configuration changes.
- [ ] Two-node cluster with QDevice: qualify normal operation, QDevice loss,
      one-node survival according to native quorum and rejoin.
- [ ] Two-node cluster without QDevice: document the availability limitation,
      prove loss of quorum blocks mutation and verify no private vote override.
- [ ] Repeat cross-node recovery of an interrupted Thick transition only after
      the old worker is demonstrably fenced or stopped.

The plugin must consume the native Proxmox quorum result. It must not configure
votes, change watchdog settings, run `pvecm expected`, or infer fencing.

## 7. Transport fault matrix

Use one named fault at a time and collect pre-fault identity and guest hashes.

- [ ] iSCSI 2-to-1-to-2 with active Thin and steady-state Thick I/O.
- [ ] iSCSI 2-to-1-to-2 during Thick materialization.
- [ ] Bounded iSCSI 2-to-0-to-2 for steady-state Thick, followed by read-only
      recovery validation before any mutation.
- [ ] Active Thin total-path loss: retain the native dm-thin outcome as a host
      storage-stack boundary; do not claim plugin recovery.
- [ ] Thick materialization total-path loss: preserve persistent transaction
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
- [ ] Perform final backup/restore validation and exact cleanup.

Any infrastructure reset, transport ambiguity or interrupted observation makes
the run invalid. It must be repeated from a clean baseline; partial elapsed
time is not accumulated into a pass.

## 9. Final acceptance

- [ ] Full Python and Perl taint-mode regressions pass from a clean checkout.
- [ ] Two independent tg7 builds are byte-identical.
- [ ] Package content, syntax, privacy and secret scans pass.
- [ ] The exact tested DEB is installed successfully on every qualified API
      version and has one recorded SHA256.
- [ ] C, Y and H project/evidence mirrors contain the exact accepted commit,
      package checksum and sanitized evidence.
- [ ] Release gate and known limitations match the actual evidence.
- [ ] No internal address, hostname, credential, personal identity or private
      laboratory detail is present in publishable files or the package.
- [ ] Do not publish Thick Generations until every required software gate is
      positive. Physical PB and physical FC remain explicit untested hardware
      boundaries until representative equipment is qualified.
