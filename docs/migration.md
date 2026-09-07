# Migration

Shared-storage live migration should transfer VM runtime state without copying the shared disk. Verify that both nodes see the same VG/PV/WWID, the destination activates the same LV, dmeventd monitoring follows active pools, and the source releases state.

Tested POC behavior includes offline migration and live migration in both-node operation. Transport health and quorum must be validated separately; migration success does not prove SAN failure recovery.
