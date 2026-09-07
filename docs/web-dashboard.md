# Read-only HTTPS dashboard

The optional dashboard shows cluster, storage, thin-pool and path health. It
has no LV create, delete, resize, snapshot, rollback, repair, rescan or service
restart endpoint.

## Configure

After installing the package, run as root on each node that should serve the
dashboard:

```bash
sharedlvmthin-web-configure
```

The wizard:

1. lists IPv4 interfaces and recommends the interface carrying the default
   route;
2. selects TCP 9443, or the next unused port;
3. generates a local RSA-3072 HTTPS certificate or validates an existing
   certificate/private-key pair;
4. restricts access to effective PVE administrators or an existing PVE group;
5. writes `/etc/pve-sharedlvmthin/web.conf` with mode 0640;
6. enables and starts `pve-sharedlvmthin-web.service`.

Open the displayed URL, normally:

```text
https://<node-address>:9443/
```

An automatically generated certificate is self-signed and browsers will show
a trust warning until its issuer/certificate is added to the organization's
trust store. For production, prefer a certificate issued by the organization's
CA with the node DNS name and/or address in Subject Alternative Name.

## Existing certificate

Choose option 2 in the wizard and provide readable PEM paths. The wizard
cryptographically verifies that the public key in the certificate matches the
private key before writing configuration. Keep the private key root-readable
only and never store it in Git, a release archive or `/etc/pve` cluster-wide
configuration.

After certificate replacement, validate and restart only the dashboard:

```bash
openssl x509 -in /path/to/server.crt -noout -subject -issuer -dates
openssl pkey -in /path/to/server.key -check -noout
systemctl restart pve-sharedlvmthin-web.service
systemctl status pve-sharedlvmthin-web.service --no-pager
```

## Firewall and reverse proxy

Allow the selected port only from trusted management networks. Do not expose
the dashboard directly to the public Internet. A reverse proxy may terminate
organization-trusted TLS, but must preserve HTTPS to the user, restrict source
networks and must not add a second authentication bypass.

The service binds to the exact address selected by the wizard. Binding to a
management address is preferred over a storage, migration, corosync or
wildcard address.

## Authentication and sessions

Authentication is delegated to the local PVE API. The password is used for the
login request and is not stored. The browser receives only a SharedLvmThin
`Secure`, `HttpOnly`, `SameSite=Strict` session cookie; PVE tickets remain
server-side.

`auth.session_idle_seconds` is an inactivity timeout (default 1800 seconds),
not an absolute time since login. Authenticated activity refreshes last-seen
time. Explicit logout invalidates the session; service restart clears in-memory
sessions. Health collector errors and HTTP 500 responses do not invalidate an
otherwise valid login.

## Monitoring cache

Health collection runs in one background worker. `/api/health` returns the
latest atomic in-memory snapshot with `refresh_running`, `generated_at`,
`age_seconds`, `last_success`, and `last_error`. A failed refresh retains the
last good snapshot and does not trigger storage mutation or logout.

The following values may be adjusted in `web.conf`:

```ini
[auth]
session_idle_seconds=1800

[health]
refresh_interval_seconds=30
collector_timeout_seconds=120
```

Restart only `pve-sharedlvmthin-web.service` after a deliberate configuration
change. The package preserves `web.conf`, certificates and private keys during
upgrade and same-version reinstall.

## Verify

```bash
systemctl is-active pve-sharedlvmthin-web.service
ss -lntp | grep ':9443'
curl --cacert /path/to/ca.crt https://<node-address>:9443/
journalctl -u pve-sharedlvmthin-web.service --since today --no-pager
```

If the dashboard was never configured, package installation leaves it disabled
and inactive. This is intentional and does not affect the storage plugin.
