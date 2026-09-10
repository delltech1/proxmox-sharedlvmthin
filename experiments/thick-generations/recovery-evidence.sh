#!/bin/sh
# Capture one transaction-scoped, read-only Thick Generations recovery sample.
set -eu

fail() {
    printf 'EVIDENCE_STATE=ERROR\nREASON=%s\n' "$1" >&2
    exit 2
}

[ "$#" -eq 6 ] || fail "usage: recovery-evidence.sh OUTPUT_DIR DEVICE VG STORE_ID VOLUME MAPPER"
output_dir=$1
device=$2
vg=$3
store_id=$4
volume=$5
mapper=$6

safe_token() {
    case "$2" in
        *[!A-Za-z0-9_.+-]*|'') fail "unsafe $1" ;;
    esac
}

safe_token "VG" "$vg"
safe_token "storage ID" "$store_id"
safe_token "volume" "$volume"
safe_token "mapper" "$mapper"
case "$device" in
    /dev/mapper/*) ;;
    *) fail "device must be an exact /dev/mapper path" ;;
esac
[ -b "$device" ] || fail "pinned mapper device is not a block device"
[ ! -e "$output_dir" ] || fail "output directory already exists"

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
guard="$script_dir/guarded-probe.sh"
[ -x "$guard" ] || fail "guarded probe helper is unavailable"

umask 077
mkdir -p "$output_dir"
probe_dir="$output_dir/probes"
mkdir -p "$probe_dir"

{
    printf 'EVIDENCE_VERSION=1\n'
    printf 'COLLECTED_AT_UTC=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    printf 'VG=%s\nSTORE_ID=%s\nVOLUME=%s\nMAPPER=%s\n' \
        "$vg" "$store_id" "$volume" "$mapper"
    printf 'DEVICE_BASENAME=%s\n' "${device##*/}"
} >"$output_dir/manifest"

run_guarded() {
    name=$1
    shift
    set +e
    "$guard" "$probe_dir" "$name" 10 -- "$@" \
        >"$output_dir/$name.guard" 2>"$output_dir/$name.guard.stderr"
    rc=$?
    set -e
    upper_name=$(printf '%s' "$name" | tr '[:lower:]' '[:upper:]')
    printf '%s_EXIT=%s\n' "$upper_name" "$rc" >>"$output_dir/manifest"
    [ "$rc" -eq 0 ] || {
        printf 'EVIDENCE_STATE=BLOCKED\nFAILED_PROBE=%s\n' "$name" >>"$output_dir/manifest"
        exit "$rc"
    }
}

# Each potentially blocking LVM inventory is issued once, against only the
# explicitly pinned multipath device. The guard prevents duplicate D-state
# probes after a timeout.
run_guarded vgs vgs --readonly --reportformat json --units b --nosuffix \
    --devices "$device" -o vg_name,vg_uuid,vg_tags "$vg"
run_guarded lvs lvs --readonly --reportformat json --units b --nosuffix \
    --devices "$device" -o lv_name,lv_uuid,lv_attr,lv_size,lv_tags "$vg"

# Device-mapper reads never flush or suspend the live table. Missing frontends
# are valid evidence and are recorded rather than reconstructed.
for probe in info table status deps; do
    set +e
    case "$probe" in
        info)
            dmsetup info -c --noheadings -o name,uuid,major,minor,open,suspended "$mapper" \
                >"$output_dir/dm-$probe.stdout" 2>"$output_dir/dm-$probe.stderr"
            ;;
        table)
            dmsetup table --noflush "$mapper" \
                >"$output_dir/dm-$probe.stdout" 2>"$output_dir/dm-$probe.stderr"
            ;;
        status)
            dmsetup status --noflush "$mapper" \
                >"$output_dir/dm-$probe.stdout" 2>"$output_dir/dm-$probe.stderr"
            ;;
        deps)
            dmsetup deps -o devname "$mapper" \
                >"$output_dir/dm-$probe.stdout" 2>"$output_dir/dm-$probe.stderr"
            ;;
    esac
    rc=$?
    set -e
    printf 'DM_%s_EXIT=%s\n' "$(printf '%s' "$probe" | tr '[:lower:]' '[:upper:]')" "$rc" \
        >>"$output_dir/manifest"
done

printf 'EVIDENCE_STATE=CAPTURED\n' >>"$output_dir/manifest"
printf 'EVIDENCE_STATE=CAPTURED\nOUTPUT_DIR=%s\n' "$output_dir"
