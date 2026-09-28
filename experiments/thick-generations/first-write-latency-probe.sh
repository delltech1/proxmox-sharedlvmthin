#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 <mapper-name> <expected-dm-uuid> <source-device> <region-sectors> <probe-count>" >&2
    exit 2
}

[[ $EUID -eq 0 ]] || { echo "ERROR: root privileges are required" >&2; exit 1; }
[[ $# -eq 5 ]] || usage
mapper=$1
expected_uuid=$2
source_device=$3
region_sectors=$4
probe_count=$5

[[ $mapper =~ ^sltg-[0-9a-f]{24}$ ]] || { echo "ERROR: unsafe mapper name" >&2; exit 1; }
[[ $expected_uuid =~ ^SLT-TG2-[0-9a-f]{24}$ ]] || { echo "ERROR: unsafe expected UUID" >&2; exit 1; }
[[ $source_device == /dev/mapper/* ]] || { echo "ERROR: source must be an exact mapper path" >&2; exit 1; }
[[ $region_sectors =~ ^[0-9]+$ && $region_sectors -ge 128 &&
   $((region_sectors & (region_sectors - 1))) -eq 0 ]] || {
    echo "ERROR: region must be a power of two of at least 128 sectors" >&2
    exit 1
}
[[ $probe_count =~ ^[0-9]+$ && $probe_count -ge 1 && $probe_count -le 1000 ]] || {
    echo "ERROR: probe count must be from 1 through 1000" >&2
    exit 1
}
[[ -b /dev/mapper/$mapper && -b $source_device ]] || {
    echo "ERROR: mapper or source block device is absent" >&2
    exit 1
}
[[ $(blockdev --getro "$source_device") -eq 1 ]] || {
    echo "ERROR: source device is not read-only" >&2
    exit 1
}

actual_uuid=$(dmsetup info -c --noheadings -o uuid "$mapper" | xargs)
[[ $actual_uuid == "$expected_uuid" ]] || { echo "ERROR: mapper UUID mismatch" >&2; exit 1; }

table=$(dmsetup table --showkeys "$mapper")
read -r table_start table_sectors table_target metadata_dev destination_dev \
    table_source table_region _table_rest <<<"$table"
[[ $table_start == 0 && $table_target == clone && $table_region == "$region_sectors" ]] || {
    echo "ERROR: mapper table geometry mismatch" >&2
    exit 1
}
source_majmin=$(lsblk -dnro MAJ:MIN "$source_device")
[[ $table_source == "$source_majmin" ]] || { echo "ERROR: source dependency mismatch" >&2; exit 1; }
[[ $metadata_dev != "$destination_dev" && $metadata_dev != "$table_source" &&
   $destination_dev != "$table_source" ]] || {
    echo "ERROR: clone metadata, destination and source must be distinct devices" >&2
    exit 1
}
[[ $(blockdev --getsz "/dev/mapper/$mapper") -eq $table_sectors ]] || {
    echo "ERROR: mapper length mismatch" >&2
    exit 1
}
[[ $(blockdev --getsz "$source_device") -eq $table_sectors ]] || {
    echo "ERROR: source length mismatch" >&2
    exit 1
}

status=$(dmsetup status --noflush "$mapper")
read -r status_start status_sectors status_target _metadata_block _metadata_usage \
    status_region hydration_usage hydrating_regions status_flags <<<"$status"
hydrated_regions=${hydration_usage%/*}
total_regions=${hydration_usage#*/}
[[ $status_start == 0 && $status_sectors == "$table_sectors" &&
   $status_target == clone && $status_region == "$region_sectors" ]] || {
    echo "ERROR: mapper status geometry mismatch" >&2
    exit 1
}
[[ " $status_flags " == *" no_hydration "* && $hydrating_regions -eq 0 ]] || {
    echo "ERROR: background hydration must be positively disabled and idle" >&2
    exit 1
}
[[ $hydrated_regions -eq 0 ]] || {
    echo "ERROR: latency qualification requires a pristine zero-hydrated clone" >&2
    exit 1
}
[[ $total_regions -gt $((probe_count + 1)) ]] || {
    echo "ERROR: insufficient distinct regions for requested probes" >&2
    exit 1
}

workdir=$(mktemp -d /var/tmp/sltg-first-write.XXXXXXXX)
chmod 0700 "$workdir"
cleanup() {
    case $workdir in
        /var/tmp/sltg-first-write.*) rm -rf -- "$workdir" ;;
        *) echo "ERROR: refusing unexpected cleanup path: $workdir" >&2 ;;
    esac
}
trap cleanup EXIT INT TERM
results=$workdir/samples.tsv
touch "$results"

timed_command_ns() {
    perl -MTime::HiRes=clock_gettime,CLOCK_MONOTONIC -e '
        my $started = clock_gettime(CLOCK_MONOTONIC);
        system @ARGV;
        my $rc = $?;
        my $finished = clock_gettime(CLOCK_MONOTONIC);
        die "timed command failed\n" if $rc != 0;
        printf "%.0f\n", ($finished - $started) * 1000000000;
    ' -- "$@"
}

for ((probe = 1; probe <= probe_count; probe++)); do
    region_index=$((probe * total_regions / (probe_count + 1)))
    block_index=$((region_index * region_sectors / 8))
    payload=$workdir/payload-$probe.bin
    observed=$workdir/observed-$probe.bin
    dd if="$source_device" of="$payload" bs=4096 skip="$block_index" count=1 \
        iflag=direct,fullblock status=none
    elapsed_ns=$(timed_command_ns dd if="$payload" of="/dev/mapper/$mapper" \
        bs=4096 seek="$block_index" count=1 oflag=direct,dsync conv=notrunc status=none)
    (( elapsed_ns > 0 ))

    status=$(dmsetup status --noflush "$mapper")
    read -r _ _ target _ _ reported_region usage in_flight flags <<<"$status"
    new_hydrated=${usage%/*}
    [[ $target == clone && $reported_region == "$region_sectors" &&
       " $flags " == *" no_hydration "* && $in_flight -eq 0 &&
       $new_hydrated -eq $probe ]] || {
        echo "ERROR: probe did not prove exactly one newly hydrated region" >&2
        echo "LAST_STATUS=$status" >&2
        exit 1
    }
    dd if="/dev/mapper/$mapper" of="$observed" bs=4096 skip="$block_index" count=1 \
        iflag=direct,fullblock status=none
    cmp "$payload" "$observed"
    printf '%s\t%s\t%s\t%s\n' "$probe" "$region_index" "$block_index" \
        "$elapsed_ns" >>"$results"
done

sorted=$workdir/samples-by-latency.tsv
sort -t $'\t' -k4,4n "$results" >"$sorted"
percentile() {
    local numerator=$1
    local index=$(((probe_count * numerator + 99) / 100))
    awk -F '\t' -v wanted="$index" 'NR == wanted { print $4 }' "$sorted"
}

echo "FIRST_WRITE_LATENCY=PASS"
echo "MODE=IDENTICAL_4K_WRITE_TO_DISTINCT_UNHYDRATED_REGIONS"
echo "MAPPER_UUID=$actual_uuid"
echo "REGION_SECTORS=$region_sectors"
echo "REGION_BYTES=$((region_sectors * 512))"
echo "WRITE_IO_BYTES=4096"
echo "PROBES=$probe_count"
echo "P50_NANOSECONDS=$(percentile 50)"
echo "P95_NANOSECONDS=$(percentile 95)"
echo "P99_NANOSECONDS=$(percentile 99)"
echo "MAX_NANOSECONDS=$(awk -F '\t' 'END { print $4 }' "$sorted")"
echo "FINAL_HYDRATED_REGIONS=$probe_count"
echo "BACKGROUND_HYDRATION=DISABLED"
echo "DATA_COMPARE=PASS"
awk -F '\t' '{
    printf "SAMPLE_%04d_REGION=%s SAMPLE_%04d_BLOCK=%s SAMPLE_%04d_NANOSECONDS=%s\n",
        $1, $2, $1, $3, $1, $4
}' "$results"
echo "MAPPER_CLEANUP=CALLER_OWNED"
