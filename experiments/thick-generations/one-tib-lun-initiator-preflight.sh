#!/bin/bash
# Read-only initiator-side admission gate for the disposable 1-TiB Thick test.
set -euo pipefail

device=
expected_wwid=
expected_host=
minimum_bytes=1099511627776
minimum_paths=1

usage() {
    cat <<'EOF'
usage: one-tib-lun-initiator-preflight.sh \
       --device /dev/mapper/WWID --expect-wwid WWID \
       --expect-host EXACT-HOSTNAME [--minimum-paths N] \
       [--minimum-bytes BYTES]

Read-only. Proves the exact multipath identity, minimum reported capacity,
path count and absence of mounts, swap, holders and visible signatures before
initialization. It cannot prove that the storage target physically reserves
the advertised capacity; retain separate target-side allocation evidence.
EOF
}

while (($#)); do
    case "$1" in
        --device) device=${2:-}; shift 2 ;;
        --expect-wwid) expected_wwid=${2:-}; shift 2 ;;
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --minimum-paths) minimum_paths=${2:-}; shift 2 ;;
        --minimum-bytes) minimum_bytes=${2:-}; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

[[ -n "$device" && -n "$expected_wwid" && -n "$expected_host" ]] || {
    usage >&2
    exit 64
}
[[ "$expected_wwid" =~ ^[0-9A-Fa-f]+$ ]] || {
    echo "expected WWID must be hexadecimal" >&2
    exit 64
}
[[ "$minimum_bytes" =~ ^[0-9]+$ && "$minimum_bytes" -ge 1099511627776 ]] || {
    echo "minimum bytes must be an integer of at least one TiB" >&2
    exit 64
}
[[ "$minimum_paths" =~ ^[0-9]+$ && "$minimum_paths" -ge 1 && "$minimum_paths" -le 64 ]] || {
    echo "minimum paths must be an integer from 1 through 64" >&2
    exit 64
}
[[ "$device" == "/dev/mapper/$expected_wwid" ]] || {
    echo "device must be the exact expected /dev/mapper/WWID path" >&2
    exit 64
}
[[ "$(hostname)" == "$expected_host" ]] || {
    echo "exact hostname confirmation failed" >&2
    exit 2
}

for command in awk blockdev findmnt grep lsblk readlink wipefs; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is unavailable: $command" >&2
        exit 2
    }
done

[[ -b "$device" && ! -L "$device" ]] || {
    # /dev/mapper entries are normally symlinks; require that the submitted
    # path itself names the canonical mapper entry and resolve it below.
    [[ -L "$device" && -b "$(readlink -f -- "$device")" ]] || {
        echo "expected mapper path is not a block device" >&2
        exit 2
    }
}
resolved=$(readlink -f -- "$device")
[[ "$resolved" =~ ^/dev/dm-[0-9]+$ ]] || {
    echo "expected mapper does not resolve to one canonical dm device" >&2
    exit 2
}
dm_name=${resolved##*/}
sysfs="/sys/block/$dm_name"
[[ -d "$sysfs" && -r "$sysfs/dm/uuid" ]] || {
    echo "device-mapper sysfs identity is unavailable" >&2
    exit 2
}
dm_uuid=$(<"$sysfs/dm/uuid")
[[ "${dm_uuid,,}" == "mpath-${expected_wwid,,}" ]] || {
    echo "device-mapper UUID does not match the expected multipath WWID" >&2
    exit 2
}

reported_bytes=$(blockdev --getsize64 "$device")
[[ "$reported_bytes" =~ ^[0-9]+$ && "$reported_bytes" -ge "$minimum_bytes" ]] || {
    echo "reported LUN capacity is below the required physical-test size" >&2
    exit 2
}

shopt -s nullglob
paths=("$sysfs"/slaves/*)
holders=("$sysfs"/holders/*)
shopt -u nullglob
[[ ${#paths[@]} -ge "$minimum_paths" ]] || {
    echo "multipath slave count is below the required minimum" >&2
    exit 2
}
[[ ${#holders[@]} -eq 0 ]] || {
    echo "the candidate LUN already has kernel holders" >&2
    exit 2
}

if findmnt -rn -S "$device" | grep -q .; then
    echo "the candidate LUN is mounted" >&2
    exit 2
fi
if lsblk -nrpo NAME,MOUNTPOINTS "$device" | awk 'NF > 1 { found=1 } END { exit !found }'; then
    echo "the candidate LUN or one of its descendants is mounted" >&2
    exit 2
fi
if awk -v wanted="$resolved" 'NR > 1 && $1 == wanted { found=1 } END { exit !found }' /proc/swaps; then
    echo "the candidate LUN is active swap" >&2
    exit 2
fi
if ! signatures=$(wipefs -n "$device"); then
    echo "read-only signature inventory failed" >&2
    exit 2
fi
if [[ -n "$signatures" ]]; then
    echo "the candidate LUN contains an existing visible signature" >&2
    printf '%s\n' "$signatures" >&2
    exit 2
fi

echo "INITIATOR_PREFLIGHT=PASS"
echo "DEVICE=$device"
echo "DM_UUID=$dm_uuid"
echo "REPORTED_BYTES=$reported_bytes"
echo "OBSERVED_PATHS=${#paths[@]}"
echo "BACKING_ALLOCATION=UNPROVEN_REQUIRES_TARGET_EVIDENCE"
echo "MUTATIONS=NONE"
