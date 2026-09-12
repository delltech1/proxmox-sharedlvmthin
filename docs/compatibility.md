# Compatibility

RC5.4 TG12 is intended exclusively for **Proxmox VE 9**. The qualified release line
is PVE 9.2.x with Storage API 14 or 15. PVE 8 and earlier are unsupported.

| PVE | Storage API | Plugin API | Status |
|---|---:|---:|---|
| 9.2.2 | 14 | 14 | Disposable-node lifecycle and packaging qualification |
| 9.2.x | 15 | 15 | Three-node lifecycle, package and endurance qualification |

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
