# Scale qualification

Scale results are evidence for the tested lab, not a certification for an
arbitrary SAN, CPU, network, guest workload or cluster size.

> **TG26 correction:** migration and HA counts below qualify materialized
> Thick Generations unless a line explicitly says otherwise. Historical Thin
> online-migration completion is not a safety qualification and is outside the
> TG26 support envelope. Thin cross-node movement now requires complete source
> deactivation before target activation.

## 2026-09-13 disposable three-node gate

- mixed PVE 9 Storage API 14 and 15 cluster;
- one pinned two-path iSCSI LUN, expanded online without changing identity;
- WWID, PV UUID and VG UUID unchanged across the expansion;
- 150 simultaneously running thin-mode VMs and 150 independent per-VM pools;
- 150 simultaneously running small thick-mode VMs, distributed across all
  three nodes, after rolling TG21 installation; both populations were running
  concurrently for a 300-VM lab baseline;
- the thick baseline retained identical package/plugin hashes, quorum, 2/2
  paths per node, exact WWID/PV/VG identity and zero D-state processes;
- 72-VM node evacuation driven by 16 migration workers;
- 60/60 thick-mode online node-evacuation migrations at 16-way concurrency,
  followed by zero source-node Thick Generations mappers, 150/150 running VMs,
  zero D-state processes and six full-disk post-migration SHA256 checks;
- 50/50 native PVE HA maintenance relocations completed from one node to
  another eligible node with all resources returning to stable `started`, no
  HA error, quorum retained and no plugin-specific HA mechanism;
- exact post-test placement reconciliation performed 106/106 online migrations
  at a bounded maximum of six workers per source node (18 cluster-wide). The
  saved per-VM baseline then had zero placement differences and mapper counts
  returned to 48/42/60 with 150/150 VMs running;
- exact source/destination configuration, local mapper, D-state, quorum,
  multipath and recovery checks after each fault or implementation change.

The gate found and fixed two real contention defects:

1. PVE calls `cluster_lock_storage()` around allocate/free with an undefined
   timeout.  On the qualified releases that can expire before a legitimate
   high-object-count allocation finishes.  The plugin now applies the
   configured bounded `slt-lock-timeout` to this outer wrapper too.
2. At that historical stage, Thin deactivation was changed to avoid the
   canonical shared-VG mutation lock because mapper teardown itself is
   node-local. TG26 supersedes that conclusion: mapper teardown remains local,
   but final owner release mutates shared VG metadata and therefore executes
   under the canonical lock only after every child and hidden `-tpool` mapper
   is positively absent.

The thick allocation path was also reduced from separate post-create tag and
autoactivation mutations to atomic creation attributes. In an isolated small
allocation this reduced the archive-producing metadata commits from nine to
five and completed in 48.3 seconds. Exact tag and autoactivation read-back,
PREPARED/MATERIALIZED phase verification, VG intent and capacity gates remain
in place.

For TG22, an exact SAN A/B used full-device SHA256 readback before and
after each operation. `BLKZEROOUT` completed in 0.50--0.81 seconds and the
direct synchronous zero fallback in 28.78--30.40 seconds. Both produced the
same all-zero SHA256, removed only their transaction-scoped test LV, and
returned the VG to the exact recorded free-space baseline. The optimization never
uses discard/UNMAP and falls back to a complete direct rewrite after any
zeroout error. Results are specific to this target and are not a throughput
claim for other SAN implementations.

TG23 closed a lifecycle race discovered only under the concurrent 300-VM
baseline. On a running two-disk Thick Generations VM, the qualified sequence
was snapshot publication, immediate stop while both anchors were HYDRATING,
start while hydration was still active, completion to two ordinary linear
frontends, stopped rollback of both disks, and restart. The stop log contained
no deactivation warning, both rollback HEADs were byte-identical to their
exact immutable snapshot generations, no OPEN VG intent or clone mapping
remained after completion, and no persistent D-state task was observed. Exact
deletion of the two test snapshots and temporary second disk restored the
one-disk configuration, 150/150 Thin plus 150/150 Thick running VMs, and the
exact recorded free-space baseline.

The matching Thin gate used a running VM with two existing disks and one
transaction-scoped temporary disk. Allocation completed in 13 seconds, online
growth in 8 seconds, the native three-disk snapshot in 21 seconds, and rollback
in 27 seconds. Stop, restart, exact snapshot deletion and forced unlink of only
the temporary disk passed. The original two-disk configuration remained,
there was no snapshot, replacement or temporary LV residue, and VG free space
again returned to the exact recorded pre-test VG free-space baseline.

A separate VM proved same-VG cross-mode movement under the 300-VM baseline.
Stopped Thin-to-Thick completed in 37 seconds. After booting from the Thick
generation, a running Thick-to-Thin move completed through native PVE/QEMU
block mirroring in 128 seconds, and only then removed the source generation.
Exact VM destruction removed the returned thin LV and per-VM pool; no VMID or
Thick namespace object remained and VG free space returned to the exact
recorded pre-test baseline with quorum, 2/2 paths and zero D-state tasks.

The full Doctor took 138.98 seconds over this deliberately large inventory.
TG24 therefore uses a distinct bounded installation preflight rather than
running the full per-volume audit from `postinst`. On the same node,
`doctor --quick` completed in 37.69 seconds with 88 PASS, eight expected
site-policy warnings and zero failures. It still verifies package/API,
quorum, VG/PV/WWID, reserve, multipath and PVE storage state, while explicitly
deferring per-volume and Thick anchor diagnostics to the full Doctor. This
changes installation latency and wording only; storage mutation callbacks
always perform fresh authoritative checks.

The reproducible TG24 package was then installed one node at a time on API 14
and API 15. Complete `dpkg` runs, including PVE consumer refresh and the quick
preflight, took 75.00--79.15 seconds. Every node reported the exact TG24
version, zero preflight failures, active scale aliases, 2/2 paths, quorum and
zero D-state tasks before the next node was changed.

The direct A/B result for the second fix was:

- before: 50 successful migrations, 5 completed-with-cleanup-timeout and 17
  not attempted after worker failure; five exact stale local mapper chains;
- exact recovery: each affected VM proven running on another node, local open
  count zero, then guest LV followed by its exact pool deactivated; no force;
- after: the remaining 17 VMs completed at 16-way concurrency, 17/17 PASS,
  with an empty source node and no stale mapper.

## Operational scaling observations

- Per-VM pool creation generates several LVM metadata transactions.  Large VG
  inventories and accumulated `/etc/lvm/archive` files materially increase
  latency and can expose bounded lock timeouts.
- `slt-lock-timeout` accepts 10..86400 seconds. It is a bounded admission wait,
  not a watchdog value or universal enterprise default. It must be chosen from
  measured worst-case serialized operations at the site. Raising it increases
  how long a worker may wait; it does not create parallel shared-metadata
  writes, configure fencing or change the PVE HA watchdog.
- `slt-lock-yield-ms` (default 1000, range 0..5000) introduces a cooperative
  delay only after an outer PVE mutation releases the pmxcfs storage lock. It
  gives already-waiting nodes an opportunity to acquire a lock which is
  bounded but not FIFO. It never sleeps inside the critical section, retries a
  callback, or treats a timeout as proof that a mutation did not occur.
- Size the admission timeout from observed P95/P99/max operation latency, the
  largest qualified disk-zeroing time and the maximum qualified number of
  contenders. A higher timeout alone does not establish fairness or scale;
  the measured scale gate remains authoritative.
- LVM archive retention is administrator-owned host policy.  The plugin does
  not prune archives or rewrite `lvm.conf`; Doctor/runbooks may report the
  condition but never clean it automatically.
- VG text-metadata capacity is a separate hard scale limit. The populated gate
  materially consumed the deliberately bounded metadata area. `pv_mda_free`
  must therefore be recorded alongside VG data
  capacity: more timeout, RAM or SAN throughput cannot recover an exhausted
  circular metadata area. Size `pvmetadatasize` before creating a production
  PV and use multiple independently pinned VG/storage domains when one VG
  cannot retain safe metadata and failure-domain headroom at the intended
  object count. The plugin diagnoses this state but never rewrites PV/VG
  metadata layout automatically.
- At 150 pools a full positive recovery check can take roughly one to two
  minutes in this lab.  Safety checks are not silently skipped to improve a
  benchmark.  Further work should cache only immutable/read-only inventory
  within one check while preserving exact postconditions.

No 200–500 LUN or enterprise-array scale claim is made by this gate.
The 150-VM result is a disposable-lab concurrency and lifecycle envelope, not
a promise that arbitrary guest memory, I/O intensity, SAN latency or HA policy
will produce the same timings.

## 2026-09-17 bounded 50-VM Thin evacuation gate

A separate repeatable gate selected exactly 50 disposable Thin VMs from one
node; six had two Thin disks. Backup proxies, Thick Generations VMs and every
VM outside the immutable manifest were excluded. The test performed a complete
offline handoff in both directions at up to 16-way orchestration concurrency:

1. stop and complete source-side deactivation;
2. verify that every manifest mapper is absent on the source;
3. move the stopped PVE configurations without copying shared storage;
4. start all 50 VMs on the target;
5. verify 50/50 running, exact target mapper presence, exact source mapper
   absence and a valid target owner-node plus 32-hex owner epoch for every
   per-VM pool;
6. repeat the sequence in reverse and restore the original placement.

The first qualification-harness run imposed an incorrect 60-second outer
process timeout while the storage itself was configured with a bounded
`slt-lock-timeout` of 600 seconds. It interrupted six legitimate deactivations
waiting behind the canonical VG lock. No VM configuration moved and no data or
metadata ambiguity occurred. Every affected VM was already stopped; exact
node-local deactivation through the PVE storage API removed only its residual
mapper. The source then had the expected non-manifest baseline.

The harness was corrected to read the configured storage lock timeout and use
that value plus a bounded orchestration margin. The identical 50-VM stop then
passed 50/50 at concurrency 16 without reconciliation. Both complete outward
and return handoffs passed, with quorum retained and zero D-state tasks.

This proves bounded high-contention offline Thin handoff for the tested lab.
It does not qualify Thin live migration, an arbitrary HA policy, or large-disk
copy duration. Disk size is largely outside the stopped shared-LUN handoff data
path, but it is material to zeroing, backup/restore, Thin↔Thick conversion and
Thick materialization. Those require separate progress, capacity and recovery
evidence; small-VM orchestration results must not be represented as an 8-TB
data-movement qualification.

## 2026-09-17 externally fenced 10-VM Thin HA gate

Ten disposable HA-managed Thin VMs were running on one PVE node when that
entire virtual PVE host was hard-powered off from its external hypervisor.
The two surviving nodes retained quorum and PVE HA fenced the missing owner.

The first run proved the old behavior safely failed closed: all target starts
were refused while persistent pool ownership still named the fenced node. The
explicit recovery path then restored all ten without duplicate activation.
This exposed an availability gap, not a metadata-safety failure.

The opt-in `slt-thin-ha-takeover pve-ha` path was then qualified with
`slt-thin-leaseguard remote-audit`. A prototype ordering defect was found:
the old owner tag could be cleared before peer audit, making an audit failure
safe but not automatically retryable. The transition was corrected to require
fresh PVE HA fencing/assignment evidence and successful audit of every
non-fenced peer before any owner-tag mutation.

After the correction, all 10/10 VMs automatically reached `started` on the
surviving node. Each pool had the exact new owner and one fresh 32-hex epoch;
the other survivor had zero relevant mappings, both survivors had zero
D-state tasks, and the fenced node rejoined with zero relevant mappings and
two healthy SAN paths. Quorum returned to 3/3.

This qualifies the tested opt-in HA transition and its fail-closed ordering.
It is not a universal certification of every fencing device, HA policy, SAN or
large-disk workload. The data path did not copy disk contents, so this test
must not be represented as 500 GB--8 TB data-movement qualification.

## 2026-09-18 migration-admission contention gate

The VG-wide materialization admission was exercised with 50 simultaneous
contenders. With one exact holder already committed, 0/50 contenders acquired
the admission and the holder identity remained unchanged. After exact release,
a fresh 50-way race produced exactly one durable winner; 49 contenders refused
the foreign transaction. Releasing the recorded winner restored an empty
admission state. No VM or volume lifecycle mutation was part of this gate.

TG31 also replaces a fixed admission wait with a site-configurable bounded
policy, 30-second persistent waiter heartbeats, and transaction-desynchronized
polling which backs off to 15 seconds. This bounds observation load during a
large evacuation without interpreting a timeout as evidence that a holder
stopped or that its mutation failed.

## 2026-09-18 8-TiB Thin control-plane lifecycle gate

A disposable Thin volume was allocated at 8 TiB on `slt-scale-thin`, attached
to an exact stopped PVE VM configuration, and resized through `qm resize` by
1 GiB. The resulting block-device size was exactly 8,797,166,764,032 bytes and
the PVE configuration reported `size=8193G`. The per-VM pool remained a 1-GiB
elastic pool with zero allocated data; this gate therefore exercised large
integer handling, PVE/plugin lifecycle integration and metadata operations,
not an 8-TiB data copy or performance workload.

Before the PVE VM configuration was created, the deliberately unreferenced
volume was classified `THIN_REFERENCES_HEALTHY=FAIL`,
`STATE=RECOVERY_REQUIRED`, and `SAFE_FOR_MUTATION=NO`. This is the expected
fail-closed orphan behavior rather than a successful health result.

Normal `qm destroy --purge 1 --destroy-unreferenced-disks 1` cleanup removed
the exact VM disk, per-VM pool and VM configuration. VG free space returned
exactly to the recorded pre-test value of 154,853,703,680 bytes. No 500-GiB to
8-TiB physical-copy, hydration-duration or failure-recovery claim follows from
this control-plane gate.

## 2026-09-18 hidden-tpool autogrow correction

The large-size gate exposed an independent live defect on VM 990100. Its
1-GiB elastic pool reached Data%=100 while dmeventd was receiving events. The
monitor rejected those events because `lvs` marked the public pool LV inactive,
even though the exact hidden `-tpool` mapper was active and open. This is a
normal LVM runtime representation when an active thin child depends on the
hidden target; public-LV activity alone was therefore not authoritative.

The monitor now reuses the plugin's exact public/hidden mapper inventory after
quorum, identity, alias-topology and ownership validation under the canonical
VG lock. A missing exact mapper still fails closed. With the corrected package
installed, the same live event grew only `sltp-990100` from 1 GiB to 2 GiB and
Data% fell from 100.00 to 50.00. A subsequent full storage recovery check
reported all 150 owned pools healthy, `STATE=HEALTHY`, and
`SAFE_FOR_MUTATION=YES`. All three nodes returned zero D-state tasks and clean
`dpkg -V` results.

## 2026-09-18 50-way dmeventd lock-backlog gate

Fifty exact active pools below the growth threshold were selected on one node.
The test delivered synthetic stale dmeventd notifications at concurrency 16;
the monitor still re-read authoritative live Data% and was not permitted to
mutate a pool from the supplied event percentage alone.

With the original fixed 30-second monitor lock timeout, 42/50 workers completed
and 8/50 failed safely on canonical VG lock timeout. All 50 pool sizes remained
unchanged. This established a real availability/backlog limit without metadata
ambiguity or speculative retry.

The monitor was changed to use the storage's already bounded
`slt-lock-timeout` policy and to reject a policy change observed after lock
acquisition. With the configured 600-second value, the identical workload
completed 50/50 workers in approximately 146 seconds. All 50 decisions were
stale/coalesced, zero `lvextend` operations occurred, and every selected pool
size remained byte-identical. The final full recovery check reported 150 owned
pools, `STATE=HEALTHY`, `SAFE_FOR_MUTATION=YES`, and zero D-state tasks.

## 2026-09-18 rapid out-of-data transition

While preparing a fully allocated 48-GiB data-movement workload, a fresh
1-GiB elastic pool crossed from a 66% dmeventd event to 100% before the monitor
could complete its serialized grow. The kernel exposed the exact hidden target
as `out_of_data_space,queue_if_no_space`; the writer received controlled
`ENOSPC`, metadata remained writable, and no unrelated pool changed. Manual
identity/quorum verification followed by an exact pool extension cleared the
capacity flag without repair and preserved the written prefix.

This identified a policy deadlock: ordinary mutations must fail closed at
Data%=100, but the guarded autogrow path must be able to perform the sole
capacity mutation that resolves the state. The safety classifier now requires
an explicit `allow_out_of_data` context used only by the monitor. Even there,
quorum, identity, same-VG topology, owner tags, exact public/hidden mapper
inventory, current Data% and protected reserve are positively revalidated
under the canonical lock. Every metadata-health failure still blocks, and a
reserve conflict executes zero mutations. A separate fast 1-GiB live write
with the candidate monitor completed successfully while the exact pool grew
from 1 GiB to 2 GiB and settled at Data%=50.

## 2026-09-18 capacity-relative elastic headroom

The physical 48-GiB workload exposed a large-volume boundary in the original
elastic policy. Absolute burst headroom alone could leave 48 GiB of allocated
data in a 49--50 GiB pool, so the otherwise healthy pool immediately met the
95% mutation-safety refusal threshold. This was safe but operationally stuck.

Elastic headroom is now a lower bound. Allocation, dmeventd autogrow and the
Thick-to-Thin import path also compute the minimum exact pool size needed to
keep known used data at or below 94%, using overflow-bounded integer arithmetic.
The final target is the larger of that capacity-relative value and configured
absolute burst headroom. It is then rounded upward to the normal allocation
unit. The 48-GiB/one-GiB case therefore targets 52 GiB rather than 49 GiB.

This does not promise that arbitrary future guest writes fit without growth;
it prevents a completed managed operation from knowingly returning a pool
which its own next mutation gate must refuse. Reserve, identity, ownership,
quorum and metadata-health checks remain unchanged and fail closed.

The same physical round trip exposed a separate teardown boundary. The legacy
95% gate was shared by all owned-volume verification, so a clean guest stop at
Data%=97.96 stopped QEMU but refused the subsequent local mapper deactivation.
That preserved data but unnecessarily retained runtime ownership. Teardown now
relaxes only the capacity admission rule. Exact storage identity, pool/volume
ownership, metadata health and mapper postconditions remain mandatory; create,
start, resize, snapshot, delete and other mutations remain capacity-blocked.

## 2026-09-18 48-GiB physical Thin--Thick--Thin round trip

A disposable 48-GiB Thin disk was fully written with deterministic data. Its
pre-transition whole-device SHA-256 was:

```text
acb838d39b42634ac37feb5aecf2f17aeff18d35892a5d53666645370bbdc9f5
```

The materialized migration bridge copied the running disk from Thin to an
independent Thick generation in 37 minutes 50 seconds. Native PVE live
migration then moved the running Thick VM to the second node in 12 seconds,
reporting 42 ms downtime. The bridge copied the complete disk back to Thin in
28 minutes 43 seconds and ended in `RETURNED_THIN`. The final whole-device
SHA-256 was byte-identical to the value above.

The old Thin source was removed only after the first mirror completed. The
temporary Thick generation and anchor were removed only after return-to-Thin
completed. No transaction-scoped Thick LV remained. The legacy package had
prepared a 49-GiB return pool, which completed at Data%=97.96 and reproduced
the capacity/teardown boundaries described above. With the corrected package,
one guarded monitor decision grew only that exact pool to 52 GiB, Data% became
92.31, the VM started, stopped and released every exact Thin mapper normally.

Normal PVE destruction removed both disposable VM families and their pools.
VG free space differed from the pre-test value by nine 4-MiB extents because
LVM permanently enlarged the shared VG's global `lvol0_pmspare` from the
largest thin-pool metadata requirement. This is neither a transaction leftover
nor VM-owned capacity and was deliberately not shrunk or removed. All three
nodes ended with zero D-state tasks; the final recovery check found 150 owned
Thin pools, `STATE=HEALTHY` and `SAFE_FOR_MUTATION=YES`.

This is physical qualification at 48 GiB, not a claim that 500-GiB--8-TiB
physical copy duration or every SAN failure mode has been qualified.
