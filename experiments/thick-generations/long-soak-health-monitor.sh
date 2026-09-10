#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "ERROR: root privileges are required" >&2
    exit 1
fi
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <storage-id> <storage-id> [storage-id ...]" >&2
    exit 2
fi

run_dir=/var/tmp/slt-long-soak-health
mkdir -p "$run_dir"
chmod 0700 "$run_dir"
log="$run_dir/health.log"
result="$run_dir/result"
exec >>"$log" 2>&1
rm -f "$result"

echo "START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "NODE=$(hostname)"
echo "ITERATIONS=18"
echo "INTERVAL_SECONDS=300"
echo "UNSCOPED_DSTATE_RECHECKS=1"

run_check() {
    local storage_id=$1 output_file=$2 rc
    set +e
    timeout --kill-after=5s 30s sharedlvmthin recovery-check "$storage_id" >"$output_file" 2>&1
    rc=$?
    set -e
    cat "$output_file"
    echo "RECOVERY_CHECK_RC=$rc"
    if pgrep -af '[s]haredlvmthin recovery-check' >/dev/null; then
        echo "RESULT=BLOCKED_PROBE_SURVIVED"
        printf 'BLOCKED_PROBE_SURVIVED\n' >"$result"
        exit 3
    fi
    return "$rc"
}

sole_unscoped_dstate_unknown() {
    local output_file=$1 gate
    [[ $(grep -Ec '=UNKNOWN$' "$output_file" || true) -eq 1 ]] || return 1
    [[ $(grep -Ec '=FAIL$' "$output_file" || true) -eq 0 ]] || return 1
    grep -qx 'NO_RELEVANT_DSTATE=UNKNOWN' "$output_file" || return 1
    for gate in PATHS_HEALTHY WWID_MATCH PV_UUID_MATCH VG_UUID_MATCH \
        POOL_FLAGS_HEALTHY BOUNDED_LVM_PROBES PVE_STORAGE_HEALTH QUORUM; do
        grep -qx "$gate=PASS" "$output_file" || return 1
    done
}

for iteration in $(seq 1 18); do
    echo "ITERATION=$iteration UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for storage_id in "$@"; do
        echo "STORAGE=$storage_id"
        first=$(mktemp "$run_dir/check.XXXXXX")
        if ! run_check "$storage_id" "$first"; then
            # The production checker must fail closed for an unscoped D-state
            # task. This read-only qualification harness may recheck exactly
            # once, and only when that attribution is the sole uncertainty.
            if sole_unscoped_dstate_unknown "$first"; then
                echo "TRANSIENT_UNSCOPED_DSTATE=OBSERVED"
                sleep 2
                retry=$(mktemp "$run_dir/recheck.XXXXXX")
                if run_check "$storage_id" "$retry"; then
                    echo "TRANSIENT_UNSCOPED_DSTATE_RECHECK=PASS"
                    rm -f "$first" "$retry"
                    continue
                fi
                rm -f "$retry"
            fi
            rm -f "$first"
            echo "RESULT=RECOVERY_CHECK_FAILED"
            printf 'RECOVERY_CHECK_FAILED\n' >"$result"
            exit 4
        fi
        rm -f "$first"
    done
    pvecm status | sed -n '1,24p'
    pvesm status | grep -E '^(Name|slt-iscsi|tg-disposable)' || true
    ps -eo pid,stat,comm | sed -n '/ [D][^ ]* /p'
    if [[ $iteration -lt 18 ]]; then
        sleep 300
    fi
done

echo "PASS_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'PASS\n' >"$result"
