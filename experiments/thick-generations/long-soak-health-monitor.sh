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
mkdir -p -m 0700 "$run_dir"
log="$run_dir/health.log"
result="$run_dir/result"
exec >>"$log" 2>&1
rm -f "$result"

echo "START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "NODE=$(hostname)"
echo "ITERATIONS=18"
echo "INTERVAL_SECONDS=300"

for iteration in $(seq 1 18); do
    echo "ITERATION=$iteration UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    for storage_id in "$@"; do
        echo "STORAGE=$storage_id"
        set +e
        timeout --kill-after=5s 30s sharedlvmthin recovery-check "$storage_id"
        rc=$?
        set -e
        echo "RECOVERY_CHECK_RC=$rc"
        if pgrep -af '[s]haredlvmthin recovery-check' >/dev/null; then
            echo "RESULT=BLOCKED_PROBE_SURVIVED"
            printf 'BLOCKED_PROBE_SURVIVED\n' >"$result"
            exit 3
        fi
        if [[ $rc -ne 0 ]]; then
            echo "RESULT=RECOVERY_CHECK_FAILED"
            printf 'RECOVERY_CHECK_FAILED\n' >"$result"
            exit 4
        fi
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
