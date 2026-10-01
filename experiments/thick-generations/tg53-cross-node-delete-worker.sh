#!/usr/bin/env bash
set -euo pipefail

VMID="${1:?usage: $0 VMID SNAP EVIDENCE}"
SNAP="${2:?usage: $0 VMID SNAP EVIDENCE}"
EVIDENCE="${3:?usage: $0 VMID SNAP EVIDENCE}"
EXPECTED_STATE="${4:-stopped}"

exec > >(tee -a "$EVIDENCE") 2>&1

echo "TEST=tg53-cross-node-delete-worker"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"
echo "VMID=$VMID"
echo "SNAP=$SNAP"
echo "EXPECTED_STATE=$EXPECTED_STATE"
dpkg-query -W pve-sharedlvmthin

test "$(qm status "$VMID" | awk '{print $2}')" = "$EXPECTED_STATE"
qm config "$VMID" --snapshot "$SNAP" >/dev/null
config_before="$(qm config "$VMID")"
mapfile -t volumes < <(
    sed -nE 's/^(ide|sata|scsi|virtio|efidisk|tpmstate)[0-9]+: ([^,]+).*/\2/p' \
        <<<"$config_before" | grep -vE '^(none|cdrom)$'
)
test "${#volumes[@]}" -gt 0
printf 'VOLUME_BEFORE=%s\n' "${volumes[@]}"

started=$(date +%s)
timeout --foreground --kill-after=10s 900s qm delsnapshot "$VMID" "$SNAP"
finished=$(date +%s)
echo "DELETE_SECONDS=$((finished-started))"

if qm listsnapshot "$VMID" | grep -Fq -- "$SNAP"; then
    echo "snapshot remains after delete" >&2
    exit 2
fi
config_after="$(qm config "$VMID")"
for volume in "${volumes[@]}"; do
    grep -Fq -- "$volume" <<<"$config_after"
done
if grep -Eq '^(lock|snapstate):' <<<"$config_after"; then
    echo "VM lock or snapshot state remains after delete" >&2
    exit 2
fi

echo "SNAPSHOT_DELETE=PASS"
echo "END_UTC=$(date -u +%FT%TZ)"
