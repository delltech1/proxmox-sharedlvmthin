#!/usr/bin/env bash
set -euo pipefail

VMID="${1:?usage: $0 VMID STORAGE NAME EVIDENCE}"
STORAGE="${2:?usage: $0 VMID STORAGE NAME EVIDENCE}"
NAME="${3:?usage: $0 VMID STORAGE NAME EVIDENCE}"
EVIDENCE="${4:?usage: $0 VMID STORAGE NAME EVIDENCE}"
EXPECTED_PHASE="${5:-MATERIALIZED}"

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-provision-test-vm"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"
echo "VMID=$VMID"
echo "STORAGE=$STORAGE"
echo "EXPECTED_PHASE=$EXPECTED_PHASE"
dpkg-query -W pve-sharedlvmthin

if qm status "$VMID" >/dev/null 2>&1; then
    echo "REFUSE=VMID_ALREADY_EXISTS"
    exit 1
fi

qm create "$VMID" --name "$NAME" --memory 64 --cores 1 \
    --ostype l26 --scsihw virtio-scsi-single --onboot 0
qm set "$VMID" --scsi0 "${STORAGE}:1,iothread=1"

config="$(qm config "$VMID")"
printf '%s\n' "$config"
volume="$(sed -n 's/^scsi0: \([^,]*\).*/\1/p' <<<"$config")"
test -n "$volume"
path="$(pvesm path "$volume")"
mapper="${path##*/}"
hash="${mapper#sltg-}"
test "$hash" != "$mapper"
anchor="sltg-a-${hash}"
vg="$(awk -v sid="$STORAGE" '
    $1 == "sharedlvmthin:" && $2 == sid { found = 1; next }
    found && $1 == "slt-vgname" { print $2; exit }
    found && NF == 0 { exit }
' /etc/pve/storage.cfg)"
test -n "$vg"
echo "VOLUME=$volume"
echo "PATH=$path"
echo "ANCHOR=$anchor"
echo "VG=$vg"

deadline=$((SECONDS + 600))
while :; do
    tags="$(lvs --noheadings -o lv_tags "${vg}/${anchor}" 2>/dev/null || true)"
    if grep -Fq "slt_tg_phase=${EXPECTED_PHASE}" <<<"$tags"; then
        echo "EXPECTED_PHASE_MATCH=PASS"
        break
    fi
    if (( SECONDS >= deadline )); then
        echo "EXPECTED_PHASE_MATCH=TIMEOUT"
        exit 1
    fi
    sleep 2
done

test "$(qm status "$VMID" | awk '{print $2}')" = stopped
echo "END_UTC=$(date -u +%FT%TZ)"
echo "PROVISION=PASS"
