#!/usr/bin/env bash
set -Eeuo pipefail

pairs=(
    pve-lab-a:994370
    pve-lab-c:994371
    pve-lab-b:994372
    pve-lab-c:994373
    pve-lab-a:994374
)

for pair in "${pairs[@]}"; do
    node="${pair%%:*}"
    vmid="${pair##*:}"
    if pvesh get "/nodes/$node/qemu/$vmid/config" >/dev/null 2>&1; then
        echo "CONFIG_STILL_PRESENT=$pair"
        exit 2
    fi
    pvesh get "/nodes/$node/tasks" --vmid "$vmid" --source all --limit 20 \
        --output-format json | python3 -c '
import json,sys
vm=sys.argv[1]
rows=[x for x in json.load(sys.stdin) if x.get("type")=="qmdestroy"]
assert rows, f"no qmdestroy receipt for {vm}"
x=rows[0]
print(f"DESTROY_{vm}_UPID={x.get('"'"'upid'"'"')}")
print(f"DESTROY_{vm}_STATUS={x.get('"'"'status'"'"')}")
assert x.get("status")=="OK", x.get("status")
' "$vmid"
done

if lvs --noheadings -o lv_name pve-slt-tg-lab-a pve-slt-tg-qual 2>/dev/null \
    | grep -E '994370|994371|994372|994373|994374'; then
    echo "OBJECTS_REMAIN"
    exit 3
fi
echo "OBJECTS_ABSENT=PASS"

for sid in slt-lab-thick-a slt-tg-thin; do
    result="$(/usr/sbin/sharedlvmthin recovery-check "$sid")"
    printf '%s\n' "$result" | grep -E '^(STATE|SAFE_FOR_MUTATION|THICK_ANCHORS_HEALTHY)='
    grep -q '^STATE=HEALTHY$' <<<"$result"
    grep -q '^SAFE_FOR_MUTATION=YES$' <<<"$result"
done
echo "TG53_FIVEWAY_CLEANUP=PASS"
