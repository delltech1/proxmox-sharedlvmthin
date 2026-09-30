#!/bin/bash
set -euo pipefail

vmid=994340
slot=scsi0
thin=slt-tg-thin
eager=slt-lab-thick-a
lazy=slt-lab-lazy-a
expected=

activate() {
    perl -MPVE::Storage -e 'my $cfg=PVE::Storage::config(); PVE::Storage::activate_volumes($cfg, [$ARGV[0]]);' "$1" >&2
}

deactivate() {
    perl -MPVE::Storage -e 'my $cfg=PVE::Storage::config(); PVE::Storage::deactivate_volumes($cfg, [$ARGV[0]]);' "$1" >&2
}

volid() {
    qm config "$vmid" | sed -n "s/^${slot}: \([^,]*\).*/\1/p"
}

hash_marker() {
    local volume=$1 path digest
    activate "$volume"
    path=$(pvesm path "$volume")
    digest=$(dd if="$path" bs=1M count=1 status=none | sha256sum | awk '{print $1}')
    deactivate "$volume"
    printf '%s\n' "$digest"
}

assert_state() {
    local want_storage=$1 old_volume=$2 current digest
    current=$(volid)
    [[ "$current" == "$want_storage:"* ]] || {
        echo "CONFIG_BACKING_MISMATCH expected=$want_storage actual=$current" >&2
        exit 1
    }
    digest=$(hash_marker "$current")
    [[ "$digest" == "$expected" ]] || {
        echo "MARKER_MISMATCH expected=$expected actual=$digest volume=$current" >&2
        exit 1
    }
    if [[ -n "$old_volume" ]]; then
        old_storage=${old_volume%%:*}
        if pvesm list "$old_storage" --vmid "$vmid" | grep -Fq -- "$old_volume"; then
            echo "SOURCE_STILL_PRESENT source=$old_volume target=$current" >&2
            exit 1
        fi
    fi
    echo "STEP_PASS storage=$want_storage volume=$current sha256=$digest source_absent=${old_volume:-initial}"
}

if qm config "$vmid" >/dev/null 2>&1; then
    echo "VMID_ALREADY_EXISTS=$vmid" >&2
    exit 1
fi

qm create "$vmid" --name tg53-rc548-six-direction --memory 256 --scsihw virtio-scsi-single
qm set "$vmid" --scsi0 "$thin:1"
initial=$(volid)
activate "$initial"
initial_path=$(pvesm path "$initial")
qemu-io -f raw -c 'write -P 0x5a 0 1M' "$initial_path"
sync
expected=$(dd if="$initial_path" bs=1M count=1 status=none | sha256sum | awk '{print $1}')
deactivate "$initial"
assert_state "$thin" ""

for target in "$eager" "$lazy" "$thin" "$lazy" "$eager" "$thin"; do
    old=$(volid)
    echo "MOVE_BEGIN source=$old target=$target"
    qm move_disk "$vmid" "$slot" "$target" --delete 1
    assert_state "$target" "$old"
done

final=$(volid)
qm destroy "$vmid" --purge 1
final_storage=${final%%:*}
if qm config "$vmid" >/dev/null 2>&1 || \
   pvesm list "$final_storage" --vmid "$vmid" | grep -Fq -- "$final"; then
    echo "CLEANUP_FAILED vmid=$vmid final=$final" >&2
    exit 1
fi
echo "SIX_DIRECTION_CYCLE=PASS sha256=$expected"
