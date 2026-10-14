# Functional runner: orchestration foundation, NOT ready for live use

## Single-command release campaign contract

The intended operator workflow is one reviewed plan, one execution command and
one retained result directory. `release-functional-configurations.py` emits the
configuration matrix, mandatory lifecycle operations and separate functional,
burst and fault campaigns. It does not dispatch them. The runner execution
command below is the common entry point once synchronous live adapters and their
independent verifiers have been implemented and admitted.

Small disposable disks are sufficient for regular correctness qualification.
`case_options` compiles literal PVE create options and disk keys for each matrix
row without executing anything. The disk factor counts data disks; the separate
boot disk and optional EFI state are additional. It fixes the stock boot fixture
at 1 GiB RAM and each test data disk at 64 MiB. Storage-mode binding, artifact and
platform identity, resource reservation, runtime admission and actual guest/data
verification remain live-adapter responsibilities. Valid options are not proof
that a firmware/controller/NIC combination boots or supports RAM snapshots.
Burst/fault campaigns are explicit additional plans, not hidden actions during
a routine check. Disk shrink must exercise native refusal and prove unchanged
size/data; it must never force a destructive reduction. A migration unsupported
by a mode must prove documented refusal and unchanged ownership/data rather
than silently skip it. A planned case, missing adapter or absent evidence is
NOT_RUN, never PASS. A stopped campaign keeps all remaining steps NOT_RUN.

The operator should receive per-step progress, an exact package/platform tuple,
independent data/RAM/config postconditions and a final machine-readable summary.
No future release is qualified merely because the orchestration fixtures pass.

`release-functional-runner.py` does not implement storage adapters, inspect live
platform identity, reserve VM IDs, or provide cluster-wide fencing. A validated
plan contains declarations, not proven admission. Never use this foundation as
evidence that functional release coverage has run.

Minimal schema (replace fixture executables with reviewed synchronous adapters):

```json
{
  "schema": 1,
  "identity": {
    "profile": "dual",
    "package": "exact-package-name",
    "version": "exact-version",
    "platform": "exact-platform-tuple-description",
    "artifact_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "platform_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  },
  "disposable_ids": [999901],
  "mandatory_coverage": ["fixture"],
  "steps": [{
    "id": "fixture",
    "argv": ["/absolute/reviewed-command-adapter", "argument"],
    "verify_argv": ["/absolute/reviewed-postcondition-adapter", "argument"],
    "success_marker": "EXACT_POSTCONDITIONS_VERIFIED",
    "poll_seconds": 30,
    "command_deadline_seconds": null,
    "coverage": ["fixture"]
  }]
}
```

Inert validation: `python3 release-functional-runner.py /absolute/plan.json`.
Execution requires Linux and explicit approval of exact plan bytes:

```text
python3 release-functional-runner.py /absolute/plan.json --execute --plan-sha256 EXACT_SHA256 --run-dir /existing-parent/new-unique-run
```

Adapters must establish exact artifact/platform/disposable identities and
exclusive mutation admission before effects. They must wait for exact native
task termination; never detach work and return success. The verifier must check
actual postconditions and print its exact marker once. Shell expansion is not
performed; explicitly invoking an interpreter remains the plan author's
responsibility. This is not an untrusted-code sandbox.

The run directory is exclusive/create-only, but another directory is not
globally fenced. Global serialization remains an adapter requirement.
Before granting GO, dispatch intent and child PID/starttime/boot are durable.
Poll intervals only record STILL_RUNNING after rechecking the same child identity.
There is no command deadline by default: a slow child can finish normally and
proceed to its verifier. An optional command_deadline_seconds is measured using
monotonic time from GO separately for each command/verifier, never reset by polls.
The old observation_seconds field is rejected to avoid silently changing intent.
On explicit deadline, identity change or interruption, the result is UNKNOWN and no further operation is
dispatched. The runner closes the grant pipe and reaps the exact child even if
journaling failed. It does not kill, retry or perform cleanup. A hung child can
therefore keep the runner supervising indefinitely. SIGKILL/host loss cannot be
handled in-process: retained intent/process evidence requires manual diagnosis.
Persistent journal failure may prevent a final summary; absence is not PASS.

`--resume` only refuses execution and directs inspection of retained evidence.
PASS means command exit zero plus verifier exit zero and exact marker; live
coverage claims require independently reviewed real adapters. FAIL, UNKNOWN and
NOT_RUN remain distinct. Unit fixtures use only isolated Python subprocesses.

The durable `*-summary.json` and final stdout contain the exact plan digest,
package/platform identity, every step result and mandatory coverage results.
Coverage is PASS only when every contributing step passes its verifier.
Skipped later operations remain NOT_RUN after an earlier failure; they are not
silently counted as successful. Keep the whole run directory for handover.

## Guest observation component

`release-functional-console.py` performs exactly one challenged exchange with
`release-functional-guest.sh` over an already identity-verified Unix console
socket. It does not admit a VM, dispatch lifecycle operations or qualify firmware
by itself. Callers must prove the VM process/socket binding before connecting.
Before a fresh guest is known to be running this protocol, receive-only
`wait_ready` / `--wait-ready` is mandatory. Even a nominally read-only SYSINFO
request can interrupt an interactive firmware/GRUB menu. No probe, challenge or
newline may be sent before the exact agent greeting. Missing greeting remains
UNKNOWN with zero request dispatch. Bind the session to the exact current QEMU
PID/starttime; a greeting alone is not a VM/platform identity proof.
GET_RAM/SET_RAM allow a real RAM-only A/snapshot/B/rollback/A oracle, while VERIFY
reads only guest-approved whole-disk ranges. Never write from the host to a disk
that remains active in the guest.

Observation expiry, disconnect, malformed matching frames and guest refusal are
UNKNOWN with no retransmission. An observed response is not a lifecycle PASS.
BIOS/OVMF coverage still requires actual firmware boot, not a direct-kernel
override; the old NIC matrix tests do not prove RAM rollback or guest boot.
