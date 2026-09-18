#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-only
# Submit bounded native PVE HA relocation requests for an exact VM manifest.

set -euo pipefail

manifest=${1:-}
target=${2:-}
delay=${3:-1}

[[ -f $manifest ]] || { echo "manifest missing" >&2; exit 64; }
[[ $target =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "invalid target" >&2; exit 64; }
[[ $delay =~ ^[0-9]+$ && $delay -le 30 ]] || { echo "invalid delay" >&2; exit 64; }

mapfile -t ids < <(sed '/^[[:space:]]*$/d' "$manifest")
(( ${#ids[@]} >= 1 && ${#ids[@]} <= 500 )) || exit 64
declare -A seen
for id in "${ids[@]}"; do
    [[ $id =~ ^[1-9][0-9]+$ ]] || exit 64
    [[ -z ${seen[$id]:-} ]] || exit 64
    seen[$id]=1
    ha-manager config | grep -q "^vm:${id}$" || {
        echo "VM $id is not HA-managed" >&2
        exit 1
    }
done

failures=0
for id in "${ids[@]}"; do
    if timeout --foreground --kill-after=5 60 \
        ha-manager migrate "vm:$id" "$target"; then
        echo "SUBMITTED vm:$id target=$target"
    else
        echo "SUBMIT_FAILED vm:$id target=$target" >&2
        failures=$((failures + 1))
    fi
    sleep "$delay"
done

echo "HA_RELOCATION_SUBMITTED=${#ids[@]}"
echo "HA_RELOCATION_SUBMIT_FAILURES=$failures"
(( failures == 0 ))
