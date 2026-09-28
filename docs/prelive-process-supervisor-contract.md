# Shared pre-live process supervisor contract

Status: **L0 INVENTORY ONLY — NO EXECUTOR AND NO LIVE AUTHORIZATION**.

The LVM synchronous-I/O qualification and lazy-zero lab need the same narrow
process primitive. They do not need a shared storage runner. The common layer
may prove only process ownership, bounded observation and terminal/reap
evidence. It must always return `postcondition_verified=false`; storage safety,
DM identity, A/B semantics, publication and cleanup stay in the caller.

## Required process model

- A fresh, single-thread supervisor with no unrelated children.
- Mandatory pidfd on the qualified PVE 9 runtime; no numeric-PID, process-name
  or process-group fallback.
- Process-local `PR_SET_CHILD_SUBREAPER`, set and read back before dispatch.
- One unarmed launcher, pinned before a separately persisted one-shot grant.
- Exact executable, argv and clean environment from the durable intent.
- One monotonic execution deadline and a separate bounded cleanup budget.
- Bounded nonblocking stdout/stderr capture in the event loop.
- Exact owned/adopted-child accounting. Empty process groups are insufficient;
  after reap the dedicated supervisor must observe `ECHILD`.
- Signals, when the persisted policy allows them, go only through an exact
  owned pidfd. There is no `killpg`, retry, redispatch or storage cleanup.

Durable `INTENT`, `CHILD_BOUND` and `EXEC_ISSUED` records precede the grant.
The active event loop must not perform synchronous journal `fsync`: blocking
the deadline observer on the evidence filesystem would defeat its purpose.
Bounded runtime observations remain in memory; the final receipt is persisted
only after terminal proof or the bounded cleanup attempt. A crash with no
final receipt is an unrecoverable UNKNOWN request and cannot be replayed.

Leader pidfd readiness alone does not prove descendant quiescence. A returned
stdout record alone does not prove that the process completed teardown. A
timeout or signal does not prove termination, especially for D-state. The
terminal success ceiling of this layer is `PROCESS_TERMINAL_EXIT0`, never
`SAFE_FOR_MUTATION`.

## Separation of responsibilities

The recovery-only LVM harness owns its fixed read-only argv, default-first A/B
ordering, configured VG UUID and output equivalence. Any ambiguous A forbids B.

The lazy-zero harness owns DM/loop identity, discard guard, exact tables,
publication, hydration and object cleanup. A terminal userspace command does
not prove that a requested kernel object exists or has the intended state.

## L0 evidence

`experiments/thick-generations/prelive-supervisor-l0.py` only inventories the
running runtime. It neither spawns a child nor changes the subreaper state. On
the disposable lab node, kernel `7.0.14-16-pve` and Python `3.13.5` expose
`pidfd_open`, `P_PIDFD`, `waitid`, `WNOWAIT`, `pidfd_send_signal`, epoll and
`PR_GET_CHILD_SUBREAPER`. The current process is not a subreaper. `/var/tmp`
resolves to the root ZFS mount advertised as `rw`. L0 does not execute a write,
prove actual writability, identify physical backing, qualify fsync durability,
or prove an independent storage/power-failure domain. Python attributes prove
API presence, not successful future pidfd/wait/signal operations; GET does not
prove a future subreaper SET. This is feature presence only.

Evidence: `/tmp/slt-prelive-supervisor-l0.json`, SHA-256
`4e3c367b6195d633db6373b1bcec33ab39b40f71beb12cdd515717d7c3655500`.

The next stage remains an isolated, fully composed executor API. The owned
leader/capture/settlement bridge and the descendant-accounting stage described
below are still separate source-only components. Only after review may a
separately authorized inert live harness spawn non-storage fixture
processes for immediate exit, exec failure, output flooding, held pipes,
`setsid` descendants, SIGSTOP timeout, grant ambiguity and supervisor crash.

## Model-only API status

`experiments/thick-generations/prelive_supervisor_model.py` now models the
common boundary without importing any OS process backend. It uses closed,
strictly typed request, executable, launcher, environment, child, command,
capture, monitor, reap and settlement receipts. Every callback receives a
defensive copy of the durable intent. The launcher is unarmed, observed,
pidfd-bound and observed again before durable `CHILD_BOUND` and `EXEC_ISSUED`
records permit a one-byte grant.

No journal callback occurs in the modeled monitor window. On every path where
a child may exist, failure invokes bounded settlement before attempting the
UNKNOWN record. Terminal success requires bounded capture, no descendants,
exact exit zero, bounded exact reap and `ECHILD` after reap. Even then the only
classification is `PROCESS_TERMINAL_EXIT0` with storage, postcondition and live
runtime authorization all false.

Three Astra review passes found and closed false-terminal cases involving
pre-reap ECHILD, callback aliasing, Python bool/int equality, unpinned launcher
identity, post-grant journal ordering, unused cleanup budgets and nested
float/bool identities. Final model-only review is GO; live remains NO-GO.

## Default-refusing fixture source

`prelive-supervisor-fixture.py` stages only the pre-grant Linux source. Its CLI
returns a plan or refuses every execute token before opening an executable,
journal or pipe and before `prctl` or `fork`. It cannot currently execute.

The unreachable reviewed source fixes the payload to pinned-FD
`/usr/bin/true`, requires safe open standard FDs, rejects low or aliased
internal descriptors, cleans every partial pipe allocation, inventories all
open descriptors before fork, and closes all non-whitelisted descriptors in
the child. The child reports readiness, accepts exactly one `G` byte followed
by EOF within its unarmed deadline, and uses FD `execve` with a closed
environment. The parent stores the caller-owned PID lifecycle record as its
first operation after the child branch, before closing FDs or attempting
pidfd work.

Source review found and closed pointer truncation in `prctl`, inherited FD
leakage, FD 0/1/2 collisions, partial-allocation leakage and a pinned
executable FD collision. Astra gives GO only for a local source-staged commit.
The journal, readiness/binding controller, monitor, settlement and reap remain
missing. No live process or process qualification is authorized.

## Source-staged journal status

The same fixture now contains an unreachable create-only journal primitive.
It creates a fresh direct child below a pinned `/var/tmp` parent and has no
reopen, resume, adoption or deletion API. Owner and sequential marker records
use bounded canonical bytes, O_EXCL/O_NOFOLLOW, exact owner/mode/link checks,
file fsync and directory fsync. Request, boot, PID/starttime and pinned
parent/root identities are present in every record.

The poison latch is monotonic. Reentrancy, cross-thread/process use, ambiguous
write/fsync/close, permission drift, renamed/replaced namespaces or record-name
replacement refuse further use without retrying the sequence. FD/name identity
is checked both before and after persistence while the record FD remains open.

This source stage contains event *markers*, not the complete command, child or
terminal evidence schemas. It is not wired to the launcher or model. Fsync may
block and therefore must never be invoked in the armed/monitor event loop.
Neither O_EXCL nor fsync proves power-loss durability, an independent failure
domain, or survival of ZFS rollback. Astra's GO applies only to committing the
inert primitive; live execution remains NO-GO.

## Source-staged owned-leader pidfd lifecycle

`prelive_owned_child.py` is a separate, unwired syscall-boundary primitive. It
accepts only a single-use internal fork-origin claim, validates the owning
supervisor process/thread, observes launcher identity on both sides of one
mandatory pidfd open, and verifies child waitability without reaping it.

Terminal observation uses `P_PIDFD` with `WEXITED|WNOHANG|WNOWAIT`. Exact reap
uses the same pidfd with `WEXITED|WNOHANG` and must reproduce the identical
PID/code/status receipt. Handle and fork ownership are nonduplicable, busy
states are monotonic, and ambiguous observe/reap/close or owner identity becomes
permanent UNKNOWN. Closing a live pidfd explicitly remains unsettled; closing a
reaped pidfd is only descriptor cleanup and cannot create terminal evidence.

The maximum result is `OWNED_LEADER_REAPED_ONLY`. Capture, descendants,
post-reap global `ECHILD`, payload exec, journal wiring and every storage
postcondition remain false/unqualified. The module has no CLI/runtime import
path and has not executed a real child.

## Source-staged bounded capture core

`prelive_capture_settlement.py` implements capture ownership, a fair
level-triggered drain turn, an absolute-deadline monitor and bounded passive
settlement. The exact owned-child receipt bridge retains opaque WNOWAIT
authority through one reap. It remains unwired to a launcher, journal, CLI or
storage operation.

An internal single-use origin binds two distinct borrowed FIFO read ends to
exact owner and FD identities. They must remain nonblocking, read-only and
exclusively used by the future supervisor. Identity and flags are rechecked
before each read and before a receipt; this detects observed drift but does not
prove uninterrupted identity against a concurrent privileged replacement.

Each stream is limited to four 64 KiB reads per turn. HUP is not EOF, EAGAIN is
not EOF, and EPOLLERR is UNKNOWN. EOF requires an actual empty read. Stored
bytes never exceed the fixed limit; overflow permanently marks truncation and
the digest scope remains `CAPTURED_PREFIX`. `FULL_CAPTURE` means only EOF with
no truncation for that stream. It does not prove leader exit, descendant
absence or any storage postcondition.

The handle and pipe claim prevent duplicate ownership; reentrancy, owner drift,
FD recycling, blocking-mode drift, foreign events and ambiguous close poison
the capture state. Partial epoll setup cleans only its internal poller and never
closes the borrowed read ends.

The source stage has an absolute execution-deadline monitor. It polls
in at most 100 ms slices, rechecks a nonregressing monotonic clock around the
terminal observer, and never resets its deadline for EINTR, output progress or
leader progress. Flooding remains fair. Leader terminal evidence and both pipe
EOFs are independent; late completion at or after the deadline is timeout.
Terminal receipts are bound to the pipe origin's exact lifecycle token,
PID/starttime and consistent exit code/status.

The primary-monitor success ceiling is
`LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED`; the attached passive-settlement
path can reuse the exact opaque receipt for one exact leader reap. Timeout and
truncation remain immutable primary outcomes. No signal or descendant claim
occurs. The separate P_ALL post-reap function is diagnostic only. The bound
assumes callbacks/syscalls are nonblocking and is not a hard-real-time latency
guarantee.

## Source-staged descendant-accounting domain

`prelive_descendant_accounting.py` is an isolated injected-boundary model. It
requires a fresh one-thread process, default non-auto-reaping SIGCHLD policy,
two empty-domain ECHILD baselines around a verified process-local subreaper SET,
one exact launcher origin and one exact leader-reap capability. Discovery uses
`P_ALL|WEXITED|WNOHANG|WNOWAIT|__WALL`; P_ALL never reaps. A discovered terminal
adopted child is identity-checked, pinned, re-observed and reaped exactly once
through P_PIDFD using the same `__WALL` domain.

Each public step is bounded to at most one discovery/bind/reap cycle. `None`
means children remain, not completion. Only exact ECHILD after the leader reap
and all prior adopted-child records can yield `MODEL_CHILD_DOMAIN_DRAINED`.
Busy/reentrant callbacks, owner/thread/SIGCHLD/subreaper drift, clock regression,
deadline crossing, identity or receipt mismatch and ambiguous close monotonically
produce UNKNOWN. A post-reap ambiguity remains recorded as pending evidence.

This component has no Linux syscall implementation, CLI or import from a
package. It has not enabled a real subreaper or spawned/reaped a live child.
`MODEL_CHILD_DOMAIN_DRAINED` is not a complete historical descendant list and
never authorizes runtime, storage or a postcondition.

## Source-staged opaque descendant bridge

`prelive_descendant_bridge.py` composes the exact owned-child, attached capture
settlement and descendant-accounting models without adding a syscall backend.
A noncopyable enrollment is created while the child domain is still PREPARED;
its separate pre-fork ticket must be consumed into the exact `_ForkOrigin`.
Origins created before enrollment or without that ticket cannot attach later.

After an internally proven leader reap plus stream EOF, two opaque capabilities
are consumed exactly once: the adapter's original `ExitReceipt`/fork-origin
chain and the settlement's frozen completion evidence. No normalized dictionary
or boolean recreates authority. The same absolute deadline and a monotonic clock
watermark flow from settlement into every descendant step. The transferred
object chain is revalidated before and after each step and immediately before
the strongest result.

The bridge preserves the original timeout/truncation independently of mutable
settlement receipts. It also preserves a returned adopted-child reap/close
record if a later authority check forces the overall result to UNKNOWN. The old
diagnostic post-reap probe and the bridge are exclusive consumers.

Its strongest classification is
`MODEL_LEADER_REAP_AND_CHILD_DOMAIN_DRAINED`, still with runtime, storage and
postcondition authorization false. It is not wired to the fixture, journal,
packages or production plugin and does not close duplicate-executor P0.

## Source-staged pre-grant composition

`prelive_pregrant_composition.py` models the only accepted pre-grant order as
three exact durable events: INTENT, CHILD_BOUND and EXEC_ISSUED. The journal is
then sealed before a single attempt-bound permit can write one byte to the
exact modeled grant channel and confirm writer EOF. No timeout, exception or
ambiguous callback permits a retry.

The possible-child and settlement latches precede the launcher callback. The
serialized child is tied to the current opaque owned handle, lifecycle owner
and token, consumed fork origin, launcher descriptor, pidfd, capture adapter
and channel. The complete chain and canonical event preimages are checked
before and after the grant callback. Post-EXEC deadline expiry, identity drift,
underlying authority poison or a changed permit returns UNKNOWN.

The strongest classification is
`MODEL_GRANT_PROTOCOL_COMPLETED_EXEC_UNPROVEN`. It does not prove payload exec,
terminal settlement, descendant drain, a storage postcondition or runtime
authorization. The implementation is an injected model with no real journal,
fork, pipe, process or storage backend and is not imported by either package.

## Source-staged exact-event journal adapter

`prelive_fixture_journal_adapter.py` validates and forwards the exact canonical
bytes of INTENT, CHILD_BOUND and EXEC_ISSUED to an injected source-only backend.
It does not reconstruct approximate marker records. Event order, previous
digest, request/owner/child/launcher/channel and attempt bindings use closed
schemas and strict types.

The session has one explicit controller and owner thread. Invalid ownership,
reentrancy or any ambiguous persistence acknowledgement permanently produces
UNKNOWN. Returned receipts and the final seal are immutable and noncopyable;
later event validation uses a separate frozen internal digest chain. The seal
prevents additional persistence and never grants execution authority.

The backend acknowledgement is only a source contract. It must bind the exact
filename, SHA-256, byte count, file and directory sync, confirmed close and a
typed record identity. A future real backend must independently prove create-
only dirfd-relative naming, inode/mode/owner continuity and fault behavior.
This source gate makes no filesystem durability, process or storage claim.

## File-only exact journal qualification

The create-only backend pins a fixed `/var/tmp` parent and one fresh nonce root
with directory FDs. Records use O_EXCL, O_NOFOLLOW and strict 0600 regular-file
identity, bounded write progress, file and directory fsync, confirmed close and
a post-close named identity check. Namespace identity is rechecked around every
record. It exposes no reopen, repair, rename or deletion operation.

UNKNOWN retains a monotonic poison latch. A one-shot descriptor release removes
FD ownership before each close, is bounded against reentrancy and cannot turn
UNKNOWN into a clean state. A separate verifier pins the preserved root again,
requires the original root and record dev/inode/uid/mode/link/size identities,
opens records nonblocking/no-follow, checks them before and after exact reads
and rechecks the final root identity and directory membership.

The single qualified run proves only exact bytes/order and identity at the
defined verification points on the tested local ZFS filesystem. It does not
prove power-loss durability, protection against later privileged replacement,
payload execution, grant safety or any storage postcondition.

## Source-model control channels

The parent control graph contains exact identities for both ends of grant,
status, stdout and stderr pipes before fork. One origin may bind one owned
child/capture/controller consumer only. Both the frozen graph and every current
endpoint are revalidated with strict integer types, FIFO identity, access mode,
nonblocking policy and close-on-exec policy at every externally callable model
boundary. Equality alone is insufficient because Python boolean and float
values can compare equal to integers.

Readiness requires exactly `R` followed by EAGAIN. An event or HUP only causes
another observation; EOF before independently proven exec is refusal. Error,
unknown bytes, duplicates, overflow, callback mutation, reentrancy, deadline
drift or identity drift monotonically poison the attempt.

The one-shot PREGRANT callback may attempt exactly one nonblocking byte `G` on
the frozen grant-writer FD and exactly one close. Confirming both operations
permits only `G_WRITE_AND_WRITER_CLOSE_CONFIRMED`. It does not prove the child
observed EOF, crossed exec, settled, or changed storage. A real adapter must
separately prove kernel FD behavior and must create the grant writer itself as
nonblocking; the current fixture pipe helper does not yet establish that fact.

## Isolated real-FD boundary

The first real boundary is deliberately smaller than a process launcher. Four
Linux pipes are allocated with close-on-exec on both ends. Only the grant
writer and the three parent readers become nonblocking. All eight descriptors,
their access modes and four distinct pipe objects are captured before any
future fork.

An internal allocation authority is retained separately from exposed graph
records. The final graph must still identify every originally allocated object;
only the planned NONBLOCK flag change is allowed. Every read, write and close
uses the authority FD after live identity and a final callback-free whole-graph
check. A replaced or unverifiable descriptor is quarantined rather than closed
by number. Close ownership is removed before the sole attempt, and a close
error can never be retried or converted to a clean result.

This boundary proves pipe allocation, flags, bounded nonblocking I/O and local
ownership cleanup only. It does not prove a child, grant observation, exec,
leader settlement, descendants or storage. A live qualifier remains forbidden
until launcher identity represents the real interpreter/launcher/payload chain
and normal completion has a one-shot exact reap handoff.

## Exact normal-completion handoff

A successful bounded capture is not settlement. When the exact attached child
has a cached terminal observation and both stdout/stderr reach untruncated EOF,
the monitor may mint one private normal-completion capability. The capability
binds the immutable completion digest, capture handle, adapter, cached exit
receipt, owner thread and execution deadline. Adapter binding is a pure
in-memory operation: it performs no callback or syscall after the monitor's
last authority check.

The settlement consumer burns the claim before any external callback and may
reap the exact cached pidfd receipt once. A deadline or identity failure before
reap dispatches no reap. A failure after the kernel reap preserves the truthful
`leader_reaped` fact but returns UNKNOWN and creates no descendant authority.
No retry or second worker is inferred from either result.

Current and transferred provenance require the canonical owned handle and exit
receipt, strict PID/starttime/pidfd types, exact lifecycle token, child boot ID,
supervisor identity, capture backlinks and the expected pre/post-reap lifecycle
phase. Mutating copies of the settlement receipt cannot create authority. Only
a valid post-reap normal capability may enter descendant accounting.

This closes the source-model normal-success-to-reap gap only. It does not prove
fork, exec, signal delivery, a real child, package integration or storage. A
live inert qualifier remains forbidden until interpreter, launcher-code and
pinned payload identities truthfully describe the future process chain.

## Allocated descendant hard-limit attachment model

The descendant-first source branch may attach its exact allocated FD graph,
owned pidfd, capture adapter and prepared `ChildDomain` only as an immutable
`ATTACHED_HARD_LIMIT_ONLY` receipt. This classification does not bind the
actual cleanup deadline, authorize descendant drain, prove runtime or permit a
storage operation. It is deliberately a different exact type from the
drain-capable descendant bridge.

Attachment consumes the shared fork capability before callback-capable
validation. It claims the exact launcher and enrolls the pinned adapter once.
Any replay, partial result or provenance drift poisons the graph, domain,
adapter and attempt and cannot be retried. Before publication a callback-free
closing check revalidates the frozen request, owned pidfd and lifecycle,
original allocated endpoint authority, current ownership sets, capture origin,
streams, limits, identities, poller and adapter backlinks. Capture stream
identities must still name the original validated stdout/stderr parent
endpoints. Callable-surface replacement is refused before the relevant effect.

This remains a source-only checkpoint. The next independent transition must
narrow the prepared hard limit exactly once to the actual settlement deadline;
only a later reviewed capability may enter descendant drain.

The attached branch now has a separate phase-aware continuation validator.
Ordinary zero-write grant close, bounded capture monitoring and exact leader
reap retain the same attachment and shared ticket; neither the shared state nor
the domain is reset to an earlier phase. Continuity is checked after monitor,
before reap dispatch and after the reap callback. A pre-reap drift dispatches no
reap. A post-reap drift retains truthful settlement evidence but poisons the
branch and cannot report qualified success or retry. The domain remains
`LAUNCHER_CLAIMED` and its hard-limit deadline remains unchanged.

After an exact normal settlement exists, a separate one-shot source transition
may narrow that hard limit to the settlement deadline. The deadline is
recomputed only from the original cleanup admission, frozen request budget and
original execution hard limit. Exact equality with the hard limit is allowed;
it does not make the attachment drainable. The transition latches before its
clock callback and revalidates complete settlement, monitor, descendant
capability, owner and monotonic-watermark provenance afterward. Its immutable
result is `DEADLINE_BOUND_NON_DRAINABLE`; settlement capability flags remain
unused and runtime/storage/descendant-drain authority remains false.

The following allocated-only transition may consume that exact deadline-bound
receipt into `ALLOCATED_DRAIN_READY`. It latches and publishes a mutable
transfer record before effects, consumes settlement/probe authority before its
first external clock, then consumes the original opaque leader-reap capability
and admits the exact leader into the dedicated domain. Deadline, owner, fork,
leader, settlement, FD allocation and nested validator provenance are checked
with exact built-in types and again after every callback-capable proof.

Partial transfer is terminal and truthful: attempted/confirmed flags and exact
capability references remain visible, no used flag is reset, no acceptance is
retried and poison cannot be overwritten by READY. The transition performs no
domain step, child wait/reap, signal, close or storage operation. Its strongest
meaning is only that a separately reviewed bounded descendant step may now be
attempted under the original absolute deadline.

One guarded allocated domain step may consume that READY authority exactly
once. The step backend is explicitly model-only and its callable surface is
pinned. Every callback is surrounded by a complete immutable authority check;
returned pidfd and wait evidence is recorded before the post-callback guard.
Leader, pipe and capture-epoll descriptors form a protected disjoint set that
an adopted pidfd cannot alias.

Pending, one adopted reap and guarded ECHILD are the only accepted outcomes,
and each remains nonqualified. A close/reap after-effect ambiguity is preserved
as UNKNOWN with no retry. The immutable receipt is bound to the exact domain
owner, leader, records, pending and ECHILD evidence. This source model neither
performs a live qualification nor proves global process quiescence, resource
closure, runtime safety or storage safety.

Only the direct guarded ECHILD outcome also receives a private immutable
terminal-domain seal. Its phase-aware validator rechecks the complete current
authority graph, exact open epoll and still-owned FD preimage; reconstructed
receipts, custom scalar/container types and mutable provenance drift are
refused. The seal is not protocol qualification: the allocated branch still
needs a separate bounded status-tail EOF observation and exact abort-canary
MATCH before a later checkpoint may mint any release successor. Consumed
settlement/probe flags are monotonic and must never be reset to reuse the
legacy release-first validator.

The allocated branch now has a separate one-read source-model status gate.
It consumes the exact terminal seal into one protected active operation,
derives the status FD only from the original allocation authority, and keeps a
separate monotonic watermark under the same absolute deadline. Exact empty EOF
creates immutable nonqualified status evidence; DATA, EAGAIN, malformed data,
deadline crossing, reentry or backend/authority drift cannot create it. Any
read that may have returned is recorded before later guards run, and the
operation is never retried. This gate performs no live read and still requires
a subsequent pure exit/reap/canary protocol matcher.

That pure allocated matcher is now implemented as a distinct one-consumer
source-model gate. Exact frozen exit 73, leader-reap identity, FULL_CAPTURE EOF
receipts and both protocol canaries are required for MATCH. Mismatch remains
terminal and mints no capability. The resulting capability explicitly keeps
descendant qualification, resource closure, exec proof, runtime authority,
storage authority and postcondition proof false. It cannot be substituted for
the later qualified-release successor.

Before any shared validation helper runs, the matcher proves exact settlement,
descendant, completion, persisted/bound authority and identity containers,
their backlinks and stored request bytes. Pre- and post-MATCH adversarial
tests require fake properties and custom comparison/key objects to be refused
without callback dispatch. No live read, close, retry or release is performed.

The protocol MATCH leaf may now be consumed once into
`MODEL_ALLOCATED_RELEASE_INPUTS_QUALIFIED_ONLY`. Terminal evidence remains
owned by the status operation and status EOF remains owned by the protocol
evaluation; neither consumed flag is reset or reassigned. Reentry and partial
publication poison the operation and attempt monotonically, retain the leaf's
true consumed state and permit no retry.

This successor proves only complete current source-model inputs. It has not
made a fresh deadline observation and therefore keeps
`current_deadline_checked=false`, `live_release_authorized=false`,
`descendants_qualified=false` and every resource/runtime/storage postcondition
false. A later live release gate must independently prove the original
deadline and exact live FD authority before issuing any close syscall.

One subsequent source-model gate may consume the release-input leaf and call
one closed modeled clock. It freezes the original absolute deadline `D` and
historical lower bound `W`; only an exact built-in integer in `W <= now < D`
creates `MODEL_ALLOCATED_RELEASE_DEADLINE_ADMITTED_ONLY`. Clock exceptions,
rollback, `now == D`, expiry and evidence/surface drift are terminal without
retry. A valid return is recorded before all post-callback checks.

The post-clock validator surface is itself frozen transitively across all
project-local validation module aliases, functions, classes and descriptors.
Checks use exact built-in namespace keys and identity comparisons before any
nested validator dispatch. Even success retains `live_clock_verified=false`,
`live_release_authorized=false` and every resource/runtime/storage outcome
false; the eventual close boundary needs an immediate independent admission.

## Forked-Python launcher identity model

`FORKED_PYTHON_LAUNCHER_V1` is a separate source-model descriptor; it does not
reinterpret the older synthetic launcher record. It keeps five evidence
domains distinct: the pinned Python interpreter object/content, verified
launcher source manifest and loaded-code assertion, inherited raw procfs
cmdline and launcher-environment policy, pinned payload object/argv/environment,
and supervisor/child/pidfd/FD-graph lifecycle binding.

All mappings, keys and scalar values use exact built-in types before copying,
hashing or equality. Current stored descriptors and child identity are fully
revalidated before deadline use and again before success. Numeric aliases and
custom scalar subclasses cannot use equality, comparison or deepcopy hooks to
manufacture a valid result. The original descriptor digest is never replaced.

Two exact post-fork unarmed observations must agree. A one-shot pre-exec model
check requires the original child, interpreter, loaded manifest, raw cmdline,
environment policy, payload object/argv/environment, FD graph, clean status and
strict original deadline. Its strongest classifications are
`MODEL_FORKED_LAUNCHER_BINDING_CONSISTENT` and
`MODEL_PREEXEC_BINDING_CONSISTENT`; both retain `exec_proven=false`,
`runtime_authorized=false` and `storage_authorized=false`.

Every object identity, hash, pin and time in this stage is supplied model data.
The model does not prove the loader, procfs observations, opaque pinned FDs,
fork/pidfd ownership, environment contents or exec. Runtime integration needs
a separately reviewed measurement backend and a consumption-time freshness
protocol before this evidence can participate in a grant.
