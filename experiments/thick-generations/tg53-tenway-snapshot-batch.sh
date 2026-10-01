#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

SNAP="${SNAP:-tg53batch}"
EVIDENCE="${EVIDENCE:-/tmp/tg53-tenway-snapshot-batch.evidence}"
VMIDS=(990105 990110 990111 990113 990115 991190 991191 991192 991193 991194)

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-tenway-snapshot-batch"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
NODE="$(hostname)"

for id in "${VMIDS[@]}"; do
    test "$(qm status "$id" | awk '{print $2}')" = stopped
    if qm listsnapshot "$id" | grep -q "$SNAP"; then
        echo "snapshot $SNAP already exists on VM $id" >&2
        exit 2
    fi
    qm config "$id" | grep -Eq '^(scsi|virtio|sata|ide)[0-9]+:'
    /usr/sbin/sharedlvmthin snapshot-preflight "$id"
done

run_batch() {
    local op="$1"
    local started finished task_type
    started=$(date +%s)
    for id in "${VMIDS[@]}"; do
        echo "${op}_VM_${id}_START=$(date -u +%FT%TZ)"
        if [ "$op" = CREATE ]; then
            tg53_dispatch_detached "tenway-create-${id}-$$" create \
                "/nodes/$NODE/qemu/$id/snapshot" \
                --snapname "$SNAP" \
                --description 'TG53 ten-way detached batch'
        else
            tg53_dispatch_detached "tenway-delete-${id}-$$" delete \
                "/nodes/$NODE/qemu/$id/snapshot/$SNAP"
        fi
    done
    task_type=qmsnapshot
    [ "$op" = CREATE ] || task_type=qmdelsnapshot
    for id in "${VMIDS[@]}"; do
        tg53_wait_exact_task "$NODE" "$id" "$task_type" \
            "$started" "$((started + 900))"
        echo "${op}_VM_${id}=PASS"
    done
    finished=$(date +%s)
    echo "${op}_BATCH_SECONDS=$((finished-started))"
}

run_batch CREATE
for id in "${VMIDS[@]}"; do
    qm config "$id" --snapshot "$SNAP" >/dev/null
    qm listsnapshot "$id" | grep -q "$SNAP"
done
echo "CREATE_BATCH_VALIDATED=PASS"

run_batch DELETE
for id in "${VMIDS[@]}"; do
    if qm listsnapshot "$id" | grep -q "$SNAP"; then
        echo "snapshot $SNAP remains on VM $id after delete" >&2
        exit 2
    fi
    if qm config "$id" | grep -Eq '^(lock|snapstate):'; then
        echo "VM $id retains a lock or snapshot state after delete" >&2
        exit 2
    fi
done
echo "DELETE_BATCH_VALIDATED=PASS"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_TENWAY_SNAPSHOT_BATCH=PASS"
