# Multipath

Use a stable `/dev/mapper/<WWID>` device for the PV. Do not create the PV directly on `/dev/sdX` when redundant paths are expected.

An indefinite `queue_if_no_path` policy can block LVM and PVE management when every path disappears. SharedLvmThin does not install or alter global multipath policy. Administrators should design a bounded recovery window using settings appropriate to their FC/iSCSI stack, including `no_path_retry`, checker interval, and FC `fast_io_fail_tmo`/`dev_loss_tmo` where applicable.

Validate policy at runtime against a dedicated test map before persistence. Single-path loss and complete path loss are different tests. Never force-remove a live map carrying VM I/O.
