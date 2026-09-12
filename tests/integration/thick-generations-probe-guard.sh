#!/bin/sh
# Non-storage integration check for the one-live-probe invariant.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
guard=${GUARDED_PROBE:-"$script_dir/../../experiments/thick-generations/guarded-probe.sh"}
state_dir="/var/tmp/slt-tg-probe-guard-$$"

cleanup() {
    [ -z "${first_pid:-}" ] || kill "$first_pid" 2>/dev/null || true
    rm -f "$state_dir"/*.stdout "$state_dir"/*.stderr \
        "$state_dir"/*.lock/pid "$state_dir"/*.lock/starttime 2>/dev/null || true
    rmdir "$state_dir"/*.lock 2>/dev/null || true
    rmdir "$state_dir" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

mkdir -m 0700 "$state_dir"
chmod 700 "$guard"

"$guard" "$state_dir" quick 5 -- true | grep -q '^PROBE_STATE=COMPLETED$'

set +e
timeout_output=$("$guard" "$state_dir" timeout 1 -- sleep 5)
timeout_result=$?
set -e
[ "$timeout_result" -eq 124 ]
printf '%s\n' "$timeout_output" | grep -q '^PROBE_STATE=TIMED_OUT_TERMINATED$'
"$guard" "$state_dir" timeout 5 -- true | grep -q '^PROBE_STATE=COMPLETED$'

"$guard" "$state_dir" duplicate 10 -- sleep 4 >"$state_dir/first.result" &
first_pid=$!
sleep 1
set +e
duplicate_output=$("$guard" "$state_dir" duplicate 2 -- true)
duplicate_result=$?
set -e
[ "$duplicate_result" -eq 125 ]
printf '%s\n' "$duplicate_output" | grep -q '^PROBE_STATE=ALREADY_RUNNING$'
wait "$first_pid"
first_pid=""

printf 'QUICK_PROBE=PASS\n'
printf 'TIMEOUT_CLEANUP=PASS\n'
printf 'PROBE_REUSE_AFTER_TERMINATION=PASS\n'
printf 'SECOND_LIVE_PROBE_REFUSED=PASS\n'
printf 'RESULT=PASS\n'
