# Installation

> [!CAUTION]
> **Disposable lab installation only. BOTH THIN AND THICK GENERATIONS ARE
> EXPERIMENTAL.** This release candidate
> can cause data loss, corruption, or prolonged unavailability. Do not attach
> production workloads or irreplaceable data. Before installation, verify an
> independent backup and recovery path, fencing, quorum, stable device
> identity, and bounded all-path-loss behavior. The operator assumes the risk
> of installation and operation to the maximum extent permitted by applicable
> law; see GPLv3 sections 15–17 and the risk notice in `README.md`.

SharedLvmThin consumes an **existing shared block device and an existing
dedicated LVM VG**. It does not turn local disks into shared storage and it
does not configure a SAN. Complete the storage preparation below before
installing or registering the plugin.

For VG-scoped serialization and the `slt-mutation-admission-timeout` setting,
see [mutation admission](mutation-admission.md). Thin and Thick must use
separate VGs; this does not replace the rolling-update checks below.

## 1. Prepare the SAN and hosts

The storage/SAN administrator must first:

1. Create one LUN on the array for this SharedLvmThin storage.
2. Map that same LUN read/write to every participating PVE host using FC,
   FCoE, iSCSI, or another qualified shared-block transport.
3. Configure zoning, host groups, initiator ACLs and redundant fabrics or
   networks according to the array vendor's documentation.
4. Ensure every host sees the same stable SCSI identity and capacity.
5. Configure device-mapper multipath on every host when more than one path is
   presented. Use the array vendor's supported multipath profile and a
   documented, bounded all-path-loss policy.

The plugin does not create sessions, zoning, ACLs or multipath policy. Do not
use a raw `/dev/sdX` path when the LUN is multipathed.

### Cluster peer SSH prerequisite

Before shared Thick lifecycle and migration operations, verify noninteractive
root SSH between every participating node using the peer identity and trusted
host keys managed by Proxmox VE. Some Thick peer checks use PVE's SSH helper;
the migration bridge uses the target node's PVE `ssh_known_hosts` file. An SSH
failure or untrusted key can block an operation even when quorum and storage
paths are healthy. Test each direction, for example:

```bash
ssh -n -o BatchMode=yes -o StrictHostKeyChecking=yes \
  -o ControlMaster=no -o ControlPath=none -o HostKeyAlias=<PEER_NODE> \
  -o UserKnownHostsFile=/etc/pve/nodes/<PEER_NODE>/ssh_known_hosts \
  -o GlobalKnownHostsFile=none root@<PEER_ADDRESS> hostname
```

Replace the placeholders using the cluster's actual peer name and address.
The two Control options prevent reuse of an existing multiplexed SSH session,
so this check establishes a new connection using the specified peer host-key file.
Independently verify host-key fingerprints before correcting trust and use the
documented PVE cluster repair procedures. Do not disable host-key verification
or interpret an unreachable peer as proof of fencing or safe takeover.

For example, on the peer's trusted local console, display its public key
fingerprint with `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub` and compare
it with the corresponding PVE-managed key using an independent trusted channel.
Use the actual key type presented by the peer if it differs. Do not accept a
new fingerprint solely because `ssh-keyscan` returned it over the same network.
When a node has been removed and rejoined with the same name or address, the
[Proxmox VE Administration Guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf)
documents `pvecm updatecerts` on the rejoined node to update its fingerprint
cluster-wide. This is a PVE administrative change, not a diagnostic command;
follow the guide for the specific situation and recheck both SSH directions.

For worked transport examples, continue with
[iSCSI, FC and FCoE setup examples](transport-setup-examples.md), then return
to this guide for common PV/VG and plugin configuration.

Install the required host packages as appropriate for the selected transport.
For a typical multipathed block device this includes `lvm2` and
`multipath-tools`; FC/iSCSI packages and configuration are vendor- and
transport-specific.

## 2. Prove one shared multipath identity

Run these read-only checks on **every** participating node. Substitute the
actual mapper device and never copy identifiers from this documentation.

```bash
multipath -ll
lsblk -S -o NAME,HCTL,TRAN,VENDOR,MODEL,SERIAL,WWN
readlink -f /dev/mapper/<WWID>
```

Required result:

- exactly one multipath map for the intended LUN on each node;
- the same WWID, vendor/model, serial and size on every node;
- the expected number of paths, with each path `active ready running`;
- no raw path and mapper duplicate presented to LVM;
- unrelated storage and the PVE system disk are clearly excluded.

Stop if any identity differs. Do not initialize a device merely because its
Linux device name looks familiar; `/dev/sdX` names are not stable identities.

## 3. Create the PV and dedicated VG exactly once

This step is destructive to the selected LUN. Perform it only after a second
administrator or equivalent change-control check has positively matched the
array LUN, mapper WWID and intended empty device. If the LUN already contains
data or LVM metadata, skip creation and investigate it read-only.

On **one node only**, and using the multipath mapper rather than a component
path:

```bash
wipefs -n /dev/mapper/<WWID>
pvs --reportformat json
vgs --reportformat json

# DESTRUCTIVE PROVISIONING EXAMPLE -- replace both placeholders and run once:
pvcreate /dev/mapper/<WWID>
vgcreate <DEDICATED_VG_NAME> /dev/mapper/<WWID>
```

Never run `pvcreate`, `vgcreate`, `pvremove`, `vgremove` or `wipefs` as a
recovery action. Never repeat `pvcreate`/`vgcreate` on the other nodes. The
other nodes must discover the metadata created by the first node.

Record the authoritative identities immediately:

```bash
pvs --noheadings -o pv_name,pv_uuid,vg_name /dev/mapper/<WWID>
vgs --noheadings -o vg_name,vg_uuid,pv_count,vg_size,vg_free <DEDICATED_VG_NAME>
```

For the current single-PV safety model, `pv_count` must be exactly `1`. Keep
the WWID, PV UUID and VG UUID in protected operational documentation.

## 4. Verify discovery on every remaining node

Without creating anything, verify on every other node:

```bash
pvs --noheadings -o pv_name,pv_uuid,vg_name
vgs --noheadings -o vg_name,vg_uuid,pv_count,vg_size,vg_free
multipath -ll
```

Every node must report the exact same WWID, PV UUID, VG name and VG UUID. The
VG must be dedicated to SharedLvmThin and must not contain unrelated LVs.
Resolve delayed SAN discovery, LVM device filtering and multipath problems
before registering the plugin.

If the correct mapper and its PV/VG UUIDs are visible with `pvs --nohints`
and `vgs --nohints`, but ordinary LVM commands or storage status cannot find
the new VG, a peer's local LVM hints can be stale. On each affected node,
first recheck that the mapper WWID and the PV and VG UUIDs match the intended
dedicated LUN. Then refresh that specific device's local LVM cache once:

```bash
pvscan --cache /dev/mapper/<VERIFIED_WWID>
pvs -o pv_name,pv_uuid,vg_name
vgs -o vg_name,vg_uuid,pv_count,vg_size,vg_free
pvesm status
```

`pvscan --cache` updates local LVM runtime state; it is not a read-only
observation. Do not use it as a substitute for matching identities or as an
automatic recovery of an unknown device. If the VG remains missing or the
identities differ, stop and investigate rather than repeating initialization.
Depending on LVM state, `vgscan --cache` alone may not refresh stale hints.

## 5. Install the plugin on every node

Choose exactly one package profile and install the same version one node at a
time. The profiles conflict and cannot coexist:

```bash
sha256sum --check SHA256SUMS
# DUAL: Thin and Thick Generations
DEB="$PWD/pve-sharedlvmthin_<dotted-version>_all.deb"

# OR Thick-only: Thick Generations only
DEB="$PWD/pve-sharedlvmthin-thick_<dotted-version>_all.deb"

HASH="$(sha256sum "$DEB" | awk '{print $1}')"
sudo python3 usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-preservation \
  --package "$DEB" --sha256 "$HASH" \
  --expect-host "$(hostname)" --expect-current none \
  --select-update-policy freeze --execute
```

The release workflow converts Debian `~` separators to dots in GitHub asset
filenames; use the exact downloaded name. Run the gate from the matching tagged
source tree. It is intentionally dry-run without `--execute`. For an upgrade,
set `--expect-current` to the installed profile (`dual` or `thick-only`). The
matching candidate source wrapper automatically inventories all locally
applicable Thick definitions, including disabled storage and Eager/Lazy aliases,
and holds both established executor locks for every existing Lazy volume.
It repeats the inventory under those locks before invoking the ordinary gate.
Once this entry point is installed, `sharedlvmthin package-upgrade` exposes
the same arguments; use matching candidate source when upgrading an older
installation that does not yet provide it. Do not replace this workflow with
raw `apt install` while `FREEZE` is active.

This is package-only preservation, not a storage mutation grant. Local active
Lazy disks require a complete stable graph proof. For remote-owned active Lazy
disks the updated node must prove no conflicting local frontend, clone, zero,
data or metadata map, including renamed UUID identities, while holding its local
alias executors. This does not certify the remote owner's runtime, clear its
ownership or allow takeover. Missing descriptors, changing identity, stale
local boot ownership, transitional state or ambiguous probes refuse the update.
If installation has already dispatched, inspect and recover its exact durable
transaction; do not blindly repeat installation or manually remove its receipts.

Existing `/etc/pve-sharedlvmthin/web.conf` and TLS files are preserved during
upgrade. Non-interactive installation never waits for the optional web
wizard.

## 6. Register the existing VG in PVE

For an empty, unregistered dedicated Thick VG, the read-only helper
`sharedlvmthin-register-preview` can discover matching multipath/PV/VG
identities across online cluster nodes and print suggested commands. It does
not execute them, initialize storage, establish fencing, or guarantee that a
later operation will succeed. Run it as root on one participating PVE node,
review the identified VG, nodes and reserve, and independently validate the
storage and cluster state immediately before executing a command.
The helper repeats read-only discovery after VG selection and refuses the
preview if the selected identity, paths, cluster membership or storage
definitions changed. This is a point-in-time check, not a lock or fencing.
Use `--discover-only` for a JSON observation that can be imported into the
dashboard's command builder; imported data can be edited or become stale and
is not independently authenticated by the dashboard. The helper deliberately
refuses automatic registration planning for nonempty or already registered
VGs until their provenance and aliases have been reviewed.

The dashboard's storage guide can request a fresh read-only cluster inventory
and prepare separate Eager and Lazy command previews. Administrator
authorization and verified PVE peer SSH host keys are required for automatic
discovery. The dashboard does not execute these commands. Execute a reviewed
command once from one participating node, then inspect the resulting PVE
storage configuration. Eager and Lazy are separate changes: if a command is
interrupted or its result is unclear, inspect the existing definition before
continuing. Do not blindly repeat either command. A discovered definition is
not by itself proof that its options match or that the storage is healthy.
When an existing association is observed, the guide displays its reported type
and allocation mode and offers a copyable, read-only `pvesh get` command for
the administrator to inspect. Missing fields remain unknown; the guide does
not automatically plan a second alias for an already registered VG.
The wizard compares observed transport hints on all nodes with the chosen
guide. A SCSI `fc` hint can represent native FC or FCoE and is not proof of
either physical transport or an operational fabric; verify those separately.

The cluster inventory uses noninteractive SSH in every peer direction and
checks each peer against the Proxmox-managed node host-key file. Independently
verify host-key fingerprints before repairing trust; do not turn off host-key
checking. As described in the [Proxmox VE administration guide](https://pve.proxmox.com/pve-docs/pve-admin-guide.pdf),
noninteractive SSH can run shell startup commands. Ensure root shell startup
files do not run interactive or state-changing commands for such sessions.
SSH failure can also indicate a network or authentication problem; it does
not by itself identify which condition occurred or prove a peer is fenced.

Use a neutral storage ID and substitute the identities recorded above. A
representative PVE CLI definition is:

```bash
pvesm add sharedlvmthin <STORAGE_ID> \
  --slt-vgname <DEDICATED_VG_NAME> \
  --slt-expected-vg-uuid <VG_UUID> \
  --slt-expected-pv-uuid <PV_UUID> \
  --slt-expected-wwid <WWID> \
  --slt-expected-min-paths <EXPECTED_PATH_COUNT> \
  --shared 1 \
  --content images,rootdir
```

Use `--nodes <node1,node2,...>` when the LUN is intentionally presented only
to a subset of the cluster. Do not mark a locally attached VG as shared. The
identity pins are strongly recommended: they make a wrong LUN, PV or VG fail
closed before mutation.

For disposable-lab evaluation of prospective deployments, configure `elastic`
absolute headroom so that
multi-terabyte virtual disks do not reserve a proportional fraction of their
logical size:

```bash
pvesm set <STORAGE_ID> \
  --slt-initial-pool-mode elastic \
  --slt-burst-headroom-gib 64 \
  --slt-vg-reserve-percent 5
```

The 64 GiB value is a conservative starting point, not a universal throughput
guarantee. Qualify it against the maximum expected write rate and grow
latency. In `elastic` mode it is a minimum: the plugin may allocate more when
required to leave known used data at or below 94% rather than knowingly finish
behind the 95% mutation gate. Existing `fixed`, `proportional`, and `full`
policies remain available
for compatibility and explicit operational choices. See
`clone-restore-burst-capacity.md`.

### Thin and Thick Generations coexistence

Use separate VGs for Thin and Thick. This is the default and recommended
failure-domain layout: Thin pool growth, metadata exhaustion and ownership do
not then compete with fully reserved Thick capacity or its transition objects.
Eager and Lazy Thick aliases may share the dedicated Thick VG.

TG53 does not support same-VG Thin+Thick operation, including as an advanced
opt-in. Every Thin storage definition must resolve to a different physical VG
from every Eager/Lazy Thick definition. `isolated` is the only operational
layout. The schema can parse the retired `mixed` token during a rolling
migration, but preinstall and runtime refuse it. Existing mixed layouts must
move their volumes and settle `storage.cfg` before installing or upgrading to
TG53 on a participating node.

Example separated topology:

```bash
pvesm add sharedlvmthin <THIN_STORAGE_ID> \
  --slt-vgname <DEDICATED_THIN_VG_NAME> \
  --slt-allocation-mode thin \
  --slt-vg-layout isolated \
  --slt-initial-pool-mode elastic \
  --slt-burst-headroom-gib 64 \
  --slt-expected-vg-uuid <VG_UUID> \
  --slt-expected-pv-uuid <PV_UUID> \
  --slt-expected-wwid <WWID> \
  --slt-expected-min-paths <EXPECTED_PATH_COUNT> \
  --slt-vg-reserve-percent 5 \
  --nodes <node1,node2,node3> \
  --shared 1 --content images,rootdir

pvesm add sharedlvmthin <THICK_STORAGE_ID> \
  --slt-vgname <DEDICATED_THICK_VG_NAME> \
  --slt-allocation-mode thick-generations \
  --slt-vg-layout isolated \
  --slt-expected-vg-uuid <VG_UUID> \
  --slt-expected-pv-uuid <PV_UUID> \
  --slt-expected-wwid <WWID> \
  --slt-expected-min-paths <EXPECTED_PATH_COUNT> \
  --slt-vg-reserve-percent 5 \
  --nodes <node1,node2,node3> \
  --shared 1 --content images,rootdir
```

Omit `--nodes` only when the relevant LUN is intentionally available on every
cluster node. Never configure native PVE `lvm`/`lvmthin` storage over either
managed VG. The plugin checks physical inventory and rejects a Thin pool in a
Thick VG, a Thick Generations object in a Thin VG, or aliases that expose both
families through one pinned VG.

The chosen PVE storage ID is the user-visible mode selector when creating,
restoring or moving a disk. Existing volumes retain their own allocation mode;
changing the default does not silently convert them. Use a normal PVE Storage
Move for an explicit Thin-to-Thick or Thick-to-Thin conversion.

Both package profiles operate only with `slt-vg-layout isolated`. Eager and Lazy
Thick aliases may share the dedicated Thick VG because both reserve their full
capacity and use the same Thick lifecycle. Thin must use another VG.

Thick storage requires an explicitly configured VG reserve, expressed as a
percentage, fixed GiB, or both. If both are set, the larger reserve applies.
The command helper defaults to 0%; an administrator may choose a reserve,
for example 5%. A zero effective reserve allows allocations to consume all
free extents. Future snapshots, rollback or disk growth may then be refused
for lack of space. A reserve alone does not guarantee that a particular
generation fits: plan for the full expected generation and its metadata above
the protected reserve. Each separately created LVM volume consumes whole VG
extents, so small data or auxiliary volumes can require more physical space
than their requested byte size. The Thick admission check accounts for these
individual allocations; a preview is not a reservation and concurrent or
external changes may still cause a later allocation to fail. Monitor free VG
extents and PV metadata independently.
Upgrades do not silently change an existing reserve.

## 7. Validate before storing a VM

```bash
pvesm status
sharedlvmthin doctor
sharedlvmthin recovery-check <STORAGE_ID>
```

Run these checks on every participating node. Require matching identity,
healthy multipath, quorum and `SAFE_FOR_MUTATION=YES`. First validate with a
disposable VM and disposable data. `sharedlvmthin doctor --deep` is
destructive, CLI-only, and must be run only against explicitly approved
disposable test space.

## Operational prerequisites

1. Present the same shared block identity to every participating PVE node.
2. Configure multipath and LVM device discovery outside this package.
3. Create the shared VG once and verify identical PV/VG identity on every node.
4. Ensure the PVE cluster and fencing design are appropriate for shared writable storage.

Configure storage through PVE using type `sharedlvmthin`, `slt-vgname`, and an
optional allocation-headroom policy. The compatibility default is `fixed` and
uses `slt-initial-pool-size`; it does not guarantee that asynchronous autogrow
can absorb a fast clone/restore. `proportional` adds
`slt-initial-pool-percent` and an optional `slt-initial-pool-max`. `full`
admits live used bytes plus the complete new disk and fails closed if an
explicit maximum is insufficient. See `clone-restore-burst-capacity.md`.
Mark genuinely shared storage as shared and restrict nodes when appropriate.
For loaded clusters, configure Thin peer connect/probe bounds and the Thick
frontend close observation window only from measured latency. Their exact
semantics, same-VG alias requirements and large-disk behavior are documented
in [TIMING-AND-SCALE-SAFETY.md](TIMING-AND-SCALE-SAFETY.md). A timeout never
constitutes fencing and must not trigger an automatic mutation retry.

The package installs a marked dmeventd autogrow fragment in
`/etc/lvm/lvmlocal.conf` only when no administrator-owned thin command or
autoextend settings already exist. Existing policy is preserved and reported,
never overwritten. Purge removes only the marked package fragment and private
Python cache/state. The RC5.97 lifecycle adapters recognize PVE Storage APIs
14, 15 and 16; runtime authorization remains tied to the exact software tuple,
boot and operation evidence, not registration alone.

## Rolling package upgrade

Select one update policy explicitly on every participating node:

- `freeze` holds the watched storage stack and refuses ordinary changes; use
  the exact package-profile settlement workflow for an intentional plugin
  replacement.
- `qualified-auto` permits only an exact manifest-qualified APT transition.
  It does not start APT, restart services or reboot the node.
- `warn` permits one exact administrator-authorized unqualified package plan,
  but the resulting runtime remains `UNQUALIFIED` and storage mutations stay
  blocked until a later qualification succeeds.

Older candidates named the last two modes `qualified-only` and
`manual-override`. They remain readable during an upgrade but are renamed only
by `sharedlvmthin update-policy migrate-policy-schema`; there is no silent
policy conversion.

Every watched update has two distinct settlements. `PACKAGE_SETTLED` proves
the exact dpkg/artifact result. Runtime permission additionally requires a
root-owned receipt matching the code identity actually loaded by the calling
PVE process, the current boot ID, running kernel and qualified plan. A stale
worker, manual payload replacement, reboot or different kernel therefore
blocks mutation even when all services merely report `active`. After a
qualified unchanged-stack reboot use `package-post-reboot-gate.sh --settle-runtime`;
the default invocation remains read-only.

The CLI commands `sharedlvmthin package-upgrade` and
`sharedlvmthin package-post-reboot` accept `--doctor-timeout-sec` (60–86400
seconds; default 1800) when a system needs a different Doctor deadline. For example,
append `--doctor-timeout-sec 3600` to either command, together with its required
host, package and identity arguments. The package-upgrade command also passes
that value to the package's installation preflight. For direct `dpkg -i`, an
administrator can set `SLT_INSTALL_DOCTOR_TIMEOUT_SEC=3600` in the installation
environment; the direct-install default is also 1800 seconds. A check that
finishes sooner continues immediately without waiting for the deadline.

These deadlines limit waiting. Reaching one leaves the result unknown; it does
not prove storage failure or health and never grants settlement. They do not
change separate deadlines for compatibility or storage checks.

A concrete kernel APT update follows a different sequence: install, complete a
new boot into the intended kernel,
run `sharedlvmthin update-policy postcheck`, then complete the public runtime
qualification workflow. The postcheck deliberately refuses a changed kernel
package before the required new boot; never clear that guard manually.

A package upgrade replaces the plugin and diagnostic files and refreshes only
active PVE management consumers. It does not restart QEMU, deactivate an LVM
volume, reload a device-mapper table, restart multipath or modify the SAN. A
qualified compatible upgrade can therefore preserve running guest I/O, while
the PVE API and web interface can be briefly unavailable during service
refresh.

Upgrade one node at a time:

1. Run `sharedlvmthin upgrade-check`. It inventories every locally applicable
   SharedLvmThin storage, runs its bounded read-only recovery gate and requires
   one exact `STATE=HEALTHY`, `SAFE_FOR_MUTATION=YES` and
   `THICK_ANCHORS_HEALTHY=PASS` result. Explicitly disabled storage is reported
   and checked too; it is not automatically skipped. Definitions outside the
   node scope are skipped. A duplicate storage ID, timeout, unavailable probe or ambiguous
   output fails closed. Continue only when the final line is
   `UPGRADE_SAFE=YES`.
2. Require every Thick Generations anchor to be `MATERIALIZED`. Do not upgrade
   a node that owns a hydration, rollback, snapshot deletion or recovery
   transaction. This condition is enforced by the upgrade check through the
   Thick anchor recovery gate.
3. Verify the package checksum and confirm that the target release explicitly
   supports the installed anchor schema and PVE Storage API.
4. When upgrading from a release without Thin exclusive ownership, stop every
   Thin guest first and positively verify that each affected pool and hidden
   `-tpool` mapper is absent on every node. The package pre-install gate refuses
   a locally active legacy pool. After all nodes have the new package, run the
   one-time `thin-adopt-owner-model` procedure documented in
   [migration.md](migration.md) for every legacy per-VM pool. Never add the
   schema tag manually.
5. Install the package on one node. Do not restart QEMU, LVM, multipath or the
   SAN merely because the plugin package changed.
6. Verify the installed version, PVE services, Doctor, recovery checks, running
   VM state and guest I/O before continuing to the next node.

Never assume that an arbitrary downgrade is safe. A release that removes
support for an existing configuration property or persistent Thick Generations
anchor schema requires an explicit downgrade procedure. If compatibility
cannot be positively proven, stop mutations and keep the current package.

Do not downgrade TG26 to a pre-TG26 package while any managed Thin pool exists.
Older code does not enforce the persistent node/epoch owner protocol. This is
not reliably preventable from the currently installed package: if its `prerm`
refuses, Debian `dpkg` may deliberately retry with the incoming older package's
maintainer script. An explicit administrator-forced downgrade can therefore
bypass a local refusal. Treat cluster-wide TG26-or-newer consistency as an
operational invariant, not as a promise made by `dpkg`.

`upgrade-check` is advisory and read-only: it never installs a package,
deactivates storage, changes an anchor or repairs a failed gate. It is not run
automatically by `dpkg`, because blocking package replacement can also prevent
installation of a recovery or security fix. The administrator remains in
control of the maintenance transaction.

## Legacy RC5 to RC4 rollback (not valid after TG26 adoption)

RC4 does not know the RC5-only identity and reserve properties. Before installing
RC4, record their exact values and remove them through `pvesm set <storage>
--delete ...`. Downgrading while they remain in `/etc/pve/storage.cfg` leaves the
storage available but produces parser errors in RC4 services.

Required order:

1. Record `slt-expected-vg-uuid`, `slt-expected-pv-uuid`,
   `slt-expected-wwid`, `slt-vg-reserve-percent`, and
   `slt-vg-reserve-gib`, plus `slt-initial-pool-mode`,
   `slt-initial-pool-percent`, and `slt-initial-pool-max` when configured, for
   every SharedLvmThin storage.
2. Remove only those RC5 properties using the PVE storage API.
3. Install the known-good RC4 package and reload PVE management services one
   node at a time. Do not restart VMs or storage transports.
4. Verify quorum, both storage backends, running VMs, Doctor, and dashboard.
5. When returning to RC5, restore the recorded properties exactly and rerun
   Doctor on both nodes.

Never edit LVM metadata, multipath policy, or the backing LUN as part of plugin
rollback.
