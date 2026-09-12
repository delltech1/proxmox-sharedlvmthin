# Known issues

1. Linux LIO/`tcm_fc` software FCoE targets may exhibit blocked recovery after
   complete communication loss. That target stack is not certified by this
   project. This boundary is independently visible in current upstream Linux:
   the [`ft_recv_seq()` error path](https://github.com/torvalds/linux/blob/master/drivers/target/tcm_fc/tfc_cmd.c)
   still records that a queued command must be found, marks the command aborted,
   and returns. The [file history](https://github.com/torvalds/linux/commits/master/drivers/target/tcm_fc/tfc_cmd.c)
   does not show a later path-specific fix. This is supporting evidence, not a
   claim that the source comment alone proves every observed target stall.
2. Indefinite `queue_if_no_path` can block LVM/PVE management during total path loss. Multipath policy remains an administrator-owned infrastructure setting; Doctor reports risk but does not change it.
3. Legacy untagged per-VM pools are preserved and are not silently adopted, tagged, grown, or deleted.
4. Thick Generations are newer than the established Thin mode and remain a
   release-candidate technology in TG12. Validate the complete lifecycle on
   disposable storage matching the intended production topology before use.
5. Web sessions are in memory; a dashboard service restart requires login again.
6. Physical FC HBA/fabric behavior requires qualification with the intended production hardware, firmware, array and multipath policy.
7. Active dm-thin metadata I/O and active dm-clone hydration did not recover
   transparently in every laboratory all-path-loss experiment. The plugin
   correctly blocked new mutations, but it cannot repair or guarantee recovery
   of an already blocked kernel/device-mapper transaction. See the release gate
   before claiming complete-path-loss availability.
8. Veeam HotAdd backup and supported-console cross-mode restore were tested.
   Veeam replication was not available in the installed edition and is not
   part of the qualification claim.
