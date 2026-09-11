#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: root privileges are required" >&2
    exit 1
fi
if [[ $# -ne 4 || ! $1 =~ ^[0-9]+$ || ! $2 =~ ^[0-9]+$ ||
      ! $3 =~ ^[0-9]+$ || ! $4 =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 <virtual-bytes> <region-sectors> <metadata-bytes> <timeout-seconds>" >&2
    exit 2
fi

virtual_bytes=$1
region_sectors=$2
metadata_bytes=$3
timeout_seconds=$4
[[ $virtual_bytes -ge 4096 && $((virtual_bytes % 512)) -eq 0 ]]
[[ $region_sectors -ge 8 && $((region_sectors & (region_sectors - 1))) -eq 0 ]]
[[ $metadata_bytes -ge 4096 && $((metadata_bytes % 4096)) -eq 0 ]]
[[ $timeout_seconds -ge 1 ]]

workdir=$(mktemp -d /var/tmp/sltg-full-hydration.XXXXXXXX)
transaction=${workdir##*.}
mapper="sltg-full-hydration-$transaction"
source_loop=
destination_loop=
metadata_loop=
mapper_live=0

cleanup() {
    if [[ $mapper_live -eq 1 ]]; then
        dmsetup message "$mapper" 0 disable_hydration >/dev/null 2>&1 || true
        dmsetup remove --retry "$mapper" >/dev/null 2>&1 || true
    fi
    for device in "$metadata_loop" "$destination_loop" "$source_loop"; do
        if [[ -n $device ]]; then
            losetup -d "$device" >/dev/null 2>&1 || true
        fi
    done
    case $workdir in
        /var/tmp/sltg-full-hydration.*) rm -rf -- "$workdir" ;;
        *) echo "ERROR: refusing unexpected cleanup path: $workdir" >&2 ;;
    esac
}
trap cleanup EXIT INT TERM

truncate -s "$virtual_bytes" "$workdir/source.img" "$workdir/destination.img"
truncate -s "$metadata_bytes" "$workdir/metadata.img"
printf 'SLTG-BEGIN\n' | dd of="$workdir/source.img" bs=1 seek=0 conv=notrunc status=none
printf 'SLTG-MIDDLE\n' | dd of="$workdir/source.img" bs=1 \
    seek=$((virtual_bytes / 2)) conv=notrunc status=none
printf 'SLTG-END\n' | dd of="$workdir/source.img" bs=1 \
    seek=$((virtual_bytes - 16)) conv=notrunc status=none

source_loop=$(losetup --read-only --find --show "$workdir/source.img")
destination_loop=$(losetup --find --show "$workdir/destination.img")
metadata_loop=$(losetup --find --show "$workdir/metadata.img")
[[ $(blockdev --getro "$source_loop") -eq 1 ]]
dd if=/dev/zero of="$metadata_loop" bs=4096 count=1 conv=fsync status=none

sectors=$((virtual_bytes / 512))
dmsetup create "$mapper" --table \
    "0 $sectors clone $metadata_loop $destination_loop $source_loop $region_sectors 1 no_hydration"
mapper_live=1
dmsetup message "$mapper" 0 enable_hydration

started=$SECONDS
metadata_high_water=0
samples=0
while :; do
    status=$(dmsetup status "$mapper")
    [[ $status != *" Fail"* && $status != *" ro"* ]]
    read -r _ _ target metadata_block_sectors metadata_usage \
        reported_region hydration_usage hydrating_regions _rest <<<"$status"
    metadata_used=${metadata_usage%/*}
    metadata_total=${metadata_usage#*/}
    hydrated_regions=${hydration_usage%/*}
    total_regions=${hydration_usage#*/}
    [[ $target == clone ]]
    [[ $reported_region == "$region_sectors" ]]
    [[ $((metadata_total * metadata_block_sectors * 512)) -eq $metadata_bytes ]]
    if (( metadata_used > metadata_high_water )); then
        metadata_high_water=$metadata_used
    fi
    samples=$((samples + 1))
    if [[ $hydrated_regions -eq $total_regions && $hydrating_regions -eq 0 ]]; then
        final_status=$status
        break
    fi
    if (( SECONDS - started >= timeout_seconds )); then
        echo "HYDRATION_TIMEOUT=FAIL" >&2
        echo "LAST_STATUS=$status" >&2
        exit 1
    fi
    sleep 1
done

sync
cmp "$workdir/source.img" "$workdir/destination.img"
elapsed=$((SECONDS - started))

echo "FULL_HYDRATION=PASS"
echo "DATA_COMPARE=PASS"
echo "VIRTUAL_BYTES=$virtual_bytes"
echo "REGION_SECTORS=$region_sectors"
echo "METADATA_BYTES=$metadata_bytes"
echo "METADATA_BLOCKS_HIGH_WATER=$metadata_high_water"
echo "METADATA_BLOCKS_FINAL=$metadata_used"
echo "METADATA_BLOCKS_TOTAL=$metadata_total"
echo "TOTAL_REGIONS=$total_regions"
echo "SAMPLES=$samples"
echo "ELAPSED_SECONDS=$elapsed"
echo "FINAL_STATUS=$final_status"

dmsetup remove --retry "$mapper"
mapper_live=0
for device in "$metadata_loop" "$destination_loop" "$source_loop"; do
    losetup -d "$device"
done
metadata_loop=
destination_loop=
source_loop=
rm -rf -- "$workdir"
trap - EXIT INT TERM
echo "CLEANUP=PASS"
