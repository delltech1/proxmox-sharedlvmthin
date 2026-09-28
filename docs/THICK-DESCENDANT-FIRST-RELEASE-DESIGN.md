# Thick Generations descendant-first release design

Status: source-design checkpoint only. No live cleanup, runtime or storage
authority is granted by this document.

## Proven conflict

The current abort terminal-release claim and descendant bridge are deliberately
single-consumer alternatives. The release claim consumes both the settlement's
`probe_used` state and its `_DescendantSettlementCapability`. The bridge and the
standalone post-reap probe require those states to be unused. Reversing the two
calls also fails because the bridge transfers the adapter into descendant
handoff while the current release claim requires the original reaped chain.

Those flags must never be reset. They record monotonic authority consumption,
not a retryable lock. A later `ECHILD` observation also cannot establish a
dedicated descendant domain, subreaper continuity or pre-fork ownership.

The existing release-first branch therefore remains permanently:

```text
descendants_qualified = false
resources_closed = false
runtime/storage/exec authority = false
```

Its source models remain useful for proving release ordering and ambiguous
outcome handling, but they cannot be promoted into a complete cleanup receipt.

## Required descendant-first branch

A qualified branch must establish one orchestration authority before fork:

```text
one supervisor identity
  -> one pre-fork enrollment/ticket
  -> allocated FD graph + dedicated ChildDomain share that exact ticket
  -> abort child origin consumes it once
  -> leader observation and exact reap
  -> status EOF and abort protocol MATCH
  -> settlement transfers once into descendant bridge
  -> bounded dedicated-domain drain reaches exact DOMAIN_DRAINED
  -> mint one opaque qualified-release capability
  -> ordered resource release may begin
```

No second ticket, late enrollment, reconstructed receipt or snapshot may
substitute for the original opaque capability chain.

## Deadline composition checkpoint

The allocated graph's `deadline_ns` is the unarmed/execution deadline. It is
not the descendant cleanup deadline. Equating the two would either remove the
cleanup budget or silently reinterpret an existing authority.

The first shared-ticket checkpoint therefore freezes three distinct values:

```text
execution_deadline_ns = allocated_graph.deadline_ns
cleanup_ms            = exact V2 request cleanup policy
cleanup_hard_limit_ns = execution_deadline_ns + cleanup_ms * 1_000_000
```

The prepared ChildDomain initially carries the hard limit. Reap admission later
computes the actual cleanup deadline as the minimum of the remaining budget and
that hard limit. A future opaque handoff may narrow the domain to that exact
settlement deadline once; it must never extend, reset or derive a new budget.
Until that narrowing gate exists, shared enrollment is only a pre-fork
source-model composition and cannot authorize descendant draining.

The graph remains the sole creator of the original pre-fork ticket. Shared
enrollment stores object-identity backlinks to the graph, ChildDomain, binding
and ticket. A second reservation, request/owner/thread/deadline drift, cleanup
policy mismatch, replay or partial composition poisons both branches and does
not create a replacement ticket.

## Next checkpoint: fork record and non-drainable attachment

The existing pre-fork validator must continue to require `ticket.used=false`.
It must not be broadened to accept either ticket state. A separate one-shot
post-fork transition records the exact `_ForkOrigin` produced by the only
`_record_owned_fork()` call and moves shared enrollment through
`FORK_RECORDING` to `FORKED`. It freezes exact attempt/lifecycle/origin object
references plus PID, token and supervisor evidence. It neither consumes the
ticket again nor creates a replacement.

After pidfd/capture binding, a separate allocated-only attach may claim the
launcher and enroll the adapter. It must publish a new opaque
`ATTACHED_HARD_LIMIT_ONLY` object, not the existing drainable
`DescendantBridge`. The existing `advance_descendant_domain()` must reject this
object by exact type. Reentry or partial attach poisons the complete shared
branch with no rollback, refork, second attach or automatic descriptor close.

This attachment remains non-drainable even if a future settlement deadline is
numerically equal to the stored hard limit. Only a later exact settlement gate
may mint a different capability after binding the one-way deadline narrowing.
The old release-first settlement consumer remains forbidden on this branch.

That deadline gate is now represented by a source-only
`DEADLINE_BOUND_NON_DRAINABLE` successor. It accepts only the exact normal
settlement produced by the attached branch, recomputes the deadline from the
original cleanup admission and frozen request, and narrows the prepared domain
once. Equality with the hard limit is valid but still consumes the one bind
attempt. The settlement probe and descendant capability remain unused.

Clock callbacks occur only after the bind attempt is latched. Complete frozen
settlement, monitor, descendant-capability, owner and watermark provenance is
checked before and after the callback. Failure before the deadline write leaves
the hard limit but poisons the branch; failure after the write must retain the
narrowed value and still mint no successor. The successor is a historical
non-drainable receipt. It does not reserve the otherwise unused settlement
capability against unrelated consumers, so every future drain gate must verify
the exact successor and revalidate that capability immediately before use.

## Allocated drain-ready transfer

The next source-only gate consumes that exact successor in one orchestration.
It first publishes a mutable `TRANSFERRING` evidence record, then consumes the
settlement capability before the first clock callback. This ordering prevents
the legacy post-reap probe from becoming a second consumer. It subsequently
consumes the original exact leader-reap capability and, within the unchanged
deadline, admits that exact leader to the dedicated domain.

The successful result is `ALLOCATED_DRAIN_READY`, with the domain only in
`DRAINING`. No `domain.step()` occurs in this transfer. Every effect has
separate attempted/confirmed evidence. Failure never resets a used capability,
rolls the adapter back to `REAPED`, repeats leader acceptance or publishes a
usable bridge. Strict preflight covers all scalar and namespace surfaces before
comparison, including the settlement deadline and full nested RealFD validator
chain. A pinned original-chain proof is followed by a complete callback-free
revalidation so mutations during nested validation cannot become READY.

This checkpoint authorizes only the next source-model domain step. It does not
qualify descendants, close resources or authorize runtime/storage work.

The first allocated domain advance is now represented as a separate one-shot
source-model gate. A strict adapter guards the complete immutable outer
authority before and after every modeled callback. It normalizes all returned
values to callback-free built-ins, pins both the nested ChildDomain dispatch
surface and the model backend methods, and records syscall effects before any
post-callback authority check.

The only successful classifications are pending, one adopted child reaped, and
model ECHILD drained. All remain explicitly nonqualified. An adopted pidfd may
not alias the leader pidfd, a pipe FD or the capture epoll FD. A reap or close
whose effect may have occurred is retained as UNKNOWN and cannot be repeated.
The returned receipt is stored immediately and must exactly match domain owner,
leader, records, pending state and terminal ECHILD. No result recreates the
original READY capability or mints qualified-release authority.

The direct guarded ECHILD branch now additionally mints one private immutable
`AllocatedTerminalDomainEvidence` seal at the original success point. The seal
binds the exact result, bridge, shared enrollment, attempt, domain, original
ticket/origin/lifecycle, settlement, consumed capabilities, cleanup deadline,
watermark, ECHILD receipt and still-owned FD graph. Pending and adopted-child
branches mint no seal. A phase-aware callback-free validator rejects mutable
receipt reconstruction, owner/request/domain drift, poisoned or active FD
graphs, a closed/replaced epoll, origin drift, custom scalar/container types
and any adopted-child effect on the direct ECHILD path.

This seal remains explicitly nonqualified. Review found that the descendant-
first branch has not yet performed its own status-tail EOF observation and
abort-protocol MATCH after consuming the settlement capability. The legacy
status/protocol validator assumes the release-first capability state and must
not be reused by resetting those monotonic flags. A separate allocated
post-drain status/protocol checkpoint is therefore mandatory before any
qualified-release successor may be minted.

The first half of that checkpoint is now represented by the isolated
`prelive_allocated_abort_protocol.py` source model. It reserves the exact
terminal seal once and issues at most one modeled one-byte read against the
original allocated `status_parent` identity. It has a separate protected
watermark and never changes the closed ECHILD/domain watermark. Only exact
`EOF` with an empty byte string, confirmed before the original absolute
deadline, mints immutable `MODEL_ALLOCATED_STATUS_EOF_NONQUALIFIED` evidence.
DATA is a terminal mismatch; EAGAIN remains nonqualified; malformed, ambiguous
or post-effect drift becomes UNKNOWN with the attempted/returned observation
retained and no retry.

The backend callable/namespace surface and complete terminal/resource
authority are guarded around every callback. Active-consumer reentry,
callback-writable classification fields, clock rollback, backend class/marker
replacement and custom namespace/scalar values cannot manufacture EOF. The
result still does not compare exit/canary evidence and therefore is not abort-
protocol MATCH or release authority.

## Qualified-release capability

Only an exact successful descendant-domain drain followed by the dedicated
allocated status/protocol qualification may mint the successor release
capability. `DRAINED` or the terminal seal alone is insufficient. The
successor must bind, by identity and immutable evidence:

- the original supervisor, request and shared pre-fork ticket;
- the allocated graph enrollment and original fork origin;
- leader terminal observation and consuming reap receipt;
- status EOF and exact abort canaries;
- settlement and descendant bridge objects;
- terminal dedicated-domain ECHILD/drain evidence;
- the unchanged absolute cleanup deadline;
- the exact still-owned resource preimage.

Pending children, held descendants, timeout, owner/subreaper drift, UNKNOWN or
any callback ambiguity mint no capability. Consumption is one-shot and cannot
fall back to the old release-first claim.

## Mandatory source-model tests

1. Existing release-first claim then bridge/probe: refusal before syscalls.
2. Bridge/probe then existing release claim: refusal before release callbacks.
3. A second ticket, late enrollment, foreign domain or detached receipt: refusal.
4. Nested cross-consumer calls cannot mint two successor authorities.
5. Pending/held descendants, deadline crossing and ambiguous reap stay
   nonqualified.
6. Exact `DOMAIN_DRAINED` mints only the original terminal seal; missing
   allocated status EOF or protocol MATCH refuses qualified release.
7. Drift during the final domain callback mints nothing.
8. A qualified source-model drain still does not prove a live FD close or
   authorize storage work.

The dedicated matcher now exists as a separate source-model checkpoint. It
consumes exact allocated status EOF once and accepts only the frozen exit-73
leader observation/reap plus exact full-capture stdout and stderr canaries.
Its MATCH capability is deliberately nonqualified: it cannot close resources
or authorize runtime/storage work. Exact namespace, backlink, identity and
request-byte preflights precede reused validators so custom getters, keys and
equality callbacks cannot execute during matching or revalidation.

The following source-model gate consumes only that MATCH leaf. It preserves
the already-established terminal/status consumer chain and produces
`MODEL_ALLOCATED_RELEASE_INPUTS_QUALIFIED_ONLY`. This is not a live release
grant: no current clock has been checked, descendants remain unqualified and
all resource/runtime/storage flags remain false. A later close boundary must
consume this leaf once, prove current time is still below the original
deadline and revalidate the live resource graph without granting a new budget.

The next isolated checkpoint performs one modeled deadline observation over
that leaf. It freezes the original deadline and prior watermarks and accepts
only an exact integer `W <= now < D`. Rollback, equality with `D`, expiry,
malformed returns, reentry or post-callback evidence drift consume the leaf
without producing a capability. The timestamp is retained before postchecks.
The success capability remains model-only: it proves neither a live clock nor
authorization to close resources later.

## Live boundary

The eventual live branch additionally needs acquisition-time child-pidfd anchor
provenance and an uninterrupted controlled compare-to-close implementation.
Neither the assumed anchor model, a late duplicate, a point-in-time KCMP probe
nor the isolated self-pidfd gate provides that proof.

The immediate successor is an opaque source-model resource plan. It binds the
modeled deadline leaf to the original epoll wrapper, capture stream objects,
owned-child pidfd object, allocation authority, five distinct descriptor
numbers and the fixed close order `epoll -> status -> stdout -> stderr ->
pidfd`. Its commit is one-shot and terminally poisoned after any partial
reservation failure.

The plan freezes an ownership binding only. It does not transfer close
ownership, establish acquisition-time OFD anchors, authorize a future live
close or prove that aliases do not exist. A later live primitive may consume
it only as provenance input; compare-to-close must be immediate and cannot
return a reusable permission to the caller. Epoll requires its own wrapper-
aware experiment because raw-closing `epoll.fileno()` while the Python object
still owns it can make later finalization target a recycled descriptor.

Disposable Linux qualification confirms the wrapper rule. The only permitted
primary epoll effect is `poller.close()` after the handle relinquishes its
poller reference; raw `os.close(poller.fileno())` is forbidden. An epoll dup
anchor remains independently owned and keeps the kernel OFD alive, so closing
the wrapper cannot set `resources_closed`. The existing capture helper is now
phase-aware for both the original `BOUND` phase and the standard terminal
`SETTLEMENT_FINISHED` phase, while an active monitor or any other state still
fails closed.
