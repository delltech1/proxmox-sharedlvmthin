# Recovery-only LVM synchronous-I/O qualification

Status: **MODEL ONLY — LIVE EXECUTION NOT AUTHORIZED**.

The captured lab refusal contained a read-only `vgs` process sleeping in
`io_destroy`. LVM 2.03.31 source shows that `global/use_aio=0` selects the
synchronous label-I/O engine and therefore avoids that specific AIO teardown
path. It does not make SAN I/O bounded and it does not prove that another
kernel path cannot enter uninterruptible sleep.

The proposed use is deliberately narrow: recovery and package-preflight
inventory probes owned by this project. It is not a global `lvm.conf` change,
not a mutation-path setting, and not evidence about nested LVM probes issued by
`pvesm` or PVE libraries.

## Qualification order

The controlled pair is default-first:

1. `A` runs one exact, scoped `vgs --readonly` query with the installed default.
2. `B` may run only after `A_TERMINAL_OK` has been durably recorded.
3. `B` repeats exactly the same query with only
   `--config 'global { use_aio=0 }'` added.
4. Both must return exactly the configured VG UUID with empty stderr.

Candidate-first is not a fallback. A separately authorized B-only diagnostic
cannot be counted as an A/B result.

`A_TERMINAL_OK` requires the exact PID, start time, boot ID and argv; stable
pidfd/owned-child identity; terminal wait evidence and reap; exit zero; no
known or ambiguous descendants; EOF on complete bounded stdout and stderr; no
truncation; no observed persistent D-state or exceeded deadline; and durable
evidence before B admission. Executable, package, kernel, boot, device and
configuration identity must remain unchanged through B.

Any uncertainty is permanent for the pair. A timeout may leave an unkillable
D-state process; the correct result is `UNKNOWN_SURVIVOR`, B is not run, and
there is no automatic retry. A later exit may close cleanup evidence but
cannot turn the failed pair into PASS.

## Current implementation

`experiments/thick-generations/lvm-sync-io-ab.py` is an inert state and
comparison model. Its wrapper uses a clean environment. It contains no process
backend, and even the exact acknowledgement token refuses execution. The
model enforces default-first ordering, exact argv, one expected UUID, empty
stderr, terminal/reap/descendant/pipe/deadline/journal evidence and an
unchanged identity manifest. A clean modeled pair is classified only as
`PAIR_MATCH_REPETITION_REQUIRED`.

The pure controller is single-use. It persists the pair intent before A, the
complete A receipt before admission, checks the identity manifest again,
persists B intent before B, and persists B receipt and the final comparison.
Journal failure, executor exception, ambiguous A/B or identity drift leaves
the controller `UNKNOWN`; there is no modeled retry path.

All model entry points use one canonical LVM UUID parser. The controller and
standalone comparator build the A/B argv pair internally, validate closed and
typed receipt/manifest schemas, bind both receipts to the manifest boot ID,
and copy callback-owned values at trust boundaries. Invalid A output is
rejected before B intent. Callback reentrancy is blocked before the first
identity lookup.

On 2026-09-23 the model generated a plan for the disposable 1 TiB Thick lab VG
without running LVM or changing storage. Remote evidence is
`/tmp/slt-lvm-sync-io-ab-plan.json`, SHA-256
`3aa2896851b32b947ef988d3d076c3fc363bd8a1d0baf46a8df30b25ea48857b`.

Before a live pair, the executor, append-only fsynced journal, bounded
nonblocking capture, subreaper/descendant accounting, pidfd wait/reap state
machine and identity collector require separate tests and review. A live pair
alone would establish only read-only output equivalence on one observed path;
repeated normal and injected slow/failure cases are still required.
