#!/usr/bin/env bash
set -Eeuo pipefail

# Disposable live qualification only.  This script deliberately performs one
# native PVE stopped backup of a mixed Thin/Eager/Lazy source and restores the
# archive independently to each provisioning policy.  It never retries an
# ambiguous task and it leaves evidence in place on failure.
# SOURCE_VMID must contain only blank disposable data disks in scsi0..scsi2;
# the test writes canaries directly at 8 MiB and must never target boot or
# valuable guest disks.

[[ "${CONFIRM_DISPOSABLE_DATA_DISKS:-}" == YES ]] || {
    echo "REFUSE=CONFIRM_DISPOSABLE_DATA_DISKS_REQUIRED" >&2
    exit 64
}

SOURCE_VMID="${1:?usage: $0 SOURCE_VMID THIN_STORAGE EAGER_STORAGE LAZY_STORAGE DEST_BASE EVIDENCE}"
THIN_STORAGE="${2:?usage: $0 SOURCE_VMID THIN_STORAGE EAGER_STORAGE LAZY_STORAGE DEST_BASE EVIDENCE}"
EAGER_STORAGE="${3:?usage: $0 SOURCE_VMID THIN_STORAGE EAGER_STORAGE LAZY_STORAGE DEST_BASE EVIDENCE}"
LAZY_STORAGE="${4:?usage: $0 SOURCE_VMID THIN_STORAGE EAGER_STORAGE LAZY_STORAGE DEST_BASE EVIDENCE}"
DEST_BASE="${5:?usage: $0 SOURCE_VMID THIN_STORAGE EAGER_STORAGE LAZY_STORAGE DEST_BASE EVIDENCE}"
EVIDENCE="${6:?usage: $0 SOURCE_VMID THIN_STORAGE EAGER_STORAGE LAZY_STORAGE DEST_BASE EVIDENCE}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/vz/dump}"
SCRIPT_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
WRITE_CANARY="$SCRIPT_DIR/tg53-write-canary-vm.sh"

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-native-backup-restore-three-mode"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"

[[ "$SOURCE_VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DEST_BASE" =~ ^[1-9][0-9]{2,8}$ ]]
for storage in "$THIN_STORAGE" "$EAGER_STORAGE" "$LAZY_STORAGE"; do
    [[ "$storage" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
    pvesm status --storage "$storage" --enabled 1 >/dev/null
done
[[ -d "$BACKUP_DIR" && -w "$BACKUP_DIR" ]]
[[ -x "$WRITE_CANARY" ]]
[[ "$(qm status "$SOURCE_VMID")" == "status: stopped" ]]
source_config="$(qm config "$SOURCE_VMID")"
if grep -Eq '^(lock|snapstate):' <<<"$source_config"; then
    echo "REFUSE=SOURCE_LOCK_OR_SNAPSTATE"
    exit 2
fi
for disk in scsi0 scsi1 scsi2; do
    grep -Eq "^${disk}: [^,]+" <<<"$source_config"
done

dest_ids=("$DEST_BASE" "$((DEST_BASE + 1))" "$((DEST_BASE + 2))")
dest_storages=("$THIN_STORAGE" "$EAGER_STORAGE" "$LAZY_STORAGE")
for vmid in "${dest_ids[@]}"; do
    if qm config "$vmid" >/dev/null 2>&1; then
        echo "REFUSE=DESTINATION_VMID_EXISTS:$vmid"
        exit 2
    fi
done

activate_volume() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::activate_volumes($cfg,[$volid]);
    ' "$1"
}

deactivate_volume() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::deactivate_volumes($cfg,[$volid]);
    ' "$1"
}

disk_digest() {
    local vmid="$1" disk="$2" volume path digest
    volume="$(qm config "$vmid" | sed -n "s/^${disk}: \([^,]*\).*/\1/p")"
    [[ -n "$volume" ]]
    # LVM/PVE activation diagnostics may be emitted on stdout.  Keep the
    # function's stdout a strict one-line digest interface.
    activate_volume "$volume" >&2
    path="$(pvesm path "$volume")"
    digest="$(dd if="$path" bs=1M skip=8 count=1 iflag=fullblock status=none | sha256sum | awk '{print $1}')"
    deactivate_volume "$volume" >&2
    printf '%s\n' "$digest"
}

zero_region_digest() {
    local vmid="$1" disk="$2" volume path digest
    volume="$(qm config "$vmid" | sed -n "s/^${disk}: \([^,]*\).*/\1/p")"
    [[ -n "$volume" ]]
    activate_volume "$volume" >&2
    path="$(pvesm path "$volume")"
    digest="$(dd if="$path" bs=1M skip=16 count=1 iflag=fullblock status=none | sha256sum | awk '{print $1}')"
    deactivate_volume "$volume" >&2
    printf '%s\n' "$digest"
}

wait_status() {
    local vmid="$1" expected="$2" deadline=$((SECONDS + 60))
    while ((SECONDS < deadline)); do
        [[ "$(qm status "$vmid")" == "status: $expected" ]] && return 0
        sleep 1
    done
    echo "REFUSE=VM_STATUS_TIMEOUT:$vmid:$expected"
    return 1
}

zero_sha="$(dd if=/dev/zero bs=1M count=1 status=none | sha256sum | awk '{print $1}')"

declare -A expected
for disk in scsi0 scsi1 scsi2; do
    marker="TG53-BACKUP-${SOURCE_VMID}-${disk}-$(date -u +%s%N)"
    write_output="$(CONFIRM_DISPOSABLE_DATA_DISK=YES "$WRITE_CANARY" "$SOURCE_VMID" "$disk" "$marker")"
    printf '%s\n' "$write_output"
    expected[$disk]="$(sed -n 's/^CANARY_SHA256=//p' <<<"$write_output")"
    [[ "${expected[$disk]}" =~ ^[0-9a-f]{64}$ ]]
done
source_config_after_canary="$(qm config "$SOURCE_VMID")"
[[ "$source_config_after_canary" == "$source_config" ]]

before_list="$(mktemp)"
after_list="$(mktemp)"
find "$BACKUP_DIR" -maxdepth 1 -type f -name "vzdump-qemu-${SOURCE_VMID}-*.vma.zst" -printf '%f\n' | sort >"$before_list"
vzdump "$SOURCE_VMID" --dumpdir "$BACKUP_DIR" --mode stop --compress zstd
find "$BACKUP_DIR" -maxdepth 1 -type f -name "vzdump-qemu-${SOURCE_VMID}-*.vma.zst" -printf '%f\n' | sort >"$after_list"
archive_name="$(comm -13 "$before_list" "$after_list")"
rm -f "$before_list" "$after_list"
[[ -n "$archive_name" && "$archive_name" != *$'\n'* ]]
archive="$BACKUP_DIR/$archive_name"
[[ -s "$archive" ]]
echo "ARCHIVE=$archive"
echo "ARCHIVE_SHA256=$(sha256sum "$archive" | awk '{print $1}')"
zstd -q -d -c "$archive" | vma verify -v -
echo "VMA_VERIFY=PASS"

for index in 0 1 2; do
    vmid="${dest_ids[$index]}"
    storage="${dest_storages[$index]}"
    echo "RESTORE_START=$vmid:$storage:$(date -u +%FT%TZ)"
    qmrestore "$archive" "$vmid" --storage "$storage" --unique 1
    restored_config="$(qm config "$vmid")"
    if grep -Eq '^(lock|snapstate):' <<<"$restored_config"; then
        echo "REFUSE=RESTORE_LOCK_OR_SNAPSTATE:$vmid"
        exit 2
    fi
    for disk in scsi0 scsi1 scsi2; do
        volume="$(sed -n "s/^${disk}: \([^,]*\).*/\1/p" <<<"$restored_config")"
        [[ "$volume" == "$storage:"* ]]
        actual="$(disk_digest "$vmid" "$disk")"
        echo "RESTORE_DIGEST=$vmid:$disk:$actual"
        [[ "$actual" == "${expected[$disk]}" ]]
        zero_actual="$(zero_region_digest "$vmid" "$disk")"
        echo "RESTORE_ZERO_DIGEST=$vmid:$disk:$zero_actual"
        [[ "$zero_actual" == "$zero_sha" ]]
    done
    qm start "$vmid"
    wait_status "$vmid" running
    qm stop "$vmid"
    wait_status "$vmid" stopped
    echo "RESTORE_START_STOP_${vmid}=PASS"
    echo "RESTORE_VM_${vmid}=PASS"
done

# Re-prove that the source did not change while the three independent restores
# were constructed and opened.
[[ "$(qm config "$SOURCE_VMID")" == "$source_config" ]]
for disk in scsi0 scsi1 scsi2; do
    actual="$(disk_digest "$SOURCE_VMID" "$disk")"
    echo "SOURCE_POST_DIGEST=$disk:$actual"
    [[ "$actual" == "${expected[$disk]}" ]]
done
echo "SOURCE_UNCHANGED=PASS"

for vmid in "${dest_ids[@]}"; do
    qm destroy "$vmid" --purge 1 --destroy-unreferenced-disks 1
    if qm config "$vmid" >/dev/null 2>&1; then
        echo "REFUSE=DESTROYED_VMID_STILL_PRESENT:$vmid"
        exit 2
    fi
    echo "DESTROY_VM_${vmid}=PASS"
done
rm -f -- "$archive"
[[ ! -e "$archive" ]]

echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_NATIVE_BACKUP_RESTORE_THREE_MODE=PASS"
