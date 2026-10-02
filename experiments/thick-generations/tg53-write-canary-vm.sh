#!/usr/bin/env bash
set -Eeuo pipefail

# This deliberately overwrites bytes at 8 MiB. It is valid only for a blank,
# disposable data disk created for this test, never a boot or valuable disk.
[[ "${CONFIRM_DISPOSABLE_DATA_DISK:-}" == YES ]] || {
    echo "REFUSE=CONFIRM_DISPOSABLE_DATA_DISK_REQUIRED" >&2
    exit 64
}

VMID="${1:?usage: $0 VMID DISK MARKER}"
DISK="${2:?usage: $0 VMID DISK MARKER}"
MARKER="${3:?usage: $0 VMID DISK MARKER}"
[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DISK" =~ ^(ide|sata|scsi|virtio)[0-9]+$ ]]
(( ${#MARKER} <= 256 ))
[[ "$(qm status "$VMID" | awk '{print $2}')" == stopped ]]

config="$(qm config "$VMID")"
volume="$(sed -n "s/^$DISK: \([^,]*\).*/\1/p" <<<"$config")"
[[ -n "$volume" ]]
perl -I/usr/share/perl5 -MPVE::Storage -e '
    my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
    PVE::Storage::activate_volumes($cfg,[$volid]);
' "$volume"
path="$(pvesm path "$volume")"
printf '%s' "$MARKER" | dd of="$path" bs=1 seek=$((8 * 1024 * 1024)) \
    conv=notrunc,fsync status=none
sha="$(dd if="$path" bs=1M skip=8 count=1 iflag=fullblock status=none \
    | sha256sum | awk '{print $1}')"
perl -I/usr/share/perl5 -MPVE::Storage -e '
    my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
    PVE::Storage::deactivate_volumes($cfg,[$volid]);
' "$volume"

echo "VMID=$VMID"
echo "DISK=$DISK"
echo "VOLUME=$volume"
echo "CANARY_SHA256=$sha"
echo "CANARY_WRITE=PASS"
