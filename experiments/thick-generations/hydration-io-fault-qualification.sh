#!/usr/bin/env bash
# Disposable loop/device-mapper qualification for upstream dm-clone I/O-fault
# behaviour. This script never touches an existing block device or LVM VG.
set -euo pipefail

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

if [[ $EUID -ne 0 ]]; then
    fail "root privileges are required"
fi
if [[ $# -lt 1 || $# -gt 4 ]]; then
    echo "Usage: $0 <source|destination> [virtual-bytes] [observe-seconds] [recovery-timeout-seconds]" >&2
    exit 2
fi

fault_side=$1
virtual_bytes=${2:-134217728}
observe_seconds=${3:-5}
recovery_timeout=${4:-180}
region_sectors=8
metadata_bytes=16777216

[[ $fault_side == source || $fault_side == destination ]] \
    || fail "fault side must be source or destination"
for value in "$virtual_bytes" "$observe_seconds" "$recovery_timeout"; do
    [[ $value =~ ^[0-9]+$ ]] || fail "numeric arguments must be positive integers"
done
[[ $virtual_bytes -ge 16777216 && $((virtual_bytes % 1048576)) -eq 0 ]] \
    || fail "virtual size must be at least 16 MiB and MiB aligned"
[[ $observe_seconds -ge 2 && $observe_seconds -le 60 ]] \
    || fail "observation interval must be between 2 and 60 seconds"
[[ $recovery_timeout -ge 10 && $recovery_timeout -le 3600 ]] \
    || fail "recovery timeout must be between 10 and 3600 seconds"

for command in awk blockdev cmp dd dmsetup losetup mktemp rm sha256sum sleep sync timeout truncate; do
    command -v "$command" >/dev/null 2>&1 || fail "required command is missing: $command"
done

dm_bounded() {
    timeout --foreground --kill-after=5 30 dmsetup "$@"
}

workdir=$(mktemp -d /var/tmp/sltg-io-fault.XXXXXXXX)
transaction=${workdir##*.}
source_map="sltg-fault-source-$transaction"
destination_map="sltg-fault-destination-$transaction"
clone_map="sltg-fault-clone-$transaction"
source_loop=
destination_loop=
metadata_loop=
source_map_live=0
destination_map_live=0
clone_map_live=0
fault_active=0
sectors=$((virtual_bytes / 512))

restore_fault_path() {
    [[ $fault_active -eq 1 ]] || return 0
    local mapper loop readonly=()
    if [[ $fault_side == source ]]; then
        mapper=$source_map
        loop=$source_loop
        readonly=(--readonly)
    else
        mapper=$destination_map
        loop=$destination_loop
    fi
    dm_bounded suspend "$mapper"
    if [[ ${#readonly[@]} -eq 1 ]]; then
        dm_bounded load "$mapper" --readonly --table "0 $sectors linear $loop 0"
    else
        dm_bounded load "$mapper" --table "0 $sectors linear $loop 0"
    fi
    dm_bounded resume "$mapper"
    fault_active=0
}

cleanup() {
    set +e
    if [[ $clone_map_live -eq 1 ]]; then
        dm_bounded message "$clone_map" 0 disable_hydration >/dev/null 2>&1
    fi
    restore_fault_path >/dev/null 2>&1
    if [[ $clone_map_live -eq 1 ]]; then
        dm_bounded remove --retry "$clone_map" >/dev/null 2>&1
    fi
    if [[ $destination_map_live -eq 1 ]]; then
        dm_bounded remove --retry "$destination_map" >/dev/null 2>&1
    fi
    if [[ $source_map_live -eq 1 ]]; then
        dm_bounded remove --retry "$source_map" >/dev/null 2>&1
    fi
    for device in "$metadata_loop" "$destination_loop" "$source_loop"; do
        [[ -z $device ]] || losetup -d "$device" >/dev/null 2>&1
    done
    case $workdir in
        /var/tmp/sltg-io-fault.*) rm -rf -- "$workdir" ;;
        *) echo "ERROR: refusing unexpected cleanup path: $workdir" >&2 ;;
    esac
}
trap cleanup EXIT INT TERM

truncate -s "$virtual_bytes" "$workdir/source.img" "$workdir/destination.img"
truncate -s "$metadata_bytes" "$workdir/metadata.img"
# Non-sparse random content makes a false all-zero success impossible; the
# recorded source hash is the authoritative value for this individual run.
dd if=/dev/urandom of="$workdir/source.img" bs=1M count=$((virtual_bytes / 1048576)) \
    conv=fsync status=none
source_hash=$(sha256sum "$workdir/source.img" | awk '{print $1}')

source_loop=$(losetup --read-only --find --show "$workdir/source.img")
destination_loop=$(losetup --find --show "$workdir/destination.img")
metadata_loop=$(losetup --find --show "$workdir/metadata.img")
[[ $(blockdev --getro "$source_loop") -eq 1 ]] || fail "source loop is not read-only"
dd if=/dev/zero of="$metadata_loop" bs=4096 count=1 conv=fsync status=none

dm_bounded create "$source_map" --readonly --table "0 $sectors linear $source_loop 0"
source_map_live=1
dm_bounded create "$destination_map" --table "0 $sectors linear $destination_loop 0"
destination_map_live=1
dm_bounded create "$clone_map" --table \
    "0 $sectors clone $metadata_loop /dev/mapper/$destination_map /dev/mapper/$source_map $region_sectors 2 no_hydration no_discard_passdown 2 hydration_threshold 1"
clone_map_live=1

fault_mapper=$source_map
[[ $fault_side == source ]] || fault_mapper=$destination_map
dm_bounded suspend "$fault_mapper"
if [[ $fault_side == source ]]; then
    dm_bounded load "$fault_mapper" --readonly --table "0 $sectors error"
else
    dm_bounded load "$fault_mapper" --table "0 $sectors error"
fi
dm_bounded resume "$fault_mapper"
fault_active=1

status_before=$(dm_bounded status --noflush "$clone_map")
progress_before=$(awk '{print $7}' <<<"$status_before")
[[ $progress_before =~ ^[0-9]+/[0-9]+$ ]] || fail "initial clone progress is malformed"
dm_bounded message "$clone_map" 0 enable_hydration
sleep "$observe_seconds"
status_stalled=$(dm_bounded status --noflush "$clone_map") \
    || fail "clone status blocked during injected $fault_side fault"
progress_stalled=$(awk '{print $7}' <<<"$status_stalled")
[[ $progress_stalled == "$progress_before" ]] \
    || fail "hydration unexpectedly advanced through the injected $fault_side fault"

dm_bounded message "$clone_map" 0 disable_hydration
status_disabled=$(dm_bounded status --noflush "$clone_map") \
    || fail "clone status blocked after disable_hydration"
[[ " $status_disabled " == *" no_hydration "* ]] \
    || fail "kernel did not confirm no_hydration after the injected fault"
[[ $(awk '{print $7}' <<<"$status_disabled") == "$progress_stalled" ]] \
    || fail "progress changed across the confirmed background stop"

restore_fault_path
dm_bounded message "$clone_map" 0 enable_hydration
deadline=$((SECONDS + recovery_timeout))
while :; do
    status_recovered=$(dm_bounded status --noflush "$clone_map") \
        || fail "clone status blocked after restoring the $fault_side path"
    progress_recovered=$(awk '{print $7}' <<<"$status_recovered")
    hydrating_recovered=$(awk '{print $8}' <<<"$status_recovered")
    hydrated=${progress_recovered%/*}
    total=${progress_recovered#*/}
    [[ $hydrated =~ ^[0-9]+$ && $total =~ ^[0-9]+$ && $hydrating_recovered =~ ^[0-9]+$ ]] \
        || fail "recovered clone status is malformed"
    if [[ $hydrated -eq $total && $hydrating_recovered -eq 0 ]]; then
        break
    fi
    (( SECONDS < deadline )) || fail "hydration did not recover within the bounded observation window"
    sleep 1
done

sync
destination_hash=$(sha256sum "$workdir/destination.img" | awk '{print $1}')
[[ $destination_hash == "$source_hash" ]] || fail "recovered destination hash differs from source"
cmp "$workdir/source.img" "$workdir/destination.img"

echo "FAULT_SIDE=$fault_side"
echo "STALL_OBSERVED=PASS"
echo "BACKGROUND_SCHEDULING_DISABLED=PASS"
echo "DISABLED_STATUS=$status_disabled"
echo "RECOVERY_HYDRATION=PASS"
echo "DATA_COMPARE=PASS"
echo "SOURCE_SHA256=$source_hash"
echo "DESTINATION_SHA256=$destination_hash"
echo "FINAL_STATUS=$status_recovered"

dm_bounded remove --retry "$clone_map"
clone_map_live=0
dm_bounded remove --retry "$destination_map"
destination_map_live=0
dm_bounded remove --retry "$source_map"
source_map_live=0
for device in "$metadata_loop" "$destination_loop" "$source_loop"; do
    losetup -d "$device"
done
metadata_loop=
destination_loop=
source_loop=
rm -rf -- "$workdir"
trap - EXIT INT TERM
echo "CLEANUP=PASS"
