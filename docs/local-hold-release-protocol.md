# Local maintenance-hold release protocol

Status: design gate only. No live hold-release executor is shipped or
authorized by this document.

## Safety boundary

The read-only `ALL_CERTIFIED` result is not permission to remove a maintenance
hold. A node may archive its exact local hold only after a separate bounded
authorization tied to the exact transaction, generation, commit, node, boot,
manifest, certificate, ALL_CERTIFIED plan and node ACK.

The only permitted local effect is:

```text
/var/lib/pve-sharedlvmthin/maintenance/active.json
  -> /var/lib/pve-sharedlvmthin/maintenance/archive/
     <tx>-<generation>-<commit-id>.json
```

Success means only `LOCAL_HOLD_ARCHIVED`. It never means that the cluster is
released. The cluster remains held until every participant has an exact local
receipt and a later coordinator barrier proves the complete set.

## Current P0 integration blocker

The installed maintenance verifier reads `active.json` securely and writes
package receipts, but there is currently no repository-owned coordinator that
creates/replaces `active.json`. The existing verifier and any external/manual
creator also do not share a single `.maintenance.lock` protocol.

A release-only lock would serialize two release processes but would not prove
exclusion from an older or non-cooperating active-manifest writer. Therefore a
live release executor is not yet safe to enable. Before integration:

1. every repository-owned active-manifest create/replace/archive path must use
   the same fixed `.maintenance.lock`;
2. the creator must publish a protocol/version marker understood by the
   release executor;
3. unknown or legacy writer protocol must refuse automatic release;
4. direct root manipulation remains outside the cooperative threat model and
   must be documented as capable of bypassing the plugin.

## Required authorization

Closed schema `slt-layout-local-hold-release-authorization/v1`:

```text
schema, authorization_id, tx, generation, commit_id,
node, boot_id, manifest_sha256, certificate_sha256,
all_certified_plan_sha256, node_ack_sha256,
candidate, target_storage_cfg_sha256, corosync_conf_sha256,
issued_at, expires_at, allowed_effects
```

`allowed_effects` must equal
`["archive-exact-local-active-manifest"]`. Issuance must follow the latest
ALL_CERTIFIED verification; expiry must not exceed the certificate release
deadline. The complete ALL_CERTIFIED chain is re-evaluated immediately before
the local effect.

## Fixed local namespace

```text
maintenance/active.json
maintenance/.maintenance.lock
maintenance/release-certificates/<tx>-<generation>.json
maintenance/archive/<tx>-<generation>-<commit-id>.json
maintenance/release-attempts/<tx>-<generation>.json
maintenance/release-attempts/<tx>-<generation>-receipt.json
```

All components are descriptor-opened from `/` with no symlink traversal,
root ownership and non-writable parent modes. Records are regular `0600`, one
link, size-bounded files. Source and archive must be directories on the same
qualified local filesystem. This protocol must not rely on `pmxcfs` atomic
`O_EXCL` or `O_TRUNC` behavior.

## Effect ordering

1. Acquire the common maintenance lock with a bounded nonblocking wait.
2. Recheck deadline, node/boot, config, quorum, package, payload, certificate,
   exact active bytes and the active inode identity from the local ACK.
3. Create and fsync a create-only attempt record bound to the authorization,
   source inode/bytes, target name and executor identity.
4. Recheck the deadline and every source/namespace identity.
5. Perform exactly one atomic no-replace rename using qualified Linux
   `renameat2(RENAME_NOREPLACE)`. There is no overwrite or link/unlink fallback.
6. Fsync the source and archive directories.
7. Prove active absence and exact archived inode/bytes.
8. Create and fsync a closed result receipt; only then report
   `LOCAL_HOLD_ARCHIVED`, always with `cluster_released=false`.

## Crash and replay rules

- Before rename: hold remains active; a new effect still requires an unexpired
  authorization and proof that the old executor ended.
- Rename stored then error, or crash after rename: first reconcile names and
  exact inode/bytes. Never issue a blind second rename.
- Active absent plus exact archive permits only receipt completion and fsync,
  including after authorization expiry.
- Active exact plus archive absent is not success.
- Foreign/newer active, conflicting archive, both names absent, inode
  ambiguity, or failed close/fsync is `UNKNOWN`/refusal without restoration.
- Never recreate `active.json` automatically after a possible release;
  operations may already have observed its absence.

## Qualification before live integration

The file-only fault suite must inject failure before and after attempt fsync,
rename, each directory fsync and receipt publication. It must cover
stored-then-error, crash replay, expiry during lock wait and immediately before
rename, concurrent executors, new generation, same bytes with a different
inode, archive conflict, cross-filesystem paths, FIFO/symlink/hardlink objects,
directory replacement and mode drift.

The executor remains development-only and must be absent from both Debian
profiles until the common writer-lock protocol and the complete fault suite
are independently reviewed and pass.
