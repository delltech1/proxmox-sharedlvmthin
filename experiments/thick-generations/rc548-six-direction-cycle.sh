#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

vmid=994340
slot=scsi0
node=$(hostname)
thin=slt-tg-thin
eager=slt-lab-thick-a
lazy=slt-lab-lazy-a
expected=
readonly sample_bytes=1048576
readonly sample_offsets=(8388608 536870912 1048576000)

activate() {
    perl -MPVE::Storage -e 'my $cfg=PVE::Storage::config(); PVE::Storage::activate_volumes($cfg, [$ARGV[0]]);' "$1" >&2
}

deactivate() {
    perl -MPVE::Storage -e 'my $cfg=PVE::Storage::config(); PVE::Storage::deactivate_volumes($cfg, [$ARGV[0]]);' "$1" >&2
}

volid() {
    qm config "$vmid" | sed -n "s/^${slot}: \([^,]*\).*/\1/p"
}

hash_markers() {
    local volume=$1 path digest rc
    activate "$volume"
    path=$(pvesm path "$volume")
    set +e
    digest=$(
        for offset in "${sample_offsets[@]}"; do
            dd if="$path" iflag=skip_bytes,count_bytes skip="$offset" \
                count="$sample_bytes" status=none | sha256sum | awk '{print $1}'
        done | sha256sum | awk '{print $1}'
    )
    rc=$?
    set -e
    deactivate "$volume"
    (( rc == 0 )) || return "$rc"
    printf '%s\n' "$digest"
}

inventory_count() {
    local storage=$1 volume=$2 inventory
    inventory=$(pvesh get "/nodes/$node/storage/$storage/content" \
        --vmid "$vmid" --output-format json)
    python3 - "$volume" "$inventory" <<'PY'
import json, sys
volume, raw = sys.argv[1:]
rows = json.loads(raw)
if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
    raise SystemExit("invalid pvesm inventory")
print(sum(row.get("volid") == volume for row in rows))
PY
}

materialize_lazy_source() {
    local volume=$1 volname rc
    volname=${volume#*:}
    activate "$volume"
    set +e
    /usr/sbin/sharedlvmthin thick-lazy-materialize "$lazy" "$volname"
    rc=$?
    set -e
    deactivate "$volume"
    (( rc == 0 )) || return "$rc"
    /usr/sbin/sharedlvmthin recovery-check "$lazy" \
        | grep -q '^SAFE_FOR_MUTATION=YES$'
    echo "LAZY_SOURCE_MATERIALIZED=$volume"
}

assert_state() {
    local want_storage=$1 old_volume=$2 current digest
    current=$(volid)
    [[ "$current" == "$want_storage:"* ]] || {
        echo "CONFIG_BACKING_MISMATCH expected=$want_storage actual=$current" >&2
        exit 1
    }
    digest=$(hash_markers "$current")
    [[ "$digest" == "$expected" ]] || {
        echo "MARKER_MISMATCH expected=$expected actual=$digest volume=$current" >&2
        exit 1
    }
    if [[ -n "$old_volume" ]]; then
        old_storage=${old_volume%%:*}
        old_count=$(inventory_count "$old_storage" "$old_volume")
        [[ "$old_count" == 0 ]] || {
            echo "SOURCE_STILL_PRESENT source=$old_volume count=$old_count target=$current" >&2
            exit 1
        }
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
qemu-io -f raw -c 'write -P 0x5a 8M 1M' "$initial_path"
qemu-io -f raw -c 'write -P 0xa5 512M 1M' "$initial_path"
qemu-io -f raw -c 'write -P 0x3c 1000M 1M' "$initial_path"
sync
deactivate "$initial"
expected=$(hash_markers "$initial")
assert_state "$thin" ""

move_index=0
for target in "$eager" "$lazy" "$thin" "$lazy" "$eager" "$thin"; do
    move_index=$((move_index + 1))
    old=$(volid)
    echo "MOVE_BEGIN source=$old target=$target"
    if [[ "${old%%:*}" == "$lazy" ]]; then
        materialize_lazy_source "$old"
        assert_state "$lazy" ""
    fi
    /usr/sbin/sharedlvmthin storage-move-preflight "$vmid" "$slot" "$target"
    move_cfg=$(pvesh get "/nodes/$node/qemu/$vmid/config" --output-format json)
    move_digest=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["digest"])' "$move_cfg")
    started=$(date +%s)
    tg53_dispatch_detached "six-${vmid}-${move_index}-$(date +%s)-$$" create \
        "/nodes/$(hostname)/qemu/$vmid/move_disk" \
        --disk "$slot" --storage "$target" --delete 1 --digest "$move_digest"
    tg53_wait_exact_task "$node" "$vmid" qmmove \
        "$started" "$((started + 900))"
    assert_state "$target" "$old"
done

final=$(volid)
qm destroy "$vmid" --purge 1
final_storage=${final%%:*}
final_count=$(inventory_count "$final_storage" "$final")
if qm config "$vmid" >/dev/null 2>&1 || [[ "$final_count" != 0 ]]; then
    echo "CLEANUP_FAILED vmid=$vmid final=$final" >&2
    exit 1
fi
echo "SIX_DIRECTION_CYCLE=PASS sha256=$expected"
