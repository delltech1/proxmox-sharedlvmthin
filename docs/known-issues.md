# Known issues

1. Linux LIO/`tcm_fc` software FCoE targets may exhibit blocked recovery after complete communication loss. That target stack is not certified by this project.
2. Indefinite `queue_if_no_path` can block LVM/PVE management during total path loss. Multipath policy remains an administrator-owned infrastructure setting; Doctor reports risk but does not change it.
3. Legacy untagged per-VM pools are preserved and are not silently adopted, tagged, grown, or deleted.
4. Thick volumes are not implemented. RC5.2 remains thin-only.
5. Web sessions are in memory; a dashboard service restart requires login again.
6. Physical FC HBA/fabric behavior requires qualification with the intended production hardware, firmware, array and multipath policy.
