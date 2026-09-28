# Control-plane mutation admission and Thick PREPARE — design audit

Status: **design and qualification plan only; not implemented or released**.

This document explores two cooperative fail-closed protocols without changing
the guest RAW format or the Thick on-SAN generation format:

1. a pre-dispatch VG mutation record outside the SAN; and
2. a per-volume Thick PREPARE reservation that blocks new activation before
   remote absence evidence is accepted.

Neither protocol is fencing. They may convert an ambiguous operation into an
indefinite refusal, but they cannot make an unobservable writer safe to ignore.

## Facts established before implementation

- pmxcfs is quorum-controlled, replicated through Corosync and becomes
  read-only without quorum. It also offers distributed locks.
- pmxcfs is only POSIX-like. The PVE documentation explicitly says that
  `O_EXCL` and `O_TRUNC` creates are not atomic. Any implementation must use
  explicitly supported primitives and exact read-back; an ad-hoc
  `echo > /etc/pve/...` is not acceptable. Upstream pve-cluster documents
  directory creation as atomic.
- `PVE::Cluster::cfs_register_file` cannot register an arbitrary plugin path:
  it rejects paths outside PVE's built-in observed-file table. A private plugin
  record would therefore need a separately qualified pmxcfs file path and PVE
  atomic-file helper, not an invented `cfs_write_file` registration.
- Current PVE 9 pmxcfs source uses SQLite WAL with `synchronous=NORMAL`.
  Successful FUSE close/read-back is useful cooperative cluster evidence but is
  not yet proven to be a whole-cluster power-loss durability barrier. Exact
  PVE package/source, replication acknowledgement and reboot behavior require
  physical qualification before the word `durable` is used.
- Current Thick `activate_volume` does not take the canonical VG mutation lock.
  Therefore two absence audits alone retain an activation race. Activation
  admission itself must participate in the new protocol.
- Old plugin versions and direct administrator LVM/dmsetup commands do not
  participate. The protocol cannot be enabled until every storage-eligible
  node proves a compatible protocol version; external commands remain outside
  the safety claim.
- Directories below `/etc/pve/priv/lock/` have special stale-lock expiry
  behavior and are unsuitable for a non-expiring safety latch. Any prototype
  latch must use a dedicated qualified namespace outside that directory.

## A. Pre-dispatch VG mutation record

Proposed identity:

```
schema        = 1
vg_uuid       = exact configured VG UUID
storage_set   = digest of every same-VG alias and participating node scope
transaction   = random 128-bit ID
operation     = closed enum
node          = exact PVE node name
boot_id       = exact /proc/sys/kernel/random/boot_id
executor      = exact transient-unit identity
state         = PENDING | DISPATCHED | RESULT_UNKNOWN | POSTCONDITION_PROVEN
```

An ordinary replaceable JSON file under the canonical lock is rejected. A
paused lock holder can resume after the lock is considered stale and overwrite
a newer owner's record. The first operation must instead be atomic acquisition
of one fixed, non-expiring directory name derived from the exact VG UUID. An
existing, empty or malformed latch blocks; it is never overwritten.

Proposed normal ordering:

```
atomically mkdir fixed VG latch outside priv/lock
    -> EEXIST means BLOCKED, never takeover
write exact immutable PENDING identity inside the owned latch
    -> read back exact bytes/identity under quorum
acquire/revalidate the existing canonical VG lock
create exact transaction-scoped systemd unit/cgroup
    -> record unit invocation identity
dispatch exactly one mutating subprocess
wait for unit and every descendant to terminate
read exact SAN postcondition
write POSTCONDITION_PROVEN
remove/close record only after a second exact read
```

Rules:

- Failed or ambiguous PENDING publication dispatches no LVM command.
- An acquired but empty/malformed latch remains BLOCKED for explicit recovery.
- No caller replaces another latch's record. The transaction identity is
  immutable after initial publication; state changes must retain exact owner
  identity and reject stale writers.
- Age, timeout, quorum recovery, missing parent PID or successful lock acquire
  never clear a record.
- A second mutation encountering PENDING, DISPATCHED or RESULT_UNKNOWN refuses
  before dispatch.
- Process scanning is supplementary UNKNOWN evidence. Absence of a matching
  process is never SAFE evidence.
- A normal successful close requires exact executor termination and the exact
  operation-specific storage postcondition.
- Recovery of an ambiguous record requires either a positively fenced former
  executor node, or evidence proving that the exact node boot changed and no
  pre-reboot kernel/userspace executor can survive, followed by complete SAN
  reconciliation. The acceptability of boot change without fencing remains a
  design question and must not be assumed.
- Loss/corruption of the control-plane record while an old boot may still run
  is `CONTROL_PLANE_HISTORY_UNKNOWN` and blocks mutation.

### Required crash/partition tests

1. Pause A after observing no latch, let its ordinary CFS lock expire, allow B
   to acquire the fixed latch, then resume A. A must fail atomic acquisition,
   must not overwrite B and must dispatch nothing.
2. Kill after atomic latch acquisition but before PENDING write: no LVM process
   exists, but the empty latch blocks until explicit recovery.
3. Fail/partition during PENDING write: no LVM dispatch; every node refuses.
4. Stop after confirmed PENDING and before dispatch: B refuses; releasing A
   must not make B mutate automatically.
5. Kill the lock-owning parent after dispatch while its exact unit/child is
   alive or in D-state: B refuses, even after it acquires the canonical lock.
6. Let the old child finish after B's refusal: record remains unresolved until
   explicit reconciliation; no automatic retry.
7. Lose quorum on executor while storage I/O continues: majority refuses.
8. Reboot only the executor, then the coordinator, then the whole cluster at
   every state boundary; verify record visibility and boot-ID rules.
9. Crash pmxcfs/SQLite and perform abrupt simultaneous power loss after write
   acknowledgement. A lost last record must never result in an automatic SAFE
   classification while the old executor could still exist.
10. Exercise same-VG DUAL aliases concurrently; both must observe one record.
11. Mix new and old plugin versions. Adoption must refuse before the first new
    record can be relied on.

## B. Thick per-volume PREPARE reservation

A remote absence response is only a sample. To close the audit/activation race,
all cooperating activation and generation-mutation paths need a common atomic,
non-expiring per-volume latch. A replaceable reservation read at activation
entry is insufficient because a paused activation could resume after ACK.

Minimal proposed ordering:

```
atomically acquire fixed per-volume activation/PREPARE latch
    -> validate no incompatible control-plane history
    -> publish PREPARE(storage, volume key, tx, coordinator node/boot)
    -> activation on every node checks PREPARE under the same lock
    -> while still excluding new activation, inspect every peer for:
         exact TG frontend name and UUID
         any alias carrying the expected TG UUID
         direct active mapping/open path to the signed HEAD LV
    -> any present/unreachable/ambiguous peer => preserve PREPARE and refuse
    -> all ABSENT, plus the expected local runtime owned by the PVE operation
       => begin the signed generation transaction
```

The non-expiring per-volume latch is acquired before the revocable canonical VG
lock in every path that needs both, giving one global ordering:

```
per-volume persistent latch -> VG persistent admission -> canonical VG lock
```

Activation acquires the same fixed latch before its final check and retains it
through exact activation completion. If its outcome is ambiguous, the latch is
not removed. PREPARE therefore cannot ACK while a delayed cooperative
activation executor can still create a mapper. This must not serialize
unrelated VMs behind the whole-VG lock. Deactivation is allowed to reduce
exposure but must never clear PREPARE or transaction ownership.

PREPARE has no time-based expiry. Rebooted peers consult the replicated record
before activation. Closing it requires the exact terminal generation state and
the same executor/fencing rules as the VG mutation record.

### Migration behavior

- Ordinary Thick live migration may temporarily have source and target
  frontends. Both activation callbacks participate in the per-volume lock.
- A concurrent snapshot, rollback, resize or whole-volume delete publishes
  PREPARE first; if migration already established a second frontend, peer
  evidence refuses the lifecycle operation before a generation intent.
- If PREPARE wins first, target activation refuses, so PVE migration fails
  closed and performs its normal cleanup. The lifecycle operation may be
  explicitly retried only after peer absence is proven.
- No plugin callback fences a compute node or remotely removes a mapper.

### Required PREPARE tests

1. Pause activation after its initial state read but before mapper create;
   attempt PREPARE. Exactly one side wins the per-volume lock.
2. Pause migration after target activation and before source close; snapshot,
   rollback, resize and delete must refuse before intent/object mutation.
3. Publish PREPARE first; target activation and direct plugin activation on
   every peer must refuse.
4. Leave mapper under another name with the expected UUID, and separately
   activate the signed HEAD directly. Both must be PRESENT/CONFLICT, not ABSENT.
5. Peer unreachable, malformed response, stale boot ID or partial node scope
   must preserve reservation and refuse.
6. Reboot peer and coordinator at every PREPARE/ACK/transaction boundary; no
   activation occurs before unresolved reservation reconstruction.
7. Validate hundreds of volumes/nodes with bounded parallel probes. Sequential
   `nodes x timeout` latency is not acceptable for large clusters.

## Adoption and compatibility gate

The protocol must be disabled in legacy clusters. Explicit adoption requires:

- every storage-eligible node reports the same supported protocol version and
  installed package profile;
- no active mutation, materialization, migration bridge or generation intent;
- no existing PREPARE/admission ambiguity;
- all Thick frontend identities reconcile with VM ownership;
- a cluster-wide marker is published and re-read before any caller relies on
  the protocol.

Rolling downgrade below the adopted protocol version must be refused. Package
preinst/postinst/reboot gates must treat an adopted but unsupported protocol as
BLOCKED, not silently ignore its records.

## Current disposition

- The local same-VG process scan is retained as useful refusal evidence, but
  is not an authoritative replacement for this protocol.
- No pmxcfs record, transient LVM executor or peer PREPARE implementation is
  present in the current release candidate.
- Implementation is blocked on exact pmxcfs durability/replication testing and
  a reviewed activation/lock-order refactor. This is an engineering gate, not
  a claim that fencing is unnecessary.
- The Perl module is an admission-decision model, not the documented runtime
  state machine. Immutable identity and a future strictly validated state/
  evidence envelope remain separate. No acquisition result authorizes dispatch.
- Absence of a latch is not sufficient after possible control-plane history
  loss. The model requires a separate positive continuity proof even to request
  atomic acquisition; how that proof survives pmxcfs rollback/power loss is an
  explicitly unsolved runtime blocker, not something the mkdir test resolves.

## Executable per-volume executor model

The candidate library now contains a **non-runtime** decision model for the
same-transaction duplicate-executor problem. It deliberately does not select a
persistence backend or authorize current plugin I/O.

Immutable identity separates the storage `transaction` from one unique
execution `attempt`, and binds the latter to node, boot ID, exact systemd unit,
code/policy digests and eventually one systemd `InvocationID`. Its states are:

```
RESERVED -> BOUND -> TERMINAL -> exact close
                \-> UNKNOWN  -> no automatic close or redispatch
```

Reservation, not the transaction ID, is the dispatch authority. A second
attempt is rejected even when it carries the same storage transaction, and an
authority must positively prove that an attempt identity has never previously
been issued. A bind evaluation is only an atomic transition proposal and grants
no dispatch. A unit receives dispatch authority only in a separate handshake
after the winning BOUND record and exact InvocationID have been authoritatively
persisted. Terminal classification requires that exact BOUND identity plus
positive cgroup termination, absence of pending jobs, terminal I/O evidence
and the operation-specific storage postcondition. Any negative/ambiguous
terminal evidence becomes persistent UNKNOWN.

Close evaluation likewise returns only an **atomic compare-and-close
proposal**, never permission for an ordinary later `unlink`. The backend must
compare the complete current attempt and InvocationID while performing the
close in the same serialization boundary. This conditional-transition
requirement, rather than the pure evaluator alone, prevents a delayed closer
from deleting a newer reservation.

`tests/unit/control_plane_admission.t` exercises these invariants. This model
does **not** close the production P0: neither pmxcfs continuity nor a pinned
admission-node journal has been qualified, and no runtime caller consumes the
model. It is the executable contract against which a backend and systemd
runner must later be tested.

### Disposable integrated boundary

The current candidate also contains a deliberately non-runtime integration
harness. Its lab-only record kind and unit namespace cannot be used as a
production executor identity. A verified-bytes adapter makes the actual Perl
model the only state-decision source, while a local fsync/CAS ledger records:

```
RESERVED + consumed attempt
    -> launch_issued
    -> BOUND + exact startup_observed
    -> dispatch_issued + complete one-use grant
```

`launch_issued` means that submission may exist; it is never permission to
submit again. `dispatch_issued` means that a grant may already have reached a
paused runner; it is never permission to republish the grant. Authority boot
or epoch mismatch, malformed history, recovery hold, inherited ledger handle,
stale CAS or uncertain child termination all refuse further progress. The
startup/grant identity includes InvocationID, PID/start ticks, run nonce,
exact cgroup, runner and command digests, and the inert operation digest.

This local result is not the persistent cluster authority proposed above. It
does not prove pmxcfs continuity, peer PREPARE, fencing, reboot recovery or
storage safety. Its next permitted use is a bounded inert systemd fault
matrix; production plugin callers remain absent.

The next disposable normal-path integration additionally proves that the
persisted one-use grant can be delivered to one exact stock-systemd invocation
without exposing the ledger as a writable runner path. A typed D-Bus ExecStart
descriptor, exact process argv/start identity and canonical operation
descriptor are bound before dispatch. The marker remains inert. This does not
change the recovery rule: a BOUND record carrying `dispatch_issued` remains
occupied after the observed runner exits until a future separately qualified
finish/close protocol proves it safe to close.

Deterministic S1-S4 tests additionally demonstrate that a same-transaction
second attempt cannot acquire the occupied per-volume slot while A is stopped
before bind, after bind, after grant validation or after marker persistence.
The canonical ledger does not change on refusal. A grant already issued to A
is not treated as revoked: after exact pidfd-bound resume, A may complete, but
B remains excluded. This is the required safety invariant; timeout or UNKNOWN
does not create a free slot.

Primary reference:

- <https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html>

## First three-node pmxcfs primitive observation

On 2026-09-23 a disposable, uniquely named directory outside
`/etc/pve/priv/lock` was exercised concurrently from all three lab nodes while
the cluster was quorate. Exactly one `mkdir` succeeded and both losing calls
observed `EEXIST`. A small identity record subsequently produced the same
SHA-256 digest on every node. Exact cleanup removed only the named test file,
latch and parent, and absence converged on both peers within the bounded
observation loop.

This supports atomic create and ordinary replication/visibility on the tested
cluster. It does **not** prove power-loss durability, partition behavior,
non-quorate behavior, stale-executor exclusion or safe automatic latch close.
The first attempt also demonstrated why immediate post-remove visibility must
not be treated as a synchronization barrier: local cleanup completed before an
unbounded immediate peer assertion could be relied on. Runtime admission must
therefore require explicit peer evidence and treat delayed/unknown close
visibility as blocked rather than infer safety from local absence.
