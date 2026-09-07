# Autogrow

The packaged dmeventd monitor handles only `sltp-<VMID>` pools with a valid storage ownership tag. It acquires the PVE cluster storage lock and invokes `lvextend --use-policies`.

The tested POC policy uses an 80% threshold and 10% growth. RC5 revalidates quorum, storage identity, ownership, current live `Data%`, and protected VG reserve while holding the same cluster storage lock immediately before `lvextend --use-policies`. Administrators must still monitor physical VG free space, thin data use, and thin metadata use independently; virtual thin allocation may legitimately exceed physical capacity.

## dmeventd lifecycle

Do not blindly run `systemctl restart dm-event.service` while thin pools remain
registered. In qualification this left systemd waiting in `deactivating` because
dmeventd schedules exit only after its monitored devices are unregistered.

The stock `dm-event.service` has `Restart=no`. A disposable-node SIGKILL test
showed that the socket can bring the daemon process back, but existing thin
pools remain `not monitored` until explicitly registered again. Recovery must
first revalidate storage identity and the ownership tag, then run
`lvchange --monitor y <exact-vg>/<exact-owned-pool>`. Never enumerate and
automatically adopt name-matching, legacy, foreign, or unknown pools. The test
preserved pool size and metadata and produced no duplicate growth.
Storage I/O remained healthy, but restart did not complete.

If a controlled dmeventd restart is required:

1. Record the exact pool name, size, ownership tag, active state, and
   `seg_monitor` state on the node.
2. Confirm quorum, storage identity, and healthy paths.
3. Disable monitoring only for the pools recorded as monitored using
   `lvchange --monitor n <VG>/<pool>`.
4. Allow the normal systemd stop/start to complete; never force-kill dmeventd.
5. Re-enable monitoring only for the same positively identified pools using
   `lvchange --monitor y <VG>/<pool>`.
6. Compare every pool size and monitoring state with the captured baseline and
   check logs for unexpected growth.

This is an administrator recovery procedure, not an automatic plugin repair.
LEGACY, FOREIGN, UNKNOWN, inactive, or ambiguously identified pools must not be
newly enrolled by an automated recovery action.
