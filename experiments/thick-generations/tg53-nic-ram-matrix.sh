#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

VMID="${1:?usage: CONFIRM_DISPOSABLE_VM=YES $0 VMID}"
STATE_STORAGE=${STATE_STORAGE:-slt-lab-thick-a}
[[ "${CONFIRM_DISPOSABLE_VM:-NO}" == YES ]] || {
    echo "refusing to modify VM $VMID without CONFIRM_DISPOSABLE_VM=YES" >&2
    exit 64
}
[[ "$VMID" =~ ^[1-9][0-9]{2,8}$ ]]

clear_nets() {
    for slot in net0 net1 net2; do
        if qm config "$VMID" | grep -q "^${slot}:"; then
            qm set "$VMID" --delete "$slot"
        fi
    done
}

start_case() {
    qm start "$VMID"
    deadline=$((SECONDS + 60))
    while (( SECONDS < deadline )); do
        [[ "$(qm status "$VMID")" == 'status: running' ]] && return 0
        sleep 1
    done
    echo "VM $VMID did not reach running state" >&2
    return 1
}

run_refusal_case() {
    name=$1
    start_case
    set +e
    output=$(/usr/sbin/sharedlvmthin snapshot-preflight "$VMID" --ram 2>&1)
    status=$?
    set -e
    printf 'CASE=%s\n%s\n' "$name" "$output"
    [[ "$status" -ne 0 && "$output" == *'SNAPSHOT_READY=NO'* \
        && "$output" == *'RESULT=BLOCKED'* ]]
    qm stop "$VMID" --timeout 20
    echo "CASE_${name}=PREFLIGHT_REFUSAL_PASS"
}

run_positive_case() {
    name=$1

    start_case
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

    printf 'CASE=%s\nEXPECTED_STRICT_PARSE=PASS\nOBSERVED_STRICT_PARSE=%s\nRAW_MTU=%s\n' \
        "$name" "$observed" "$raw_line"
    sed -n '1,5p' /tmp/tg53-nic-parse.out

    test "$observed" = PASS
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
run_refusal_case nonic

clear_nets
qm set "$VMID" --net0 e1000,bridge=vmbr0
run_refusal_case e1000only

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0
run_positive_case virtioone

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0 --net1 e1000,bridge=vmbr0
run_positive_case mixedvirtioe1000

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0 --net1 virtio,bridge=vmbr0,link_down=1
run_positive_case twovirtiooneoffline

clear_nets
qm set "$VMID" --net0 virtio,bridge=vmbr0,firewall=1,tag=123,rate=1
run_positive_case virtiopolicy

rm -f -- /tmp/tg53-nic-parse.out
printf 'TG53_NIC_RAM_MATRIX=PASS\n'
