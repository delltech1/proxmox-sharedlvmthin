#!/usr/bin/env bash
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

set -euo pipefail

usage() {
    echo "Usage: $0 <thin-storage-id> <thick-storage-id> <unused-disposable-vmid>" >&2
    exit 2
}

[[ $EUID -eq 0 ]] || { echo "ERROR: root privileges are required" >&2; exit 1; }
[[ $# -eq 3 ]] || usage
[[ ${SLT_DISPOSABLE_ACK:-} == I_ACCEPT_DESTRUCTIVE_DISPOSABLE_TEST ]] || {
    echo "ERROR: set SLT_DISPOSABLE_ACK=I_ACCEPT_DESTRUCTIVE_DISPOSABLE_TEST" >&2
    exit 1
}

thin_store=$1
thick_store=$2
vmid=$3
[[ $thin_store =~ ^[a-z][a-z0-9_-]+$ ]] || usage
[[ $thick_store =~ ^[a-z][a-z0-9_-]+$ ]] || usage
[[ $vmid =~ ^[1-9][0-9]{2,8}$ ]] || usage
[[ $thin_store != "$thick_store" ]] || { echo "ERROR: storage IDs must differ" >&2; exit 1; }

pvesm_alloc_volid() {
    local storage=$1 owner=$2 name=$3 size=$4 output volid
    output="$(pvesm alloc "$storage" "$owner" "$name" "$size" --format raw)"
    printf '%s\n' "$output" >&2
    volid="$(awk -F"'" '/^successfully created / { print $2 }' <<<"$output")"
    [[ $volid == "$storage:$name" ]] || {
        echo "ERROR: pvesm did not report the exact expected volume ID" >&2
        return 1
    }
    printf '%s\n' "$volid"
}

run_dir="/var/tmp/slt-same-vg-coexistence-$(date -u +%Y%m%dT%H%M%SZ)-$vmid"
umask 077
mkdir "$run_dir"
exec > >(tee -a "$run_dir/qualification.log") 2>&1

echo "RUN_DIR=$run_dir"
echo "START_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "THIN_STORAGE=$thin_store"
echo "THICK_STORAGE=$thick_store"
echo "VMID=$vmid"

if qm status "$vmid" >/dev/null 2>&1 || pct status "$vmid" >/dev/null 2>&1; then
    echo "ERROR: VMID $vmid is already allocated"
    exit 1
fi

config_info="$(perl -I/usr/share/perl5 \
    -MPVE::Storage \
    -MPVE::Storage::Custom::SharedLvmThinPlugin \
    -e '
        my ($thin_sid, $thick_sid) = @ARGV;
        my $cfg = PVE::Storage::config();
        my $class = "PVE::Storage::Custom::SharedLvmThinPlugin";
        my $thin = PVE::Storage::storage_config($cfg, $thin_sid);
        my $thick = PVE::Storage::storage_config($cfg, $thick_sid);
        die "first storage is not thin mode\n"
            if $class->_allocation_mode($thin) ne "thin";
        die "second storage is not Thick Generations mode\n"
            if $class->_allocation_mode($thick) ne "thick-generations";
        $class->_verify_same_vg_alias_configuration($thin_sid, $thin);
        $class->_verify_same_vg_alias_configuration($thick_sid, $thick);
        die "storage aliases do not resolve to one VG\n"
            if $thin->{"slt-vgname"} ne $thick->{"slt-vgname"};
        print join("|", $thin->{"slt-vgname"},
            $thin->{"slt-expected-wwid"}, $thin->{"slt-expected-vg-uuid"});
    ' "$thin_store" "$thick_store")"

IFS='|' read -r vg wwid vg_uuid <<<"$config_info"
[[ $vg =~ ^[A-Za-z0-9+_.-]+$ ]] || { echo "ERROR: unsafe VG identity"; exit 1; }
[[ $wwid =~ ^[0-9A-Fa-f]+$ ]] || { echo "ERROR: unsafe WWID identity"; exit 1; }
[[ $vg_uuid =~ ^[A-Za-z0-9-]+$ ]] || { echo "ERROR: unsafe VG UUID"; exit 1; }
device="/dev/mapper/$wwid"
[[ -b $device ]] || { echo "ERROR: pinned mapper is not a block device"; exit 1; }

sharedlvmthin recovery-check "$thin_store"
sharedlvmthin recovery-check "$thick_store"

lock_marker="$run_dir/thin-lock-held"
perl -I/usr/share/perl5 \
    -MPVE::Storage \
    -MPVE::Storage::Custom::SharedLvmThinPlugin \
    -e '
        my ($sid, $marker) = @ARGV;
        my $cfg = PVE::Storage::config();
        my $scfg = PVE::Storage::storage_config($cfg, $sid);
        my $class = "PVE::Storage::Custom::SharedLvmThinPlugin";
        $class->_with_vg_lock($sid, $scfg, sub {
            open(my $fh, ">", $marker) or die "cannot publish held-lock evidence: $!\n";
            print {$fh} "HELD\n";
            close($fh) or die "cannot close held-lock evidence: $!\n";
            sleep(4);
            return 1;
        }, q{/dev/mapper/} . $scfg->{q{slt-expected-wwid}});
    ' "$thin_store" "$lock_marker" &
holder_pid=$!
for _ in $(seq 1 50); do
    [[ -f $lock_marker ]] && break
    kill -0 "$holder_pid" 2>/dev/null || {
        wait "$holder_pid"
        echo "ERROR: thin alias lock holder exited before publishing evidence"
        exit 1
    }
    sleep 0.1
done
[[ -f $lock_marker ]] || { echo "ERROR: canonical lock holder was not observed"; exit 1; }
lock_wait_start="$(date +%s)"
perl -I/usr/share/perl5 \
    -MPVE::Storage \
    -MPVE::Storage::Custom::SharedLvmThinPlugin \
    -e '
        my ($sid) = @ARGV;
        my $cfg = PVE::Storage::config();
        my $scfg = PVE::Storage::storage_config($cfg, $sid);
        my $class = "PVE::Storage::Custom::SharedLvmThinPlugin";
        $class->_with_vg_lock($sid, $scfg, sub { return 1; },
            q{/dev/mapper/} . $scfg->{q{slt-expected-wwid}});
    ' "$thick_store"
lock_wait_end="$(date +%s)"
wait "$holder_pid"
lock_wait_seconds=$((lock_wait_end - lock_wait_start))
[[ $lock_wait_seconds -ge 2 ]] || {
    echo "ERROR: same-VG aliases did not contend on one canonical lock"
    exit 1
}
echo "SAME_VG_CANONICAL_LOCK_CONTENTION=PASS"

preexisting="$(lvs --readonly --devices "$device" --noheadings --separator '|' \
    -o lv_name,lv_tags "$vg")"
if grep -Eq "(^|[|,])(sltp-$vmid|vm-$vmid-|snap_vm-$vmid-|slt_tg_vol=vm-$vmid-)" \
        <<<"$preexisting"; then
    echo "ERROR: disposable VMID has pre-existing storage objects"
    exit 1
fi

# The first thin-pool creation in a VG can legitimately create or enlarge the
# VG-global LVM metadata spare. Stabilize that LVM-owned object before taking
# the leak-detection baseline; never classify it as a plugin-owned leftover.
warm_name="vm-$vmid-disk-99"
warm_vol="$(pvesm_alloc_volid "$thin_store" "$vmid" "$warm_name" 64M)"
[[ $warm_vol == "$thin_store:$warm_name" ]] || {
    echo "ERROR: unexpected warm-up volume ID"
    exit 1
}
pvesm free "$warm_vol"
warm_post="$(lvs --readonly --devices "$device" --noheadings --separator '|' \
    -o lv_name,lv_tags "$vg")"
if grep -Eq "(^|[|,])(sltp-$vmid|vm-$vmid-|snap_vm-$vmid-|slt_tg_vol=vm-$vmid-)" \
        <<<"$warm_post"; then
    echo "ERROR: warm-up allocation did not clean up exactly"
    exit 1
fi
echo "LVM_METADATA_SPARE_BASELINE_STABILIZED=PASS"

baseline_free="$(vgs --readonly --devices "$device" --noheadings --units b \
    --nosuffix -o vg_free "$vg" | xargs | cut -d. -f1)"
baseline_seqno="$(vgs --readonly --devices "$device" --noheadings \
    -o vg_seqno "$vg" | xargs)"
echo "BASELINE_VG_FREE_BYTES=$baseline_free"
echo "BASELINE_VG_SEQNO=$baseline_seqno"

thin_name="vm-$vmid-disk-0"
thick_name="vm-$vmid-disk-1"
thin_vol="$(pvesm_alloc_volid "$thin_store" "$vmid" "$thin_name" 64M)"
thick_vol="$(pvesm_alloc_volid "$thick_store" "$vmid" "$thick_name" 64M)"
[[ $thin_vol == "$thin_store:$thin_name" ]] || { echo "ERROR: unexpected thin volume ID"; exit 1; }
[[ $thick_vol == "$thick_store:$thick_name" ]] || { echo "ERROR: unexpected thick volume ID"; exit 1; }

pvesm list "$thin_store" --vmid "$vmid" >"$run_dir/thin-list.txt"
pvesm list "$thick_store" --vmid "$vmid" >"$run_dir/thick-list.txt"
grep -Fq "$thin_vol" "$run_dir/thin-list.txt"
if grep -Fq "$thick_vol" "$run_dir/thin-list.txt"; then
    echo "ERROR: thick volume leaked into thin inventory" >&2
    exit 1
fi
grep -Fq "$thick_vol" "$run_dir/thick-list.txt"
if grep -Fq "$thin_vol" "$run_dir/thick-list.txt"; then
    echo "ERROR: thin volume leaked into thick inventory" >&2
    exit 1
fi
echo "INVENTORY_ISOLATION=PASS"

qm create "$vmid" --name slt-coexistence-disposable --memory 256 \
    --scsihw virtio-scsi-single --scsi0 "$thin_vol" --scsi1 "$thick_vol"
qm snapshot "$vmid" coexistence-s1
qm disk resize "$vmid" scsi0 +64M
qm disk resize "$vmid" scsi1 +64M
qm rollback "$vmid" coexistence-s1 --start 0
qm delsnapshot "$vmid" coexistence-s1
echo "PVE_SNAPSHOT_RESIZE_ROLLBACK_DELETE=PASS"

qm destroy "$vmid" --destroy-unreferenced-disks 1 --purge 1

post="$(lvs --readonly --devices "$device" --noheadings --separator '|' \
    -o lv_name,lv_tags "$vg")"
if grep -Eq "(^|[|,])(sltp-$vmid|vm-$vmid-|snap_vm-$vmid-|slt_tg_vol=vm-$vmid-)" \
        <<<"$post"; then
    echo "ERROR: exact test objects remain after successful PVE cleanup"
    exit 1
fi

final_free="$(vgs --readonly --devices "$device" --noheadings --units b \
    --nosuffix -o vg_free "$vg" | xargs | cut -d. -f1)"
final_seqno="$(vgs --readonly --devices "$device" --noheadings \
    -o vg_seqno "$vg" | xargs)"
echo "FINAL_VG_FREE_BYTES=$final_free"
echo "FINAL_VG_SEQNO=$final_seqno"

if [[ $final_free != "$baseline_free" ]]; then
    echo "ERROR: VG free-space delta is non-zero; preserving evidence for review"
    exit 1
fi

echo "VG_FREE_BYTES_AFTER_COMPLETE_LIFECYCLE_DELTA=0"
echo "SAME_VG_LIVE_COEXISTENCE=PASS"
echo "PASS_UTC=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'PASS\n' >"$run_dir/result"
