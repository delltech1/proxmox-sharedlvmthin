#!/usr/bin/env bash
set -Eeuo pipefail

# Disposable RC5.85 qualification: distinct-data four-disk composition,
# two snapshot generations, rollback, one-disk grow and Lazy VMA restore.
# Ambiguous outcomes are retained for explicit recovery; no mutation is retried.

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

SOURCE_VMID="${SOURCE_VMID:-994341}"
RESTORE_VMID="${RESTORE_VMID:-994342}"
THIN_STORAGE="${THIN_STORAGE:-slt-tg-thin}"
EAGER_STORAGE="${EAGER_STORAGE:-slt-lab-thick-a}"
LAZY_STORAGE="${LAZY_STORAGE:-slt-lab-lazy-a}"
SNAP_A="${SNAP_A:-tg53four-a}"
SNAP_B="${SNAP_B:-tg53four-b}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/vz/dump}"
EVIDENCE="${EVIDENCE:-/root/tg53-four-disk-${SOURCE_VMID}.evidence}"
NODE="$(hostname)"
OBSERVE_SEC="${OBSERVE_SEC:-3600}"
readonly SAMPLE_BYTES=1048576
readonly SAMPLE_OFFSETS=(8388608 536870912 1048576000)
readonly ORIGINAL_BYTES=1073741824
readonly GROWN_BYTES=1140850688
readonly GROWN_TAIL_BYTES=67108864
readonly TAIL_MARKER_OFFSET=1107296256

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-four-disk-composition"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$NODE"
echo "SOURCE_VMID=$SOURCE_VMID"
echo "RESTORE_VMID=$RESTORE_VMID"

[[ "$SOURCE_VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$RESTORE_VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$SOURCE_VMID" != "$RESTORE_VMID" ]]
[[ "$OBSERVE_SEC" =~ ^[0-9]+$ && "$OBSERVE_SEC" -ge 900 && "$OBSERVE_SEC" -le 86400 ]]
for storage in "$THIN_STORAGE" "$EAGER_STORAGE" "$LAZY_STORAGE"; do
    [[ "$storage" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
    pvesm status --storage "$storage" --enabled 1 >/dev/null
    /usr/sbin/sharedlvmthin recovery-check "$storage" \
        | grep -q '^SAFE_FOR_MUTATION=YES$'
done
for vmid in "$SOURCE_VMID" "$RESTORE_VMID"; do
    if qm config "$vmid" >/dev/null 2>&1; then
        echo "REFUSE=VMID_EXISTS:$vmid"
        exit 2
    fi
done
[[ -d "$BACKUP_DIR" && -w "$BACKUP_DIR" ]]

activate_volume() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::activate_volumes($cfg,[$volid]);
    ' "$1" >&2
}

deactivate_volume() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::deactivate_volumes($cfg,[$volid]);
    ' "$1" >&2
}

volume_for() {
    qm config "$1" | sed -n "s/^$2: \([^,]*\).*/\1/p"
}

volume_size() {
    local volume=$1 path size rc
    activate_volume "$volume"
    path=$(pvesm path "$volume")
    set +e
    size=$(blockdev --getsize64 "$path")
    rc=$?
    set -e
    deactivate_volume "$volume"
    (( rc == 0 )) || return "$rc"
    printf '%s\n' "$size"
}

volume_digest() {
    local volume=$1 path digest rc
    activate_volume "$volume"
    path=$(pvesm path "$volume")
    set +e
    digest=$(
        for offset in "${SAMPLE_OFFSETS[@]}"; do
            dd if="$path" iflag=skip_bytes,count_bytes skip="$offset" \
                count="$SAMPLE_BYTES" status=none | sha256sum | awk '{print $1}'
        done | sha256sum | awk '{print $1}'
    )
    rc=$?
    set -e
    deactivate_volume "$volume"
    (( rc == 0 )) || return "$rc"
    printf '%s\n' "$digest"
}

region_digest() {
    local volume=$1 offset=$2 bytes=$3 path digest rc
    activate_volume "$volume"
    path=$(pvesm path "$volume")
    set +e
    digest=$(dd if="$path" iflag=skip_bytes,count_bytes skip="$offset" \
        count="$bytes" status=none | sha256sum | awk '{print $1}')
    rc=$?
    set -e
    deactivate_volume "$volume"
    (( rc == 0 )) || return "$rc"
    printf '%s\n' "$digest"
}

write_pattern() {
    local volume=$1 base=$2 path rc index offset pattern
    activate_volume "$volume"
    path=$(pvesm path "$volume")
    set +e
    rc=0
    for index in "${!SAMPLE_OFFSETS[@]}"; do
        offset=${SAMPLE_OFFSETS[$index]}
        printf -v pattern '0x%02x' "$((base + index + 1))"
        qemu-io -f raw -c "write -P $pattern $offset $SAMPLE_BYTES" "$path" || {
            rc=$?
            break
        }
    done
    sync
    set -e
    deactivate_volume "$volume"
    (( rc == 0 )) || return "$rc"
}

materialize_lazy() {
    local volume=$1 volname rc
    volname=${volume#*:}
    activate_volume "$volume"
    set +e
    /usr/sbin/sharedlvmthin thick-lazy-materialize "$LAZY_STORAGE" "$volname"
    rc=$?
    set -e
    deactivate_volume "$volume"
    (( rc == 0 )) || return "$rc"
    /usr/sbin/sharedlvmthin recovery-check "$LAZY_STORAGE" \
        | grep -q '^SAFE_FOR_MUTATION=YES$'
    echo "LAZY_MATERIALIZED=$volume"
}

observe_mutation() {
    local request=$1 kind=$2; shift 2
    local started
    started=$(date +%s)
    tg53_dispatch_detached "$request" "$@"
    tg53_wait_exact_task "$NODE" "$SOURCE_VMID" "$kind" "$started" \
        "$((started + OBSERVE_SEC))"
}

snapshot_create() {
    local snap=$1 index=$2
    /usr/sbin/sharedlvmthin snapshot-preflight "$SOURCE_VMID"
    observe_mutation "four-s${index}-${SOURCE_VMID}-$(date +%s)-$$" qmsnapshot create \
        "/nodes/$NODE/qemu/$SOURCE_VMID/snapshot" --snapname "$snap" \
        --description "TG53 four-disk composition $snap"
    qm config "$SOURCE_VMID" --snapshot "$snap" >/dev/null
    echo "SNAPSHOT_CREATED=$snap"
}

snapshot_rollback() {
    local snap=$1 index=$2
    observe_mutation "four-r${index}-${SOURCE_VMID}-$(date +%s)-$$" qmrollback create \
        "/nodes/$NODE/qemu/$SOURCE_VMID/snapshot/$snap/rollback" --start 0
    echo "SNAPSHOT_ROLLBACK=$snap"
}

snapshot_delete() {
    local snap=$1 index=$2
    observe_mutation "four-d${index}-${SOURCE_VMID}-$(date +%s)-$$" qmdelsnapshot delete \
        "/nodes/$NODE/qemu/$SOURCE_VMID/snapshot/$snap"
    echo "SNAPSHOT_DELETED=$snap"
}

declare -A VOLUME EXPECT_A EXPECT_B SIZE_BEFORE
DISKS=(scsi0 scsi1 scsi2 scsi3)
PATTERN_A=(16 32 48 64)
PATTERN_B=(80 96 112 128)
PATTERN_C=(144 160 176 192)

qm create "$SOURCE_VMID" --name tg53-four-disk-composition \
    --memory 256 --scsihw virtio-scsi-single
qm set "$SOURCE_VMID" --scsi0 "$THIN_STORAGE:1"
qm set "$SOURCE_VMID" --scsi1 "$EAGER_STORAGE:1"
qm set "$SOURCE_VMID" --scsi2 "$LAZY_STORAGE:1"
qm set "$SOURCE_VMID" --scsi3 "$LAZY_STORAGE:1"
[[ "$(qm status "$SOURCE_VMID")" == 'status: stopped' ]]

for index in "${!DISKS[@]}"; do
    disk=${DISKS[$index]}
    VOLUME[$disk]=$(volume_for "$SOURCE_VMID" "$disk")
    [[ -n "${VOLUME[$disk]}" ]]
    SIZE_BEFORE[$disk]=$(volume_size "${VOLUME[$disk]}")
    [[ "${SIZE_BEFORE[$disk]}" == "$ORIGINAL_BYTES" ]]
done
materialize_lazy "${VOLUME[scsi2]}"
materialize_lazy "${VOLUME[scsi3]}"

for index in "${!DISKS[@]}"; do
    disk=${DISKS[$index]}
    write_pattern "${VOLUME[$disk]}" "${PATTERN_A[$index]}"
    EXPECT_A[$disk]=$(volume_digest "${VOLUME[$disk]}")
    echo "STATE_A=$disk:${VOLUME[$disk]}:${EXPECT_A[$disk]}"
done
snapshot_create "$SNAP_A" 1

for index in "${!DISKS[@]}"; do
    disk=${DISKS[$index]}
    VOLUME[$disk]=$(volume_for "$SOURCE_VMID" "$disk")
    write_pattern "${VOLUME[$disk]}" "${PATTERN_B[$index]}"
    EXPECT_B[$disk]=$(volume_digest "${VOLUME[$disk]}")
    [[ "${EXPECT_B[$disk]}" != "${EXPECT_A[$disk]}" ]]
    echo "STATE_B=$disk:${VOLUME[$disk]}:${EXPECT_B[$disk]}"
done
snapshot_create "$SNAP_B" 2

for index in "${!DISKS[@]}"; do
    disk=${DISKS[$index]}
    VOLUME[$disk]=$(volume_for "$SOURCE_VMID" "$disk")
    write_pattern "${VOLUME[$disk]}" "${PATTERN_C[$index]}"
done

snapshot_rollback "$SNAP_B" 1
for disk in "${DISKS[@]}"; do
    VOLUME[$disk]=$(volume_for "$SOURCE_VMID" "$disk")
    actual=$(volume_digest "${VOLUME[$disk]}")
    [[ "$actual" == "${EXPECT_B[$disk]}" ]]
    echo "ROLLBACK_B_DIGEST=$disk:$actual"
done

snapshot_rollback "$SNAP_A" 2
for disk in "${DISKS[@]}"; do
    VOLUME[$disk]=$(volume_for "$SOURCE_VMID" "$disk")
    actual=$(volume_digest "${VOLUME[$disk]}")
    [[ "$actual" == "${EXPECT_A[$disk]}" ]]
    echo "ROLLBACK_A_DIGEST=$disk:$actual"
done

snapshot_delete "$SNAP_B" 1
snapshot_delete "$SNAP_A" 2
snapshot_inventory=$(pvesh get "/nodes/$NODE/qemu/$SOURCE_VMID/snapshot" --output-format json)
python3 - "$SNAP_A" "$SNAP_B" "$snapshot_inventory" <<'PY'
import json, sys
snap_a, snap_b, raw = sys.argv[1:]
rows = json.loads(raw)
if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
    raise SystemExit("invalid snapshot inventory")
names = [row.get("name") for row in rows]
if snap_a in names or snap_b in names or any(name != "current" for name in names):
    raise SystemExit(f"snapshot inventory did not settle: {names!r}")
PY
if qm config "$SOURCE_VMID" | grep -Eq '^(lock|snapstate):'; then
    echo "REFUSE=SOURCE_LOCK_OR_SNAPSTATE"
    exit 2
fi

for disk in "${DISKS[@]}"; do
    VOLUME[$disk]=$(volume_for "$SOURCE_VMID" "$disk")
done
qm resize "$SOURCE_VMID" scsi1 +64M
[[ "$(volume_size "${VOLUME[scsi1]}")" == "$GROWN_BYTES" ]]
zero_tail_sha=$(dd if=/dev/zero bs=1M count=64 status=none | sha256sum | awk '{print $1}')
actual=$(region_digest "${VOLUME[scsi1]}" "$ORIGINAL_BYTES" "$GROWN_TAIL_BYTES")
[[ "$actual" == "$zero_tail_sha" ]]
echo "GROWN_FULL_TAIL_ZERO_SHA256=$actual"
for disk in "${DISKS[@]}"; do
    actual=$(volume_digest "${VOLUME[$disk]}")
    [[ "$actual" == "${EXPECT_A[$disk]}" ]]
    echo "POST_GROW_DIGEST=$disk:$actual"
done

# Prove that backup/restore covers written data in the newly allocated tail,
# not merely the original fixed-offset canaries.
activate_volume "${VOLUME[scsi1]}"
grown_path=$(pvesm path "${VOLUME[scsi1]}")
qemu-io -f raw -c "write -P 0xd1 $TAIL_MARKER_OFFSET $SAMPLE_BYTES" "$grown_path"
sync
deactivate_volume "${VOLUME[scsi1]}"
tail_marker_sha=$(region_digest "${VOLUME[scsi1]}" "$TAIL_MARKER_OFFSET" "$SAMPLE_BYTES")
echo "GROWN_TAIL_MARKER_SHA256=$tail_marker_sha"

before=$(mktemp)
after=$(mktemp)
find "$BACKUP_DIR" -maxdepth 1 -type f -name "vzdump-qemu-${SOURCE_VMID}-*.vma.zst" \
    -printf '%f\n' | sort >"$before"
vzdump "$SOURCE_VMID" --dumpdir "$BACKUP_DIR" --mode stop --compress zstd
find "$BACKUP_DIR" -maxdepth 1 -type f -name "vzdump-qemu-${SOURCE_VMID}-*.vma.zst" \
    -printf '%f\n' | sort >"$after"
archive_name=$(comm -13 "$before" "$after")
rm -f "$before" "$after"
[[ -n "$archive_name" && "$archive_name" != *$'\n'* ]]
archive="$BACKUP_DIR/$archive_name"
[[ -s "$archive" ]]
zstd -q -d -c "$archive" | vma verify -v -
echo "VMA_VERIFY=PASS"

qmrestore "$archive" "$RESTORE_VMID" --storage "$LAZY_STORAGE" --unique 1
restore_cfg=$(qm config "$RESTORE_VMID")
if grep -Eq '^(lock|snapstate):' <<<"$restore_cfg"; then
    echo "REFUSE=RESTORE_LOCK_OR_SNAPSTATE"
    exit 2
fi
for disk in "${DISKS[@]}"; do
    restored=$(volume_for "$RESTORE_VMID" "$disk")
    [[ "$restored" == "$LAZY_STORAGE:"* ]]
    actual=$(volume_digest "$restored")
    [[ "$actual" == "${EXPECT_A[$disk]}" ]]
    if [[ "$disk" == scsi1 ]]; then
        [[ "$(volume_size "$restored")" == "$GROWN_BYTES" ]]
        actual_tail=$(region_digest "$restored" "$TAIL_MARKER_OFFSET" "$SAMPLE_BYTES")
        [[ "$actual_tail" == "$tail_marker_sha" ]]
        echo "RESTORE_TAIL_MARKER_SHA256=$actual_tail"
    else
        [[ "$(volume_size "$restored")" == "$ORIGINAL_BYTES" ]]
    fi
    echo "RESTORE_DIGEST=$disk:$restored:$actual"
done
echo "LAZY_RESTORE_FOUR_DISK=PASS"

# Restoring elsewhere must not mutate the source VM or any of its four heads.
for disk in "${DISKS[@]}"; do
    source_now=$(volume_for "$SOURCE_VMID" "$disk")
    [[ "$source_now" == "${VOLUME[$disk]}" ]]
    actual=$(volume_digest "$source_now")
    [[ "$actual" == "${EXPECT_A[$disk]}" ]]
done
[[ "$(region_digest "${VOLUME[scsi1]}" "$TAIL_MARKER_OFFSET" "$SAMPLE_BYTES")" == "$tail_marker_sha" ]]
echo "SOURCE_AFTER_RESTORE=UNCHANGED"

qm destroy "$RESTORE_VMID" --purge 1 --destroy-unreferenced-disks 1
qm destroy "$SOURCE_VMID" --purge 1 --destroy-unreferenced-disks 1
rm -f -- "$archive"
for vmid in "$SOURCE_VMID" "$RESTORE_VMID"; do
    [[ ! -e "/etc/pve/qemu-server/$vmid.conf" ]]
done
for storage in "$THIN_STORAGE" "$EAGER_STORAGE" "$LAZY_STORAGE"; do
    inventory=$(pvesh get "/nodes/$NODE/storage/$storage/content" \
        --vmid "$SOURCE_VMID" --output-format json)
    [[ "$inventory" == '[]' ]]
    inventory=$(pvesh get "/nodes/$NODE/storage/$storage/content" \
        --vmid "$RESTORE_VMID" --output-format json)
    [[ "$inventory" == '[]' ]]
    /usr/sbin/sharedlvmthin recovery-check "$storage" \
        | grep -q '^SAFE_FOR_MUTATION=YES$'
done

echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_FOUR_DISK_COMPOSITION=PASS"
