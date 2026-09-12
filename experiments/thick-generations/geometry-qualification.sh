#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: root privileges are required" >&2
    exit 1
fi
if [[ $# -ne 3 || ! $1 =~ ^[0-9]+$ || ! $2 =~ ^[0-9]+$ || ! $3 =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 <virtual-bytes> <region-sectors> <metadata-bytes>" >&2
    exit 2
fi

virtual_bytes=$1
region_sectors=$2
metadata_bytes=$3
transaction="sltg-geometry-$$-$(date +%s)"
workdir="/var/tmp/$transaction"
mapper="$transaction"
source_loop=
destination_loop=
metadata_loop=

cleanup() {
    if [[ -n $mapper ]]; then
        dmsetup remove --retry "$mapper" >/dev/null 2>&1 || true
    fi
    for device in "$metadata_loop" "$destination_loop" "$source_loop"; do
        if [[ -n $device ]]; then
            losetup -d "$device" >/dev/null 2>&1 || true
        fi
    done
    rm -rf -- "$workdir"
}
trap cleanup EXIT INT TERM

mkdir -m 0700 -- "$workdir"
truncate -s "$virtual_bytes" "$workdir/source.img" "$workdir/destination.img"
truncate -s "$metadata_bytes" "$workdir/metadata.img"
source_loop=$(losetup --find --show "$workdir/source.img")
destination_loop=$(losetup --find --show "$workdir/destination.img")
metadata_loop=$(losetup --find --show "$workdir/metadata.img")
dd if=/dev/zero of="$metadata_loop" bs=4096 count=1 conv=fsync status=none

sectors=$((virtual_bytes / 512))
dmsetup create "$mapper" --readonly --table \
    "0 $sectors clone $metadata_loop $destination_loop $source_loop $region_sectors 1 no_hydration"
status=$(dmsetup status "$mapper")
table=$(dmsetup table "$mapper")
read -r _ _ target metadata_block_sectors metadata_usage \
    reported_region hydration_usage hydrating_regions _rest <<<"$status"
metadata_used=${metadata_usage%/*}
metadata_total=${metadata_usage#*/}
hydrated_regions=${hydration_usage%/*}
total_regions=${hydration_usage#*/}

[[ $status != *" Fail"* && $status != *" ro"* ]]
[[ $status == *" $region_sectors "* ]]
[[ $table == "0 $sectors clone "* ]]
[[ $target == clone ]]
[[ $reported_region == "$region_sectors" ]]
[[ $((metadata_total * metadata_block_sectors * 512)) -eq $metadata_bytes ]]
[[ $metadata_used -gt 0 && $metadata_used -lt $metadata_total ]]
[[ $hydrated_regions -eq 0 && $total_regions -gt 0 && $hydrating_regions -eq 0 ]]

echo "GEOMETRY_CREATE=PASS"
echo "VIRTUAL_BYTES=$virtual_bytes"
echo "REGION_SECTORS=$region_sectors"
echo "METADATA_BYTES=$metadata_bytes"
echo "METADATA_BLOCK_SECTORS=$metadata_block_sectors"
echo "METADATA_BLOCKS_USED=$metadata_used"
echo "METADATA_BLOCKS_TOTAL=$metadata_total"
echo "TOTAL_REGIONS=$total_regions"
echo "STATUS=$status"

dmsetup remove --retry "$mapper"
mapper=
for device in "$metadata_loop" "$destination_loop" "$source_loop"; do
    losetup -d "$device"
done
metadata_loop=
destination_loop=
source_loop=
rm -rf -- "$workdir"
echo "CLEANUP=PASS"
