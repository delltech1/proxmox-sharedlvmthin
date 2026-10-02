#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

SNAP="${SNAP:-tg53batch}"
EVIDENCE="${EVIDENCE:-/tmp/tg53-tenway-snapshot-batch.evidence}"
VMIDS_CSV="${VMIDS_CSV:-990105,990110,990111,990113,990115,991190,991191,991192,991193,991194}"
OBSERVE_DEADLINE_SEC="${OBSERVE_DEADLINE_SEC:-7200}"
MIN_CREATE_SUCCESS="${MIN_CREATE_SUCCESS:-3}"
REQUIRED_SUCCESS_MODES="${REQUIRED_SUCCESS_MODES:-thin,eager,lazy}"
[[ "$OBSERVE_DEADLINE_SEC" =~ ^[0-9]+$ && "$OBSERVE_DEADLINE_SEC" -ge 900 \
    && "$OBSERVE_DEADLINE_SEC" -le 86400 ]] || {
    echo "OBSERVE_DEADLINE_SEC must be between 900 and 86400" >&2
    exit 64
}
[[ "$MIN_CREATE_SUCCESS" =~ ^[0-9]+$ && "$MIN_CREATE_SUCCESS" -ge 1 \
    && "$MIN_CREATE_SUCCESS" -le 10 ]] || {
    echo "MIN_CREATE_SUCCESS must be between 1 and 10" >&2
    exit 64
}
IFS=, read -r -a VMIDS <<<"$VMIDS_CSV"
[[ "${#VMIDS[@]}" -eq 10 ]] || {
    echo "VMIDS_CSV must contain exactly ten VMIDs" >&2
    exit 64
}
declare -A OWNER
declare -A CREATED
declare -A WAVE
declare -A VM_MODES
declare -A MODE_SUCCESS
create_success=0
create_refusal=0
SAFE_REFUSAL='timed out waiting for an exact foreign Thick transition; no storage mutation was issued'

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-tenway-snapshot-batch"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"

storage_config="$(pvesh get /storage --output-format json)"
IFS=, read -r -a required_modes <<<"$REQUIRED_SUCCESS_MODES"
[[ "${#required_modes[@]}" -ge 1 ]] || exit 64
for mode in "${required_modes[@]}"; do
    case "$mode" in thin|eager|lazy) MODE_SUCCESS[$mode]=0 ;; *) echo "invalid required mode: $mode" >&2; exit 64 ;; esac
done

for id in "${VMIDS[@]}"; do
    [[ "$id" =~ ^[1-9][0-9]{2,8}$ ]] || exit 64
    owner="$(pvesh get /cluster/resources --type vm --output-format json \
        | python3 -c 'import json,sys; v=int(sys.argv[1]); r=[x for x in json.load(sys.stdin) if x.get("type")=="qemu" and int(x.get("vmid",-1))==v]; assert len(r)==1; print(r[0]["node"])' "$id")"
    [[ "$owner" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || exit 2
    OWNER[$id]="$owner"
    echo "VM_${id}_OWNER=$owner"
    test "$(pvesh get "/nodes/$owner/qemu/$id/status/current" --output-format json \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')" = stopped
    snapshots="$(pvesh get "/nodes/$owner/qemu/$id/snapshot" --output-format json)"
    inventory_state="$(python3 -c 'import json,sys; n=sys.argv[1]; rows=json.loads(sys.argv[2]); assert isinstance(rows,list); print("PRESENT" if any(isinstance(x,dict) and x.get("name")==n for x in rows) else "ABSENT")' "$SNAP" "$snapshots")"
    case "$inventory_state" in
        ABSENT) ;;
        PRESENT) echo "snapshot $SNAP already exists on VM $id" >&2; exit 2 ;;
        *) echo "invalid snapshot inventory verdict for VM $id" >&2; exit 2 ;;
    esac
    vm_config="$(pvesh get "/nodes/$owner/qemu/$id/config" --output-format json)"
    printf '%s' "$vm_config" \
        | python3 -c 'import json,re,sys; d=json.load(sys.stdin); raise SystemExit(0 if any(re.fullmatch(r"(?:(?:scsi|virtio|sata|ide)[0-9]+|efidisk0|tpmstate0)", k) for k in d) else 1)'
    modes="$(python3 -c '
import json,re,sys
cfg=json.loads(sys.argv[1]); stores={x["storage"]:x for x in json.loads(sys.argv[2])}
found=set()
for key,value in cfg.items():
    if not re.fullmatch(r"(?:(?:scsi|virtio|sata|ide)[0-9]+|efidisk0|tpmstate0)", key) or not isinstance(value,str) or ":" not in value:
        continue
    storage=value.split(":",1)[0]
    definition=stores.get(storage,{})
    raw=definition.get("slt-allocation-mode")
    if definition.get("type") == "sharedlvmthin" and raw in (None,"thin"): found.add("thin")
    elif raw == "thick-generations": found.add("eager")
    elif raw == "thick-generations-lazy": found.add("lazy")
print(",".join(sorted(found)))
' "$vm_config" "$storage_config")"
    [[ -n "$modes" ]] || { echo "VM $id has no classified SharedLVM disk" >&2; exit 2; }
    VM_MODES[$id]="$modes"
    echo "VM_${id}_MODES=$modes"
    if [[ "$owner" = "$(hostname)" ]]; then
        /usr/sbin/sharedlvmthin snapshot-preflight "$id"
    else
        ssh -o BatchMode=yes -- "root@$owner" /usr/sbin/sharedlvmthin snapshot-preflight "$id"
    fi
done

max_wave=0
while IFS=$'\t' read -r wave id resources; do
    [[ "$wave" =~ ^[0-9]+$ && "$id" =~ ^[1-9][0-9]{2,8}$ && -n "$resources" ]] || exit 2
    [[ -n "${OWNER[$id]:-}" && -z "${WAVE[$id]:-}" ]] || exit 2
    WAVE[$id]="$wave"
    (( wave > max_wave )) && max_wave="$wave"
    echo "VM_${id}_WAVE=$wave RESOURCES=$resources"
done < <(python3 "$SCRIPT_DIR/tg53-vg-wave-plan.py" --vmids "$VMIDS_CSV")
[[ "${#WAVE[@]}" -eq 10 ]] || {
    echo "VG wave planner did not return every VM exactly once" >&2
    exit 2
}

run_batch() {
    local op="$1"
    local started finished task_type
    for ((wave=0; wave<=max_wave; wave++)); do
    started=$(date +%s)
    echo "${op}_WAVE_${wave}_START=$(date -u +%FT%TZ)"
    for id in "${VMIDS[@]}"; do
        [ "${WAVE[$id]}" = "$wave" ] || continue
        owner="${OWNER[$id]}"
        if [ "$op" = DELETE ] && [ "${CREATED[$id]:-0}" != 1 ]; then
            echo "${op}_VM_${id}=SKIPPED_SAFE_REFUSAL"
            continue
        fi
        echo "${op}_VM_${id}_START=$(date -u +%FT%TZ)"
        if [ "$op" = CREATE ]; then
            tg53_dispatch_detached "tenway-create-${id}-$$" create \
                "/nodes/$owner/qemu/$id/snapshot" \
                --snapname "$SNAP" \
                --description 'TG53 ten-way detached batch'
        else
            tg53_dispatch_detached "tenway-delete-${id}-$$" delete \
                "/nodes/$owner/qemu/$id/snapshot/$SNAP"
        fi
    done
    task_type=qmsnapshot
    [ "$op" = CREATE ] || task_type=qmdelsnapshot
    for id in "${VMIDS[@]}"; do
        [ "${WAVE[$id]}" = "$wave" ] || continue
        if [ "$op" = DELETE ] && [ "${CREATED[$id]:-0}" != 1 ]; then
            continue
        fi
        tg53_wait_exact_task "${OWNER[$id]}" "$id" "$task_type" \
            "$started" "$((started + OBSERVE_DEADLINE_SEC))" any-terminal
        [[ "$TG53_LAST_TASK_UPID" == UPID:* \
            && "$TG53_LAST_TASK_STATE" == stopped \
            && -n "$TG53_LAST_TASK_EXITSTATUS" ]] || {
            echo "RESULT=EXACT_TASK_RECEIPT_UNKNOWN vmid=$id" >&2
            exit 2
        }
        status="$TG53_LAST_TASK_EXITSTATUS"
        echo "${op}_VM_${id}_UPID=$TG53_LAST_TASK_UPID"
        if [ "$status" = OK ]; then
            CREATED[$id]=1
            if [ "$op" = CREATE ]; then
                create_success=$((create_success + 1))
                IFS=, read -r -a success_modes <<<"${VM_MODES[$id]}"
                for mode in "${success_modes[@]}"; do
                    if [[ -v "MODE_SUCCESS[$mode]" ]]; then
                        MODE_SUCCESS[$mode]=$((MODE_SUCCESS[$mode] + 1))
                    fi
                done
            fi
            echo "${op}_VM_${id}=PASS"
        elif [ "$op" = CREATE ] && [ "$status" = "$SAFE_REFUSAL" ]; then
            CREATED[$id]=0
            create_refusal=$((create_refusal + 1))
            current="$(pvesh get "/nodes/${OWNER[$id]}/qemu/$id/snapshot" --output-format json)"
            case "$current" in
                *"\"$SNAP\""*) echo "refused VM $id unexpectedly owns snapshot $SNAP" >&2; exit 2 ;;
            esac
            cfg="$(pvesh get "/nodes/${OWNER[$id]}/qemu/$id/config" --output-format json)"
            case "$cfg" in
                *"\"lock\""*|*"\"snapstate\""*) echo "refused VM $id retains transient config state" >&2; exit 2 ;;
            esac
            echo "${op}_VM_${id}=SAFE_REFUSAL"
        else
            printf '%s_VM_%s=UNEXPECTED_TERMINAL_STATUS:%s\n' "$op" "$id" "$status" >&2
            exit 2
        fi
    done
    finished=$(date +%s)
    echo "${op}_WAVE_${wave}_SECONDS=$((finished-started))"
    done
}

run_batch CREATE
echo "CREATE_SUCCESS_COUNT=$create_success"
echo "CREATE_SAFE_REFUSAL_COUNT=$create_refusal"
coverage_failure=0
if (( create_success < MIN_CREATE_SUCCESS )); then
    printf 'RESULT=INSUFFICIENT_SUCCESSFUL_SNAPSHOTS required=%s observed=%s\n' \
        "$MIN_CREATE_SUCCESS" "$create_success" >&2
    coverage_failure=1
fi
if (( create_success + create_refusal != 10 )); then
    echo "RESULT=CREATE_ACCOUNTING_UNKNOWN" >&2
    coverage_failure=1
fi
for mode in "${required_modes[@]}"; do
    echo "CREATE_${mode^^}_SUCCESS_COUNT=${MODE_SUCCESS[$mode]}"
    if (( MODE_SUCCESS[$mode] < 1 )); then
        echo "RESULT=MISSING_REQUIRED_MODE_SUCCESS mode=$mode" >&2
        coverage_failure=1
    fi
done
for id in "${VMIDS[@]}"; do
    owner="${OWNER[$id]}"
    if [ "${CREATED[$id]:-0}" != 1 ]; then
        continue
    fi
    pvesh get "/nodes/$owner/qemu/$id/config" --snapshot "$SNAP" >/dev/null
    pvesh get "/nodes/$owner/qemu/$id/snapshot" --output-format json \
        | python3 -c 'import json,sys; n=sys.argv[1]; raise SystemExit(0 if any(x.get("name")==n for x in json.load(sys.stdin)) else 1)' "$SNAP"
done
echo "CREATE_BATCH_VALIDATED=PASS"

run_batch DELETE
for id in "${VMIDS[@]}"; do
    owner="${OWNER[$id]}"
    snapshots="$(pvesh get "/nodes/$owner/qemu/$id/snapshot" --output-format json)"
    inventory_state="$(python3 -c 'import json,sys; n=sys.argv[1]; rows=json.loads(sys.argv[2]); assert isinstance(rows,list); print("PRESENT" if any(isinstance(x,dict) and x.get("name")==n for x in rows) else "ABSENT")' "$SNAP" "$snapshots")"
    case "$inventory_state" in
        ABSENT) ;;
        PRESENT) echo "snapshot $SNAP remains on VM $id after delete" >&2; exit 2 ;;
        *) echo "invalid snapshot inventory verdict for VM $id" >&2; exit 2 ;;
    esac
    cfg="$(pvesh get "/nodes/$owner/qemu/$id/config" --output-format json)"
    config_state="$(python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert isinstance(d,dict); print("TRANSIENT" if any(k in d for k in ("lock","snapstate")) else "CLEAR")' "$cfg")"
    case "$config_state" in
        CLEAR) ;;
        TRANSIENT) echo "VM $id retains a lock or snapshot state after delete" >&2; exit 2 ;;
        *) echo "invalid VM config inventory verdict for VM $id" >&2; exit 2 ;;
    esac
done
echo "DELETE_BATCH_VALIDATED=PASS"
echo "END_UTC=$(date -u +%FT%TZ)"
if (( coverage_failure != 0 )); then
    echo "TG53_TENWAY_SNAPSHOT_BATCH=FAIL_COVERAGE_CLEANED" >&2
    exit 2
fi
echo "TG53_TENWAY_SNAPSHOT_BATCH=PASS"
