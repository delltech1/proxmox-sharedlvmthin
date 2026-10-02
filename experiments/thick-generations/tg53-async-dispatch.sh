#!/usr/bin/env bash
# Sourced helper for destructive PVE lab operations.  Dispatch is detached from
# the observer so an SSH/terminal disconnect cannot SIGPIPE the PVE worker.

tg53_dispatch_detached() {
    local request_id="$1"; shift
    local unit="tg53-${request_id}"
    [[ "$request_id" =~ ^[a-z0-9][a-z0-9-]{0,47}$ ]] || return 64

    printf 'REQUEST_ID=%s\nDISPATCH_EPOCH=%s\nUNIT=%s\n' \
        "$request_id" "$(date +%s)" "$unit"
    # No RuntimeMaxSec/timeout is intentional.  A deadline belongs to the
    # observer, never to the process that owns a storage mutation.
    systemd-run --quiet --collect --unit "$unit" \
        --property=Type=exec --property=KillMode=control-group \
        --property=TimeoutStartSec=infinity -- \
        /usr/bin/pvesh "$@"
    printf 'DISPATCH_STATE=DETACHED\n'
}

tg53_observe_exact_upid() {
    local node="$1" upid="$2" deadline="$3"
    local terminal_policy="${4:-require-ok}"
    local probe_sec="${TG53_OBSERVER_PROBE_SEC:-30}"
    local exact observation rc now remaining end_seconds
    [[ "$node" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || return 64
    [[ "$upid" == UPID:* && "$upid" == *: \
        && "$upid" != *$'\n'* && "$upid" != *$'\r'* ]] || return 64
    [[ "$deadline" =~ ^[0-9]+$ ]] || return 64
    [[ "$terminal_policy" =~ ^(require-ok|any-terminal)$ ]] || return 64
    [[ "$probe_sec" =~ ^[0-9]+$ && "$probe_sec" -ge 5 && "$probe_sec" -le 300 ]] || return 64

    now="$(date +%s)"
    remaining=$((deadline > now ? deadline - now : 0))
    end_seconds=$((SECONDS + remaining))
    TG53_LAST_TASK_UPID="$upid"
    TG53_LAST_TASK_STATE=
    TG53_LAST_TASK_EXITSTATUS=
    echo "TASK_OBSERVER_UPID=$upid"
    while :; do
        exact="$(timeout --signal=TERM --kill-after=5s "$probe_sec" \
            pvesh get "/nodes/$node/tasks/$upid/status" --output-format json)" || {
            printf 'TASK_UPID=%s\nRESULT=EXACT_TASK_PROBE_UNKNOWN\nRESUME_OBSERVATION=YES\nMUTATION_REDISPATCH_AUTHORIZED=NO\n' "$upid"
            return 75
        }
        set +e
        observation="$(python3 - "$terminal_policy" "$upid" "$exact" <<'PY'
import json, sys
policy, expected_upid, raw = sys.argv[1:]
value = json.loads(raw)
if not isinstance(value, dict):
    raise SystemExit(2)
upid = value.get("upid")
state = value.get("status")
if not isinstance(upid, str) or upid != expected_upid:
    print("RESULT=EXACT_TASK_IDENTITY_UNKNOWN")
    raise SystemExit(2)
if not isinstance(state, str):
    print("RESULT=EXACT_TASK_TERMINAL_UNKNOWN")
    raise SystemExit(2)
print(f"TASK_UPID={upid}")
print(f"TASK_STATE={state}")
if state == "running":
    raise SystemExit(3)
exitstatus = value.get("exitstatus")
if not isinstance(exitstatus, str):
    print("RESULT=EXACT_TASK_TERMINAL_UNKNOWN")
    raise SystemExit(2)
print(f"TASK_EXITSTATUS={exitstatus}")
if state != "stopped" or not exitstatus:
    print("RESULT=EXACT_TASK_TERMINAL_UNKNOWN")
    raise SystemExit(2)
if policy == "require-ok" and exitstatus != "OK":
    raise SystemExit(5)
raise SystemExit(0)
PY
        )"
        rc=$?
        set -e
        if (( rc == 0 )); then
            TG53_LAST_TASK_STATE=stopped
            TG53_LAST_TASK_EXITSTATUS="$(sed -n 's/^TASK_EXITSTATUS=//p' <<<"$observation")"
            printf '%s\n' "$observation"
            return 0
        fi
        if (( rc == 5 )); then
            TG53_LAST_TASK_STATE=stopped
            TG53_LAST_TASK_EXITSTATUS="$(sed -n 's/^TASK_EXITSTATUS=//p' <<<"$observation")"
            printf '%s\n' "$observation"
            return 76
        fi
        if (( rc == 3 )); then
            TG53_LAST_TASK_STATE=running
            if (( SECONDS >= end_seconds )); then
                printf 'TASK_UPID=%s\nTASK_STATE=running\nRESULT=OBSERVATION_DEADLINE_RESUMABLE_UNKNOWN\nRESUME_OBSERVATION=YES\nMUTATION_REDISPATCH_AUTHORIZED=NO\n' "$upid"
                return 75
            fi
            sleep 2
            continue
        fi
        printf '%s\nRESUME_OBSERVATION=YES\nMUTATION_REDISPATCH_AUTHORIZED=NO\n' "$observation"
        return 75
    done
}

tg53_wait_exact_task() {
    local node="$1" vmid="$2" kind="$3" since="$4" deadline="$5"
    local terminal_policy="${6:-require-ok}"
    local probe_sec="${TG53_OBSERVER_PROBE_SEC:-30}"
    local now rows observation rc upid='' past_deadline
    [[ "$terminal_policy" =~ ^(require-ok|any-terminal)$ ]] || return 64
    [[ "$probe_sec" =~ ^[0-9]+$ && "$probe_sec" -ge 5 && "$probe_sec" -le 300 ]] || return 64
    # Public result variables are consumed by callers after this sourced
    # helper returns; they are intentionally assigned in this function.
    # shellcheck disable=SC2034
    TG53_LAST_TASK_UPID=
    # shellcheck disable=SC2034
    TG53_LAST_TASK_STATE=
    # shellcheck disable=SC2034
    TG53_LAST_TASK_EXITSTATUS=
    while :; do
        if [[ -z "$upid" ]]; then
            now="$(date +%s)"
            past_deadline=0
            (( now < deadline )) || past_deadline=1
            # Even after the observation deadline, perform one final bounded
            # discovery and exact status read.  This closes the sequential-wave
            # case where the task already finished while an earlier receipt was
            # being observed.
            rows="$(timeout --signal=TERM --kill-after=5s "$probe_sec" \
                pvesh get "/nodes/$node/tasks" --vmid "$vmid" --source all \
                --since "$since" --output-format json)" || {
                echo "RESULT=TASK_LIST_PROBE_UNKNOWN"
                return 75
            }
            set +e
            observation="$(python3 - "$kind" "$rows" <<'PY'
import json, re, sys
kind, raw = sys.argv[1:]
items = [x for x in json.loads(raw) if x.get("type") == kind]
if not items:
    raise SystemExit(3)
if len(items) != 1:
    print(f"RESULT=AMBIGUOUS_TASK_UNKNOWN count={len(items)}")
    raise SystemExit(4)
x = items[0]
upid = x.get("upid", "")
if not re.fullmatch(r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[^\r\n]+:", upid):
    raise SystemExit(2)
print(f"TASK_UPID={upid}")
raise SystemExit(0)
PY
            )"
            rc=$?
            set -e
            if (( rc == 4 )); then
                printf '%s\n' "$observation"
                return 75
            fi
            if (( rc == 3 )); then
                if (( past_deadline )); then
                    echo "RESULT=TASK_DISCOVERY_DEADLINE_UNKNOWN"
                    return 75
                fi
                sleep 2
                continue
            fi
            (( rc == 0 )) || return 75

            upid="${observation#TASK_UPID=}"
            [[ "$upid" == UPID:* && "$upid" != *$'\n'* ]] || return 75
            # shellcheck disable=SC2034
            TG53_LAST_TASK_UPID="$upid"
            echo "TASK_PINNED_UPID=$upid"
        fi

        # Once identity is pinned, inspect only that exact task.  The observer
        # can be resumed later with tg53_observe_exact_upid; it never dispatches.
        tg53_observe_exact_upid "$node" "$upid" "$deadline" "$terminal_policy"
        return $?
    done
}
