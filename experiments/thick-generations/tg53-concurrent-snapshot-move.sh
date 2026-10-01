#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

SNAP_NODE="${SNAP_NODE:?set SNAP_NODE}"
SNAP_VMID="${SNAP_VMID:?set SNAP_VMID}"
MOVE_NODE="${MOVE_NODE:?set MOVE_NODE}"
MOVE_VMID="${MOVE_VMID:?set MOVE_VMID}"
TARGET_STORAGE="${TARGET_STORAGE:?set TARGET_STORAGE}"
SOURCE_STORAGE="${SOURCE_STORAGE:?set SOURCE_STORAGE}"
SNAP_NAME="${SNAP_NAME:-tg53concurrent}"
EVIDENCE="${EVIDENCE:-/tmp/tg53-concurrent-snapshot-move.evidence}"
DEADLINE_SEC="${DEADLINE_SEC:-900}"

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-concurrent-snapshot-move"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "COORDINATOR=$(hostname)"
echo "SNAP_NODE=$SNAP_NODE"
echo "SNAP_VMID=$SNAP_VMID"
echo "MOVE_NODE=$MOVE_NODE"
echo "MOVE_VMID=$MOVE_VMID"
echo "TARGET_STORAGE=$TARGET_STORAGE"
echo "SOURCE_STORAGE=$SOURCE_STORAGE"

for value in "$SNAP_NODE" "$MOVE_NODE" "$TARGET_STORAGE" "$SOURCE_STORAGE" "$SNAP_NAME"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
done
[[ "$SNAP_VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$MOVE_VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DEADLINE_SEC" =~ ^[1-9][0-9]*$ ]]

snap_cfg="$(pvesh get "/nodes/$SNAP_NODE/qemu/$SNAP_VMID/config" --output-format json)"
move_cfg="$(pvesh get "/nodes/$MOVE_NODE/qemu/$MOVE_VMID/config" --output-format json)"
snap_status="$(pvesh get "/nodes/$SNAP_NODE/qemu/$SNAP_VMID/status/current" --output-format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
move_status="$(pvesh get "/nodes/$MOVE_NODE/qemu/$MOVE_VMID/status/current" --output-format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
[[ "$snap_status" == stopped && "$move_status" == stopped ]]
snap_volume="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["scsi0"].split(",",1)[0])' "$snap_cfg")"
move_volume="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["scsi0"].split(",",1)[0])' "$move_cfg")"
move_digest="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["digest"])' "$move_cfg")"
[[ "$snap_volume" == "$TARGET_STORAGE:"* ]]
[[ "$move_volume" == "$SOURCE_STORAGE:"* ]]
ssh -o BatchMode=yes -- "root@$MOVE_NODE" \
    /usr/sbin/sharedlvmthin storage-move-preflight \
    "$MOVE_VMID" scsi0 "$TARGET_STORAGE"
pvesh get "/nodes/$SNAP_NODE/qemu/$SNAP_VMID/snapshot" --output-format json \
    | python3 -c 'import json,sys; n=sys.argv[1]; raise SystemExit(0 if all(x.get("name") != n for x in json.load(sys.stdin)) else 1)' "$SNAP_NAME"

snap_out="$(mktemp)"
move_out="$(mktemp)"
trap 'rm -f -- "$snap_out" "$move_out"' EXIT HUP INT TERM
started="$(date +%s)"
tg53_dispatch_detached "concurrent-snap-${SNAP_VMID}-$$" create \
  "/nodes/$SNAP_NODE/qemu/$SNAP_VMID/snapshot" \
    --snapname "$SNAP_NAME" --vmstate 0 --description 'TG53 concurrent VG test' \
    --output-format json >"$snap_out"
tg53_dispatch_detached "concurrent-move-${MOVE_VMID}-$$" create \
  "/nodes/$MOVE_NODE/qemu/$MOVE_VMID/move_disk" \
    --disk scsi0 --storage "$TARGET_STORAGE" --delete 1 --digest "$move_digest" \
    --output-format json >"$move_out"

deadline="$((started + DEADLINE_SEC))"
tg53_wait_exact_task "$SNAP_NODE" "$SNAP_VMID" qmsnapshot "$started" "$deadline"
tg53_wait_exact_task "$MOVE_NODE" "$MOVE_VMID" qmmove "$started" "$deadline"

# pvesh's CLI layer waits for a worker and may emit no UPID even though the
# REST operation has one.  Bind the result to the only exact node/VM/type task
# started in this test window; ambiguity is UNKNOWN and never authorizes retry.
task_receipt() {
    local node="$1" vmid="$2" type="$3"
    pvesh get "/nodes/$node/tasks" --vmid "$vmid" --source all \
        --since "$((started - 1))" --output-format json \
        | python3 -c '
import json,re,sys
kind=sys.argv[1]
rows=[x for x in json.load(sys.stdin) if x.get("type")==kind]
assert len(rows)==1, f"ambiguous {kind} receipts: {len(rows)}"
x=rows[0]
upid=x.get("upid","")
assert re.fullmatch(r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[^\r\n]+:",upid)
print(upid)
print(x.get("status",""))
' "$type"
}
mapfile -t snap_receipt < <(task_receipt "$SNAP_NODE" "$SNAP_VMID" qmsnapshot) || {
    echo "RESULT=SNAP_TASK_UNKNOWN"
    exit 2
}
mapfile -t move_receipt < <(task_receipt "$MOVE_NODE" "$MOVE_VMID" qmmove) || {
    echo "RESULT=MOVE_TASK_UNKNOWN"
    exit 2
}
snap_upid="${snap_receipt[0]}"
snap_exit="${snap_receipt[1]}"
move_upid="${move_receipt[0]}"
move_exit="${move_receipt[1]}"
echo "SNAP_UPID=$snap_upid"
echo "MOVE_UPID=$move_upid"
echo "SNAP_EXITSTATUS=$snap_exit"
echo "MOVE_EXITSTATUS=$move_exit"
[[ "$snap_exit" == OK ]]

snap_after="$(pvesh get "/nodes/$SNAP_NODE/qemu/$SNAP_VMID/snapshot" --output-format json)"
python3 -c 'import json,sys; n=sys.argv[1]; rows=[x for x in json.loads(sys.argv[2]) if x.get("name")==n]; assert len(rows)==1' \
    "$SNAP_NAME" "$snap_after"
move_after="$(pvesh get "/nodes/$MOVE_NODE/qemu/$MOVE_VMID/config" --output-format json)"
target_volume="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["scsi0"].split(",",1)[0])' "$move_after")"

if [[ "$move_exit" != OK ]]; then
    [[ "$move_exit" == *"unresolved transaction"*"mutation refused"* ]]
    [[ "$target_volume" == "$move_volume" ]]
    source_inventory="$(pvesm list "$SOURCE_STORAGE" --vmid "$MOVE_VMID")" || {
        echo "RESULT=SOURCE_INVENTORY_UNKNOWN"
        exit 2
    }
    target_inventory="$(pvesm list "$TARGET_STORAGE" --vmid "$MOVE_VMID")" || {
        echo "RESULT=TARGET_INVENTORY_UNKNOWN"
        exit 2
    }
    grep -Fq "$move_volume" <<<"$source_inventory"
    if grep -Fq "vm-$MOVE_VMID-" <<<"$target_inventory"; then
        echo "unexpected target volume exists after refused move" >&2
        exit 2
    fi
    echo "SNAP_VOLUME=$snap_volume"
    echo "MOVE_SOURCE_VOLUME=$move_volume"
    echo "MOVE_TARGET_VOLUME=ABSENT"
    echo "CONCURRENT_SECONDS=$(($(date +%s)-started))"
    echo "END_UTC=$(date -u +%FT%TZ)"
    echo "CONCURRENT_SNAPSHOT_MOVE=SAFE_REFUSAL"
    exit 0
fi

[[ "$target_volume" == "$TARGET_STORAGE:"* ]]
source_inventory="$(pvesm list "$SOURCE_STORAGE" --vmid "$MOVE_VMID")" || {
    echo "RESULT=SOURCE_INVENTORY_UNKNOWN"
    exit 2
}
if grep -Fq "$move_volume" <<<"$source_inventory"; then
    echo "source volume remains after successful move" >&2
    exit 2
fi

echo "SNAP_VOLUME=$snap_volume"
echo "MOVE_SOURCE_VOLUME=$move_volume"
echo "MOVE_TARGET_VOLUME=$target_volume"
echo "CONCURRENT_SECONDS=$(($(date +%s)-started))"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "CONCURRENT_SNAPSHOT_MOVE=PASS"
