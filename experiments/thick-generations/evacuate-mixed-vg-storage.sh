#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=experiments/thick-generations/tg53-async-dispatch.sh
source "$SCRIPT_DIR/tg53-async-dispatch.sh"

usage() {
    echo "usage: $0 [--execute] [--skip-snapshots] [--only=all|thin|thick] [--thin-destination=sharedthin-test|sharedthin-fcoe]" >&2
}

execute=0
thin_destination=sharedthin-test
skip_snapshots=0
only=all
for argument in "$@"; do
    case "$argument" in
        --execute) execute=1 ;;
        --skip-snapshots) skip_snapshots=1 ;;
        --only=all|--only=thin|--only=thick) only=${argument#*=} ;;
        --thin-destination=sharedthin-test|--thin-destination=sharedthin-fcoe)
            thin_destination=${argument#*=}
            ;;
        *) usage; exit 64 ;;
    esac
done

command -v qm >/dev/null
command -v pvesm >/dev/null
command -v flock >/dev/null

exec 9>/run/lock/slt-mixed-vg-evacuation.lock
flock -n 9 || { echo "REFUSED: another evacuation worker owns the lock" >&2; exit 75; }

declare -A target=(
    [slt-scale-thin]="$thin_destination"
    [slt-tg-thin]="$thin_destination"
    [slt-scale-thick]=slt-lab-thick-a
    [slt-tg-thick]=slt-lab-thick-a
)

for destination in "$thin_destination" slt-lab-thick-a; do
    status=$(pvesm status --storage "$destination" --content images 2>/dev/null |
        awk -v id="$destination" '$1 == id { print $3 }')
    [[ "$status" == active ]] || {
        echo "REFUSED: destination $destination is not active" >&2
        exit 69
    }
done

mapfile -t entries < <(
    grep -RHE '^(scsi|sata|virtio|ide)[0-9]+: (slt-scale-thin|slt-scale-thick|slt-tg-thin|slt-tg-thick):' \
        /etc/pve/qemu-server 2>/dev/null | sort
)

declare -A skipped_snapshot_vm=()

for entry in "${entries[@]}"; do
    config=${entry%%:*}
    rest=${entry#*:}
    vmid=${config##*/}; vmid=${vmid%.conf}
    slot=${rest%%:*}
    value=${rest#*: }
    source=${value%%:*}
    destination=${target[$source]:-}

    case "$source:$only" in
        slt-scale-thin:thick|slt-tg-thin:thick|slt-scale-thick:thin|slt-tg-thick:thin)
            continue
            ;;
    esac

    if grep -q '^\[' "$config"; then
        if (( skip_snapshots == 0 )); then
            echo "REFUSED: VM $vmid has snapshots; no snapshot-destructive migration is allowed" >&2
            exit 70
        fi
        if [[ -z "${skipped_snapshot_vm[$vmid]:-}" ]]; then
            echo "SNAPSHOT_BLOCKED vmid=$vmid action=SKIPPED"
            skipped_snapshot_vm[$vmid]=1
        fi
        continue
    fi

    [[ -n "$destination" ]] || {
        echo "REFUSED: no destination policy for $source" >&2
        exit 65
    }
    [[ "$(qm status "$vmid" | awk '{print $2}')" == stopped ]] || {
        echo "REFUSED: VM $vmid is not stopped" >&2
        exit 70
    }
    grep -Fqx "$slot: $value" "$config" || {
        echo "REFUSED: config changed while planning VM $vmid $slot" >&2
        exit 73
    }

    echo "PLAN vmid=$vmid slot=$slot source=$source destination=$destination"
    (( execute == 1 )) || continue

    available_kib=$(pvesm status --storage "$destination" --content images 2>/dev/null |
        awk -v id="$destination" '$1 == id { print $6 }')
    [[ "$available_kib" =~ ^[0-9]+$ ]] || {
        echo "REFUSED: cannot prove free capacity for $destination" >&2
        exit 69
    }
    (( available_kib >= 20 * 1024 * 1024 )) || {
        echo "REFUSED: $destination has less than the 20 GiB evacuation reserve" >&2
        exit 70
    }

    /usr/sbin/sharedlvmthin storage-move-preflight \
        "$vmid" "$slot" "$destination"
    move_cfg=$(pvesh get "/nodes/$(hostname)/qemu/$vmid/config" --output-format json)
    move_digest=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["digest"])' "$move_cfg")
    started=$(date +%s)
    tg53_dispatch_detached "evacuate-${vmid}-${slot}-$(date +%s)-$$" create \
        "/nodes/$(hostname)/qemu/$vmid/move_disk" \
        --disk "$slot" --storage "$destination" --delete 1 \
        --digest "$move_digest"
    tg53_wait_exact_task "$(hostname)" "$vmid" qmmove \
        "$started" "$((started + 1800))"
    new_value=$(awk -F': ' -v slot="$slot" '$1 == slot { print $2 }' "$config")
    [[ "$new_value" == "$destination:"* ]] || {
        echo "FAILED_POSTCONDITION: VM $vmid $slot is not on $destination" >&2
        exit 74
    }
    echo "EVAC_PASS vmid=$vmid slot=$slot destination=$destination"
done

remaining_pattern='slt-scale-thin|slt-scale-thick|slt-tg-thin|slt-tg-thick'
[[ "$only" == thin ]] && remaining_pattern='slt-scale-thin|slt-tg-thin'
[[ "$only" == thick ]] && remaining_pattern='slt-scale-thick|slt-tg-thick'
remaining=$(
    { grep -RHEc "^(scsi|sata|virtio|ide)[0-9]+: ($remaining_pattern):" \
        /etc/pve/qemu-server 2>/dev/null || true; } |
        awk -F: '{ total += $NF } END { print total + 0 }'
)

if (( execute == 1 )) && (( remaining != 0 )); then
    echo "FAILED_POSTCONDITION: $remaining legacy references remain" >&2
    exit 74
fi

echo "RESULT=$([[ $execute == 1 ]] && echo PASS || echo PLAN_ONLY) remaining=$remaining"
