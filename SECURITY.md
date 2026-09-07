# Security policy

## Supported version

Security fixes target the latest published release-candidate branch. Older
candidates may not receive fixes.

## Reporting a vulnerability

Use GitHub Private Vulnerability Reporting for this repository. Do not open a
public issue for an unpatched vulnerability. Do not include credentials, PVE
tickets, private keys, guest data, internal addresses, hostnames, WWIDs, PV/VG
UUIDs, or unredacted diagnostic archives.

The dashboard delegates authentication to PVE, maintains a server-side
read-only session, and exposes no storage mutation endpoint. Passwords are
forwarded only to the local PVE authentication API for login and are not
stored. PVE tickets are not sent to browser JavaScript.

The storage plugin runs privileged LVM commands. Install only a
checksum-verified release and validate SAN identity, quorum, fencing and
multipath behavior before production use.
