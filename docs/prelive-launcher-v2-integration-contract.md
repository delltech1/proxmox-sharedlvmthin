# PREGRANT V2 launcher identity integration contract

Status: source-design gate only. No fork, grant, exec, package or storage use is
authorized by this document.

## Compatibility boundary

V1 remains an explicitly legacy model. V2 is a closed request/event contract;
there is no fallback and no shim that fills legacy `launcher_sha256` from an
unrelated digest. A V2 consumer must reject V1, a missing descriptor or an
unknown schema before any child-producing callback.

The existing synthetic relation

```text
owned.launcher.cmdline_sha256 = request.launcher.sha256
```

is forbidden in V2. Its only permitted projection is:

```text
owned.launcher.exe_dev     = descriptor.interpreter.dev
owned.launcher.exe_inode   = descriptor.interpreter.inode
owned.launcher.cmdline_sha256 =
    descriptor.inherited.proc_cmdline_sha256
```

The interpreter content hash, launcher source-manifest hash, inherited raw
cmdline hash and payload content hash stay in separate typed fields.

## Single-controller capability flow

1. Before the model fork boundary, one consumer binds the exact identity
   handle, existing PREGRANT controller and journal session, pre-fork ticket,
   control origin, request/owner/boot, original absolute deadline and frozen FD
   graph. Equal serialized dictionaries do not substitute for object identity.
2. `INTENT` must be durably acknowledged before a child-producing callback.
   The ACK binds the complete V2 request, canonical descriptor and digest,
   owner/boot/deadline, FD-graph digest and pre-fork enrollment identifier.
3. The existing controller supplies the actual opaque fork origin, owned pidfd
   handle, capture adapter/origin and control-channel objects. Two strict
   unarmed observations bind these objects to the descriptor.
4. `CHILD_BOUND` persists the INTENT/descriptor digests, exact child and
   lifecycle token, serialized unarmed observation and digest, interpreter/raw-
   cmdline projection and control/capture graph binding.
5. `EXEC_ISSUED` persists the two earlier event digests, descriptor and
   observation digests, attempt ID, unchanged deadline and
   `ONE_BYTE_G_THEN_WRITER_CLOSE`. An unknown ACK means no grant and no retry.
6. Only after that ACK may a fresh pre-exec observation mint one exact
   `PreExecModelCapability`. It is bound to the controller, control consumer and
   attempt and retained by exact reference in the identity handle.
7. Consumption marks both capability and attempt used before the first grant
   callback. Callback ambiguity cannot restore either. After every callback,
   the whole authority chain is rechecked; the last callback is followed by a
   callback-free final check.

There is one PREGRANT controller and one grant attempt. The integration must not
introduce a parallel supervisor, second worker or reconstructable capability.

## Consumption-time freshness

Before consumption, require exact owner thread, supervisor/boot, child
PID/starttime, lifecycle token, pidfd handle, fork origin, capture/control
backlinks, descriptor and FD graph. The child must be unarmed and nonterminal;
no status error, unexpected EOF, UNKNOWN state or prior reap is permitted.

Time is sampled from the same monotonic domain, cannot regress and must be
strictly below the original deadline. The deadline is immutable and cannot be
extended by a journal record, retry or recovery. The final in-memory check
after the last clock/status callback performs no further observation callback
before the grant attempt.

This checkpoint proves only consistency at defined observations. It does not
prove that privileged state cannot change before a future syscall.

## Result ceilings

`INTENT`, `CHILD_BOUND`, `EXEC_ISSUED`, readiness `R`, one-byte grant write,
writer close, status EOF and leader exit are independent facts. None proves
that the child observed EOF or crossed exec. Until a separately qualified live
backend exists, every result retains:

```text
runtime_authorized = false
storage_authorized = false
exec_proven = false
```

## Required negative gates

- V1/missing/unknown descriptor and source hash substituted for cmdline.
- Equal serialized state with foreign handle, ticket, controller or consumer.
- Foreign attempt, copied capability, second consumer or replay.
- Mutation/reentrancy during INTENT, CHILD_BOUND or EXEC_ISSUED ACK.
- Mutation after the last clock/status callback.
- Extended deadline, clock regression and exact deadline boundary.
- Lost EXEC_ISSUED ACK: no grant and no retry.
- Grant side effect followed by error/close ambiguity: attempt remains used.
- Float/bool aliases and custom scalar/container callbacks rejected before the
  custom method runs.
- Readiness plus EOF/writer-close/exit-zero never sets `exec_proven`.

The first implementation gate is a V2 request/descriptor validator and exact
one-shot consumer model. Real-FD wiring, fork, exec and storage remain separate
later gates.

## Implemented enrollment boundary

The closed schema-2 request validator and enrollment binding are now source-
implemented. They bind request/boot/owner, payload and interpreter projections,
the original computed absolute deadline and the exact identity handle. Current
state is strictly revalidated and cannot substitute an equal second handle.

This implements only the input/enrollment half of the first gate. It does not
yet persist or consume INTENT, CHILD_BOUND or EXEC_ISSUED capabilities and
therefore cannot produce a pre-exec or grant authority.

## Implemented model event chain

The exact V2 binding now owns one ordered model-only event chain. Canonical
INTENT, synthetic-fixture CHILD_BOUND and EXEC_ISSUED bytes are frozen before
an injected callback. A closed model ACK must identify the exact event,
sequence, byte length and SHA-256 before an immutable event capability exists.

The chain freezes enrollment, child and attempted-event evidence independently
and makes callback ambiguity terminal. This is a contract test for journal
ordering and acknowledgements, not proof of actual filesystem durability.
The source model now includes the exact existing-controller/session claim and a
fresh post-EXEC pre-exec consumer. The raw backend is not the journal session;
one canonical event chain claims that session, and direct, fake-chain or
reentrant appends fail closed. The controller claim freezes the original
request, owner, deadline, pre-fork ticket and pre-grant control origin and is
revalidated before and after every model ACK.

After an acknowledged `EXEC_ISSUED`, a distinct strict observation may retain
one exact `PreExecModelCapability` in the identity handle. One immutable
authority binds that capability to the same chain, controller, session,
attempt and control origin. Consumption revalidates the full chain and deadline
then marks capability, authority and attempt used before returning. The current
receipt explicitly records no grant attempt and no runtime, storage or exec
authority.

This remains a source-only consistency model. It has no live child backend,
durable filesystem journal, control-byte write or storage authority.
