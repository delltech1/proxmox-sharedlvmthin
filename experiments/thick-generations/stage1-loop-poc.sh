#!/bin/sh
# Disposable dm-clone semantic PoC. Never points at an existing block device.
set -eu

run_id="$$"
work_dir="/var/tmp/slt-tg-poc-${run_id}"
map_name="slt-tg-poc-${run_id}"
map_uuid="SLT-TG-POC-${run_id}"
source_img="$work_dir/source.img"
destination_img="$work_dir/destination.img"
metadata_img="$work_dir/metadata.img"
source_loop=""
destination_loop=""
metadata_loop=""

cleanup() {
    dmsetup remove "$map_name" >/dev/null 2>&1 || true
    [ -z "$metadata_loop" ] || losetup -d "$metadata_loop" >/dev/null 2>&1 || true
    [ -z "$destination_loop" ] || losetup -d "$destination_loop" >/dev/null 2>&1 || true
    [ -z "$source_loop" ] || losetup -d "$source_loop" >/dev/null 2>&1 || true
    rm -f "$source_img" "$destination_img" "$metadata_img"
    rmdir "$work_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

fail() {
    printf 'RESULT=FAIL\nREASON=%s\n' "$1" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || fail "root required"
[ ! -e "$work_dir" ] || fail "work directory collision"
[ ! -e "/dev/mapper/$map_name" ] || fail "mapper collision"
mkdir -m 0700 "$work_dir"

modprobe dm_clone
truncate -s 128M "$source_img"
truncate -s 128M "$destination_img"
truncate -s 32M "$metadata_img"
dd if=/dev/urandom of="$source_img" bs=1M count=128 conv=fsync status=none

source_before=$(sha256sum "$source_img" | awk '{print $1}')
source_loop=$(losetup --find --show --read-only "$source_img")
destination_loop=$(losetup --find --show "$destination_img")
metadata_loop=$(losetup --find --show "$metadata_img")
sectors=$(blockdev --getsz "$source_loop")

dmsetup --verifyudev create "$map_name" --uuid "$map_uuid" --table \
    "0 $sectors clone $metadata_loop $destination_loop $source_loop 8 2 no_hydration no_discard_passdown"

target_version=$(dmsetup targets | awk '$1 == "clone" { print $2 }')
[ "$target_version" = "v1.0.0" ] || fail "unqualified dm-clone target version '$target_version'"
status_initial=$(dmsetup status --noflush "$map_name")
actual_uuid=$(dmsetup info -c --noheadings -o uuid "$map_name" | tr -d '[:space:]')
[ "$actual_uuid" = "$map_uuid" ] || fail "mapper UUID mismatch after create"
[ -b "/dev/mapper/$map_name" ] || fail "udev did not publish mapper block node"
printf 'STATUS_INITIAL=%s\n' "$status_initial"
printf '%s\n' "$status_initial" | grep -q 'no_hydration' \
    || fail "no_hydration not reported"
printf '%s\n' "$status_initial" | grep -q 'no_discard_passdown' \
    || fail "no_discard_passdown not reported"

visible_before=$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')
[ "$visible_before" = "$source_before" ] || fail "initial visible data differs from source"

printf 'SharedLvmThin thick generation foreground write %s\n' "$run_id" \
    | dd of="/dev/mapper/$map_name" bs=4096 seek=2048 conv=notrunc,fsync status=none
expected_after_write=$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')
source_after_write=$(sha256sum "$source_img" | awk '{print $1}')
[ "$source_after_write" = "$source_before" ] || fail "read-only source changed"
[ "$expected_after_write" != "$source_before" ] || fail "foreground write was not visible"

status_after_write=$(dmsetup status --noflush "$map_name")
dmsetup remove "$map_name"
dmsetup --verifyudev create "$map_name" --uuid "$map_uuid" --table \
    "0 $sectors clone $metadata_loop $destination_loop $source_loop 8 2 no_hydration no_discard_passdown"
reopened_hash=$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')
[ "$(dmsetup info -c --noheadings -o uuid "$map_name" | tr -d '[:space:]')" = "$map_uuid" ] \
    || fail "mapper UUID mismatch after persistent reopen"
[ "$reopened_hash" = "$expected_after_write" ] \
    || fail "persistent metadata did not reconstruct foreground write"

event_before=$(dmsetup info -c --noheadings -o events "$map_name" | tr -d '[:space:]')
dmsetup message "$map_name" 0 enable_hydration
# Completion/mode-change events are the primary wakeup. The bounded watchdog
# below still proves the exact counters and metadata mode.
timeout 120 dmsetup wait "$map_name" "$event_before" || fail "no DM event within 120 seconds"
complete=0
last_status=""
i=0
while [ "$i" -lt 120 ]; do
    last_status=$(dmsetup status --noflush "$map_name")
    region_progress=$(printf '%s\n' "$last_status" | awk '{print $7}')
    hydrating=$(printf '%s\n' "$last_status" | awk '{print $8}')
    hydrated=${region_progress%/*}
    total=${region_progress#*/}
    if [ "$total" -gt 0 ] && [ "$hydrated" -eq "$total" ] && [ "$hydrating" -eq 0 ]; then
        complete=1
        break
    fi
    sleep 1
    i=$((i + 1))
done
[ "$complete" -eq 1 ] || fail "hydration did not complete within 120 seconds"

dmsetup suspend "$map_name"
dmsetup load "$map_name" --table "0 $sectors linear $destination_loop 0"
inactive_table=$(dmsetup table --inactive "$map_name")
destination_devno=$(lsblk -ndo MAJ:MIN "$destination_loop" | tr -d '[:space:]')
printf '%s\n' "$inactive_table" | grep -q " linear $destination_devno 0$" \
    || fail "inactive linear table read-back mismatch"
dmsetup --verifyudev resume "$map_name"

linear_table=$(dmsetup table "$map_name")
printf 'LINEAR_TABLE_ACTUAL=%s\n' "$linear_table"
printf 'DESTINATION_DEVNO_EXPECTED=%s\n' "$destination_devno"
printf '%s\n' "$linear_table" | grep -q " linear $destination_devno 0$" \
    || fail "linear table read-back mismatch"
linear_hash=$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')
destination_hash=$(sha256sum "$destination_img" | awk '{print $1}')
[ "$linear_hash" = "$expected_after_write" ] || fail "linear frontend data mismatch"
[ "$destination_hash" = "$expected_after_write" ] || fail "destination data mismatch"
deps_after_pivot=$(dmsetup deps "$map_name")
destination_major=${destination_devno%:*}
destination_minor=${destination_devno#*:}
expected_deps="1 dependencies : ($destination_major, $destination_minor)"
normalized_deps=$(printf '%s\n' "$deps_after_pivot" | tr -s '[:space:]' ' ' | sed 's/^ //; s/ $//')
[ "$normalized_deps" = "$expected_deps" ] \
    || fail "linear mapping dependencies are not destination-only: $deps_after_pivot"

losetup -d "$source_loop"
source_loop=""
independent_hash=$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')
[ "$independent_hash" = "$expected_after_write" ] \
    || fail "destination still depended on source after linear pivot"

printf 'SOURCE_SHA256=%s\n' "$source_before"
printf 'DM_CLONE_TARGET_VERSION=%s\n' "$target_version"
printf 'EXPECTED_DESTINATION_SHA256=%s\n' "$expected_after_write"
printf 'STATUS_AFTER_FOREGROUND_WRITE=%s\n' "$status_after_write"
printf 'STATUS_FINAL_CLONE=%s\n' "$last_status"
printf 'LINEAR_TABLE=%s\n' "$linear_table"
printf 'INACTIVE_TABLE_VERIFIED=%s\n' "$inactive_table"
printf 'DEPS_AFTER_PIVOT=%s\n' "$deps_after_pivot"
printf 'NO_HYDRATION=PASS\n'
printf 'SOURCE_IMMUTABLE=PASS\n'
printf 'PERSISTENT_REOPEN=PASS\n'
printf 'HYDRATION_COMPLETE=PASS\n'
printf 'LINEAR_PIVOT=PASS\n'
printf 'UDEV_SYNCHRONIZATION=PASS\n'
printf 'DESTINATION_INDEPENDENT=PASS\n'
printf 'DATA_INTEGRITY=PASS\n'
printf 'RESULT=PASS\n'

