#!/usr/bin/env bash
set -euo pipefail

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
qm snapshot "$VMID" "$SNAP" --vmstate 1 --description 'TG53 mixed Thin Eager Lazy RAM snapshot'
qm config "$VMID" --snapshot "$SNAP" >/tmp/tg53-mixed-snapshot-config.txt
grep -q '^vmstate:' /tmp/tg53-mixed-snapshot-config.txt
for key in scsi0 scsi1 scsi2; do
    grep -q "^${key}: ${VOL[$key]}" /tmp/tg53-mixed-snapshot-config.txt
done
echo "SNAPSHOT_STRICT_PARSE=PASS"

qm stop "$VMID" --timeout 60
qm rollback "$VMID" "$SNAP"
cfg="$(qm config "$VMID")"
for key in scsi0 scsi1 scsi2; do
    grep -q "^${key}: ${VOL[$key]}" <<<"$cfg"
done
echo "ROLLBACK_ALL_DISK_IDENTITIES=PASS"

qm delsnapshot "$VMID" "$SNAP"
! qm listsnapshot "$VMID" | grep -q "$SNAP"
! qm config "$VMID" | grep -Eq '^(lock|snapstate):'
echo "DELETE_CLEAN=PASS"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_MIXED_THREE_DISK=PASS"
