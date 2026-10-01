#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

EVIDENCE="${EVIDENCE:-/tmp/tg53-fiveway-mixed-wave.evidence}"
SNAP_NAME="${SNAP_NAME:-tg53wavea}"
DEADLINE_SEC="${DEADLINE_SEC:-900}"
EAGER_SNAP_NODE="${EAGER_SNAP_NODE:-pve-lab-a}"
EAGER_SNAP_VMID="${EAGER_SNAP_VMID:-994370}"
LAZY_SNAP_NODE="${LAZY_SNAP_NODE:-pve-lab-c}"
LAZY_SNAP_VMID="${LAZY_SNAP_VMID:-994371}"
THIN_MOVE_NODE="${THIN_MOVE_NODE:-pve-lab-b}"
THIN_MOVE_VMID="${THIN_MOVE_VMID:-994372}"
EAGER_MOVE_NODE="${EAGER_MOVE_NODE:-pve-lab-c}"
EAGER_MOVE_VMID="${EAGER_MOVE_VMID:-994373}"
LAZY_MOVE_NODE="${LAZY_MOVE_NODE:-pve-lab-a}"
LAZY_MOVE_VMID="${LAZY_MOVE_VMID:-994374}"
THIN_STORAGE="${THIN_STORAGE:-slt-tg-thin}"
EAGER_STORAGE="${EAGER_STORAGE:-slt-lab-thick-a}"
LAZY_STORAGE="${LAZY_STORAGE:-slt-lab-lazy-a}"

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-fiveway-mixed-wave"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "COORDINATOR=$(hostname)"

for value in "$SNAP_NAME" "$EAGER_SNAP_NODE" "$LAZY_SNAP_NODE" \
    "$THIN_MOVE_NODE" "$EAGER_MOVE_NODE" "$LAZY_MOVE_NODE" \
    "$THIN_STORAGE" "$EAGER_STORAGE" "$LAZY_STORAGE"; do
    [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
done
for vmid in "$EAGER_SNAP_VMID" "$LAZY_SNAP_VMID" "$THIN_MOVE_VMID" \
    "$EAGER_MOVE_VMID" "$LAZY_MOVE_VMID"; do
    [[ "$vmid" =~ ^[1-9][0-9]{2,8}$ ]]
done
[[ "$DEADLINE_SEC" =~ ^[1-9][0-9]*$ ]]

config_json() {
    pvesh get "/nodes/$1/qemu/$2/config" --output-format json
}
volume_from_config() {
    python3 -c 'import json,sys; print(json.loads(sys.argv[1])["scsi0"].split(",",1)[0])' "$1"
}
digest_from_config() {
    python3 -c 'import json,sys; print(json.loads(sys.argv[1])["digest"])' "$1"
}
storage_move_preflight() {
    local node="$1" vmid="$2" target="$3"
    ssh -o BatchMode=yes -- "root@$node" \
        /usr/sbin/sharedlvmthin storage-move-preflight "$vmid" scsi0 "$target"
}

eager_snap_cfg="$(config_json "$EAGER_SNAP_NODE" "$EAGER_SNAP_VMID")"
lazy_snap_cfg="$(config_json "$LAZY_SNAP_NODE" "$LAZY_SNAP_VMID")"
thin_move_cfg="$(config_json "$THIN_MOVE_NODE" "$THIN_MOVE_VMID")"
eager_move_cfg="$(config_json "$EAGER_MOVE_NODE" "$EAGER_MOVE_VMID")"
lazy_move_cfg="$(config_json "$LAZY_MOVE_NODE" "$LAZY_MOVE_VMID")"

[[ "$(volume_from_config "$eager_snap_cfg")" == "$EAGER_STORAGE:"* ]]
[[ "$(volume_from_config "$lazy_snap_cfg")" == "$LAZY_STORAGE:"* ]]
[[ "$(volume_from_config "$thin_move_cfg")" == "$THIN_STORAGE:"* ]]
[[ "$(volume_from_config "$eager_move_cfg")" == "$EAGER_STORAGE:"* ]]
[[ "$(volume_from_config "$lazy_move_cfg")" == "$LAZY_STORAGE:"* ]]

# Native PVE activates a source before clone_disk() and does not deactivate it
# on every early clone failure.  Refuse an unmaterialized Lazy source before
# dispatch, while it is still DORMANT.  The API digest below closes the config
# race; the storage plugin repeats authoritative mutation checks internally.
storage_move_preflight "$THIN_MOVE_NODE" "$THIN_MOVE_VMID" "$EAGER_STORAGE"
storage_move_preflight "$EAGER_MOVE_NODE" "$EAGER_MOVE_VMID" "$LAZY_STORAGE"
storage_move_preflight "$LAZY_MOVE_NODE" "$LAZY_MOVE_VMID" "$THIN_STORAGE"

for pair in \
    "$EAGER_SNAP_NODE:$EAGER_SNAP_VMID" "$LAZY_SNAP_NODE:$LAZY_SNAP_VMID" \
    "$THIN_MOVE_NODE:$THIN_MOVE_VMID" "$EAGER_MOVE_NODE:$EAGER_MOVE_VMID" \
    "$LAZY_MOVE_NODE:$LAZY_MOVE_VMID"; do
    node="${pair%%:*}"; vmid="${pair##*:}"
    [[ "$(pvesh get "/nodes/$node/qemu/$vmid/status/current" --output-format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')" == stopped ]]
done

started="$(date +%s)"
tmpdir="$(mktemp -d)"
echo "DISPATCH_LOG_DIR=$tmpdir"

tg53_dispatch_detached "eager-snap-${EAGER_SNAP_VMID}-$$" create \
  "/nodes/$EAGER_SNAP_NODE/qemu/$EAGER_SNAP_VMID/snapshot" \
    --snapname "$SNAP_NAME" --vmstate 0 --description 'TG53 five-way Eager snapshot' \
    >"$tmpdir/eager-snapshot" 2>&1
tg53_dispatch_detached "lazy-snap-${LAZY_SNAP_VMID}-$$" create \
  "/nodes/$LAZY_SNAP_NODE/qemu/$LAZY_SNAP_VMID/snapshot" \
    --snapname "$SNAP_NAME" --vmstate 0 --description 'TG53 five-way Lazy snapshot' \
    >"$tmpdir/lazy-snapshot" 2>&1
tg53_dispatch_detached "thin-move-${THIN_MOVE_VMID}-$$" create \
  "/nodes/$THIN_MOVE_NODE/qemu/$THIN_MOVE_VMID/move_disk" \
    --disk scsi0 --storage "$EAGER_STORAGE" --delete 1 \
    --digest "$(digest_from_config "$thin_move_cfg")" \
    >"$tmpdir/thin-move" 2>&1
tg53_dispatch_detached "eager-move-${EAGER_MOVE_VMID}-$$" create \
  "/nodes/$EAGER_MOVE_NODE/qemu/$EAGER_MOVE_VMID/move_disk" \
    --disk scsi0 --storage "$LAZY_STORAGE" --delete 1 \
    --digest "$(digest_from_config "$eager_move_cfg")" \
    >"$tmpdir/eager-move" 2>&1
tg53_dispatch_detached "lazy-move-${LAZY_MOVE_VMID}-$$" create \
  "/nodes/$LAZY_MOVE_NODE/qemu/$LAZY_MOVE_VMID/move_disk" \
    --disk scsi0 --storage "$THIN_STORAGE" --delete 1 \
    --digest "$(digest_from_config "$lazy_move_cfg")" \
    >"$tmpdir/lazy-move" 2>&1

deadline="$((started + DEADLINE_SEC))"
tg53_wait_exact_task "$EAGER_SNAP_NODE" "$EAGER_SNAP_VMID" qmsnapshot "$started" "$deadline"
tg53_wait_exact_task "$LAZY_SNAP_NODE" "$LAZY_SNAP_VMID" qmsnapshot "$started" "$deadline"
tg53_wait_exact_task "$THIN_MOVE_NODE" "$THIN_MOVE_VMID" qmmove "$started" "$deadline"
tg53_wait_exact_task "$EAGER_MOVE_NODE" "$EAGER_MOVE_VMID" qmmove "$started" "$deadline"
tg53_wait_exact_task "$LAZY_MOVE_NODE" "$LAZY_MOVE_VMID" qmmove "$started" "$deadline"

receipt() {
    local label="$1" node="$2" vmid="$3" kind="$4"
    pvesh get "/nodes/$node/tasks" --vmid "$vmid" --source all \
        --since "$((started - 1))" --output-format json \
        | python3 -c '
import json,re,sys
label,kind=sys.argv[1:]
rows=[x for x in json.load(sys.stdin) if x.get("type")==kind]
assert len(rows)==1, f"{label}: ambiguous {kind} receipts: {len(rows)}"
x=rows[0]; upid=x.get("upid",""); status=x.get("status","")
assert re.fullmatch(r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[^\r\n]+:",upid)
assert status and status != "running", f"{label}: nonterminal status"
print(f"{label}_UPID={upid}")
print(f"{label}_STATUS={status}")
' "$label" "$kind"
}

receipt EAGER_SNAPSHOT "$EAGER_SNAP_NODE" "$EAGER_SNAP_VMID" qmsnapshot
receipt LAZY_SNAPSHOT "$LAZY_SNAP_NODE" "$LAZY_SNAP_VMID" qmsnapshot
receipt THIN_TO_EAGER "$THIN_MOVE_NODE" "$THIN_MOVE_VMID" qmmove
receipt EAGER_TO_LAZY "$EAGER_MOVE_NODE" "$EAGER_MOVE_VMID" qmmove
receipt LAZY_TO_THIN "$LAZY_MOVE_NODE" "$LAZY_MOVE_VMID" qmmove

for file in "$tmpdir"/*; do
    echo "DISPATCH_LOG=$(basename "$file")"
    sed 's/^/  /' "$file"
done
echo "WAVE_SECONDS=$(($(date +%s)-started))"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_FIVEWAY_RECEIPTS=COLLECTED"
echo "NOTE=COLLECTED proves exact terminal receipts only; each operation status and postcondition must be evaluated separately"
