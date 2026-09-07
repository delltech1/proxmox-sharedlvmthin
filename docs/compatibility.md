# Compatibility

| PVE | Storage API | Plugin API | Status |
|---|---:|---:|---|
| 9.2.2 | 14 | 14 | Disposable-node lifecycle and packaging qualification |
| 9.2.11 | 15 | 15 | Tested POC |

Storage transports:

- iSCSI multipath: tested in the POC.
- Linux FCoE/LIO multipath: experimentally tested; target-side `tcm_fc` total-loss recovery remains a known infrastructure issue.
- Physical FC/enterprise SAN: expected to work when shared block requirements are satisfied, but not validated by this POC.

The explicit tested Storage API range is 14..15. API 13 and API 16+ remain
unsupported until their exact hook/semantic delta is audited and qualified;
the plugin does not dynamically claim an unknown API merely to suppress a PVE
warning.
