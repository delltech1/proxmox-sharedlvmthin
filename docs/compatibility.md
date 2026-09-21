# Compatibility

Published RC5.11 TG32 is intended exclusively for **Proxmox VE 9**. The
qualified release line is PVE 9.2.x with Storage API 14 or 15. PVE 8 and
earlier are unsupported. Changes on the Thick Generations audit branch are
unreleased candidates and do not extend this compatibility claim.

| PVE | Storage API | Plugin API | Status |
|---|---:|---:|---|
| 9.2.x | 14 | 14 | Lifecycle, install/reinstall and mixed-API qualification |
| 9.2.x | 15 | 15 | Lifecycle, package-update, reboot and endurance qualification |

Storage transports:

- iSCSI multipath: tested in the POC.
- Linux FCoE/LIO multipath: experimentally tested; target-side `tcm_fc` total-loss recovery remains a known infrastructure issue.
- Physical FC/enterprise SAN: architecturally supported when shared-block
  requirements are satisfied, but not hardware-qualified by this POC. Treat
  it as unqualified until validated on the intended HBA, fabric, array,
  firmware and multipath profile.

The explicit tested Storage API range is 14..15 on Proxmox VE 9. API 13 and
API 16+ remain
unsupported until their exact hook/semantic delta is audited and qualified;
the plugin advertises the exact host API only inside this qualified range and
does not claim an unknown API merely to suppress a PVE warning.

## Upstream source audit boundary

The current Thick Generations audit was compared with the authoritative
upstream repository heads below on 2026-09-21:

- `pve-storage` `b519de3c117c95b508ce20bb0230f0002e61be0c`;
- `qemu-server` `7b44050a7a66451954dc53b2d38e5834b01777ab`;
- `pve-container` `3a26da6db5e949418b2ec4eb6b383450feb7064d`.

This records the reviewed source boundary; it is not a permanent assertion
about newer upstream commits. Any change to storage API/API age, lifecycle
hooks, migration/snapshot ordering, activation semantics or container
filesystem-freeze orchestration reopens compatibility qualification. Package
installation success alone never closes that gate.
