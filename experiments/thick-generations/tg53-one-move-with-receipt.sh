#!/usr/bin/env bash
set -Eeuo pipefail

NODE="${1:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
VMID="${2:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
DISK="${3:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
TARGET="${4:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
EXPECTED_STATUS="${5:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"
EVIDENCE="${6:?usage: $0 NODE VMID DISK TARGET EXPECTED_STATUS EVIDENCE}"

[[ "$NODE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DISK" =~ ^(ide|sata|scsi|virtio)[0-9]+$ ]]
[[ "$TARGET" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$EXPECTED_STATUS" == OK || "$EXPECTED_STATUS" == REFUSED ]]

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-one-move-with-receipt"
echo "START_UTC=$(date -u +%FT%TZ)"
started="$(date +%s)"
cfg="$(pvesh get "/nodes/$NODE/qemu/$VMID/config" --output-format json)"
digest="$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["digest"])' "$cfg")"

pvesh create "/nodes/$NODE/qemu/$VMID/move_disk" --disk "$DISK" \
    --storage "$TARGET" --delete 1 --digest "$digest" || true

pvesh get "/nodes/$NODE/tasks" --vmid "$VMID" --source all \
    --since "$((started - 1))" --output-format json \
    | python3 -c '
import json,re,sys
expected=sys.argv[1]
rows=[x for x in json.load(sys.stdin) if x.get("type")=="qmmove"]
assert len(rows)==1, f"ambiguous qmmove receipts: {len(rows)}"
x=rows[0]; upid=x.get("upid",""); status=x.get("status","")
assert re.fullmatch(r"UPID:[A-Za-z0-9][A-Za-z0-9.-]*:[^\r\n]+:",upid)
assert status and status != "running", "nonterminal task"
print(f"MOVE_UPID={upid}")
print(f"MOVE_STATUS={status}")
if expected=="OK": assert status=="OK", status
else: assert status!="OK", "expected refusal but task succeeded"
' "$EXPECTED_STATUS"

echo "END_UTC=$(date -u +%FT%TZ)"
echo "MOVE_RECEIPT_VERIFIED=PASS"
