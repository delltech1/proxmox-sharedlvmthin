# Fix2 timing hardening qualification — 2026-09-19

## Scope

This candidate is a local continuation of the already qualified Fix2 tree.
It is not a GitHub release and has not been installed on cluster nodes.

Changed behavior:

- configurable fail-closed Thin SSH-connect and whole-probe bounds;
- the same peer policy in the plugin and runtime-guard inventory;
- identical timing policy required across same-VG Thin/Thick aliases;
- configurable monotonic Thick frontend close observation window;
- no arbitrary `qm start`/`qm stop` kill in the rolling-cycle helper;
- explicit timing, large-disk and idempotency documentation.
- exact post-error Thin rollback classification without mutation retry.
- exact post-error Thin resize and snapshot create/delete classification;
  same-name snapshot absence is a required create precondition.
- migration-bridge resume refuses multiple candidate state files instead of
  guessing transaction authority from modification time.

Unchanged safety behavior:

- two Thin peer audits remain required around owner claim/commit;
- a timeout or malformed peer response remains `UNKNOWN` and refuses activation;
- PVE HA fencing is not inferred from SSH, a timer or an offline flag;
- Thick long-copy and hydration work has no total wall-clock deadline;
- ambiguous Thick mutations preserve persistent intent and exact objects;
- cleanup remains gated by exact identity, topology and postconditions.

## Source checks completed

- PASS: no hard-coded `ConnectTimeout=5` or `timeout => 10` remains in the
  runtime-guard peer audit.
- PASS: rolling helper contains no `timeout --foreground` and no per-VM task
  killer.
- PASS: new plugin properties are present in both schema and options.
- PASS: same-VG alias checks include all three new timing properties.
- PASS: static regression assertions cover plugin, runtime guard, helper and
  timing documentation.
- PASS: review found no fixed total deadline around Thick allocation zeroing,
  dm-clone hydration or migration-bridge data copy.

## Required executable gates before installation

The current Windows workspace has no Perl, Python or Bash runtime. The supplied
Ubuntu host was unreachable on TCP/22 during this audit. Consequently, no
claim is made that the edited candidate has passed executable tests yet.

Before any node installation, require all of the following on a disposable
Linux/PVE test environment:

1. `perl -c` for every changed Perl source.
2. Complete Perl unit suite, including `plugin_lifecycle.t` and
   `thin_guard_client.t`.
3. Complete Python unit suite.
4. `bash -n lab/fix2-rolling-node-cycle.sh`.
5. Reproducible package build and compatibility check.
6. Small Thin start/stop, Thick start/stop and Thin↔Thick bridge smoke tests.
7. A loaded timing test that delays peer evidence below and above the chosen
   bound and proves respectively success and zero-mutation refusal.
8. A Thick close-race test that reaches zero within the configured window and
   a second test that expires while proving the mapper is preserved.
9. Rolling node reboot with post-boot quorum, service, mapper uniqueness,
   intent, failed-unit and guest-I/O verification.
10. Thin rollback fault injection at replacement creation, origin removal and
    final rename, including the case where each command commits but reports an
    error; require zero duplicate mutation attempts.
11. Thin resize and snapshot fault injection where each LVM command is tested
    both as not committed and committed-but-error; require exact classification
    and one mutation attempt only.

No Git commit, push, release, package installation or node configuration
change is authorized by this report.
