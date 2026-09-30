#!/usr/bin/env bash
set -Eeuo pipefail

VMID="${1:?usage: $0 VMID STORAGE NAME MARKER EVIDENCE}"
STORAGE="${2:?usage: $0 VMID STORAGE NAME MARKER EVIDENCE}"
NAME="${3:?usage: $0 VMID STORAGE NAME MARKER EVIDENCE}"
MARKER="${4:?usage: $0 VMID STORAGE NAME MARKER EVIDENCE}"
EVIDENCE="${5:?usage: $0 VMID STORAGE NAME MARKER EVIDENCE}"

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-provision-canary-vm"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)"
echo "VMID=$VMID"
echo "STORAGE=$STORAGE"

[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$STORAGE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
(( ${#MARKER} <= 256 ))
qm status "$VMID" >/dev/null 2>&1 && {
    echo "REFUSE=VMID_ALREADY_EXISTS"
    exit 1
}

qm create "$VMID" --name "$NAME" --memory 128 --cores 1 \
    --ostype l26 --scsihw virtio-scsi-single --onboot 0 \
    --scsi0 "${STORAGE}:1,iothread=1"
config="$(qm config "$VMID")"
printf '%s\n' "$config"
volume="$(sed -n 's/^scsi0: \([^,]*\).*/\1/p' <<<"$config")"
[[ "$volume" == "$STORAGE:vm-$VMID-disk-"* ]]

activate() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::activate_volumes($cfg,[$volid]);
    ' "$volume"
}
deactivate() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::deactivate_volumes($cfg,[$volid]);
    ' "$volume"
}

activate
path="$(pvesm path "$volume")"
[[ -b "$path" ]]
printf '%s' "$MARKER" | dd of="$path" bs=1 seek=$((8 * 1024 * 1024)) \
    conv=notrunc,fsync status=none
sha="$(dd if="$path" bs=1M skip=8 count=1 iflag=fullblock status=none \
    | sha256sum | awk '{print $1}')"
deactivate

echo "VOLUME=$volume"
echo "CANARY_OFFSET=8388608"
echo "CANARY_SHA256=$sha"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "PROVISION_CANARY=PASS"
