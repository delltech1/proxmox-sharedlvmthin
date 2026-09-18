#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Read-only post-fence audit for an exact batch of HA-managed Thin VMs.

set -euo pipefail

manifest=${1:-}
vg=${2:-pve-slt-scale-20260913}
[[ -f $manifest ]] || { echo "manifest missing" >&2; exit 64; }
[[ $vg =~ ^[A-Za-z0-9_.+-]+$ ]] || exit 64

mapfile -t ids < <(sed '/^[[:space:]]*$/d' "$manifest")
(( ${#ids[@]} >= 1 && ${#ids[@]} <= 500 )) || exit 64

# pvecm's human table differs between releases; use pmxcfs membership as the
# authoritative address map and require a live SSH probe for every peer.
mapfile -t configured_nodes < <(perl -MJSON::PP -0777 -e '
    my $x = decode_json(<STDIN>);
    print "$_\n" for sort keys %{$x->{nodelist}};
' </etc/pve/.members)

remote() {
    local node=$1
    shift
    if [[ ${HOSTNAME,,} == ${node,,} ]]; then
        "$@"
        return
    fi
    local ip
    ip=$(NODE="$node" perl -MJSON::PP -0777 -e '
        my $x = decode_json(<STDIN>);
        print $x->{nodelist}->{$ENV{NODE}}->{ip};
    ' </etc/pve/.members)
    /usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=10 \
        -o "HostKeyAlias=$node" \
        -o "UserKnownHostsFile=/etc/pve/nodes/$node/ssh_known_hosts" \
        -o GlobalKnownHostsFile=none "root@$ip" -- "$@"
}

declare -A reachable
for node in "${configured_nodes[@]}"; do
    if remote "$node" true 2>/dev/null; then
        reachable[$node]=1
    fi
done
(( ${#reachable[@]} >= 2 )) || { echo "fewer than two reachable cluster nodes" >&2; exit 1; }

failures=0
for id in "${ids[@]}"; do
    [[ $id =~ ^[1-9][0-9]+$ ]] || exit 64
    mapfile -t configs < <(compgen -G "/etc/pve/nodes/*/qemu-server/$id.conf" || true)
    if (( ${#configs[@]} != 1 )); then
        echo "CONFIG_AMBIGUOUS vm:$id count=${#configs[@]}" >&2
        failures=$((failures + 1)); continue
    fi
    node=${configs[0]#/etc/pve/nodes/}; node=${node%%/*}
    if [[ -z ${reachable[$node]:-} ]]; then
        echo "PLACEMENT_OFFLINE vm:$id node=$node" >&2
        failures=$((failures + 1)); continue
    fi
    if [[ $(remote "$node" qm status "$id" | awk '{print $2}') != running ]]; then
        echo "NOT_RUNNING vm:$id node=$node" >&2
        failures=$((failures + 1)); continue
    fi

    tags=$(lvs --noheadings -o lv_tags "$vg/sltp-$id" | tr -d ' ')
    if ! grep -q "pve-slt-owner-node-$node" <<<"$tags" \
        || ! grep -Eq 'pve-slt-owner-epoch-[0-9a-f]{32}' <<<"$tags"; then
        echo "OWNER_MISMATCH vm:$id node=$node tags=$tags" >&2
        failures=$((failures + 1)); continue
    fi

    mapper=${vg//-/--}-sltp--${id}-tpool
    mapper_count=0
    mapper_node=
    for peer in "${!reachable[@]}"; do
        # Do not use grep -q under pipefail here.  With hundreds of mapper
        # lines an early grep exit can SIGPIPE dmsetup/ssh and turn a real
        # match into a false negative.
        if remote "$peer" /sbin/dmsetup info -c --noheadings -o name 2>/dev/null \
            | sed 's/^[[:space:]]*//;s/[[:space:]]*$//' \
            | grep -Fx "$mapper" >/dev/null; then
            mapper_count=$((mapper_count + 1)); mapper_node=$peer
        fi
    done
    if (( mapper_count != 1 )) || [[ $mapper_node != "$node" ]]; then
        echo "MAPPER_CARDINALITY vm:$id placement=$node count=$mapper_count mapper_node=$mapper_node" >&2
        failures=$((failures + 1)); continue
    fi
    echo "PASS vm:$id node=$node owner=exact mapper=unique"
done

echo "HARD_FAILOVER_AUDIT_TOTAL=${#ids[@]}"
echo "HARD_FAILOVER_AUDIT_FAILURES=$failures"
(( failures == 0 ))
