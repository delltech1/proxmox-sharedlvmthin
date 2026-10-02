#!/usr/bin/env bash
set -Eeuo pipefail

VMID="${1:?usage: $0 VMID DISK EXPECTED_SHA256}"
DISK="${2:?usage: $0 VMID DISK EXPECTED_SHA256}"
EXPECTED="${3:?usage: $0 VMID DISK EXPECTED_SHA256}"
[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DISK" =~ ^(ide|sata|scsi|virtio)[0-9]+$ ]]
[[ "$EXPECTED" =~ ^[0-9a-f]{64}$ ]]

config="$(qm config "$VMID")"
status="$(qm status "$VMID")"
[[ "$status" == "status: stopped" ]] || {
    echo "CANARY_VERIFY=REFUSED_VM_NOT_STOPPED" >&2
    exit 2
}
volume="$(sed -n "s/^$DISK: \([^,]*\).*/\1/p" <<<"$config")"
[[ -n "$volume" ]]
perl -I/usr/share/perl5 -MPVE::Storage -e '
    my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
    PVE::Storage::activate_volumes($cfg,[$volid]);
' "$volume"
path="$(pvesm path "$volume")"
actual="$(dd if="$path" bs=1M skip=8 count=1 iflag=fullblock status=none \
    | sha256sum | awk '{print $1}')"
perl -I/usr/share/perl5 -MPVE::Storage -e '
    my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
    PVE::Storage::deactivate_volumes($cfg,[$volid]);
' "$volume"

echo "VMID=$VMID"
echo "DISK=$DISK"
echo "VOLUME=$volume"
echo "EXPECTED_SHA256=$EXPECTED"
echo "ACTUAL_SHA256=$actual"
[[ "$actual" == "$EXPECTED" ]]
echo "CANARY_VERIFY=PASS"
