# Security policy

> [!CAUTION]
> Both Thin and Thick Generations in this release candidate are experimental
> and intended only for disposable lab systems and disposable data. Neither
> mode is a supported, certified, or production-ready storage product. The
> operator must independently provide fencing, backups, recovery
> testing, SAN identity validation, and safe change control. See `LICENSE`
> sections 15–17 and the operational risk notice in `README.md`.

## Security-report handling

Security reports may be evaluated for the latest published release-candidate
branch. No fix, response time, continued maintenance, or support commitment is
promised. Older candidates may never receive fixes.

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
checksum-verified release in a disposable lab and validate SAN identity,
quorum, fencing and multipath behavior. Successful validation does not convert
this release candidate into a supported or certified production product.
