#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

VMID="${VMID:-111}"
SNAP="${SNAP:-mixed3a}"
EVIDENCE="${EVIDENCE:-/tmp/tg53-mixed-three-disk-${VMID}.evidence}"

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-mixed-three-disk-cycle"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "VMID=$VMID"

cfg="$(qm config "$VMID")"
printf '%s\n' "$cfg"
for key in scsi0 scsi1 scsi2; do
    grep -q "^${key}:" <<<"$cfg" || { echo "MISSING_DISK=$key"; exit 1; }
done

volid() {
    sed -n "s/^$1: \([^,]*\).*/\1/p" <<<"$cfg"
}

if qm status "$VMID" | grep -q running; then
    qm stop "$VMID" --timeout 60
fi

declare -A VOL
for key in scsi0 scsi1 scsi2; do
    VOL[$key]="$(volid "$key")"
    echo "VOLUME_${key}=${VOL[$key]}"
done

qm start "$VMID"
/usr/sbin/sharedlvmthin snapshot-preflight "$VMID" --ram
started="$(date +%s)"
request_id="snap-${VMID}-$(date +%s)-$$"
tg53_dispatch_detached "$request_id" create \
    "/nodes/$(hostname)/qemu/$VMID/snapshot" \
    --snapname "$SNAP" --vmstate 1 \
    --description 'TG53 mixed Thin Eager Lazy RAM snapshot'
tg53_wait_exact_task "$(hostname)" "$VMID" qmsnapshot "$started" "$((started + 900))"
qm config "$VMID" --snapshot "$SNAP" >/tmp/tg53-mixed-snapshot-config.txt
grep -q '^vmstate:' /tmp/tg53-mixed-snapshot-config.txt
for key in scsi0 scsi1 scsi2; do
    grep -q "^${key}: ${VOL[$key]}" /tmp/tg53-mixed-snapshot-config.txt
done
echo "SNAPSHOT_STRICT_PARSE=PASS"

qm stop "$VMID" --timeout 60
started="$(date +%s)"
tg53_dispatch_detached "rollback-${VMID}-$(date +%s)-$$" create \
    "/nodes/$(hostname)/qemu/$VMID/snapshot/$SNAP/rollback" --start 0
tg53_wait_exact_task "$(hostname)" "$VMID" qmrollback \
    "$started" "$((started + 900))"
cfg="$(qm config "$VMID")"
for key in scsi0 scsi1 scsi2; do
    grep -q "^${key}: ${VOL[$key]}" <<<"$cfg"
done
echo "ROLLBACK_ALL_DISK_IDENTITIES=PASS"

started="$(date +%s)"
tg53_dispatch_detached "delete-${VMID}-$(date +%s)-$$" delete \
    "/nodes/$(hostname)/qemu/$VMID/snapshot/$SNAP"
tg53_wait_exact_task "$(hostname)" "$VMID" qmdelsnapshot \
    "$started" "$((started + 900))"
if qm listsnapshot "$VMID" | grep -q "$SNAP"; then
    echo "snapshot remains after delete" >&2
    exit 2
fi
if qm config "$VMID" | grep -Eq '^(lock|snapstate):'; then
    echo "VM lock or snapshot state remains after delete" >&2
    exit 2
fi
echo "DELETE_CLEAN=PASS"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_MIXED_THREE_DISK=PASS"
