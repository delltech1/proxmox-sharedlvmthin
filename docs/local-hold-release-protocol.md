# Local maintenance-hold release protocol

Status: design gate only. No live hold-release executor is shipped or
authorized by this document.

## Exact-restoration model checkpoint

`experiments/thick-generations/layout-migration-exact-restore-model.py` is an
unshipped coordinator model, not a production executor. It adds a create-only
four-node pre-barrier baseline, exact control-only package preservation,
independent full release-chain verification before restoration, and phased
per-unit restoration. Its only effect vocabulary is typed `UNMASK` and `START`;
neither maps to a live command in this module. Every result retains authority
`NONE` and all rollout/runtime/release authorization bits remain false.

Previously inactive units are never started. Preexisting masks are retained.
`pve-guests` is never stopped or started: only a mask introduced by the barrier
may be removed. Active-and-masked target services are currently refused before
baseline enrollment, since their exact restoration needs an additional reviewed
contract. Essential and preserved service state is checked across the cohort.

An intent without an exact durable completion is inspection-only after a lost
ACK; the model never redispatches it, even when current service state looks
successful. A durable completion can be reread by a new coordinator, subject
to fresh exact state, boot, quorum, workload and certificate-chain checks.

Production rollout remains blocked on all of the following integrations:

- A pinned, authenticated four-node transport and trusted collector capturing
  the baseline before the first barrier effect, including systemd unit/mask
  namespace identity, jobs, executor/cgroup evidence and overrides.
- A durable backend with root-owned pinned namespaces, create-only fsync
  acknowledgements and independently qualified crash/namespace fault tests.
- The full composed ALL_CERTIFIED plus exact three SAN archive receipt adapter;
  the control-only fourth node must participate without package/SAN mutation.
- A reviewed local restoration executor with per-effect attempts, identity
  rechecks, explicit uncertain-outcome reconciliation and no mutation retry.
- Common writer/archiver lock and writer-version exclusion qualification,
  package-source identity pinning and explicit release authorization review.

The existing target-service executor is not a substitute: it targets all
services active rather than their recorded pre-barrier state and has an
effectful existing-intent path. The new model does not enable that path.

### Unshipped local prerequisites

`layout-migration-exact-local-journal.py` provides a local NONE-authority
create-only adapter below the fixed production-compatible
`/var/lib/pve-sharedlvmthin/maintenance-coordinator/<tx>` namespace. It pins
ancestor/transaction/lock identities, uses `O_NOFOLLOW`, private modes and
single-link regular records, nonblocking flock, bounded canonical JSON and
file/directory/parent fsync. Reconciliation only confirms an exact expected
record; it never returns permission to retry an effect. A partial record is
retained and refuses. The explicit `for_test` constructor confines disposable
filesystem tests to their private anchor; it is not an exposed root override.

`layout-migration-exact-baseline-collector.py` is root-only, local-only and
read-only. It brackets the existing scoped workload/process/DM collector with
two observations, pins configuration directories and systemd vendor/mask
namespaces, and preserves the package artifact identity. Runtime/local service
overrides and drop-ins outside the exact persistent-mask case are refused.
Collection runs in a bounded private child process with bounded output and
memory; proc/sysfs semantics remain the existing collector's trust boundary.
Its durable `LOCAL_BASELINE_RECORDED` result is not `CLUSTER_HELD`.

Effective source binding additionally requires fixed-argv `systemctl show
--all` evidence for `Id`, `LoadState`, `FragmentPath`, `DropInPaths`,
`SourcePath`, `UnitFileState`, `Transient` and `NeedDaemonReload`. Every key
must be present exactly once. Empty SourcePath/DropInPaths explicitly mean no
generator/source script and no loaded drop-ins; missing keys or empty critical
identity/state fields are not accepted as evidence. Dirty/reload-required,
transient, generated, control, linked, alias and runtime-only states refuse.
An unmasked fragment must be the exact stock vendor unit. The sole path alias
exception is a root-owned, inode-pinned `/lib -> usr/lib` (or `/usr/lib`)
usrmerge link, whose target and identity are rechecked. A masked fragment must
be `/dev/null` with both an observed persistent mask and exact masked states.
Effective properties bracket a vendor inode/content reread; both properties
and filesystem identities must remain identical across the full double capture.

Neither module is shipped or connected to any service command. Native POSIX
filesystem/child fault tests, source-identity qualification, qualified local
filesystem behavior, and the authenticated cross-node transport/release-chain
adapters are still required. No live effect authority is granted by this
checkpoint, by dependency injection, or by an `authority=NONE` receipt.

### One-effect local restoration component (unshipped)

`layout-migration-exact-local-restore.py` consumes one exact model request.
It has no CLI and no default authorizer or baseline provider. Its fixed
production identity reader additionally refuses execution outside the future
installed path or without a trusted fresh-loader digest map for its complete
local source dependency set. These missing integrations are intentional
fail-closed boundaries, not caller-selectable bypasses.

Only `systemctl unmask -- <fixed-unit>` or `systemctl --job-mode=fail start --
<fixed-unit>` can cross the effect boundary. STOP, MASK, RESTART, arbitrary
argv and `pve-guests` START are absent. The node/boot/package, preserved or
drained predecessor, durable baseline, exact source namespace and bounded
explicit one-effect authorization are checked before a create-only local
intent and again immediately before dispatch. The proposed local journal root
is `/var/lib/pve-sharedlvmthin/maintenance-restore`; it is separate from the
coordinator journal. CONTROL_ONLY nodes use the same service contract while
retaining their bound unchanged package identity.

A completion receipt follows an exact zero-exit result and verified target
service/source state. A durably completed effect can be re-observed after lost
ACK without running a command, including after authorization expiry. An intent
without completion remains `UNKNOWN_RETAIN`: even an apparently correct
current service state never authorizes redispatch or manufactures historical
success. Every returned receipt has authority `NONE` and cannot release the
cluster. The trusted transport, composed release validator and fresh-process
loader remain unimplemented prerequisites to any live use.

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
package receipts. An experimental repository-owned writer now creates and
transitions the local hold using `.maintenance.lock`; the file-only archiver
also uses this lock. This is not yet a production coordinator qualification:
all installed and legacy receipt/writer paths still require a complete shared
lock and writer-version exclusion audit before live release is enabled.

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

### Closed local release verifier (unshipped)

`layout-migration-exact-release-validator.py` is a pure, offline adapter. It
re-runs the real role-aware ALL_CERTIFIED v2 evaluator, reconstructs the model
baseline from four exact local captures, recomputes the workload digest and
managed guest/config scope, and compares raw service executor evidence with
all four projected current observations. Three exact SAN archive authorizations,
attempts, completed receipts and fresh active-absent/archive/certificate readbacks
must match the freshly evaluated certificate chain. No CONTROL_ONLY archive is
accepted. Current observations must postdate the entire SAN archive cohort.

One canonical, closed evidence envelope has a maximum 30-second validity.
The grant binds the model request, plan, boot/roles, package, configuration,
workload, baseline, release chain, executor identity and exact before/after
service sources. Earlier restoration phases must be complete; originally
inactive services and preexisting masks retain their protections. One validator
instance permits only an identical pre-effect bracket, never another request
or cohort. Durable one-shot enforcement remains the local executor's journal.

All thirteen imported verifier sources execute from immutable, individually
hash-pinned bytes, including nested imports, without reopening source paths or
reusing cached verifier modules. The source map is trusted code supplied by a
future reviewed fresh loader, not a field in the evidence envelope. This avoids
claiming that a before/after file hash proves which intermediate bytes ran.
The initial release adapter deliberately refuses `/lib` fragment aliases;
its accepted fragment path is the exact `/usr/lib/systemd/system` vendor path.

**Authority ceiling:** the production constructor always refuses. Only the
explicit `for_offline_test` constructor exists; its executor-shaped output is
not a live authorization or a transport authenticity claim. There is no CLI,
network access, effect invocation or generic callback. Root-owned source-loader
integration, authenticated/pinned cross-node collection of the exact evidence
and current readbacks, and fresh cohort challenge delivery remain unimplemented.
Self-consistent caller JSON cannot establish those facts. No package ships this
adapter and no existing local executor is wired to it.

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
