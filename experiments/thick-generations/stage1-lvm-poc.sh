#!/bin/sh
# Disposable LVM-resident dm-clone PoC. It uses one unique file-backed loop PV
# and refuses every caller-supplied block device.
set -eu

run_id="$$"
work_dir="/var/tmp/slt-tg-lvm-${run_id}"
disk_img="$work_dir/pv.img"
loop_dev=""
vg="slt_tg_poc_${run_id}"
source_lv="source"
destination_lv="destination"
metadata_lv="metadata"
anchor_lv="anchor"
map_name="slt-tg-lvm-${run_id}"
map_uuid="SLT-TG-LVM-POC-${run_id}"

fail() {
    printf 'RESULT=FAIL\nREASON=%s\n' "$1" >&2
    exit 1
}

cleanup() {
    dmsetup remove "$map_name" >/dev/null 2>&1 || true
    if [ -n "$loop_dev" ]; then
        vgchange -an "$vg" >/dev/null 2>&1 || true
        vgremove -f "$vg" >/dev/null 2>&1 || true
        pvremove -ff -y "$loop_dev" >/dev/null 2>&1 || true
        losetup -d "$loop_dev" >/dev/null 2>&1 || true
    fi
    rm -f "$disk_img"
    rmdir "$work_dir" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

[ "$(id -u)" -eq 0 ] || fail "root required"
[ "$#" -eq 0 ] || fail "this PoC accepts no device arguments"
[ ! -e "$work_dir" ] || fail "work directory collision"
[ ! -e "/dev/mapper/$map_name" ] || fail "mapper collision"
vgs "$vg" >/dev/null 2>&1 && fail "VG collision"

mkdir -m 0700 "$work_dir"
truncate -s 512M "$disk_img"
loop_dev=$(losetup --find --show "$disk_img")
case "$loop_dev" in /dev/loop[0-9]*) ;; *) fail "unexpected loop device" ;; esac

modprobe dm_clone
pvcreate -y "$loop_dev" >/dev/null
vgcreate "$vg" "$loop_dev" >/dev/null
lvcreate -L 128M -n "$source_lv" --setactivationskip y "$vg" >/dev/null
lvcreate -L 128M -n "$destination_lv" --setactivationskip y "$vg" >/dev/null
lvcreate -L 32M -n "$metadata_lv" --setactivationskip y "$vg" >/dev/null
lvcreate -L 8M -n "$anchor_lv" --setactivationskip y "$vg" >/dev/null

lvchange -ay -K "$vg/$source_lv" "$vg/$destination_lv" "$vg/$metadata_lv" >/dev/null
dd if=/dev/urandom of="/dev/$vg/$source_lv" bs=1M count=128 conv=fsync status=none
dd if=/dev/zero of="/dev/$vg/$metadata_lv" bs=4M count=8 conv=fsync status=none
blockdev --flushbufs "/dev/$vg/$source_lv"
blockdev --flushbufs "/dev/$vg/$metadata_lv"
source_before=$(sha256sum "/dev/$vg/$source_lv" | awk '{print $1}')
source_uuid=$(lvs --noheadings -o lv_uuid "$vg/$source_lv" | tr -d '[:space:]')
destination_uuid=$(lvs --noheadings -o lv_uuid "$vg/$destination_lv" | tr -d '[:space:]')
metadata_uuid=$(lvs --noheadings -o lv_uuid "$vg/$metadata_lv" | tr -d '[:space:]')

lvchange -pr "$vg/$source_lv" >/dev/null
source_attr=$(lvs --noheadings -o lv_attr "$vg/$source_lv" | tr -d '[:space:]')
[ "$(printf '%s' "$source_attr" | cut -c2)" = r ] || fail "source LV is not read-only"
[ "$(printf '%s' "$source_attr" | cut -c10)" = k ] || fail "source activation-skip is not set"
for lv in "$destination_lv" "$metadata_lv" "$anchor_lv"; do
    attr=$(lvs --noheadings -o lv_attr "$vg/$lv" | tr -d '[:space:]')
    [ "$(printf '%s' "$attr" | cut -c10)" = k ] || fail "$lv activation-skip is not set"
done

sectors=$(blockdev --getsz "/dev/$vg/$source_lv")
dmsetup --verifyudev create "$map_name" --uuid "$map_uuid" --table \
    "0 $sectors clone /dev/$vg/$metadata_lv /dev/$vg/$destination_lv /dev/$vg/$source_lv 8 2 no_hydration no_discard_passdown"
[ "$(dmsetup info -c --noheadings -o uuid "$map_name" | tr -d '[:space:]')" = "$map_uuid" ] \
    || fail "mapper UUID mismatch"
[ "$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')" = "$source_before" ] \
    || fail "initial clone view differs from source"

printf 'LVM resident foreground write %s\n' "$run_id" \
    | dd of="/dev/mapper/$map_name" bs=4096 seek=4096 conv=notrunc,fsync status=none
expected_hash=$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')
[ "$(sha256sum "/dev/$vg/$source_lv" | awk '{print $1}')" = "$source_before" ] \
    || fail "read-only LVM source changed"

dmsetup remove "$map_name"
dmsetup --verifyudev create "$map_name" --uuid "$map_uuid" --table \
    "0 $sectors clone /dev/$vg/$metadata_lv /dev/$vg/$destination_lv /dev/$vg/$source_lv 8 2 no_hydration no_discard_passdown"
[ "$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')" = "$expected_hash" ] \
    || fail "persistent clone metadata did not reconstruct the visible data"

event_before=$(dmsetup info -c --noheadings -o events "$map_name" | tr -d '[:space:]')
dmsetup message "$map_name" 0 enable_hydration
status=$(dmsetup status --noflush "$map_name")
progress=$(printf '%s\n' "$status" | awk '{print $7}')
hydrating=$(printf '%s\n' "$status" | awk '{print $8}')
hydrated=${progress%/*}
total=${progress#*/}
if ! { [ "$total" -gt 0 ] && [ "$hydrated" -eq "$total" ] && [ "$hydrating" -eq 0 ]; }; then
    timeout --kill-after=5s 120s dmsetup wait "$map_name" "$event_before" \
        || fail "bounded hydration wait did not observe completion"
fi
status=$(dmsetup status --noflush "$map_name")
progress=$(printf '%s\n' "$status" | awk '{print $7}')
hydrating=$(printf '%s\n' "$status" | awk '{print $8}')
hydrated=${progress%/*}
total=${progress#*/}
if ! { [ "$total" -gt 0 ] && [ "$hydrated" -eq "$total" ] && [ "$hydrating" -eq 0 ]; }; then
    fail "hydration event did not produce an exact completed state"
fi

dmsetup load "$map_name" --table "0 $sectors linear /dev/$vg/$destination_lv 0"
inactive=$(dmsetup table --inactive "$map_name")
printf '%s\n' "$inactive" | grep -q ' linear ' || fail "inactive frontend is not linear"
dmsetup --verifyudev suspend --noflush "$map_name"
dmsetup --verifyudev resume "$map_name"
active=$(dmsetup table "$map_name")
printf '%s\n' "$active" | grep -q ' linear ' || fail "frontend is not linear after pivot"
[ "$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')" = "$expected_hash" ] \
    || fail "linear frontend data mismatch"

deps=$(dmsetup deps "$map_name" | tr -s '[:space:]' ' ')
destination_devno=$(lsblk -ndo MAJ:MIN "/dev/$vg/$destination_lv" | tr -d '[:space:]')
destination_major=${destination_devno%:*}
destination_minor=${destination_devno#*:}
printf '%s\n' "$deps" | grep -q "1 dependencies : ($destination_major, $destination_minor)" \
    || fail "linear frontend has a non-destination dependency"

lvchange -an "$vg/$source_lv" "$vg/$metadata_lv" >/dev/null
lvremove -f "$vg/$source_lv" "$vg/$metadata_lv" >/dev/null
[ "$(sha256sum "/dev/mapper/$map_name" | awk '{print $1}')" = "$expected_hash" ] \
    || fail "destination was not independent after source and metadata removal"

printf 'SOURCE_LV_UUID=%s\n' "$source_uuid"
printf 'DESTINATION_LV_UUID=%s\n' "$destination_uuid"
printf 'METADATA_LV_UUID=%s\n' "$metadata_uuid"
printf 'SOURCE_LV_READ_ONLY=PASS\n'
printf 'NO_AUTOACTIVATION=PASS\n'
printf 'PERSISTENT_REOPEN=PASS\n'
printf 'HYDRATION_COMPLETE=PASS\n'
printf 'LINEAR_PIVOT=PASS\n'
printf 'DESTINATION_ONLY_DEPENDENCY=PASS\n'
printf 'DESTINATION_INDEPENDENT=PASS\n'
printf 'DATA_INTEGRITY=PASS\n'
printf 'RESULT=PASS\n'
