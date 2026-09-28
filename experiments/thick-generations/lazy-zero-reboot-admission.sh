#!/bin/bash
# Seal a fresh host-wide/peer read-only admission for one controlled-reboot recovery.
set -euo pipefail
umask 077

host='' tx='' nonce='' ready_sha='' witness_sha='' peer_1='' peer_2=''
while (($#)); do
    case $1 in
        --expect-host) host=${2:-}; shift 2 ;;
        --tx) tx=${2:-}; shift 2 ;;
        --nonce) nonce=${2:-}; shift 2 ;;
        --ready-sha) ready_sha=${2:-}; shift 2 ;;
        --witness-sha) witness_sha=${2:-}; shift 2 ;;
        --peer-1) peer_1=${2:-}; shift 2 ;;
        --peer-2) peer_2=${2:-}; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
[[ $(hostname) == "$host" && $tx =~ ^[0-9a-f]{32}$ && $nonce =~ ^[0-9a-f]{32}$ \
    && $ready_sha =~ ^[0-9a-f]{64}$ && $witness_sha =~ ^[0-9a-f]{64}$ \
    && $peer_1 =~ ^[A-Za-z0-9_.-]+$ && $peer_2 =~ ^[A-Za-z0-9_.-]+$ \
    && $peer_1 != "$peer_2" && $peer_1 != "$host" && $peer_2 != "$host" ]] || exit 64
for tool in awk dmsetup grep pvecm pct pgrep qm sha256sum ssh sync; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

root="/var/tmp/slt-lazy-shared-$tx"
ready="$root/controlled-reboot-ready.manifest"
witness="$root/controlled-reboot-prepare.observed"
admission="$root/controlled-reboot-admission.manifest"
peer_hosts="$root/peer-known-hosts"
[[ -d $root && ! -L $root && -f $ready && ! -L $ready \
    && -f $witness && ! -L $witness && ! -e $admission ]] \
    || { echo "unsafe admission evidence set" >&2; exit 2; }
[[ $(sha256sum "$ready" | awk '{print $1}') == "$ready_sha" \
    && $(sha256sum "$witness" | awk '{print $1}') == "$witness_sha" ]] \
    || { echo "prepare evidence SHA mismatch" >&2; exit 2; }
old_boot=$(awk -F= '$1=="BOOT_ID" {n++; v=$2} END {if(n!=1) exit 2; print v}' "$ready")
new_boot=$(cat /proc/sys/kernel/random/boot_id)
[[ $old_boot =~ ^[0-9a-f-]{36}$ && $new_boot =~ ^[0-9a-f-]{36}$ && $old_boot != "$new_boot" ]] \
    || { echo "new boot identity is unproven" >&2; exit 2; }

pvecm status | grep -q '^Quorate:[[:space:]]*Yes$' || { echo "cluster is not quorate" >&2; exit 2; }
! qm list | awk 'NR>1 && $3=="running" {found=1} END {exit !found}' \
    || { echo "local running VM blocks reboot recovery" >&2; exit 2; }
! pct list | awk 'NR>1 && $2=="running" {found=1} END {exit !found}' \
    || { echo "local running CT blocks reboot recovery" >&2; exit 2; }
# A complete process-state inventory is required; matching command names is not equivalent.
# shellcheck disable=SC2009
! ps -eo stat= | grep -q '^D' || { echo "local D-state blocks reboot recovery" >&2; exit 2; }
! pgrep -af 'vzdump|qmigrate|pvesr|lv(create|remove|convert|rename|extend|reduce)|vg(change|extend|reduce)' \
    | grep -v 'lazy-zero-reboot-admission' >/dev/null \
    || { echo "local storage worker blocks reboot recovery" >&2; exit 2; }

audit_peer() {
    local peer=$1 output
    [[ -f $peer_hosts && ! -L $peer_hosts ]] || { echo "unsafe peer host-key set" >&2; exit 2; }
    output=$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$peer_hosts" "root@$peer" bash -s -- \
        "$peer" "$nonce" <<'PEER'
set -euo pipefail
peer=$1; nonce=$2
[[ $(hostname) == "$peer" ]] || exit 2
boot=$(cat /proc/sys/kernel/random/boot_id)
[[ $boot =~ ^[0-9a-f-]{36}$ ]] || exit 2
inventory=$(dmsetup info -c --noheadings --separator '|' -o name,uuid 2>&1) || exit 2
! grep -q "$nonce" <<<"$inventory" || exit 2
printf '%s\n' "$boot"
PEER
    ) || { echo "peer audit failed: $peer" >&2; exit 2; }
    [[ $output =~ ^[0-9a-f-]{36}$ ]] || { echo "peer audit malformed: $peer" >&2; exit 2; }
    printf '%s' "$output"
}
peer_1_boot=$(audit_peer "$peer_1")
peer_2_boot=$(audit_peer "$peer_2")

tmp="$admission.tmp"
{
    printf 'SCHEMA=1\nKIND=CONTROLLED_REBOOT_ADMISSION_V1\nHOST=%s\nNEW_BOOT_ID=%s\n' \
        "$host" "$new_boot"
    printf 'READY_MANIFEST_SHA256=%s\nWITNESS_SHA256=%s\nQUORUM=PASS\n' \
        "$ready_sha" "$witness_sha"
    printf 'LOCAL_NO_RUNNING_GUESTS=PASS\nLOCAL_NO_DSTATE=PASS\nLOCAL_NO_STORAGE_WORKERS=PASS\n'
    printf 'PEER_COUNT=2\nPEER_1_NAME=%s\nPEER_1_BOOT_ID=%s\nPEER_1_REACHABLE=PASS\nPEER_1_LAB_ABSENT=PASS\n' \
        "$peer_1" "$peer_1_boot"
    printf 'PEER_2_NAME=%s\nPEER_2_BOOT_ID=%s\nPEER_2_REACHABLE=PASS\nPEER_2_LAB_ABSENT=PASS\n' \
        "$peer_2" "$peer_2_boot"
} >"$tmp"
chmod 0600 "$tmp"; sync -f "$tmp"
mv -n "$tmp" "$admission"; sync -f "$root"
echo "CONTROLLED_REBOOT_ADMISSION=$admission"
echo "CONTROLLED_REBOOT_ADMISSION_SHA256=$(sha256sum "$admission" | awk '{print $1}')"
echo 'RESULT=CONTROLLED_REBOOT_ADMISSION_SEALED_NO_STORAGE_MUTATION'
