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

tg53_wait_exact_task() {
    local node="$1" vmid="$2" kind="$3" since="$4" deadline="$5"
    local now rows observation rc
    while :; do
        now="$(date +%s)"
        if (( now >= deadline )); then
            echo "RESULT=OBSERVATION_DEADLINE_UNKNOWN"
            return 75
        fi
        rows="$(pvesh get "/nodes/$node/tasks" --vmid "$vmid" --source all \
            --since "$since" --output-format json)" || return 75
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
status = str(x.get("status", ""))
print(f"TASK_STATE={status}")
print(f"TASK_EXITSTATUS={x.get('exitstatus', status)}")
raise SystemExit(0 if status.casefold() != "running" else 3)
PY
        )"
        rc=$?
        set -e
        if (( rc == 0 )); then
            printf '%s\n' "$observation"
            return 0
        fi
        if (( rc == 4 )); then
            printf '%s\n' "$observation"
            return 75
        fi
        (( rc == 3 )) || return 75
        sleep 2
    done
}
