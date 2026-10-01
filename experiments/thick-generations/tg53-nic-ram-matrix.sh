#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

VMID=${VMID:-107}
STATE_STORAGE=${STATE_STORAGE:-slt-lab-thick-a}

clear_nets() {
    for slot in net0 net1 net2; do
        if qm config "$VMID" | grep -q "^${slot}:"; then
            qm set "$VMID" --delete "$slot"
        fi
    done
}

run_case() {
    name=$1
    expected=$2

    qm start "$VMID"
    sleep 3
    /usr/sbin/sharedlvmthin snapshot-preflight "$VMID" --ram
    started=$(date +%s)
    tg53_dispatch_detached "nic-ram-${VMID}-${name}-$$" create \
        "/nodes/$(hostname)/qemu/$VMID/snapshot" \
        --snapname "$name" --vmstate 1
    tg53_wait_exact_task "$(hostname)" "$VMID" qmsnapshot \
        "$started" "$((started + 900))"

    raw_line=$(grep '^running-nets-host-mtu:' "/etc/pve/qemu-server/${VMID}.conf" | tail -1 || true)
    if qm config "$VMID" --snapshot "$name" >/tmp/tg53-nic-parse.out 2>&1; then
        observed=PASS
    else
        observed=FAIL
    fi

    printf 'CASE=%s\nEXPECTED_STRICT_PARSE=%s\nOBSERVED_STRICT_PARSE=%s\nRAW_MTU=%s\n' \
        "$name" "$expected" "$observed" "$raw_line"
    sed -n '1,5p' /tmp/tg53-nic-parse.out

    test "$observed" = "$expected"
    started=$(date +%s)
    tg53_dispatch_detached "nic-delete-${VMID}-${name}-$$" delete \
        "/nodes/$(hostname)/qemu/$VMID/snapshot/$name"
    tg53_wait_exact_task "$(hostname)" "$VMID" qmdelsnapshot \
        "$started" "$((started + 900))"
    if qm listsnapshot "$VMID" | grep -q "$name"; then
        echo "snapshot $name remains after delete" >&2
        exit 2
    fi
    qm stop "$VMID" --timeout 20
}

qm set "$VMID" --vmstatestorage "$STATE_STORAGE"
if qm status "$VMID" | grep -q running; then
    qm stop "$VMID" --timeout 20
fi

clear_nets
run_case nonic FAIL

clear_nets
qm set "$VMID" --net0 e1000,bridge=vmbr0
run_case e1000only FAIL

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0
run_case virtioone PASS

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0 --net1 e1000,bridge=vmbr0
run_case mixedvirtioe1000 PASS

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0 --net1 virtio,bridge=vmbr0,link_down=1
run_case twovirtiooneoffline PASS

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0,firewall=1,tag=123,rate=1
run_case virtiopolicy PASS

rm -f -- /tmp/tg53-nic-parse.out
printf 'TG53_NIC_RAM_MATRIX=PASS\n'
