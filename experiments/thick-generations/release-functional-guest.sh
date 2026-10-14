#!/bin/busybox sh
# READ-ONLY disposable guest protocol foundation; not a boot/QMP/RAM test PASS.
# Manifest: /etc/slt-functional-readonly.manifest, exact lines:
#   vda 8388608 <64 lowercase hex SHA256 of exactly 4096 bytes>
# VERIFY reads only an approved, 4096-aligned whole-disk range. No disk writes.
# Transport is stdin/stdout: dedicate a guest console before invoking this file.
PATH=/bin:/usr/bin:/sbin:/usr/sbin
export PATH
LC_ALL=C
export LC_ALL
ram_nonce=UNSET
periodic_beacon=NO

token_ok() {
    case "$1" in ''|*[!A-Za-z0-9_-]*) return 1 ;; esac
    [ "${#1}" -le 64 ]
}
hash_ok() {
    case "$1" in *[!0-9a-f]*) return 1 ;; esac
    [ "${#1}" -eq 64 ]
}
disk_ok() {
    case "$1" in vd*|sd*) ;; *) return 1 ;; esac
    suffix=${1#??}
    case "$suffix" in ''|*[!a-z]*) return 1 ;; esac
    [ "${#1}" -le 12 ]
}
offset_ok() {
    case "$1" in ''|*[!0-9]*|0[0-9]*) return 1 ;; esac
    # Allow TiB-scale offsets while staying well below signed 64-bit arithmetic.
    [ "${#1}" -le 15 ] || return 1
    [ "$(( $1 % 4096 ))" -eq 0 ]
}
hex_value() { printf '%s' "$1" | od -An -tx1 | tr -d ' \n'; }
sysinfo() {
    challenge=$1
    boot=$(cat /proc/sys/kernel/random/boot_id) || return 1
    case "$boot" in ''|*[!0-9a-f-]*) return 1 ;; esac
    efi=NO; [ ! -d /sys/firmware/efi ] || efi=YES
    printf 'SYSINFO %s BOOT %s EFI %s\n' "$challenge" "$boot" "$efi"
    for entry in /sys/class/block/*; do
        [ -e "$entry" ] || continue
        name=${entry##*/}
        size=$(cat "$entry/size") || return 1
        path=$(readlink -f "$entry") || return 1
        serial=UNAVAILABLE
        if [ -r "$entry/serial" ]; then serial=$(cat "$entry/serial") || return 1;
        elif [ -r "$entry/device/serial" ]; then serial=$(cat "$entry/device/serial") || return 1; fi
        printf 'BLOCK %s %s %s %s %s\n' "$challenge" "$(hex_value "$name")" "$size" "$(hex_value "$path")" "$(hex_value "$serial")"
    done
    for entry in /sys/class/net/*; do
        [ -e "$entry" ] || continue
        name=${entry##*/}; driver=UNAVAILABLE; vendor=UNAVAILABLE; model=UNAVAILABLE
        if [ -L "$entry/device/driver" ]; then driver=$(readlink -f "$entry/device/driver") || return 1; fi
        if [ -r "$entry/device/vendor" ]; then vendor=$(cat "$entry/device/vendor") || return 1; fi
        if [ -r "$entry/device/device" ]; then model=$(cat "$entry/device/device") || return 1; fi
        printf 'NIC %s %s %s %s %s\n' "$challenge" "$(hex_value "$name")" "$(hex_value "$driver")" "$(hex_value "$vendor")" "$(hex_value "$model")"
    done
    printf 'SYSINFO_END %s\n' "$challenge"
}
verify_read() {
    challenge=$1; disk=$2; offset=$3; expected=$4
    disk_ok "$disk" && offset_ok "$offset" && hash_ok "$expected" || return 1
    manifest=/etc/slt-functional-readonly.manifest
    [ -f "$manifest" ] && [ ! -L "$manifest" ] || return 1
    allowed=0
    while IFS= read -r entry || [ -n "$entry" ]; do
        [ "$entry" != "$disk $offset $expected" ] || allowed=$((allowed+1))
    done < "$manifest"
    [ "$allowed" -eq 1 ] || return 1
    [ -b "/dev/$disk" ] && [ -e "/sys/class/block/$disk" ] || return 1
    sectors=$(cat "/sys/class/block/$disk/size") || return 1
    case "$sectors" in ''|*[!0-9]*) return 1 ;; esac
    [ "${#sectors}" -le 13 ] || return 1
    [ "$((offset / 512 + 8))" -le "$sectors" ] || return 1
    digest=$(dd if="/dev/$disk" bs=4096 skip="$((offset / 4096))" count=1 iflag=fullblock 2>/dev/null | sha256sum) || return 1
    digest=${digest%% *}
    [ "$digest" = "$expected" ] || return 1
    printf 'VERIFY_OK %s %s %s %s\n' "$challenge" "$disk" "$offset" "$digest"
}
protocol() {
    while :; do
        if [ "$periodic_beacon" = YES ]; then
            # BusyBox ash supports read -t; timeout/beacon behavior is covered by isolated PTY tests.
            # shellcheck disable=SC3045
            if ! IFS=' ' read -r -t 1 command challenge a b c extra; then
                # One process owns all output: never interleave a heartbeat
                # with a challenged response. Not a disk/progress watchdog.
                [ -t 0 ] || return 0
                printf 'SLT_READONLY_GUEST_PROTOCOL=1\n'
                sleep 1
                continue
            fi
        else
            IFS=' ' read -r command challenge a b c extra || break
        fi
        if ! token_ok "$challenge" || [ -n "$extra" ]; then printf 'ERROR INVALID_REQUEST\n'; continue; fi
        case "$command" in
            SYSINFO)
                if [ -z "$a$b$c" ]; then sysinfo "$challenge" || printf 'ERROR %s SYSINFO_FAILED\n' "$challenge";
                else printf 'ERROR %s ARGUMENTS\n' "$challenge"; fi ;;
            GET_RAM)
                if [ -z "$a$b$c" ]; then printf 'RAM %s %s\n' "$challenge" "$ram_nonce";
                else printf 'ERROR %s ARGUMENTS\n' "$challenge"; fi ;;
            SET_RAM)
                if token_ok "$a" && [ -z "$b$c" ]; then ram_nonce=$a; printf 'RAM_SET %s %s\n' "$challenge" "$ram_nonce";
                else printf 'ERROR %s ARGUMENTS\n' "$challenge"; fi ;;
            VERIFY) verify_read "$challenge" "$a" "$b" "$c" || printf 'ERROR %s VERIFY_REFUSED\n' "$challenge" ;;
            *) printf 'ERROR %s UNKNOWN_COMMAND\n' "$challenge" ;;
        esac
    done
}

# MAIN_GUEST_GUARD: unit tests extract functions only, never invoke this guard
# with a replacement cmdline. Production has no test flag or environment bypass.
set -f
cmdline=$(cat /proc/cmdline) || exit 2
approved=0
for argument in $cmdline; do
    [ "$argument" != slt_disposable_guest=YES ] || approved=$((approved+1))
done
[ "$approved" -eq 1 ] || { printf 'REFUSED NOT_DISPOSABLE_GUEST\n' >&2; exit 2; }
set +f
# BusyBox ash pipefail is required: read failure cannot become a hash success.
set -o pipefail || exit 2
periodic_beacon=YES
printf 'SLT_READONLY_GUEST_PROTOCOL=1\n'
protocol
