# Thick authority continuity contract review

Status: **`BLOCKED_ON_CONTINUITY_PROOF` for production runtime wiring**.

This document selects the smallest candidate architecture for further review:
one explicitly pinned admission node, one local persistent ledger outside the
SAN and no automatic authority failover. It does not claim that this backend,
pmxcfs or a filesystem `fsync` is a rollback-resistant cluster safety barrier.

## Safety property

An unavailable, missing, corrupt, older, restored or otherwise unproven
authority history never means an empty executor slot. It means refusal. A new
authority, epoch or enrollment cannot be created merely because the old one is
unreachable.

Normal reserve admission requires all of the following to agree exactly:

- cluster, canonical VG and storage-set identity;
- authority ID, node, boot ID and enrollment epoch;
- policy/code binding;
- the digest of the complete canonical enrollment, including storage alias and
  scope;
- canonical ledger revision and byte hash;
- an authority-head witness in a rollback-independent trust domain.

Matching these fields in the current review model proves only that its supplied
assumptions agree. It does not authorize reserve, dispatch or storage I/O. A boolean named
`continuity_proven`, a larger revision in an untrusted copy, a CFS lock, a
process scan or current mapper absence is not a substitute for this evidence.

## Why the selected candidate is not yet sufficient

The pinned node and its local ledger remove automatic split authority and SAN
write ambiguity from the design. They do not detect restoration of an older,
internally consistent ledger and enrollment. A revision, hash, epoch and boot
ID stored only inside the same rollback domain can all be restored together.

pmxcfs may distribute enrollment and discovery data, but its exact
acknowledgement, reboot, quorum-loss, backup/restore and power-loss semantics
have not been qualified as an independent monotonic witness. It is therefore
not assigned that role by this contract.

An object that merely calls itself `ROLLBACK_INDEPENDENT` is not a witness.
The future witness must be authenticated, current and non-replayable, and its
head update must have a qualified atomic relationship with ledger CAS. An old
ledger plus the correspondingly old but once-valid witness must not pass.
Timestamps and additional self-reported booleans do not establish this.

Until a suitable witness and verifier are selected and qualified, production
reserve remains blocked even when the supplied model objects match. The
review model under `experiments/thick-generations/authority-continuity-model.py`
makes this missing proof an explicit input instead of manufacturing it from a
same-domain boolean.

## Failure decisions

| Observation | Required result |
|---|---|
| Authority unreachable | Refuse; no fallback node |
| Missing or corrupt ledger | Refuse; do not initialize |
| Authority node, boot, ID or epoch mismatch | Refuse |
| Observed ledger older than independent head | Refuse as rollback |
| Observed ledger newer/different from independent head | Refuse as ambiguous, not success |
| Restored old enrollment points at former authority | Refuse; replay-resistant current witness verification is absent |
| Same-VG aliases name different authority bindings | Refuse all affected mutations |
| Old writer cannot be observed | Refuse re-enrollment |
| Old plugin or direct administrator LVM command | Outside cooperative guarantee; recovery cannot infer safety |

Lost acknowledgement remains UNKNOWN. It never causes a retry, alternate
authority selection or epoch reset. Read-only observation may follow, but it
cannot turn unknown history into continuity proof.

## Manual re-enrollment boundary

Re-enrollment is an explicit recovery operation, not failover. Before it may
become eligible, evidence must positively establish all of these facts:

1. the complete participant/potential-executor set is known;
2. every potential old executor is fenced or otherwise proven unable to run;
3. storage and all writer paths are reconciled;
4. the former authority is disabled;
5. the former history/witness generation is retired.

The model binds these assertions to the exact cluster, VG, old enrollment,
participant-set digest and a declared new recovery transaction whose freshness
is not yet verified. Even then it returns
only `MODEL_REENROLLMENT_PRECONDITIONS_ASSUMED`, with re-enrollment authorization
still false. It never dispatches an executor. Missing, false,
truthy-noninteger or ambiguous evidence refuses. The operational fencing,
non-replay and reconciliation verifiers remain unimplemented; the old epoch
must never be reused.

The alias evaluator checks only a supplied list of unique storage IDs. It does
not prove that the list contains every alias or participant. A singleton,
duplicate or internally consistent partial list cannot be treated as cluster
completeness evidence.

## Deliberately deferred

- production plugin or package wiring;
- automatic failover, lease, TTL, takeover or close/reuse;
- peer PREPARE reservations;
- SSH/RPC transport and remote executor dispatch;
- declaring pmxcfs or local ZFS/file persistence a monotonic witness;
- treating this model as proof against administrator bypass or old writers.

The next design decision is the independent authority-head witness and its
atomic update relationship with the local ledger. If no such supported trust
domain is acceptable, the honest outcome is permanent fail-closed operation
after history ambiguity followed by manually fenced reconciliation.

## Process-lifetime authority candidate

The smallest no-new-external-package candidate is not continuity across
restart. It requires one new long-lived, non-HA authority process holding the
expected ledger revision and canonical byte hash in RAM. No existing suitable
runtime owner has yet been selected. Its state machine is:

```text
COLD --explicit qualified enrollment--> ACTIVE(session, RAM head)
ACTIVE --head mismatch or ambiguous persist--> POISONED
ACTIVE/POISONED --process loss--> COLD
```

There is deliberately no automatic COLD-to-ACTIVE path. `Restart=no`, unit
name, PID, boot ID or systemd InvocationID can help identify a process lifetime
but cannot authorize its successor. Every session additionally needs a fresh
RAM nonce, exact process start identity and client pin. A delayed request or
response from another session refuses. The current response evaluator proves
only correlation with session, request and current head after in-flight
completion; replay uniqueness and ACK consumption remain unimplemented.

One authority serializes mutations. Before a modeled mutation it compares the
disk head with the RAM head and marks the exact request in flight. After an
exact persisted CAS it advances the RAM head before acknowledging. Ambiguous
persist or any mismatch poisons the session. Process death before or after
persistence produces COLD; a successor cannot infer ACTIVE from disk.

At qualified observation points this detects a ledger mismatch while the exact
process remains alive. It does not prove detection of restore-and-restore
between observations, nor survive daemon loss, reboot or VM checkpoint/restore.
An old executor may survive authority death, so recovery still requires
complete fencing and reconciliation. The pure model is
`experiments/thick-generations/process-lifetime-authority-model.py`; all of its
apparently positive transitions retain `runtime_authorized=0`.

Current upstream facts do not change that boundary:

- [pmxcfs](https://pve.proxmox.com/pve-docs/chapter-pmxcfs.html) provides
  cluster replication, quorum read-only behavior and distributed locks, but
  those properties alone do not prove a non-replayable head atomically coupled
  to the separate ledger.
- Local ZFS snapshots can intentionally restore older filesystem state, so a
  file/hash in the same snapshot domain is not an anti-rollback witness.
- TPM NV counters are a possible future independent primitive, but require
  hardware/provisioning qualification and crash-ordering design; a vTPM
  restored with the same VM is not assumed independent.

The model may be classified only as
`MODEL_PROCESS_LIFETIME_REFUSAL_CONTRACT_REVIEWED`. A real daemon, enrollment
procedure, RPC authentication, fencing verifier and production wiring remain
out of scope.
