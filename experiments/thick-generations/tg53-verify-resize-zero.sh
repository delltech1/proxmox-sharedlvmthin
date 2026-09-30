#!/usr/bin/env bash
set -Eeuo pipefail

VMID="${1:?usage: $0 VMID DISK EXPECTED_CANARY_SHA256 EXPECTED_BYTES EVIDENCE}"
DISK="${2:?usage: $0 VMID DISK EXPECTED_CANARY_SHA256 EXPECTED_BYTES EVIDENCE}"
EXPECTED_CANARY="${3:?usage: $0 VMID DISK EXPECTED_CANARY_SHA256 EXPECTED_BYTES EVIDENCE}"
EXPECTED_BYTES="${4:?usage: $0 VMID DISK EXPECTED_CANARY_SHA256 EXPECTED_BYTES EVIDENCE}"
EVIDENCE="${5:?usage: $0 VMID DISK EXPECTED_CANARY_SHA256 EXPECTED_BYTES EVIDENCE}"

[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]
[[ "$DISK" =~ ^(ide|sata|scsi|virtio)[0-9]+$ ]]
[[ "$EXPECTED_CANARY" =~ ^[0-9a-f]{64}$ ]]
[[ "$EXPECTED_BYTES" =~ ^[1-9][0-9]+$ ]]

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-verify-resize-zero"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "VMID=$VMID"
echo "DISK=$DISK"

[[ "$(qm status "$VMID")" == "status: stopped" ]] || {
    echo "RESIZE_VERIFY=REFUSED_VM_NOT_STOPPED"
    exit 2
}
config="$(qm config "$VMID")"
volume="$(sed -n "s/^$DISK: \([^,]*\).*/\1/p" <<<"$config")"
[[ -n "$volume" ]]

deactivate() {
    perl -I/usr/share/perl5 -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::deactivate_volumes($cfg,[$volid]);
    ' "$volume"
}
trap deactivate EXIT HUP INT TERM
perl -I/usr/share/perl5 -MPVE::Storage -e '
    my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
    PVE::Storage::activate_volumes($cfg,[$volid]);
' "$volume"
path="$(pvesm path "$volume")"
[[ -b "$path" ]]
actual_bytes="$(blockdev --getsize64 "$path")"
[[ "$actual_bytes" == "$EXPECTED_BYTES" ]]

canary="$(dd if="$path" bs=1M skip=8 count=1 iflag=fullblock status=none \
    | sha256sum | awk '{print $1}')"
[[ "$canary" == "$EXPECTED_CANARY" ]]

old_bytes=$((EXPECTED_BYTES - 64 * 1024 * 1024))
[[ $((old_bytes % (1024 * 1024))) -eq 0 ]]
new_sha="$(dd if="$path" bs=1M skip=$((old_bytes / 1024 / 1024)) count=64 \
    iflag=fullblock status=none | sha256sum | awk '{print $1}')"
zero_sha="$(dd if=/dev/zero bs=1M count=64 status=none \
    | sha256sum | awk '{print $1}')"
[[ "$new_sha" == "$zero_sha" ]]

deactivate
trap - EXIT HUP INT TERM
echo "VOLUME=$volume"
echo "PATH=$path"
echo "ACTUAL_BYTES=$actual_bytes"
echo "CANARY_SHA256=$canary"
echo "NEW_RANGE_SHA256=$new_sha"
echo "ZERO_RANGE_SHA256=$zero_sha"
echo "END_UTC=$(date -u +%FT%TZ)"
echo "RESIZE_VERIFY=PASS"
