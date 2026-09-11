#!/bin/sh
# Run at most one instance of a potentially blocking read-only probe.
set -eu

fail() {
    printf 'PROBE_STATE=ERROR\nREASON=%s\n' "$1" >&2
    exit 2
}

[ "$#" -ge 5 ] || fail "usage: guarded-probe.sh STATE_DIR NAME TIMEOUT -- COMMAND [ARG...]"
state_dir=$1
probe_name=$2
timeout_seconds=$3
shift 3
[ "$1" = "--" ] || fail "missing -- separator"
shift
[ "$#" -gt 0 ] || fail "missing probe command"

case "$probe_name" in
    *[!A-Za-z0-9_.-]*|'') fail "unsafe probe name" ;;
esac
case "$timeout_seconds" in
    *[!0-9]*|'') fail "timeout must be a positive integer" ;;
esac
[ "$timeout_seconds" -gt 0 ] || fail "timeout must be a positive integer"

mkdir -p "$state_dir"
lock_dir="$state_dir/$probe_name.lock"
pid_file="$lock_dir/pid"
start_file="$lock_dir/starttime"
stdout_file="$state_dir/$probe_name.stdout"
stderr_file="$state_dir/$probe_name.stderr"

proc_starttime() {
    [ -r "/proc/$1/stat" ] || return 1
    stat_tail=$(sed 's/^.*) //' "/proc/$1/stat") || return 1
    printf '%s\n' "$stat_tail" | awk '{print $20}'
}

if ! mkdir "$lock_dir" 2>/dev/null; then
    if ! { [ -r "$pid_file" ] && [ -r "$start_file" ]; }; then
        fail "probe lock exists without verifiable ownership"
    fi
    existing_pid=$(cat "$pid_file")
    existing_start=$(cat "$start_file")
    current_start=$(proc_starttime "$existing_pid" 2>/dev/null || true)
    if [ -n "$current_start" ] && [ "$current_start" = "$existing_start" ]; then
        printf 'PROBE_NAME=%s\nPROBE_PID=%s\nPROBE_STATE=ALREADY_RUNNING\n' \
            "$probe_name" "$existing_pid"
        exit 125
    fi
    # A stale lock is removed only after positive proof that its recorded
    # process identity no longer exists. Ambiguous ownership fails closed.
    [ -z "$current_start" ] || fail "probe lock process identity is ambiguous"
    rm -f "$pid_file" "$start_file"
    rmdir "$lock_dir" || fail "could not remove stale probe lock"
    mkdir "$lock_dir" || fail "could not acquire probe lock"
fi

"$@" >"$stdout_file" 2>"$stderr_file" &
probe_pid=$!
printf '%s\n' "$probe_pid" >"$pid_file"
probe_start=$(proc_starttime "$probe_pid" 2>/dev/null || true)
if [ -z "$probe_start" ]; then
    set +e
    wait "$probe_pid"
    result=$?
    set -e
    rm -f "$pid_file" "$start_file"
    rmdir "$lock_dir"
    printf 'PROBE_NAME=%s\nPROBE_STATE=COMPLETED\nPROBE_EXIT=%s\n' \
        "$probe_name" "$result"
    exit "$result"
fi
printf '%s\n' "$probe_start" >"$start_file"

deadline=$(( $(date +%s) + timeout_seconds ))
while kill -0 "$probe_pid" 2>/dev/null; do
    [ "$(date +%s)" -lt "$deadline" ] || break
    sleep 1
done

if ! kill -0 "$probe_pid" 2>/dev/null; then
    set +e
    wait "$probe_pid"
    result=$?
    set -e
    rm -f "$pid_file" "$start_file"
    rmdir "$lock_dir"
    printf 'PROBE_NAME=%s\nPROBE_STATE=COMPLETED\nPROBE_EXIT=%s\n' \
        "$probe_name" "$result"
    exit "$result"
fi

kill -TERM "$probe_pid" 2>/dev/null || true
sleep 1
kill -KILL "$probe_pid" 2>/dev/null || true
sleep 1

current_start=$(proc_starttime "$probe_pid" 2>/dev/null || true)
if [ -n "$current_start" ] && [ "$current_start" = "$probe_start" ]; then
    process_state=$(sed 's/^.*) //' "/proc/$probe_pid/stat" | awk '{print $1}')
    printf 'PROBE_NAME=%s\nPROBE_PID=%s\nPROBE_STATE=BLOCKED\nPROCESS_STATE=%s\n' \
        "$probe_name" "$probe_pid" "$process_state"
    # Preserve the lock: another invocation must not create a second probe.
    exit 124
fi

set +e
wait "$probe_pid" 2>/dev/null
set -e
rm -f "$pid_file" "$start_file"
rmdir "$lock_dir"
printf 'PROBE_NAME=%s\nPROBE_STATE=TIMED_OUT_TERMINATED\n' "$probe_name"
exit 124
