# Compatibility

RC5.3 is intended exclusively for **Proxmox VE 9**. The qualified release line
is PVE 9.2.x with Storage API 14 or 15. PVE 8 and earlier are unsupported.

| PVE | Storage API | Plugin API | Status |
|---|---:|---:|---|
| 9.2.2 | 14 | 14 | Disposable-node lifecycle and packaging qualification |
| 9.2.11 | 15 | 15 | Tested POC |

Storage transports:

- iSCSI multipath: tested in the POC.
- Linux FCoE/LIO multipath: experimentally tested; target-side `tcm_fc` total-loss recovery remains a known infrastructure issue.
- Physical FC/enterprise SAN: expected to work when shared block requirements are satisfied, but not validated by this POC.

The explicit tested Storage API range is 14..15 on Proxmox VE 9. API 13 and
API 16+ remain
unsupported until their exact hook/semantic delta is audited and qualified;
the plugin advertises the exact host API only inside this qualified range and
does not claim an unknown API merely to suppress a PVE warning.
