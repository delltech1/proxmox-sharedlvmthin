#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Bounded qualification harness for disposable SharedLvmThin VMs.
# It never deletes a VM or storage object and never performs Thin live migration.

set -euo pipefail

phase=${1:-}
manifest=${2:-}
source_node=${3:-}
target_node=${4:-}
parallel=${5:-8}
storage=${6:-slt-scale-thin}
vg=${7:-pve-slt-scale-20260913}

[[ $phase =~ ^(stop|migrate|start|audit)$ ]] || {
    echo "Usage: $0 <stop|migrate|start|audit> <manifest> <source-node> <target-node> [parallel] [storage] [vg]" >&2
    exit 64
}
[[ -f $manifest ]] || { echo "manifest missing" >&2; exit 64; }
[[ $source_node =~ ^[A-Za-z0-9_.-]+$ && $target_node =~ ^[A-Za-z0-9_.-]+$ ]] || exit 64
[[ $parallel =~ ^[1-9][0-9]*$ && $parallel -le 64 ]] || exit 64
[[ $storage =~ ^[A-Za-z0-9_.-]+$ && $vg =~ ^[A-Za-z0-9_.+-]+$ ]] || exit 64

mapfile -t ids < <(sed '/^[[:space:]]*$/d' "$manifest")
(( ${#ids[@]} > 0 )) || { echo "empty manifest" >&2; exit 64; }
declare -A seen
for id in "${ids[@]}"; do
    [[ $id =~ ^[1-9][0-9]+$ ]] || { echo "invalid VMID: $id" >&2; exit 64; }
    [[ -z ${seen[$id]:-} ]] || { echo "duplicate VMID: $id" >&2; exit 64; }
    seen[$id]=1
done

remote() {
    local node=$1
    shift
    if [[ $(hostname) == "$node" ]]; then
        "$@"
    else
        local node_ip
        node_ip=$(NODE="$node" perl -MJSON::PP -0777 -e '
            my $doc = decode_json(<STDIN>);
            my $ip = $doc->{nodelist}->{$ENV{NODE}}->{ip};
            die "node address missing\n" if !defined($ip);
            print $ip;
        ' </etc/pve/.members)
        /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 \
            -o "HostKeyAlias=$node" \
            -o "UserKnownHostsFile=/etc/pve/nodes/$node/ssh_known_hosts" \
            -o GlobalKnownHostsFile=none "root@$node_ip" -- "$@"
    fi
}

current_node=$(hostname)
if [[ $phase == start ]]; then
    [[ ${current_node,,} == ${source_node,,} || ${current_node,,} == ${target_node,,} ]] || {
        echo "run start on source $source_node or target $target_node (current: $current_node)" >&2
        exit 64
    }
else
    [[ ${current_node,,} == ${source_node,,} ]] || {
        echo "run this harness on source node $source_node (current: $current_node)" >&2
        exit 64
    }
fi

run_parallel() {
    local action=$1
    local node=$2
    local failures=0
    local -a pids=()
    local configured_lock_timeout
    configured_lock_timeout=$(
        pvesh get "/storage/$storage" --output-format json |
            perl -MJSON::PP -0777 -e '
                my $doc = decode_json(<STDIN>);
                print($doc->{"slt-lock-timeout"} // 30);
            '
    )
    [[ $configured_lock_timeout =~ ^[0-9]+$ ]] || exit 1
    local storage_operation_timeout=$((configured_lock_timeout + 120))
    for id in "${ids[@]}"; do
        (
            case "$action" in
                stop) timeout --foreground --kill-after=10 "$storage_operation_timeout" qm stop "$id" --timeout 30 ;;
                migrate) timeout --foreground --kill-after=10 300 qm migrate "$id" "$target_node" ;;
                start) remote "$node" timeout --foreground --kill-after=10 "$storage_operation_timeout" qm start "$id" ;;
            esac
        ) >"/tmp/slt-${action}-${id}.log" 2>&1 &
        pids+=("$!")
        if (( ${#pids[@]} >= parallel )); then
            for pid in "${pids[@]}"; do wait "$pid" || failures=$((failures + 1)); done
            pids=()
        fi
    done
    for pid in "${pids[@]}"; do wait "$pid" || failures=$((failures + 1)); done
    (( failures == 0 )) || { echo "$action failures=$failures" >&2; exit 1; }
}

case "$phase" in
    stop)
        run_parallel stop "$source_node"
        for id in "${ids[@]}"; do
            [[ $(qm status "$id" | awk '{print $2}') == stopped ]] || exit 1
        done
        ;;
    migrate)
        for id in "${ids[@]}"; do
            [[ $(qm status "$id" | awk '{print $2}') == stopped ]] || {
                echo "VM $id is not stopped" >&2; exit 1;
            }
        done
        run_parallel migrate "$source_node"
        for id in "${ids[@]}"; do
            [[ ! -e /etc/pve/nodes/$source_node/qemu-server/$id.conf ]] || {
                echo "source config remains for $id" >&2; exit 1;
            }
            [[ -e /etc/pve/nodes/$target_node/qemu-server/$id.conf ]] || {
                echo "target config missing for $id" >&2; exit 1;
            }
        done
        ;;
    start)
        run_parallel start "$target_node"
        for id in "${ids[@]}"; do
            [[ $(remote "$target_node" qm status "$id" | awk '{print $2}') == running ]] || exit 1
        done
        ;;
    audit)
        local_maps=$(dmsetup ls --target thin-pool 2>/dev/null || true)
        target_maps=$(remote "$target_node" dmsetup ls --target thin-pool 2>/dev/null || true)
        for id in "${ids[@]}"; do
            encoded=${vg//-/--}-sltp--${id}-tpool
            ! grep -q "^${encoded}[[:space:]]" <<<"$local_maps" || {
                echo "source mapper remains for $id" >&2; exit 1;
            }
            grep -q "^${encoded}[[:space:]]" <<<"$target_maps" || {
                echo "target mapper missing for $id" >&2; exit 1;
            }
            tags=$(remote "$target_node" lvs --noheadings -o lv_tags "$vg/sltp-$id" | tr -d ' ')
            grep -q "pve-slt-sid-${storage}" <<<"$tags" || exit 1
            grep -q "pve-slt-owner-node-${target_node}" <<<"$tags" || exit 1
            grep -Eq 'pve-slt-owner-epoch-[0-9a-f]{32}' <<<"$tags" || exit 1
        done
        ;;
esac

echo "BULK_OFFLINE_${phase^^}=PASS count=${#ids[@]} parallel=$parallel source=$source_node target=$target_node"
