#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: root privileges are required" >&2
    exit 1
fi
if [[ $# -lt 2 || $# -gt 4 || ! $1 =~ ^[A-Za-z0-9_.+-]+$ ||
      ! $2 =~ ^[0-9]+$ || ! ${3:-10} =~ ^[0-9]+$ ||
      ! ${4:-0} =~ ^[0-9]+$ ]]; then
    echo "Usage: $0 <mapper-name> <worker-pid> [interval-seconds] [max-samples]" >&2
    echo "       max-samples=0 observes until pivot or an unsafe/unknown terminal state" >&2
    exit 2
fi

mapper=$1
worker_pid=$2
interval=${3:-10}
max_samples=${4:-0}
if (( interval < 1 || interval > 300 || max_samples < 0 )); then
    echo "ERROR: interval must be 1..300 and max-samples must be non-negative" >&2
    exit 2
fi

run_dir=$(mktemp -d /var/tmp/sltg-hydration-observer.XXXXXXXX)
chmod 0700 "$run_dir"
samples_file="$run_dir/samples.tsv"
summary_file="$run_dir/summary"
printf 'monotonic_ns\tepoch\ttimestamp\ttarget\thydrated\ttotal\thydrating\tworker_state\tworker_rss_kib\tmem_available_kib\tseconds_since_progress\n' >"$samples_file"

monotonic_ns() {
    perl -MTime::HiRes=clock_gettime,CLOCK_MONOTONIC -e \
        'printf "%.0f\n", clock_gettime(CLOCK_MONOTONIC) * 1000000000'
}

started_mono_ns=$(monotonic_ns)
last_progress_mono_ns=$started_mono_ns
initial_hydrated=-1
last_hydrated=-1
observed_total=-1
observed_region=-1
max_gap=0
samples=0

finish() {
    local result=$1 rc=$2 now_mono_ns elapsed_ns elapsed progress_regions
    local region_bytes observed_bytes average_bytes_per_second
    now_mono_ns=$(monotonic_ns)
    elapsed_ns=$((now_mono_ns - started_mono_ns))
    (( elapsed_ns >= 0 )) || elapsed_ns=0
    elapsed=$((elapsed_ns / 1000000000))
    progress_regions=0
    region_bytes=0
    observed_bytes=0
    average_bytes_per_second=0
    if (( initial_hydrated >= 0 && last_hydrated >= initial_hydrated && observed_region > 0 )); then
        progress_regions=$((last_hydrated - initial_hydrated))
        region_bytes=$((observed_region * 512))
        observed_bytes=$(awk -v regions="$progress_regions" -v bytes="$region_bytes" \
            'BEGIN { printf "%.0f", regions * bytes }')
        if (( elapsed_ns > 0 )); then
            average_bytes_per_second=$(awk -v bytes="$observed_bytes" -v nanoseconds="$elapsed_ns" \
                'BEGIN { printf "%.0f", bytes * 1000000000 / nanoseconds }')
        fi
    fi
    {
        echo "RESULT=$result"
        echo "MAPPER=$mapper"
        echo "WORKER_PID=$worker_pid"
        echo "SAMPLES=$samples"
        echo "ELAPSED_SECONDS=$elapsed"
        echo "ELAPSED_NANOSECONDS=$elapsed_ns"
        echo "MAX_PROVEN_PROGRESS_GAP_SECONDS=$max_gap"
        echo "REGION_SECTORS=$observed_region"
        echo "REGION_BYTES=$region_bytes"
        echo "INITIAL_HYDRATED_REGIONS=$initial_hydrated"
        echo "LAST_HYDRATED_REGIONS=$last_hydrated"
        echo "TOTAL_REGIONS=$observed_total"
        echo "OBSERVED_PROGRESS_REGIONS=$progress_regions"
        echo "OBSERVED_PROGRESS_BYTES=$observed_bytes"
        echo "AVERAGE_OBSERVED_BYTES_PER_SECOND=$average_bytes_per_second"
        echo "RUN_DIR=$run_dir"
    } | tee "$summary_file"
    exit "$rc"
}

echo "RUN_DIR=$run_dir"
echo "MODE=READ_ONLY"
while :; do
    now_mono_ns=$(monotonic_ns)
    now=$(date +%s)
    timestamp=$(date -Is)
    set +e
    status=$(timeout --foreground --kill-after=5s 30s dmsetup status --noflush "$mapper" 2>"$run_dir/status.err")
    status_rc=$?
    set -e
    if (( status_rc != 0 )) || [[ $(printf '%s\n' "$status" | sed '/^$/d' | wc -l) -ne 1 ]]; then
        finish STATUS_UNKNOWN 4
    fi

    read -r start length target rest <<<"$status"
    worker_state=ABSENT
    worker_rss=0
    if [[ -r /proc/$worker_pid/stat ]]; then
        stat=$(<"/proc/$worker_pid/stat")
        tail=${stat##*) }
        worker_state=${tail%% *}
        worker_rss=$(awk '/^VmRSS:/ { print $2; found=1 } END { if (!found) print 0 }' "/proc/$worker_pid/status" 2>/dev/null || echo 0)
    fi
    mem_available=$(awk '/^MemAvailable:/ { print $2; found=1 } END { if (!found) print 0 }' /proc/meminfo)

    if [[ $start != 0 || ! $length =~ ^[0-9]+$ ]]; then
        finish STATUS_MALFORMED 4
    fi
    if [[ $target == linear ]]; then
        gap=$(((now_mono_ns - last_progress_mono_ns) / 1000000000))
        (( gap > max_gap )) && max_gap=$gap
        printf '%s\t%s\t%s\tlinear\t%s\t%s\t0\t%s\t%s\t%s\t%s\n' \
            "$now_mono_ns" "$now" "$timestamp" "$last_hydrated" "$last_hydrated" \
            "$worker_state" "$worker_rss" "$mem_available" \
            "$gap" >>"$samples_file"
        samples=$((samples + 1))
        finish PIVOT_OBSERVED 0
    fi
    if [[ $target != clone ]]; then
        finish TARGET_UNKNOWN 4
    fi

    read -r _metadata_block _metadata_usage region hydration_usage hydrating_regions _ <<<"$rest"
    hydrated=${hydration_usage%/*}
    total=${hydration_usage#*/}
    if [[ ! $region =~ ^[0-9]+$ || ! $hydrated =~ ^[0-9]+$ ||
          ! $total =~ ^[0-9]+$ || ! $hydrating_regions =~ ^[0-9]+$ ||
          $hydration_usage != */* || $hydrated -gt $total ]]; then
        finish STATUS_MALFORMED 4
    fi

    if (( initial_hydrated < 0 )); then
        initial_hydrated=$hydrated
        last_hydrated=$hydrated
        observed_total=$total
        observed_region=$region
        last_progress_mono_ns=$now_mono_ns
    elif (( region != observed_region || total != observed_total )); then
        finish GEOMETRY_CHANGED 4
    elif (( hydrated < last_hydrated )); then
        finish PROGRESS_REGRESSION 4
    elif (( hydrated > last_hydrated )); then
        last_hydrated=$hydrated
        last_progress_mono_ns=$now_mono_ns
    fi
    gap=$(((now_mono_ns - last_progress_mono_ns) / 1000000000))
    (( gap > max_gap )) && max_gap=$gap
    printf '%s\t%s\t%s\tclone\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$now_mono_ns" "$now" "$timestamp" "$hydrated" "$total" "$hydrating_regions" \
        "$worker_state" "$worker_rss" "$mem_available" "$gap" >>"$samples_file"
    samples=$((samples + 1))

    if (( hydrated == total && hydrating_regions == 0 )); then
        finish HYDRATION_COMPLETE 0
    fi
    if [[ $worker_state == ABSENT ]]; then
        finish WORKER_ABSENT_CLONE_INCOMPLETE 3
    fi
    if (( max_samples > 0 && samples >= max_samples )); then
        finish SAMPLE_LIMIT_REACHED 0
    fi
    sleep "$interval"
done
