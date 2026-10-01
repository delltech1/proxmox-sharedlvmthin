#!/usr/bin/env bash
set -Eeuo pipefail

NODE="${1:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
VMID="${2:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
DISK="${3:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
TARGET="${4:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
EXPECTED_STATUS="${5:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
EVIDENCE="${6:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
DISPATCH_DEADLINE="${TG53_DISPATCH_DEADLINE_SEC:-30}"
RECEIPT_DEADLINE="${TG53_RECEIPT_DEADLINE_SEC:-600}"
REFUSAL_REGEX="${TG53_REFUSAL_REGEX:-}"

[[ "$NODE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DISK" =~ ^(ide|sata|scsi|virtio)[0-9]+$ ]]
[[ "$TARGET" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$EXPECTED_STATUS" == OK || "$EXPECTED_STATUS" == REFUSED ]]
[[ "$DISPATCH_DEADLINE" =~ ^[1-9][0-9]{0,3}$ ]]
[[ "$RECEIPT_DEADLINE" =~ ^[1-9][0-9]{0,4}$ ]]
if [[ "$EXPECTED_STATUS" == REFUSED && -z "$REFUSAL_REGEX" ]]; then
    echo "REFUSED requires TG53_REFUSAL_REGEX for one exact admission reason" >&2
    exit 64
fi

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-one-move-with-receipt"
echo "START_UTC=$(date -u +%FT%TZ)"
started="$(date +%s)"
tmp="$(mktemp -d /tmp/tg53-move.XXXXXX)"
trap 'rm -rf -- "$tmp"' EXIT
script_dir="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"

pvesh get "/nodes/$NODE/qemu/$VMID/config" --output-format json >"$tmp/config-before.json"
python3 - "$tmp/config-before.json" "$DISK" >"$tmp/identity" <<'PY'
import json,sys
with open(sys.argv[1], encoding="utf-8") as handle:
    doc=json.load(handle)
value=doc.get(sys.argv[2])
if not isinstance(value,str) or ":" not in value:
    raise SystemExit("disk has no exact storage volid")
volid=value.split(",",1)[0]
print(doc["digest"])
print(volid)
print(volid.split(":",1)[0])
PY
mapfile -t identity <"$tmp/identity"
digest="${identity[0]}"
source_volid="${identity[1]}"
source_storage="${identity[2]}"

pvesm list "$source_storage" --vmid "$VMID" --output-format json >"$tmp/source-before.json"
pvesm list "$TARGET" --vmid "$VMID" --output-format json >"$tmp/target-before.json"

set +e
timeout --foreground --kill-after=5s "${DISPATCH_DEADLINE}s" \
    pvesh create "/nodes/$NODE/qemu/$VMID/move_disk" --disk "$DISK" \
    --storage "$TARGET" --delete 1 --digest "$digest"
dispatch_rc=$?
set -e
echo "DISPATCH_CLIENT_RC=$dispatch_rc"
if [[ $dispatch_rc -eq 124 || $dispatch_rc -eq 137 ]]; then
    echo "DISPATCH_CLIENT_RESULT=TIMED_OUT_UNKNOWN"
else
    echo "DISPATCH_CLIENT_RESULT=RETURNED_NONAUTHORITATIVE"
fi

deadline=$((SECONDS + RECEIPT_DEADLINE))
upid=""
while ((SECONDS < deadline)); do
    pvesh get "/nodes/$NODE/tasks" --vmid "$VMID" --source all \
        --since "$((started - 1))" --output-format json >"$tmp/tasks.json"
    set +e
    upid="$(python3 - "$tmp/tasks.json" <<'PY'
import json,re,sys
with open(sys.argv[1], encoding="utf-8") as handle:
    rows=[x for x in json.load(handle) if x.get("type")=="qmmove"]
if len(rows)!=1: raise SystemExit(2)
upid=rows[0].get("upid","")
if not re.fullmatch(r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[^\r\n]+:",upid): raise SystemExit(2)
print(upid)
PY
)"
    receipt_rc=$?
    set -e
    if [[ $receipt_rc -eq 0 ]]; then
        pvesh get "/nodes/$NODE/tasks/$upid/status" --output-format json >"$tmp/task-status.json"
        if python3 - "$tmp/task-status.json" <<'PY'
import json,sys
with open(sys.argv[1], encoding="utf-8") as handle:
    doc=json.load(handle)
raise SystemExit(0 if doc.get("status")=="stopped" and doc.get("exitstatus") else 1)
PY
        then
            break
        fi
    elif [[ $receipt_rc -ne 2 ]]; then
        echo "task receipt discovery failed" >&2
        exit 2
    fi
    sleep 1
done
if [[ -z "$upid" || ! -s "$tmp/task-status.json" ]]; then
    echo "MOVE_ORACLE=UNKNOWN: no unique terminal receipt before deadline" >&2
    exit 2
fi

pvesh get "/nodes/$NODE/qemu/$VMID/config" --output-format json >"$tmp/config-after.json"
pvesm list "$source_storage" --vmid "$VMID" --output-format json >"$tmp/source-after.json"
pvesm list "$TARGET" --vmid "$VMID" --output-format json >"$tmp/target-after.json"

python3 "$script_dir/tg53-move-receipt-oracle.py" \
    --expected "$EXPECTED_STATUS" --refusal-regex "$REFUSAL_REGEX" \
    --disk "$DISK" --source-volid "$source_volid" --target-storage "$TARGET" \
    --config-before "$tmp/config-before.json" --config-after "$tmp/config-after.json" \
    --source-before "$tmp/source-before.json" --source-after "$tmp/source-after.json" \
    --target-before "$tmp/target-before.json" --target-after "$tmp/target-after.json" \
    --task-status "$tmp/task-status.json"

echo "END_UTC=$(date -u +%FT%TZ)"
echo "MOVE_RECEIPT_VERIFIED=PASS"
