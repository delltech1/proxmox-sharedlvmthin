# Troubleshooting

Start with `sharedlvmthin doctor`, `pvesm status`, narrowly scoped `vgs <vg>`/`lvs <vg>`, and `multipath -ll`.

Classify failures by layer: plugin, PVE storage framework, LVM/dmeventd, device mapper/multipath, FC/FCoE/iSCSI, fabric, target, network, quorum, or monitoring. A VM I/O stall below LVM is not automatically a plugin defect.

If one backend is unavailable, avoid global LVM scans where a scoped command suffices. Do not add blind userspace timeouts to mutating LVM commands; killing userspace does not prove a kernel operation was cancelled.

For dashboard issues, record HTTP status, service PID, `NRestarts`, journal, and cache metadata. A collector error and `401 Unauthorized` are different failure classes.

PVE guest shutdown and `qmeventd` cleanup can briefly race the final QEMU
close. Thick Generations waits for that exact verified frontend for a bounded
interval, revalidates its table after the close, and only then removes the
stable mapper. A frontend that remains open after the bounded wait is refused;
the plugin never forces removal of an open device.
