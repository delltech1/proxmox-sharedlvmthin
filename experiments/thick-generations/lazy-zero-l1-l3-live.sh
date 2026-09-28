#!/bin/bash
# Disposable live L1-L3 dm-zero/dm-clone qualification. Fixed loop files only.
set -euo pipefail
umask 077

ACK=I_UNDERSTAND_DISPOSABLE_LAZY_ZERO_L1_L3
bytes=134217728
meta_bytes=33554432
sectors=262144
region_bytes=1048576
region_sectors=2048
expected_host=
l0_file=
l0_sha=
execute_token=

fail() { printf 'RESULT=UNKNOWN\nREASON=%s\nEVIDENCE=%s\n' "$*" "${evidence:-UNCREATED}" >&2; exit 2; }
usage() {
    echo "usage: $0 --expect-host HOST --l0-file ABS --l0-sha256 HEX --execute-token $ACK" >&2
    exit 64
}
while (($#)); do
    case $1 in
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --l0-file) l0_file=${2:-}; shift 2 ;;
        --l0-sha256) l0_sha=${2:-}; shift 2 ;;
        --execute-token) execute_token=${2:-}; shift 2 ;;
        *) usage ;;
    esac
done
[[ $EUID -eq 0 && -n $expected_host && $l0_file = /* && $l0_sha =~ ^[0-9a-f]{64}$ ]] || usage
[[ $execute_token == "$ACK" ]] || fail "explicit disposable acknowledgement missing"
[[ $(hostname) == "$expected_host" ]] || fail "host mismatch"
[[ -f $l0_file && ! -L $l0_file ]] || fail "unsafe L0 evidence"
[[ $(sha256sum "$l0_file" | awk '{print $1}') == "$l0_sha" ]] || fail "L0 digest mismatch"
grep -qx 'VERDICT=READY_FOR_L1_DESIGN' "$l0_file" || fail "L0 is not ready"
grep -qx "HOST=$expected_host" "$l0_file" || fail "L0 host mismatch"
grep -qx "BOOT_ID=$(cat /proc/sys/kernel/random/boot_id)" "$l0_file" || fail "L0 boot mismatch"
grep -qx "KERNEL=$(uname -r)" "$l0_file" || fail "L0 kernel mismatch"
for tool in awk blockdev cat dmsetup findmnt grep hostname losetup lsblk mktemp python3 sed seq sha256sum sleep stat tee tr uname; do
    command -v "$tool" >/dev/null || fail "missing tool: $tool"
done
fstype=$(findmnt -n -o FSTYPE -T /var/tmp)
case $fstype in nfs*|cifs|smb*|fuse*|9p) fail "nonlocal /var/tmp filesystem: $fstype" ;; esac

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
io="$script_dir/lazy-zero-live-io.py"
[[ -f $io && ! -L $io ]] || fail "I/O helper is unsafe"
io_sha=$(sha256sum "$io" | awk '{print $1}')
nonce=$(tr -d - < /proc/sys/kernel/random/uuid)
root=$(mktemp -d "/var/tmp/slt-lazy-loop-lab-${nonce}.XXXXXXXX")
[[ $root = /var/tmp/slt-lazy-loop-lab-* && -d $root && ! -L $root ]] || fail "unsafe work root"
chmod 0700 "$root"
evidence="$root/evidence.log"
exec > >(tee -a "$evidence") 2>&1
on_exit() {
    local exit_rc=$?
    if ((exit_rc != 0)); then
        echo "TERMINAL=UNKNOWN_RETAIN_EXACT_OBJECTS"
        echo "EXIT=$exit_rc"
    fi
}
trap on_exit EXIT
printf 'SCHEMA=1\nHOST=%s\nBOOT_ID=%s\nKERNEL=%s\nL0_SHA256=%s\nIO_HELPER_SHA256=%s\nROOT=%s\n' \
    "$(hostname)" "$(cat /proc/sys/kernel/random/boot_id)" "$(uname -r)" "$l0_sha" "$io_sha" "$root"

declare -A data data_seed meta data_loop meta_loop data_inode meta_inode data_loop_id meta_loop_id data_block_id zero zero_block_id clone clone_block_id clone_table uuid_zero uuid_clone
dm_info() { dmsetup info -c --noheadings --separator '|' -o name,uuid,major,minor,open,attr "$1" | tr -d ' '; }
dm_uuid() { dmsetup info -c --noheadings -o uuid "$1" | tr -d '[:space:]'; }
devno() { lsblk -dn -o MAJ:MIN "$1" | tr -d '[:space:]'; }
dm_devno() { dmsetup info -c --noheadings --separator ':' -o major,minor "$1" | tr -d ' '; }
dm_table() { dmsetup table "$1" | sed 's/[[:space:]]*$//'; }
diskseq() { local number=$1; cat "/sys/dev/block/$number/diskseq"; }
loop_snapshot() { losetup --list --noheadings --raw --output NAME,MAJ:MIN,BACK-FILE,BACK-INO,BACK-MAJ:MIN,OFFSET,SIZELIMIT,RO,PARTSCAN,LOG-SEC "$1"; }
check_block() {
    local path=$1 byte=$2 size=$3 frozen=$4 offset=${5:-0} length=${6:-}
    local number major minor sequence
    number=${frozen%%|*}; sequence=${frozen#*|}; major=${number%:*}; minor=${number#*:}
    if [[ -n $length ]]; then
        python3 -I -B "$io" check "$path" "$byte" "$major" "$minor" "$sequence" "$size" --offset "$offset" --length "$length"
    else
        python3 -I -B "$io" check "$path" "$byte" "$major" "$minor" "$sequence" "$size"
    fi
}
discard_block() {
    local path=$1 offset=$2 length=$3 expectation=$4 size=$5 frozen=$6
    local number major minor sequence
    number=${frozen%%|*}; sequence=${frozen#*|}; major=${number%:*}; minor=${number#*:}
    python3 -I -B "$io" discard "$path" "$offset" "$length" "$expectation" "$major" "$minor" "$sequence" "$size"
}
write_block() {
    local path=$1 offset=$2 length=$3 byte=$4 size=$5 frozen=$6
    local number major minor sequence
    number=${frozen%%|*}; sequence=${frozen#*|}; major=${number%:*}; minor=${number#*:}
    python3 -I -B "$io" write-byte "$path" "$offset" "$length" "$byte" "$major" "$minor" "$sequence" "$size"
}
check_pattern_block() {
    local path=$1 seed=$2 size=$3 frozen=$4 offset=${5:-0} length=${6:-}
    local number major minor sequence
    number=${frozen%%|*}; sequence=${frozen#*|}; major=${number%:*}; minor=${number#*:}
    if [[ -n $length ]]; then
        python3 -I -B "$io" check-pattern "$path" "$seed" "$major" "$minor" "$sequence" "$size" --offset "$offset" --length "$length"
    else
        python3 -I -B "$io" check-pattern "$path" "$seed" "$major" "$minor" "$sequence" "$size"
    fi
}
status() { dmsetup status --noflush "$1"; }
progress() { status "$1" | awk '{print $7":"$8}'; }

create_fixture() {
    local role=$1
    data[$role]="$root/$role-data.img"
    data_seed[$role]="slt-lazy-$nonce-$role"
    meta[$role]="$root/$role-meta.img"
    zero[$role]="slt-lz-zero-$role-$nonce"
    clone[$role]="slt-lz-clone-$role-$nonce"
    uuid_zero[$role]="SLT-LZ-ZERO-${role^^}-$nonce"
    uuid_clone[$role]="SLT-LZ-CLONE-${role^^}-$nonce"
    printf 'INTENT=FILL_DATA ROLE=%s\n' "$role"
    python3 -I -B "$io" fill-pattern "${data[$role]}" "$bytes" "${data_seed[$role]}"
    data_inode[$role]=$(stat -Lc '%d:%i' "${data[$role]}")
    printf 'INTENT=FILL_META ROLE=%s\n' "$role"
    python3 -I -B "$io" fill "${meta[$role]}" "$meta_bytes" 0 --allow-compressed
    meta_inode[$role]=$(stat -Lc '%d:%i' "${meta[$role]}")
    printf 'INTENT=ATTACH_LOOPS ROLE=%s\n' "$role"
    data_loop[$role]=$(losetup --find --show --nooverlap "${data[$role]}")
    meta_loop[$role]=$(losetup --find --show --nooverlap "${meta[$role]}")
    [[ $(blockdev --getsize64 "${data_loop[$role]}") -eq $bytes ]] || fail "$role data loop size"
    [[ $(blockdev --getsize64 "${meta_loop[$role]}") -eq $meta_bytes ]] || fail "$role metadata loop size"
    [[ $(losetup -l -n -O BACK-FILE "${data_loop[$role]}") == "${data[$role]}" ]] || fail "$role data backing"
    [[ $(losetup -l -n -O BACK-FILE "${meta_loop[$role]}") == "${meta[$role]}" ]] || fail "$role metadata backing"
    data_loop_id[$role]=$(loop_snapshot "${data_loop[$role]}")
    meta_loop_id[$role]=$(loop_snapshot "${meta_loop[$role]}")
    printf 'IDENTITY_%s_DATA_LOOP=%s\nIDENTITY_%s_META_LOOP=%s\n' \
        "${role^^}" "${data_loop_id[$role]}" "${role^^}" "${meta_loop_id[$role]}"
    local data_number
    data_number=$(devno "${data_loop[$role]}")
    data_block_id[$role]="$data_number|$(diskseq "$data_number")"
    check_pattern_block "${data_loop[$role]}" "${data_seed[$role]}" "$bytes" "${data_block_id[$role]}"
    printf 'INTENT=CREATE_ZERO ROLE=%s\n' "$role"
    dmsetup create "${zero[$role]}" --readonly --uuid "${uuid_zero[$role]}" --table "0 $sectors zero"
    [[ $(dm_uuid "${zero[$role]}") == "${uuid_zero[$role]}" ]] || fail "$role zero UUID"
    [[ $(dm_table "${zero[$role]}") == "0 $sectors zero" ]] || fail "$role zero table"
    local zero_number
    zero_number=$(dm_devno "${zero[$role]}")
    [[ $(devno "/dev/mapper/${zero[$role]}") == "$zero_number" ]] || fail "$role zero node devno"
    zero_block_id[$role]="$zero_number|$(diskseq "$zero_number")"
    check_block "/dev/mapper/${zero[$role]}" 0 "$bytes" "${zero_block_id[$role]}"
    local meta_dev data_dev zero_dev expected actual
    meta_dev=$(devno "${meta_loop[$role]}")
    data_dev=$(devno "${data_loop[$role]}")
    zero_dev=$(devno "/dev/mapper/${zero[$role]}")
    expected="0 $sectors clone $meta_dev $data_dev $zero_dev $region_sectors 2 no_hydration no_discard_passdown"
    clone_table[$role]=$expected
    printf 'INTENT=CREATE_CLONE ROLE=%s TABLE=%s\n' "$role" "$expected"
    dmsetup create "${clone[$role]}" --uuid "${uuid_clone[$role]}" --table "$expected"
    [[ $(dm_uuid "${clone[$role]}") == "${uuid_clone[$role]}" ]] || fail "$role clone UUID"
    local clone_number
    clone_number=$(dm_devno "${clone[$role]}")
    [[ $(devno "/dev/mapper/${clone[$role]}") == "$clone_number" ]] || fail "$role clone node devno"
    clone_block_id[$role]="$clone_number|$(diskseq "$clone_number")"
    actual=$(dm_table "${clone[$role]}")
    [[ $actual == "$expected" ]] || fail "$role clone table mismatch: $actual"
    [[ $(progress "${clone[$role]}") == 0/128:0 ]] || fail "$role initial progress"
    printf 'IDENTITY_%s_ZERO=%s\nIDENTITY_%s_CLONE=%s\n' "${role^^}" "$(dm_info "${zero[$role]}")" "${role^^}" "$(dm_info "${clone[$role]}")"
}

guard_clone() {
    local role=$1 dev queue
    dev=$(dm_devno "${clone[$role]}")
    [[ "$dev|$(diskseq "$dev")" == "${clone_block_id[$role]}" ]] || fail "$role guard incarnation drift"
    [[ $(devno "/dev/mapper/${clone[$role]}") == "$dev" ]] || fail "$role guard node devno"
    queue="/sys/dev/block/$dev/queue/discard_max_bytes"
    [[ -f $queue && ! -L $queue ]] || fail "$role discard queue identity"
    printf 'INTENT=SET_DISCARD_GUARD ROLE=%s DEVNO=%s\n' "$role" "$dev"
    printf '0\n' > "$queue"
    [[ $(cat "$queue") == 0 ]] || fail "$role discard guard readback"
    [[ $(dm_uuid "${clone[$role]}") == "${uuid_clone[$role]}" ]] || fail "$role clone drift after guard"
    printf 'GUARD_%s_QUEUE=%s\nGUARD_%s_VALUE=0\n' "${role^^}" "$queue" "${role^^}"
}

create_fixture guarded
guard_clone guarded
echo 'CASE=L1_ZERO_READS START'
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}"
check_pattern_block "${data_loop[guarded]}" "${data_seed[guarded]}" "$bytes" "${data_block_id[guarded]}"
[[ $(progress "${clone[guarded]}") == 0/128:0 ]] || fail "L1 changed progress"
echo 'CASE=L1_ZERO_READS RESULT=PASS'

create_fixture hazard
echo 'CASE=L2_UNGUARDED_DISCARD START'
hazard_queue="/sys/dev/block/$(dm_devno "${clone[hazard]}")/queue/discard_max_bytes"
[[ "$(dm_devno "${clone[hazard]}")|$(diskseq "$(dm_devno "${clone[hazard]}")")" == "${clone_block_id[hazard]}" ]] || fail "L2 clone incarnation drift"
[[ -f $hazard_queue && $(cat "$hazard_queue") -gt 0 ]] || fail "L2 discard was not advertised"
before=$(progress "${clone[hazard]}")
discard_block "/dev/mapper/${clone[hazard]}" 0 "$region_bytes" success "$bytes" "${clone_block_id[hazard]}"
after=$(progress "${clone[hazard]}")
[[ $before == 0/128:0 && $after == 1/128:0 ]] || fail "L2 exact hydrated transition missing: $before -> $after"
check_pattern_block "/dev/mapper/${clone[hazard]}" "${data_seed[hazard]}" "$bytes" "${clone_block_id[hazard]}" 0 "$region_bytes"
check_pattern_block "${data_loop[hazard]}" "${data_seed[hazard]}" "$bytes" "${data_block_id[hazard]}"
echo 'CASE=L2_UNGUARDED_DISCARD RESULT=PASS HAZARD=STALE_DESTINATION_DISCLOSED'

echo 'CASE=L3_GUARDED_DISCARD START'
guarded_before=$(progress "${clone[guarded]}")
for spec in "0 512" "$region_bytes $region_bytes" "$((region_bytes-4096)) 8192" "0 $bytes"; do
    read -r offset length <<<"$spec"
    [[ $(cat "/sys/dev/block/$(dm_devno "${clone[guarded]}")/queue/discard_max_bytes") == 0 ]] || fail "L3 guard drift"
    discard_block "/dev/mapper/${clone[guarded]}" "$offset" "$length" eopnotsupp "$bytes" "${clone_block_id[guarded]}"
    [[ $(progress "${clone[guarded]}") == "$guarded_before" ]] || fail "L3 progress changed"
done
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}"
check_pattern_block "${data_loop[guarded]}" "${data_seed[guarded]}" "$bytes" "${data_block_id[guarded]}"
echo 'CASE=L3_GUARDED_DISCARD RESULT=PASS'

echo 'CASE=L4A_STOPPED_SUSPEND_RESUME START'
dmsetup suspend "${clone[guarded]}"
[[ $(dm_uuid "${clone[guarded]}") == "${uuid_clone[guarded]}" ]] || fail "L4a suspended UUID drift"
dmsetup resume "${clone[guarded]}"
[[ $(dm_table "${clone[guarded]}") == "${clone_table[guarded]}" ]] || fail "L4a resumed table drift"
guard_clone guarded
discard_block "/dev/mapper/${clone[guarded]}" "$((3*region_bytes))" "$region_bytes" eopnotsupp "$bytes" "${clone_block_id[guarded]}"
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}"
check_pattern_block "${data_loop[guarded]}" "${data_seed[guarded]}" "$bytes" "${data_block_id[guarded]}"
[[ $(progress "${clone[guarded]}") == 0/128:0 ]] || fail "L4a progress changed"
echo 'CASE=L4A_STOPPED_SUSPEND_RESUME RESULT=PASS'

echo 'CASE=L4B_EXACT_RELOAD START'
dmsetup suspend "${clone[guarded]}"
dmsetup load "${clone[guarded]}" --table "${clone_table[guarded]}"
inactive=$(dmsetup table --inactive "${clone[guarded]}" | sed 's/[[:space:]]*$//')
[[ $inactive == "${clone_table[guarded]}" ]] || fail "L4b inactive table mismatch"
dmsetup resume "${clone[guarded]}"
[[ $(dm_table "${clone[guarded]}") == "${clone_table[guarded]}" ]] || fail "L4b live table mismatch"
guard_clone guarded
discard_block "/dev/mapper/${clone[guarded]}" "$((4*region_bytes))" "$region_bytes" eopnotsupp "$bytes" "${clone_block_id[guarded]}"
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}"
[[ $(progress "${clone[guarded]}") == 0/128:0 ]] || fail "L4b progress changed"
echo 'CASE=L4B_EXACT_RELOAD RESULT=PASS'

echo 'CASE=L4C_WRITE_FLUSH_REOPEN START'
write_offset=$((2*region_bytes))
write_length=4096
write_block "/dev/mapper/${clone[guarded]}" "$write_offset" "$write_length" 0x3c "$bytes" "${clone_block_id[guarded]}"
[[ $(progress "${clone[guarded]}") == 1/128:0 ]] || fail "L4c write did not hydrate exactly one region"
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}" 0 "$write_offset"
check_block "/dev/mapper/${clone[guarded]}" 0x3c "$bytes" "${clone_block_id[guarded]}" "$write_offset" "$write_length"
tail_offset=$((write_offset+write_length))
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}" "$tail_offset" "$((bytes-tail_offset))"
[[ $(dm_uuid "${clone[guarded]}") == "${uuid_clone[guarded]}" ]] || fail "L4c pre-remove UUID drift"
[[ $(dmsetup info -c --noheadings -o open "${clone[guarded]}" | tr -d ' ') == 0 ]] || fail "L4c clone open before reopen"
dmsetup remove "${clone[guarded]}"
inventory=$(dmsetup ls 2>&1) || fail "L4c DM inventory failed"
! grep -q "^${clone[guarded]}[[:space:]]" <<<"$inventory" || fail "L4c clone remained after remove"
dmsetup create "${clone[guarded]}" --uuid "${uuid_clone[guarded]}" --table "${clone_table[guarded]}"
[[ $(dm_uuid "${clone[guarded]}") == "${uuid_clone[guarded]}" ]] || fail "L4c reopened UUID"
reopen_number=$(dm_devno "${clone[guarded]}")
[[ $(devno "/dev/mapper/${clone[guarded]}") == "$reopen_number" ]] || fail "L4c reopened node"
clone_block_id[guarded]="$reopen_number|$(diskseq "$reopen_number")"
guard_clone guarded
[[ $(progress "${clone[guarded]}") == 1/128:0 ]] || fail "L4c persisted progress"
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}" 0 "$write_offset"
check_block "/dev/mapper/${clone[guarded]}" 0x3c "$bytes" "${clone_block_id[guarded]}" "$write_offset" "$write_length"
check_block "/dev/mapper/${clone[guarded]}" 0 "$bytes" "${clone_block_id[guarded]}" "$tail_offset" "$((bytes-tail_offset))"
discard_block "/dev/mapper/${clone[guarded]}" "$((5*region_bytes))" "$region_bytes" eopnotsupp "$bytes" "${clone_block_id[guarded]}"
echo 'CASE=L4C_WRITE_FLUSH_REOPEN RESULT=PASS'

remove_dm_exact() {
    local name=$1 uuid=$2
    [[ $(dm_uuid "$name") == "$uuid" ]] || fail "refusing foreign mapper cleanup: $name"
    [[ $(dmsetup info -c --noheadings -o open "$name" | tr -d ' ') == 0 ]] || fail "open mapper at cleanup: $name"
    printf 'INTENT=REMOVE_DM NAME=%s UUID=%s\n' "$name" "$uuid"
    dmsetup remove "$name"
    local inventory
    inventory=$(dmsetup ls 2>&1) || fail "DM inventory failed after remove: $name"
    ! grep -q "^${name}[[:space:]]" <<<"$inventory" || fail "mapper remained after remove: $name"
}
detach_loop_exact() {
    local loop=$1 backing=$2 inode=$3 expected=$4 observed inventory detached=0 i
    observed=$(losetup -l -n -O BACK-FILE "$loop")
    [[ $observed == "$backing" ]] || fail "refusing foreign loop cleanup: $loop"
    [[ $(stat -Lc '%d:%i' "$backing") == "$inode" ]] || fail "backing inode changed: $loop"
    [[ $(loop_snapshot "$loop") == "$expected" ]] || fail "loop association identity changed: $loop"
    printf 'INTENT=DETACH_LOOP DEV=%s BACKING=%s\n' "$loop" "$backing"
    losetup -d "$loop"
    for i in $(seq 1 50); do
        inventory=$(losetup --list --noheadings --raw --output NAME,MAJ:MIN,BACK-FILE,BACK-INO,BACK-MAJ:MIN,OFFSET,SIZELIMIT,RO,PARTSCAN,LOG-SEC) \
            || fail "loop inventory failed after detach: $loop"
        if ! grep -q "^${loop}[[:space:]]" <<<"$inventory"; then
            detached=1
            printf 'DETACH_OBSERVED_ABSENT DEV=%s OBSERVATION=%s\n' "$loop" "$i"
            break
        fi
        sleep 0.1
    done
    [[ $detached -eq 1 ]] || fail "loop detach did not become terminal: $loop"
}
for role in hazard guarded; do
    remove_dm_exact "${clone[$role]}" "${uuid_clone[$role]}"
    remove_dm_exact "${zero[$role]}" "${uuid_zero[$role]}"
    detach_loop_exact "${meta_loop[$role]}" "${meta[$role]}" "${meta_inode[$role]}" "${meta_loop_id[$role]}"
    detach_loop_exact "${data_loop[$role]}" "${data[$role]}" "${data_inode[$role]}" "${data_loop_id[$role]}"
done
echo 'CLEANUP=PASS_FILES_RETAINED'
echo 'PRODUCTION_AUTHORIZED=NO'
echo 'SHARED_OWNER_QUALIFIED=NO'
echo 'ONLINE_RELOAD_QUALIFIED=NO'
echo 'POWER_LOSS_QUALIFIED=NO'
echo 'RESULT=PASS'
trap - EXIT
