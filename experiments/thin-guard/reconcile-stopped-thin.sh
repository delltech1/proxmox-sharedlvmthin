#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Retry only node-local deactivation for explicitly listed, already-stopped VMs.

set -euo pipefail

manifest=${1:-}
storage=${2:-slt-scale-thin}
[[ -f $manifest ]] || { echo "manifest missing" >&2; exit 64; }
[[ $storage =~ ^[A-Za-z0-9_.-]+$ ]] || exit 64

while read -r id; do
    [[ $id =~ ^[1-9][0-9]+$ ]] || { echo "invalid VMID: $id" >&2; exit 64; }
    [[ $(qm status "$id" | awk '{print $2}') == stopped ]] || {
        echo "VM $id is not stopped" >&2
        exit 1
    }
    mapfile -t volumes < <(
        qm config "$id" |
            sed -nE "s/^(ide|sata|scsi|virtio)[0-9]+: (${storage}:[^,[:space:]]+).*/\2/p"
    )
    (( ${#volumes[@]} > 0 )) || { echo "no $storage volumes for $id" >&2; exit 1; }
    perl -MPVE::Storage -e '
        my $cfg = PVE::Storage::config();
        PVE::Storage::deactivate_volumes($cfg, \@ARGV);
    ' "${volumes[@]}"
    echo "RECONCILED=$id volumes=${#volumes[@]}"
done < "$manifest"
