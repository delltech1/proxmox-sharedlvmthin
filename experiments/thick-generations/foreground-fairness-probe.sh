#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: $0 <mapper-name> <expected-dm-uuid> <source-device> <region-sectors> <worker-pid> <probe-count>" >&2
    exit 2
}

[[ $EUID -eq 0 ]] || { echo "ERROR: root privileges are required" >&2; exit 1; }
[[ $# -eq 6 ]] || usage
mapper=$1
expected_uuid=$2
source_device=$3
region_sectors=$4
worker_pid=$5
probe_count=$6
start_wait_seconds=${SLTG_FAIRNESS_START_WAIT_SEC:-30}

[[ $mapper =~ ^sltg-[0-9a-f]{24}$ ]] || { echo "ERROR: unsafe mapper name" >&2; exit 1; }
[[ $expected_uuid =~ ^SLT-TG2-[0-9a-f]{24}$ ]] || { echo "ERROR: unsafe expected UUID" >&2; exit 1; }
[[ $source_device == /dev/mapper/* ]] || { echo "ERROR: source must be an exact mapper path" >&2; exit 1; }
[[ $region_sectors =~ ^[0-9]+$ && $region_sectors -ge 128 &&
   $((region_sectors & (region_sectors - 1))) -eq 0 ]] || {
    echo "ERROR: region must be a power of two of at least 128 sectors" >&2
    exit 1
}
[[ $worker_pid =~ ^[0-9]+$ && -r /proc/$worker_pid/stat ]] || {
    echo "ERROR: exact materialization worker is absent" >&2
    exit 1
}
[[ $probe_count =~ ^[0-9]+$ && $probe_count -ge 5 && $probe_count -le 10000 ]] || {
    echo "ERROR: probe count must be from 5 through 10000" >&2
    exit 1
}
[[ $start_wait_seconds =~ ^[0-9]+$ && $start_wait_seconds -ge 1 &&
   $start_wait_seconds -le 300 ]] || {
    echo "ERROR: SLTG_FAIRNESS_START_WAIT_SEC must be from 1 through 300" >&2
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
[[ $(blockdev --getsz "/dev/mapper/$mapper") -eq $table_sectors &&
   $(blockdev --getsz "$source_device") -eq $table_sectors ]] || {
    echo "ERROR: mapper/source length mismatch" >&2
    exit 1
}

read_clone_status() {
    local status
    status=$(dmsetup status --noflush "$mapper")
    read -r status_start status_sectors status_target _ _ status_region \
        status_usage status_in_flight status_flags <<<"$status"
    [[ $status_start == 0 && $status_sectors == "$table_sectors" &&
       $status_target == clone && $status_region == "$region_sectors" &&
       $status_usage == */* ]] || return 1
    status_hydrated=${status_usage%/*}
    status_total=${status_usage#*/}
    [[ $status_hydrated =~ ^[0-9]+$ && $status_total =~ ^[0-9]+$ &&
       $status_in_flight =~ ^[0-9]+$ && $status_hydrated -le $status_total ]] || return 1
}

monotonic_ns() {
    perl -MTime::HiRes=clock_gettime,CLOCK_MONOTONIC -e \
        'printf "%.0f\n", clock_gettime(CLOCK_MONOTONIC) * 1000000000'
}

read_clone_status || { echo "ERROR: malformed initial clone status" >&2; exit 1; }
[[ $status_hydrated -ge 1 ]] || {
    echo "ERROR: caller must prove hot-region hydration before enabling background hydration" >&2
    exit 1
}
[[ $status_hydrated -lt $status_total ]] || {
    echo "ERROR: clone is already complete; no concurrent fairness benchmark is possible" >&2
    exit 1
}
pre_enable_hydrated=$status_hydrated
expected_total=$status_total
max_in_flight=$status_in_flight

# Benchmark admission, not a safety timeout: wait boundedly for positive proof
# that the same worker enabled background hydration and that work is actually
# concurrent. Failure yields no benchmark and never authorizes a retry or
# lifecycle mutation.
admission_started_ns=$(monotonic_ns)
admission_deadline_ns=$((admission_started_ns + start_wait_seconds * 1000000000))
while [[ " $status_flags " == *" no_hydration "* ]]; do
    sleep 0.1
    read_clone_status || {
        echo "ERROR: clone status became unknown before background enable" >&2
        exit 3
    }
    [[ $status_total -eq $expected_total &&
       $status_hydrated -ge $pre_enable_hydrated &&
       $status_hydrated -lt $status_total ]] || {
        echo "ERROR: hydration state regressed or completed before background enable" >&2
        exit 4
    }
    [[ -r /proc/$worker_pid/stat ]] || {
        echo "ERROR: materialization worker disappeared before background enable" >&2
        exit 3
    }
    admission_now_ns=$(monotonic_ns)
    (( admission_now_ns < admission_deadline_ns )) || {
        echo "ERROR: background hydration was not enabled before admission deadline" >&2
        exit 3
    }
done
initial_hydrated=$status_hydrated
while (( status_in_flight == 0 && status_hydrated == initial_hydrated )); do
    sleep 0.1
    read_clone_status || {
        echo "ERROR: clone completed or status became unknown before background admission" >&2
        exit 3
    }
    [[ " $status_flags " != *" no_hydration "* &&
       $status_total -eq $expected_total && $status_hydrated -ge $initial_hydrated ]] || {
        echo "ERROR: hydration state regressed before background admission" >&2
        exit 4
    }
    [[ -r /proc/$worker_pid/stat ]] || {
        echo "ERROR: materialization worker disappeared before background admission" >&2
        exit 3
    }
    (( status_in_flight > max_in_flight )) && max_in_flight=$status_in_flight
    admission_now_ns=$(monotonic_ns)
    (( admission_now_ns < admission_deadline_ns )) || {
        echo "ERROR: no concurrent background hydration activity was proven before admission deadline" >&2
        exit 3
    }
done
admission_finished_ns=$(monotonic_ns)
admission_elapsed_ns=$((admission_finished_ns - admission_started_ns))

workdir=$(mktemp -d /var/tmp/sltg-foreground-fairness.XXXXXXXX)
chmod 0700 "$workdir"
cleanup() {
    case $workdir in
        /var/tmp/sltg-foreground-fairness.*) rm -rf -- "$workdir" ;;
        *) echo "ERROR: refusing unexpected cleanup path: $workdir" >&2 ;;
    esac
}
trap cleanup EXIT INT TERM
payload=$workdir/payload.bin
observed=$workdir/observed.bin
samples=$workdir/samples.tsv
read_latencies=$workdir/read-latency.tsv
write_latencies=$workdir/write-latency.tsv

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

# Region zero is the deterministic foreground hot set. The caller must have
# written and proven this region while background hydration was disabled, then
# enabled background hydration without recreating the mapper. This untimed
# identical write only confirms content; it is not accepted as preparation.
dd if="$source_device" of="$payload" bs=4096 count=1 iflag=direct,fullblock status=none
dd if="$payload" of="/dev/mapper/$mapper" bs=4096 count=1 \
    oflag=direct,dsync conv=notrunc status=none
dd if="/dev/mapper/$mapper" of="$observed" bs=4096 count=1 \
    iflag=direct,fullblock status=none
cmp "$payload" "$observed"

previous_hydrated=$initial_hydrated
for ((probe = 1; probe <= probe_count; probe++)); do
    read_ns=$(timed_command_ns dd if="/dev/mapper/$mapper" of="$observed" \
        bs=4096 count=1 iflag=direct,fullblock status=none)
    cmp "$payload" "$observed"
    write_ns=$(timed_command_ns dd if="$payload" of="/dev/mapper/$mapper" \
        bs=4096 count=1 oflag=direct,dsync conv=notrunc status=none)

    read_clone_status || {
        echo "ERROR: clone completed or status became unknown during fairness probe" >&2
        exit 3
    }
    [[ " $status_flags " != *" no_hydration "* &&
       $status_total -eq $expected_total && $status_hydrated -ge $previous_hydrated ]] || {
        echo "ERROR: hydration state regressed or changed during fairness probe" >&2
        exit 4
    }
    [[ -r /proc/$worker_pid/stat ]] || {
        echo "ERROR: materialization worker disappeared during fairness probe" >&2
        exit 3
    }
    previous_hydrated=$status_hydrated
    (( status_in_flight > max_in_flight )) && max_in_flight=$status_in_flight
    printf '%s\t%s\t%s\t%s\t%s\n' "$probe" "$read_ns" "$write_ns" \
        "$status_hydrated" "$status_in_flight" >>"$samples"
    printf '%s\t%s\n' "$probe" "$read_ns" >>"$read_latencies"
    printf '%s\t%s\n' "$probe" "$write_ns" >>"$write_latencies"
done

background_progress=$((previous_hydrated - initial_hydrated))
(( background_progress > 0 || max_in_flight > 0 )) || {
    echo "ERROR: no concurrent background hydration activity was proven" >&2
    exit 3
}

percentile() {
    local file numerator sorted index
    file=$1
    numerator=$2
    sorted=$workdir/sorted-$numerator-$(basename "$file")
    index=$(((probe_count * numerator + 99) / 100))
    sort -t $'\t' -k2,2n "$file" >"$sorted"
    awk -F '\t' -v wanted="$index" 'NR == wanted { print $2 }' "$sorted"
}

echo "FOREGROUND_FAIRNESS=PASS"
echo "MODE=IDENTICAL_4K_IO_ON_HYDRATED_HOT_REGION_DURING_BACKGROUND_HYDRATION"
echo "HOT_REGION_INDEX=0"
echo "HOT_REGION_PREPARATION=CALLER_PROVEN_BEFORE_BACKGROUND_ENABLE"
echo "MAPPER_UUID=$actual_uuid"
echo "REGION_SECTORS=$region_sectors"
echo "REGION_BYTES=$((region_sectors * 512))"
echo "FOREGROUND_IO_BYTES=4096"
echo "PROBES=$probe_count"
echo "BACKGROUND_ADMISSION_WAIT_NANOSECONDS=$admission_elapsed_ns"
echo "INITIAL_HYDRATED_REGIONS=$initial_hydrated"
echo "FINAL_HYDRATED_REGIONS=$previous_hydrated"
echo "BACKGROUND_PROGRESS_REGIONS=$background_progress"
echo "MAX_BACKGROUND_INFLIGHT_REGIONS=$max_in_flight"
echo "READ_P50_NANOSECONDS=$(percentile "$read_latencies" 50)"
echo "READ_P95_NANOSECONDS=$(percentile "$read_latencies" 95)"
echo "READ_P99_NANOSECONDS=$(percentile "$read_latencies" 99)"
echo "WRITE_P50_NANOSECONDS=$(percentile "$write_latencies" 50)"
echo "WRITE_P95_NANOSECONDS=$(percentile "$write_latencies" 95)"
echo "WRITE_P99_NANOSECONDS=$(percentile "$write_latencies" 99)"
awk -F '\t' '{
    printf "SAMPLE_%04d_READ_NS=%s SAMPLE_%04d_WRITE_NS=%s SAMPLE_%04d_HYDRATED=%s SAMPLE_%04d_INFLIGHT=%s\n",
        $1, $2, $1, $3, $1, $4, $1, $5
}' "$samples"
echo "DATA_COMPARE=PASS"
echo "BACKGROUND_HYDRATION=ENABLED"
echo "MAPPER_CLEANUP=CALLER_OWNED"
