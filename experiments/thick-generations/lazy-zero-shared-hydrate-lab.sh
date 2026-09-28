#!/bin/bash
# Feature-frozen physical shared-LVM lazy hydration/write/pivot qualification.
set -euo pipefail
umask 077

host='' storeid='' vg='' tx='' nonce='' data_uuid='' meta_uuid='' before='' fault_mode=none
data_gib='' hydration_budget_sec=''
while (($#)); do
    case $1 in
        --expect-host) host=${2:-}; shift 2 ;;
        --storeid) storeid=${2:-}; shift 2 ;;
        --vg) vg=${2:-}; shift 2 ;;
        --tx) tx=${2:-}; shift 2 ;;
        --nonce) nonce=${2:-}; shift 2 ;;
        --data-uuid) data_uuid=${2:-}; shift 2 ;;
        --meta-uuid) meta_uuid=${2:-}; shift 2 ;;
        --before) before=${2:-}; shift 2 ;;
        --fault-mode) fault_mode=${2:-}; shift 2 ;;
        --data-gib) data_gib=${2:-}; shift 2 ;;
        --hydration-budget-sec) hydration_budget_sec=${2:-}; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
[[ $(hostname) == "$host" && $storeid =~ ^[A-Za-z0-9_.-]+$ && $vg =~ ^[A-Za-z0-9+_.-]+$ \
    && $tx =~ ^[0-9a-f]{32}$ && $nonce =~ ^[0-9a-f]{32}$ \
    && $data_uuid =~ ^[A-Za-z0-9-]+$ && $meta_uuid =~ ^[A-Za-z0-9-]+$ \
    && $data_gib =~ ^([1-9]|[1-9][0-9]|[1-4][0-9]{2}|5(0[0-9]|1[0-2]))$ \
    && $hydration_budget_sec =~ ^[1-9][0-9]{1,4}$ \
    && $hydration_budget_sec -ge 60 && $hydration_budget_sec -le 21600 \
    && ( $fault_mode == none || $fault_mode == controller-kill-after-partial \
        || $fault_mode == controlled-reboot-prepare ) ]] || exit 64
[[ $fault_mode != controlled-reboot-prepare || $before =~ ^[0-9a-f]{32}$ ]] || exit 64
[[ $fault_mode == none || $data_gib == 8 ]] || {
    echo "scale fault recovery is not yet geometry-qualified" >&2
    exit 64
}
for tool in awk blockdev cat chmod cp dirname dmsetup grep head kill lsblk lvs mkdir mv perl pvs python3 sed seq sha256sum sleep stat sync tail tee timeout touch tr uname vgs; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

bytes=$((data_gib * 1024 * 1024 * 1024))
sectors=$((bytes / 512))
region_sectors=2048
total_regions=$((bytes / 1048576))
meta_bytes=268435456
((bytes == sectors * 512 && bytes == total_regions * 1048576)) || exit 64
data_name="sltlz-d-$nonce"
meta_name="sltlz-m-$nonce"
data="/dev/$vg/$data_name"
meta="/dev/$vg/$meta_name"
zero="slt-lz-zero-$nonce"
delay="slt-lz-delay-$nonce"
clone="slt-lz-clone-$nonce"
zero_uuid="SLT-LZ-ZERO-$nonce"
delay_uuid="SLT-LZ-DELAY-$nonce"
clone_uuid="SLT-LZ-CLONE-$nonce"
root="/var/tmp/slt-lazy-shared-$tx"
[[ ! -e $root ]] || { echo "evidence root exists" >&2; exit 2; }
mkdir -m 0700 "$root"
evidence="$root/evidence.log"
exec > >(tee -a "$evidence") 2>&1
on_exit() {
    local exit_rc=$?
    if ((exit_rc)); then
        echo "RESULT=UNKNOWN_RETAIN_LVS_INTENT_AND_EXACT_DM_GRAPH"
        echo "EXIT=$exit_rc"
    fi
}
trap on_exit EXIT

fail() { echo "FAIL=$*" >&2; exit 2; }
lv_field() { lvs --readonly --noheadings --separator '|' -o "$2" "$vg/$1" | tr -d '[:space:]'; }
[[ $(lv_field "$data_name" lv_uuid) == "$data_uuid" ]] || fail "data UUID"
[[ $(lv_field "$meta_name" lv_uuid) == "$meta_uuid" ]] || fail "metadata UUID"
[[ $(blockdev --getsize64 "$data") -eq $bytes ]] || fail "data size"
[[ $(blockdev --getsize64 "$meta") -eq $meta_bytes ]] || fail "metadata size"
for run_lv in "$data_name" "$meta_name"; do
    tags=",$(lv_field "$run_lv" lv_tags),"
    [[ $tags == *,slt_lazy_lab_v1,* && $tags == *,slt_lazy_tx_$tx,* \
        && $tags == *,slt_lazy_bytes_$bytes,* ]] || fail "owned geometry tag $run_lv"
    [[ $(tr ',' '\n' <<<"$tags" | grep -c '^slt_lazy_bytes_') -eq 1 ]] \
        || fail "ambiguous geometry tag $run_lv"
done
intent=$(vgs --readonly --noheadings -o vg_tags "$vg" | tr -d '[:space:]')
[[ $intent == *"slt_tg_vgi_tx=$tx"* && $intent == *"slt_tg_vgi_object=lazy-$nonce"* \
    && $intent == *"slt_tg_vgi_op=DM_PIVOT"* ]] || fail "exact VG intent absent"
for run_mapper in "$zero" "$delay" "$clone"; do
    ! dmsetup info "$run_mapper" >/dev/null 2>&1 || fail "run mapper already exists: $run_mapper"
done

devno() { lsblk -dn -o MAJ:MIN "$1" | tr -d '[:space:]'; }
diskseq() { cat "/sys/dev/block/$1/diskseq"; }
dm_uuid() { dmsetup info -c --noheadings -o uuid "$1" | tr -d '[:space:]'; }
dm_table() { dmsetup table "$1" | sed 's/[[:space:]]*$//'; }
status() { dmsetup status --noflush "$clone"; }
wait_open_zero() {
    local name=$1 open_value
    for _ in $(seq 1 50); do
        open_value=$(timeout --foreground --kill-after=2s 5s \
            dmsetup info -c --noheadings -o open "$name" | tr -d ' ') \
            || fail "cleanup open-count inventory $name"
        [[ $open_value =~ ^[0-9]+$ ]] || fail "malformed cleanup open count $name"
        ((open_value == 0)) && return 0
        sleep 0.1
    done
    fail "cleanup open count did not settle $name"
}
progress_fields() {
    local value=$1
    awk -v sectors="$sectors" -v region="$region_sectors" -v total="$total_regions" '
        NF >= 9 && $1 == 0 && $2 == sectors && $3 == "clone" &&
        $4 ~ /^[0-9]+$/ && $5 ~ /^[0-9]+\/[0-9]+$/ && $6 == region &&
        $7 ~ /^[0-9]+\/[0-9]+$/ && $8 ~ /^[0-9]+$/ && $NF == "rw" {
            split($5, m, "/"); split($7, p, "/");
            if (m[1] <= m[2] && p[1] <= p[2] && p[2] == total && p[1] + $8 <= total)
                print p[1], p[2], $8
        }' <<<"$value"
}
metadata_fields() {
    local value=$1
    awk -v sectors="$sectors" -v region="$region_sectors" '
        NF >= 9 && $1 == 0 && $2 == sectors && $3 == "clone" &&
        $5 ~ /^[0-9]+\/[0-9]+$/ && $6 == region && $NF == "rw" {
            split($5, m, "/");
            if (m[1] > 0 && m[1] <= m[2]) print m[1], m[2], $NF
        }' <<<"$value"
}
json_sha() { sed -n 's/.*"sha256":"\([0-9a-f]\{64\}\)".*/\1/p'; }
io="$(dirname "$0")/lazy-zero-live-io.py"
[[ -f $io && ! -L $io ]] || fail "unsafe I/O helper"

vg_uuid_raw=$(vgs --readonly --noheadings -o vg_uuid "$vg" | tr -d '[:space:]')
vg_uuid=${vg_uuid_raw//-/}
mapfile -t pv_records < <(pvs --readonly --noheadings --separator '|' \
    -o pv_uuid,pv_name --select "vg_name=$vg")
[[ ${#pv_records[@]} -eq 1 ]] || fail "shared VG must have one exact PV for this lab"
IFS='|' read -r pv_uuid pv_device <<<"${pv_records[0]}"
pv_uuid=${pv_uuid//[[:space:]]/}; pv_device=${pv_device//[[:space:]]/}
[[ $pv_uuid =~ ^[A-Za-z0-9-]+$ && $pv_device =~ ^/dev/mapper/[0-9A-Fa-f]+$ ]] \
    || fail "shared VG PV identity"
wwid=${pv_device##*/}
expected_data_dm_uuid="LVM-${vg_uuid}${data_uuid//-/}"
expected_meta_dm_uuid="LVM-${vg_uuid}${meta_uuid//-/}"
[[ $(dm_uuid "$data") == "$expected_data_dm_uuid" ]] || fail "data block node is not exact LVM UUID"
[[ $(dm_uuid "$meta") == "$expected_meta_dm_uuid" ]] || fail "metadata block node is not exact LVM UUID"
data_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$data" | tr -d ' ')
meta_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$meta" | tr -d ' ')
[[ $(devno "$data") == "$data_dev" && $(devno "$meta") == "$meta_dev" ]] || fail "LVM block node devno mismatch"
data_seq=$(diskseq "$data_dev"); meta_seq=$(diskseq "$meta_dev")
data_major=${data_dev%:*}; data_minor=${data_dev#*:}
meta_major=${meta_dev%:*}; meta_minor=${meta_dev#*:}
printf 'HOST=%s\nBOOT_ID=%s\nKERNEL=%s\nTX=%s\nNONCE=%s\nDATA_UUID=%s\nMETA_UUID=%s\nDATA_DEVNO=%s\nDATA_DISKSEQ=%s\nMETA_DEVNO=%s\nMETA_DISKSEQ=%s\n' \
    "$host" "$(cat /proc/sys/kernel/random/boot_id)" "$(uname -r)" "$tx" "$nonce" \
    "$data_uuid" "$meta_uuid" "$data_dev" "$data_seq" "$meta_dev" "$meta_seq"
printf 'DATA_GIB=%s\nDATA_BYTES=%s\nDATA_SECTORS=%s\nREGION_SECTORS=%s\nTOTAL_REGIONS=%s\nMETA_BYTES=%s\nHYDRATION_BUDGET_SEC=%s\n' \
    "$data_gib" "$bytes" "$sectors" "$region_sectors" "$total_regions" \
    "$meta_bytes" "$hydration_budget_sec"

echo 'INTENT=POISON_FULL_RAW_DESTINATION'
poison_result=$(python3 -I -B "$io" fill-block "$data" random 0 "$data_major" "$data_minor" "$data_seq" "$bytes")
echo "$poison_result"
poison_sha=$(json_sha <<<"$poison_result")
[[ $poison_sha =~ ^[0-9a-f]{64}$ ]] || fail "poison SHA result"
echo "POISON_SHA256=$poison_sha"
echo 'INTENT=INITIALIZE_FULL_METADATA'
meta_result=$(python3 -I -B "$io" fill-block "$meta" byte 0 "$meta_major" "$meta_minor" "$meta_seq" "$meta_bytes")
echo "$meta_result"

echo 'INTENT=CREATE_DELAY_ZERO_CLONE'
delay_table="0 $sectors delay $data_dev 0 5 $data_dev 0 5"
dmsetup create "$delay" --uuid "$delay_uuid" --table "$delay_table"
[[ $(dm_uuid "$delay") == "$delay_uuid" && $(dm_table "$delay") == "$delay_table" ]] || fail "delay identity/table"
delay_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$delay" | tr -d ' ')
zero_table="0 $sectors zero"
dmsetup create "$zero" --readonly --uuid "$zero_uuid" --table "$zero_table"
[[ $(dm_uuid "$zero") == "$zero_uuid" && $(dm_table "$zero") == "$zero_table" ]] || fail "zero identity/table"
zero_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$zero" | tr -d ' ')
clone_table="0 $sectors clone $meta_dev $delay_dev $zero_dev $region_sectors 2 no_hydration no_discard_passdown 2 hydration_threshold 1"
dmsetup create "$clone" --uuid "$clone_uuid" --table "$clone_table"
[[ $(dm_uuid "$clone") == "$clone_uuid" && $(dm_table "$clone") == "$clone_table" ]] || fail "clone identity/table"
clone_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$clone" | tr -d ' ')
clone_seq=$(diskseq "$clone_dev")
clone_major=${clone_dev%:*}; clone_minor=${clone_dev#*:}
queue="/sys/dev/block/$clone_dev/queue/discard_max_bytes"
[[ -f $queue ]] || fail "clone discard queue"
printf '0\n' > "$queue"
[[ $(cat "$queue") == 0 ]] || fail "discard guard"
initial_status=$(status)
read -r initial_hydrated initial_total initial_inflight <<<"$(progress_fields "$initial_status")"
read -r initial_meta_used initial_meta_total initial_mode <<<"$(metadata_fields "$initial_status")"
[[ ${initial_hydrated:-x} == 0 && ${initial_total:-x} == "$total_regions" && ${initial_inflight:-x} == 0 ]] \
    || fail "initial clone status: $initial_status"
[[ ${initial_meta_used:-x} =~ ^[0-9]+$ && ${initial_meta_total:-x} =~ ^[0-9]+$ \
    && $initial_mode == rw ]] || fail "initial clone metadata status: $initial_status"

echo 'INTENT=VERIFY_INITIAL_ZERO_VIEW_AND_POISON'
zero_sha=$(python3 - "$total_regions" <<'PY'
import hashlib
import sys
h=hashlib.sha256(); z=b'\0'*1048576
for _ in range(int(sys.argv[1])): h.update(z)
print(h.hexdigest())
PY
)
initial_frontend_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" "$clone_major" "$clone_minor" "$clone_seq" "$bytes" --progress-bytes 1073741824)
echo "$initial_frontend_result"
[[ $(json_sha <<<"$initial_frontend_result") == "$zero_sha" ]] || fail "initial frontend is not zero"
initial_raw_result=$(python3 -I -B "$io" direct-hash "$data" "$data_major" "$data_minor" "$data_seq" "$bytes" --progress-bytes 1073741824)
echo "$initial_raw_result"
[[ $(json_sha <<<"$initial_raw_result") == "$poison_sha" ]] || fail "initial read changed poisoned destination"

writer="$root/writer.py"
cp "$(dirname "$0")/lazy-zero-concurrent-writer.py" "$writer"
cp "$(dirname "$0")/lazy-zero-live-io.py" "$root/lazy-zero-live-io.py"
chmod 0700 "$writer"
expected="$root/expected.img"; start="$root/start"; journal="$root/writer.jsonl"
writer_count=1027
writer_extra=()
if [[ $fault_mode != none ]]; then
    writer_count=32
    writer_extra=(--progress-every 1 --write-delay-ms 20)
fi
python3 -I -B "$writer" --device "/dev/mapper/$clone" --major "$clone_major" --minor "$clone_minor" \
    --diskseq "$clone_seq" --size "$bytes" --expected "$expected" --start "$start" --journal "$journal" \
    --max-writes "$writer_count" "${writer_extra[@]}" &
writer_pid=$!
writer_start=$(awk '{print $22}' "/proc/$writer_pid/stat")
for _ in $(seq 1 200); do grep -q '"state":"READY"' "$journal" 2>/dev/null && break; sleep 0.05; done
grep -q '"state":"READY"' "$journal" || fail "writer readiness"
echo "WRITER_PID=$writer_pid WRITER_STARTTIME=$writer_start"

echo 'INTENT=ENABLE_HYDRATION_AND_RELEASE_WRITER'
dmsetup message "$clone" 0 enable_hydration
active_before_release=0
for _ in $(seq 1 200); do
    pre_release_status=$(status)
    read -r h t f <<<"$(progress_fields "$pre_release_status")"
    if [[ ${h:-x} =~ ^[0-9]+$ && ${t:-x} == "$total_regions" && ${f:-x} =~ ^[0-9]+$ ]] \
        && ((h > 0 && h < t && f > 0)); then
        active_before_release=1
        break
    fi
    sleep 0.01
done
[[ $active_before_release -eq 1 ]] || fail "hydration was not active/incomplete before writer release"
touch "$start"
overlap=0
deadline=$((SECONDS+hydration_budget_sec))
while kill -0 "$writer_pid" 2>/dev/null; do
    before_status=$(status)
    read -r before_h before_t before_f <<<"$(progress_fields "$before_status")"
    before_writes=$(sed -n 's/.*"writes":\([0-9][0-9]*\).*/\1/p' "$journal" | tail -n1); before_writes=${before_writes:-0}
    sleep 0.05
    after_writes=$(sed -n 's/.*"writes":\([0-9][0-9]*\).*/\1/p' "$journal" | tail -n1); after_writes=${after_writes:-0}
    after_status=$(status)
    read -r after_h after_t after_f <<<"$(progress_fields "$after_status")"
    if [[ ${before_h:-x} =~ ^[0-9]+$ && ${after_h:-x} =~ ^[0-9]+$ \
        && ${before_t:-x} == "$total_regions" && ${after_t:-x} == "$total_regions" \
        && ${before_f:-x} =~ ^[0-9]+$ && ${after_f:-x} =~ ^[0-9]+$ ]] \
        && ((before_h < before_t && after_h < after_t && before_f > 0 && after_f > 0 \
            && after_writes > before_writes)); then overlap=1; fi
    ((SECONDS < deadline)) || fail "writer/hydration overlap deadline"
done
wait "$writer_pid" || fail "foreground writer failed"
[[ $(awk '{print $22}' "/proc/$$/stat") =~ ^[0-9]+$ ]] || fail "controller identity"
[[ $overlap -eq 1 ]] || fail "concurrent foreground write was not observed during hydration"

if [[ $fault_mode == controller-kill-after-partial || $fault_mode == controlled-reboot-prepare ]]; then
    [[ $(tail -n1 "$journal") == '{"fsync":true,"state":"COMPLETE","writes":32}' ]] \
        || fail "writer durable completion record"
    journal_sha=$(sha256sum "$journal" | awk '{print $1}')
    echo 'INTENT=DISABLE_HYDRATION_FOR_CONTROLLER_CRASH_CHECKPOINT'
    dmsetup message "$clone" 0 disable_hydration
    partial_status=
    for _ in $(seq 1 600); do
        candidate=$(status)
        read -r partial_h partial_t partial_f <<<"$(progress_fields "$candidate")"
        if [[ ${partial_h:-x} =~ ^[0-9]+$ && ${partial_t:-x} == "$total_regions" \
            && ${partial_f:-x} == 0 ]] && ((partial_h > 0 && partial_h < partial_t)); then
            partial_status=$candidate
            break
        fi
        sleep 0.05
    done
    [[ -n $partial_status ]] || fail "no stable partial hydration checkpoint"
    timeout --foreground --kill-after=5s 60s blockdev --flushbufs "/dev/mapper/$clone"
    expected_sha=$(sha256sum "$expected" | awk '{print $1}')
    partial_frontend_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" \
        "$clone_major" "$clone_minor" "$clone_seq" "$bytes" --progress-bytes 1073741824)
    echo "$partial_frontend_result"
    partial_frontend_sha=$(json_sha <<<"$partial_frontend_result")
    [[ $partial_frontend_sha == "$expected_sha" ]] || fail "partial checkpoint SHA mismatch"
    controller_start=$(awk '{print $22}' "/proc/$$/stat")
    checkpoint_kind=CONTROLLER_CRASH
    checkpoint_phase=CONTROLLER_CRASH_READY
    manifest_tmp="$root/controller-crash.manifest.tmp"
    manifest="$root/controller-crash.manifest"
    if [[ $fault_mode == controlled-reboot-prepare ]]; then
        checkpoint_kind=CONTROLLED_REBOOT_PARTIAL_V1
        checkpoint_phase=CONTROLLED_REBOOT_PARTIAL_FLUSHED
        manifest_tmp="$root/controlled-reboot-partial.manifest.tmp"
        manifest="$root/controlled-reboot-partial.manifest"
        [[ $(lv_field "$data_name" lv_skip_activation) == skipactivation \
            && -z $(lv_field "$data_name" lv_autoactivation) \
            && $(lv_field "$meta_name" lv_skip_activation) == skipactivation \
            && -z $(lv_field "$meta_name" lv_autoactivation) ]] \
            || fail "controlled reboot persistent activation policy"
    fi
    [[ ! -e $manifest_tmp && ! -e $manifest ]] || fail "checkpoint manifest collision"
    {
        printf 'SCHEMA=1\nKIND=%s\nPHASE=%s\nHOST=%s\nBOOT_ID=%s\nKERNEL=%s\nTX=%s\nNONCE=%s\nVG=%s\nSTOREID=%s\n' \
            "$checkpoint_kind" "$checkpoint_phase" "$host" \
            "$(cat /proc/sys/kernel/random/boot_id)" "$(uname -r)" \
            "$tx" "$nonce" "$vg" "$storeid"
        [[ $fault_mode != controlled-reboot-prepare ]] || printf 'BEFORE=%s\n' "$before"
        printf 'DATA_UUID=%s\nMETA_UUID=%s\nCLONE_UUID=%s\nDELAY_UUID=%s\nZERO_UUID=%s\n' \
            "$data_uuid" "$meta_uuid" "$clone_uuid" "$delay_uuid" "$zero_uuid"
        printf 'VG_UUID=%s\nDATA_DM_UUID=%s\nMETA_DM_UUID=%s\n' \
            "$vg_uuid_raw" "$expected_data_dm_uuid" "$expected_meta_dm_uuid"
        printf 'PV_UUID=%s\nPV_DEVICE=%s\nWWID=%s\n' "$pv_uuid" "$pv_device" "$wwid"
        printf 'DATA_NAME=%s\nMETA_NAME=%s\nDATA_GIB=%s\nDATA_BYTES=%s\nDATA_SECTORS=%s\nMETA_BYTES=%s\nREGION_SECTORS=%s\nTOTAL_REGIONS=%s\nHYDRATION_BUDGET_SEC=%s\n' \
            "$data_name" "$meta_name" "$data_gib" "$bytes" "$sectors" \
            "$meta_bytes" "$region_sectors" "$total_regions" "$hydration_budget_sec"
        if [[ $fault_mode == controlled-reboot-prepare ]]; then
            printf 'DATA_SKIP_ACTIVATION=1\nMETA_SKIP_ACTIVATION=1\n'
            printf 'DATA_AUTOACTIVATION=0\nMETA_AUTOACTIVATION=0\n'
            printf 'DATA_TAGS=%s\nMETA_TAGS=%s\n' \
                "$(lv_field "$data_name" lv_tags)" "$(lv_field "$meta_name" lv_tags)"
        fi
        printf 'DATA_DEVNO=%s\nDATA_DISKSEQ=%s\nMETA_DEVNO=%s\nMETA_DISKSEQ=%s\n' \
            "$data_dev" "$data_seq" "$meta_dev" "$meta_seq"
        printf 'CLONE_DEVNO=%s\nCLONE_DISKSEQ=%s\nHYDRATED=%s\nTOTAL=%s\nINFLIGHT=%s\nWRITES=%s\n' \
            "$clone_dev" "$clone_seq" "$partial_h" "$partial_t" "$partial_f" "$writer_count"
        printf 'EXPECTED_SHA256=%s\nFRONTEND_SHA256=%s\nCLONE_STATUS=%s\n' \
            "$expected_sha" "$partial_frontend_sha" "$partial_status"
        printf 'WRITER_JOURNAL_SHA256=%s\n' "$journal_sha"
        printf 'CLONE_TABLE=%s\nDELAY_TABLE=%s\nZERO_TABLE=%s\n' \
            "$(dm_table "$clone")" "$(dm_table "$delay")" "$(dm_table "$zero")"
        printf 'CONTROLLER_PID=%s\nCONTROLLER_STARTTIME=%s\n' "$$" "$controller_start"
        printf 'RUNNER_SHA256=%s\nIO_HELPER_SHA256=%s\nWRITER_SHA256=%s\n' \
            "$(sha256sum "$0" | awk '{print $1}')" \
            "$(sha256sum "$io" | awk '{print $1}')" \
            "$(sha256sum "$writer" | awk '{print $1}')"
    } >"$manifest_tmp"
    chmod 0600 "$manifest_tmp"
    sync -f "$manifest_tmp"
    mv -n "$manifest_tmp" "$manifest"
    sync -f "$root"
    manifest_sha=$(sha256sum "$manifest" | awk '{print $1}')
    if [[ $fault_mode == controller-kill-after-partial ]]; then
        echo "CONTROLLER_CRASH_MANIFEST=$manifest"
        echo "CONTROLLER_CRASH_MANIFEST_SHA256=$manifest_sha"
        echo "RESULT=READY_TO_KILL_EXACT_CONTROLLER"
        trap - EXIT
        kill -KILL "$$"
        exit 99
    fi

    echo "CONTROLLED_REBOOT_PARTIAL_MANIFEST=$manifest"
    echo "CONTROLLED_REBOOT_PARTIAL_MANIFEST_SHA256=$manifest_sha"
    echo 'INTENT=CONTROLLED_REBOOT_REMOVE_EXACT_DM_GRAPH'
    for pair in "$clone:$clone_uuid" "$delay:$delay_uuid" "$zero:$zero_uuid"; do
        name=${pair%%:*}; uuid=${pair#*:}
        [[ $(dm_uuid "$name") == "$uuid" ]] || fail "controlled reboot cleanup UUID $name"
        wait_open_zero "$name"
        dmsetup remove "$name"
        inventory=$(dmsetup ls 2>&1) || fail "controlled reboot DM inventory after $name"
        ! grep -q "^${name}[[:space:]]" <<<"$inventory" || fail "controlled reboot mapper remained $name"
    done
    metadata_sha_result=$(python3 -I -B "$io" direct-hash "$meta" \
        "$meta_major" "$meta_minor" "$meta_seq" "$meta_bytes")
    echo "$metadata_sha_result"
    metadata_sha=$(json_sha <<<"$metadata_sha_result")
    [[ $metadata_sha =~ ^[0-9a-f]{64}$ ]] || fail "controlled reboot metadata SHA"

    lvm_helper="$(dirname "$0")/lazy-zero-shared-lvm-lab.pl"
    [[ -f $lvm_helper && ! -L $lvm_helper ]] || fail "unsafe controlled reboot LVM helper"
    perl -I/usr/share/perl5 "$lvm_helper" deactivate \
        --storeid "$storeid" --tx "$tx" --nonce "$nonce" --before "$before" \
        --data-uuid "$data_uuid" --meta-uuid "$meta_uuid" --data-gib "$data_gib"
    inventory=$(dmsetup info -c --noheadings -o name 2>&1 | tr -d ' ') \
        || fail "controlled reboot final kernel inventory"
    for forbidden in "$clone" "$delay" "$zero" \
        "${vg//-/--}-${data_name//-/--}" "${vg//-/--}-${meta_name//-/--}"; do
        ! grep -Fxq "$forbidden" <<<"$inventory" || fail "controlled reboot mapper survived: $forbidden"
    done

    ready_tmp="$root/controlled-reboot-ready.manifest.tmp"
    ready="$root/controlled-reboot-ready.manifest"
    [[ ! -e $ready_tmp && ! -e $ready ]] || fail "controlled reboot ready manifest collision"
    {
        printf 'SCHEMA=1\nKIND=CONTROLLED_REBOOT_READY_V1\nPHASE=CONTROLLED_REBOOT_READY\n'
        printf 'HOST=%s\nBOOT_ID=%s\nKERNEL=%s\nTX=%s\nNONCE=%s\nVG=%s\nSTOREID=%s\nBEFORE=%s\n' \
            "$host" "$(cat /proc/sys/kernel/random/boot_id)" "$(uname -r)" \
            "$tx" "$nonce" "$vg" "$storeid" "$before"
        printf 'DATA_UUID=%s\nMETA_UUID=%s\nPARTIAL_MANIFEST_SHA256=%s\nMETADATA_SHA256=%s\n' \
            "$data_uuid" "$meta_uuid" "$manifest_sha" "$metadata_sha"
        printf 'VG_UUID=%s\nPV_UUID=%s\nPV_DEVICE=%s\nWWID=%s\n' \
            "$vg_uuid_raw" "$pv_uuid" "$pv_device" "$wwid"
        printf 'DATA_SKIP_ACTIVATION=1\nMETA_SKIP_ACTIVATION=1\n'
        printf 'DATA_AUTOACTIVATION=0\nMETA_AUTOACTIVATION=0\n'
        printf 'EXPECTED_SHA256=%s\nWRITER_JOURNAL_SHA256=%s\nHYDRATED=%s\nTOTAL=%s\nINFLIGHT=%s\nWRITES=%s\n' \
            "$expected_sha" "$journal_sha" "$partial_h" "$partial_t" "$partial_f" "$writer_count"
        printf 'PREPARE_PID=%s\nPREPARE_STARTTIME=%s\n' "$$" "$controller_start"
    } >"$ready_tmp"
    chmod 0600 "$ready_tmp"
    sync -f "$ready_tmp"
    mv -n "$ready_tmp" "$ready"
    sync -f "$root"
    echo "CONTROLLED_REBOOT_READY_MANIFEST=$ready"
    echo "CONTROLLED_REBOOT_READY_MANIFEST_SHA256=$(sha256sum "$ready" | awk '{print $1}')"
    echo 'RESULT=CONTROLLED_REBOOT_PREPARE_EXIT0_READY_FOR_EXTERNAL_ADMISSION'
    trap - EXIT
    exit 0
fi

while :; do
    current=$(status)
    read -r hydrated total hydrating <<<"$(progress_fields "$current")"
    [[ ${hydrated:-x} =~ ^[0-9]+$ && ${total:-x} =~ ^[0-9]+$ && ${hydrating:-x} =~ ^[0-9]+$ ]] \
        || fail "malformed completion status"
    if ((hydrated == total_regions && total == total_regions && hydrating == 0)); then break; fi
    ((SECONDS < deadline)) || fail "hydration completion deadline"
    sleep 0.1
done
grep -q '"state":"COMPLETE"' "$journal" || fail "writer completion evidence"
read -r final_meta_used final_meta_total final_mode <<<"$(metadata_fields "$current")"
[[ ${final_meta_used:-x} =~ ^[0-9]+$ && ${final_meta_total:-x} =~ ^[0-9]+$ \
    && $final_mode == rw ]] || fail "final clone metadata status: $current"
printf 'METADATA_BLOCKS_USED=%s\nMETADATA_BLOCKS_TOTAL=%s\nMETADATA_MODE=%s\n' \
    "$final_meta_used" "$final_meta_total" "$final_mode"

echo 'INTENT=FULL_SHA_BEFORE_PIVOT'
expected_sha=$(sha256sum "$expected" | awk '{print $1}')
frontend_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" "$clone_major" "$clone_minor" "$clone_seq" "$bytes" --progress-bytes 1073741824)
destination_result=$(python3 -I -B "$io" direct-hash "$data" "$data_major" "$data_minor" "$data_seq" "$bytes" --progress-bytes 1073741824)
echo "$frontend_result"; echo "$destination_result"
frontend_sha=$(json_sha <<<"$frontend_result")
destination_sha=$(json_sha <<<"$destination_result")
[[ $frontend_sha == "$expected_sha" && $destination_sha == "$expected_sha" ]] || fail "pre-pivot SHA mismatch"
printf 'EXPECTED_SHA256=%s\nFRONTEND_SHA256=%s\nDESTINATION_SHA256=%s\nFINAL_CLONE_STATUS=%s\nOVERLAP=PASS\n' \
    "$expected_sha" "$frontend_sha" "$destination_sha" "$current"

[[ $(dmsetup info -c --noheadings -o open "$clone" | tr -d ' ') == 0 ]] || fail "clone open before pivot"
echo 'INTENT=VERIFIED_LINEAR_PIVOT'
dmsetup suspend "$clone"
suspended_status=$(status)
read -r suspended_h suspended_t suspended_f <<<"$(progress_fields "$suspended_status")"
[[ ${suspended_h:-x} == "$total_regions" && ${suspended_t:-x} == "$total_regions" && ${suspended_f:-x} == 0 ]] \
    || fail "completion drift while suspended"
linear_table="0 $sectors linear $data_dev 0"
dmsetup load "$clone" --table "$linear_table"
inactive=$(dmsetup table --inactive "$clone" | sed 's/[[:space:]]*$//')
[[ $inactive == "$linear_table" ]] || fail "inactive linear table"
dmsetup resume "$clone"
[[ $(dm_table "$clone") == "$linear_table" ]] || fail "active linear table"
deps=$(dmsetup deps "$clone")
[[ $deps == *"(${data_dev%:*}, ${data_dev#*:})"* ]] || fail "linear dependency"
pivot_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" "$clone_major" "$clone_minor" "$clone_seq" "$bytes" --progress-bytes 1073741824)
echo "$pivot_result"
pivot_sha=$(json_sha <<<"$pivot_result")
[[ $pivot_sha == "$expected_sha" ]] || fail "post-pivot SHA"
echo "PIVOT_SHA256=$pivot_sha"

echo 'INTENT=REMOVE_EXACT_DM_GRAPH'
for pair in "$clone:$clone_uuid" "$delay:$delay_uuid" "$zero:$zero_uuid"; do
    name=${pair%%:*}; uuid=${pair#*:}
    [[ $(dm_uuid "$name") == "$uuid" ]] || fail "cleanup UUID $name"
    wait_open_zero "$name"
    dmsetup remove "$name"
    inventory=$(dmsetup ls 2>&1) || fail "DM inventory after $name"
    ! grep -q "^${name}[[:space:]]" <<<"$inventory" || fail "mapper remained $name"
done
echo 'FULL_MATERIALIZATION=PASS'
echo 'VERIFIED_LINEAR_PIVOT=PASS'
echo 'DM_CLEANUP=PASS'
echo 'RESULT=PASS_LVS_AND_INTENT_RETAINED_FOR_EXACT_CLEANUP'
trap - EXIT
