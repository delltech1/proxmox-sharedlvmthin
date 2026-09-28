#!/bin/bash
# Recover one exact disposable lazy-zero graph after a controlled graceful reboot.
set -euo pipefail
umask 077

host='' storeid='' vg='' tx='' nonce='' data_uuid='' meta_uuid='' manifest_sha='' witness_sha='' admission_sha=''
while (($#)); do
    case $1 in
        --expect-host) host=${2:-}; shift 2 ;;
        --storeid) storeid=${2:-}; shift 2 ;;
        --vg) vg=${2:-}; shift 2 ;;
        --tx) tx=${2:-}; shift 2 ;;
        --nonce) nonce=${2:-}; shift 2 ;;
        --data-uuid) data_uuid=${2:-}; shift 2 ;;
        --meta-uuid) meta_uuid=${2:-}; shift 2 ;;
        --manifest-sha) manifest_sha=${2:-}; shift 2 ;;
        --witness-sha) witness_sha=${2:-}; shift 2 ;;
        --admission-sha) admission_sha=${2:-}; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
[[ $(hostname) == "$host" && $storeid =~ ^[A-Za-z0-9_.-]+$ && $vg =~ ^[A-Za-z0-9+_.-]+$ \
    && $tx =~ ^[0-9a-f]{32}$ && $nonce =~ ^[0-9a-f]{32}$ \
    && $data_uuid =~ ^[A-Za-z0-9-]+$ && $meta_uuid =~ ^[A-Za-z0-9-]+$ \
    && $manifest_sha =~ ^[0-9a-f]{64}$ && $witness_sha =~ ^[0-9a-f]{64}$ \
    && $admission_sha =~ ^[0-9a-f]{64}$ ]] || exit 64
for tool in awk blockdev cat chmod dmsetup grep lsblk lvs mkdir mv perl pvs python3 sed seq sha256sum sleep ssh sync tail tee timeout tr uname vgs; do
    command -v "$tool" >/dev/null || { echo "missing tool: $tool" >&2; exit 2; }
done

bytes=8589934592
sectors=16777216
region_sectors=2048
total_regions=8192
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
manifest="$root/controlled-reboot-ready.manifest"
partial="$root/controlled-reboot-partial.manifest"
witness="$root/controlled-reboot-prepare.observed"
admission="$root/controlled-reboot-admission.manifest"
expected="$root/expected.img"
journal="$root/writer.jsonl"
[[ -d $root && ! -L $root && -f $manifest && ! -L $manifest \
    && -f $partial && ! -L $partial && -f $expected && ! -L $expected \
    && -f $journal && ! -L $journal && -f $witness && ! -L $witness \
    && -f $admission && ! -L $admission ]] \
    || { echo "unsafe or missing recovery evidence" >&2; exit 2; }
[[ $(sha256sum "$manifest" | awk '{print $1}') == "$manifest_sha" ]] \
    || { echo "manifest SHA mismatch" >&2; exit 2; }
[[ $(sha256sum "$witness" | awk '{print $1}') == "$witness_sha" ]] \
    || { echo "witness SHA mismatch" >&2; exit 2; }
[[ $(sha256sum "$admission" | awk '{print $1}') == "$admission_sha" ]] \
    || { echo "admission SHA mismatch" >&2; exit 2; }
exec > >(tee -a "$root/controlled-reboot-recovery.log") 2>&1
on_exit() {
    local exit_rc=$?
    if ((exit_rc)); then
        echo "RESULT=UNKNOWN_RETAIN_LVS_INTENT_AND_EXACT_DM_GRAPH"
        echo "EXIT=$exit_rc"
    fi
}
trap on_exit EXIT

fail() { echo "FAIL=$*" >&2; exit 2; }
mf() {
    awk -v key="$1" '
        index($0, key "=") == 1 { n++; value=substr($0, length(key) + 2) }
        END { if (n != 1) exit 2; print value }
    ' "$manifest"
}
pf() {
    awk -v key="$1" '
        index($0, key "=") == 1 { n++; value=substr($0, length(key) + 2) }
        END { if (n != 1) exit 2; print value }
    ' "$partial"
}
wf() {
    awk -v key="$1" '
        index($0, key "=") == 1 { n++; value=substr($0, length(key) + 2) }
        END { if (n != 1) exit 2; print value }
    ' "$witness"
}
af() {
    awk -v key="$1" '
        index($0, key "=") == 1 { n++; value=substr($0, length(key) + 2) }
        END { if (n != 1) exit 2; print value }
    ' "$admission"
}
lv_field() {
    lvs --readonly --noheadings --units b --nosuffix --separator '|' \
        -o "$2" "$vg/$1" | tr -d '[:space:]'
}
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
json_sha() { sed -n 's/.*"sha256":"\([0-9a-f]\{64\}\)".*/\1/p'; }
script_dir=$(cd -- "$(dirname -- "$0")" && pwd -P)
lvm_helper="$script_dir/lazy-zero-shared-lvm-lab.pl"
[[ -f $lvm_helper && ! -L $lvm_helper ]] || fail "unsafe controlled reboot LVM helper"
establish_peer_hold() {
    local peer=$1 peer_boot=$2 output
    local peer_hosts="$root/peer-known-hosts"
    [[ -f $peer_hosts && ! -L $peer_hosts ]] || fail "unsafe peer host-key set"
    output=$(ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes \
        -o "UserKnownHostsFile=$peer_hosts" "root@$peer" bash -s -- \
        "$peer" "$peer_boot" "$host" "$new_boot" "$storeid" "$vg" "$tx" "$nonce" \
        "$data_name" "$meta_name" "$data_uuid" "$meta_uuid" \
        "$expected_data_dm_uuid" "$expected_meta_dm_uuid" \
        "$clone" "$delay" "$zero" "$clone_uuid" "$delay_uuid" "$zero_uuid" \
        "$lvm_helper" "$(mf BEFORE)" <<'PEER'
set -euo pipefail
peer=$1; expected_boot=$2; coordinator=$3; coordinator_boot=$4; storeid=$5; vg=$6
tx=$7; nonce=$8; data_name=$9; meta_name=${10}; data_uuid=${11}; meta_uuid=${12}
data_dm_uuid=${13}; meta_dm_uuid=${14}; clone=${15}; delay=${16}; zero=${17}
clone_uuid=${18}; delay_uuid=${19}; zero_uuid=${20}
helper=${21}; before=${22}
[[ $(hostname) == "$peer" && $(cat /proc/sys/kernel/random/boot_id) == "$expected_boot" ]] \
    || { echo PEER_IDENTITY_CHANGED >&2; exit 2; }
[[ -f $helper && ! -L $helper ]] || { echo PEER_HELPER_UNSAFE >&2; exit 2; }
hold_result=$(perl -I/usr/share/perl5 "$helper" hold --storeid "$storeid" --tx "$tx" \
    --nonce "$nonce" --before "$before" --data-uuid "$data_uuid" --meta-uuid "$meta_uuid" \
    --coordinator "$coordinator" --coordinator-boot "$coordinator_boot" --data-gib 8) \
    || { echo PEER_HOLD_INSTALL_FAILED >&2; exit 2; }
[[ $hold_result == *'"classification":"LAB_PEER_HOLD_INSTALLED_ABSENCE_PROVEN"'* \
    || $hold_result == *'"classification":"LAB_PEER_HOLD_RETAINED_EXACT_ABSENCE_PROVEN"'* ]] \
    || { echo PEER_HOLD_RESULT_MALFORMED >&2; exit 2; }

set +e
recovery=$(timeout --foreground --kill-after=5s 60s sharedlvmthin recovery-check "$storeid" 2>&1)
rc=$?
set -e
[[ $rc -eq 2 && $recovery == *$'VG_INTENT_CLEAR=FAIL\n'* \
    && $recovery == *$'STATE=RECOVERY_REQUIRED\n'* \
    && $recovery == *'OPEN DM_PIVOT intent blocks mutation'* ]] \
    || { echo PEER_RECOVERY_REFUSAL_MISSING >&2; exit 2; }
for gate in PATHS_HEALTHY WWID_MATCH PV_UUID_MATCH VG_UUID_MATCH POOL_FLAGS_HEALTHY \
    THICK_ANCHORS_HEALTHY THIN_REFERENCES_HEALTHY THIN_OWNER_STATE NO_RELEVANT_DSTATE \
    NO_RELEVANT_STORAGE_WORKER BOUNDED_LVM_PROBES PVE_STORAGE_HEALTH QUORUM; do
    grep -q "^${gate}=PASS$" <<<"$recovery" || { echo "PEER_GATE_FAILED=$gate" >&2; exit 2; }
done
lvs --readonly --noheadings --separator '|' \
    -o lv_name,lv_uuid,lv_skip_activation,lv_autoactivation \
    "$vg/$data_name" "$vg/$meta_name" | awk -F'|' \
    -v dn="$data_name" -v du="$data_uuid" -v mn="$meta_name" -v mu="$meta_uuid" '
function trim(v) { sub(/^[[:space:]]+/, "", v); sub(/[[:space:]]+$/, "", v); return v }
{
    if (NF != 4) exit 2
    n=trim($1); u=trim($2); s=trim($3); a=trim($4)
    if (s != "skip activation" || a != "") exit 2
    if (n == dn && u == du) d++
    else if (n == mn && u == mu) m++
    else exit 2
}
END { if (d != 1 || m != 1) exit 2 }
' || { echo PEER_LV_IDENTITY_POLICY >&2; exit 2; }
inventory=$(dmsetup info -c --noheadings --separator '|' -o name,uuid 2>&1) \
    || { echo PEER_DM_INVENTORY_UNKNOWN >&2; exit 2; }
compact=${inventory// /}
for identity in "$data_dm_uuid" "$meta_dm_uuid" "$clone_uuid" "$delay_uuid" "$zero_uuid"; do
    ! grep -q "|${identity}$" <<<"$compact" || { echo PEER_UUID_ACTIVE >&2; exit 2; }
done
for name in "$clone" "$delay" "$zero"; do
    ! grep -q "^${name}|" <<<"$compact" || { echo PEER_MAPPER_ACTIVE >&2; exit 2; }
done
echo PEER_HOLD_ACTIVE_EXACT_AUDIT_PASS
PEER
    ) || fail "peer hold/audit failed for $peer"
    [[ $output == PEER_HOLD_ACTIVE_EXACT_AUDIT_PASS ]] \
        || fail "peer hold/audit response malformed for $peer"
}
io="$(dirname "$0")/lazy-zero-live-io.py"
[[ -f $io && ! -L $io ]] || fail "unsafe I/O helper"

partial_sha=$(sha256sum "$partial" | awk '{print $1}')
old_boot=$(mf BOOT_ID); new_boot=$(cat /proc/sys/kernel/random/boot_id)
[[ $(mf SCHEMA) == 1 && $(mf KIND) == CONTROLLED_REBOOT_READY_V1 \
    && $(mf PHASE) == CONTROLLED_REBOOT_READY && $(mf HOST) == "$host" \
    && $old_boot =~ ^[0-9a-f-]{36}$ && $new_boot =~ ^[0-9a-f-]{36}$ \
    && $old_boot != "$new_boot" && $(mf KERNEL) == "$(uname -r)" \
    && $(mf TX) == "$tx" && $(mf NONCE) == "$nonce" && $(mf VG) == "$vg" \
    && $(mf STOREID) == "$storeid" && $(mf DATA_UUID) == "$data_uuid" \
    && $(mf META_UUID) == "$meta_uuid" && $(mf PARTIAL_MANIFEST_SHA256) == "$partial_sha" ]] \
    || fail "controlled reboot ready identity or new boot proof"
[[ $(pf SCHEMA) == 1 && $(pf KIND) == CONTROLLED_REBOOT_PARTIAL_V1 \
    && $(pf PHASE) == CONTROLLED_REBOOT_PARTIAL_FLUSHED && $(pf HOST) == "$host" \
    && $(pf BOOT_ID) == "$old_boot" && $(pf TX) == "$tx" && $(pf NONCE) == "$nonce" \
    && $(pf VG) == "$vg" && $(pf STOREID) == "$storeid" \
    && $(pf DATA_UUID) == "$data_uuid" && $(pf META_UUID) == "$meta_uuid" \
    && $(pf CLONE_UUID) == "$clone_uuid" && $(pf DELAY_UUID) == "$delay_uuid" \
    && $(pf ZERO_UUID) == "$zero_uuid" ]] || fail "partial manifest identity"
[[ $(wf SCHEMA) == 1 && $(wf EVENT) == CONTROLLED_REBOOT_PREPARE_EXIT0_OBSERVED \
    && $(wf READY_MANIFEST_SHA256) == "$manifest_sha" \
    && $(wf PARTIAL_MANIFEST_SHA256) == "$partial_sha" && $(wf WAIT_RESULT) == EXIT_0 \
    && $(wf PREPARE_PID) == "$(mf PREPARE_PID)" \
    && $(wf PREPARE_STARTTIME) == "$(mf PREPARE_STARTTIME)" ]] \
    || fail "independent controlled reboot prepare witness"
[[ $(af SCHEMA) == 1 && $(af KIND) == CONTROLLED_REBOOT_ADMISSION_V1 \
    && $(af HOST) == "$host" && $(af NEW_BOOT_ID) == "$new_boot" \
    && $(af READY_MANIFEST_SHA256) == "$manifest_sha" \
    && $(af WITNESS_SHA256) == "$witness_sha" && $(af QUORUM) == PASS \
    && $(af LOCAL_NO_RUNNING_GUESTS) == PASS && $(af LOCAL_NO_DSTATE) == PASS \
    && $(af LOCAL_NO_STORAGE_WORKERS) == PASS && $(af PEER_COUNT) == 2 \
    && $(af PEER_1_REACHABLE) == PASS && $(af PEER_1_LAB_ABSENT) == PASS \
    && $(af PEER_2_REACHABLE) == PASS && $(af PEER_2_LAB_ABSENT) == PASS ]] \
    || fail "controlled reboot host-wide/peer admission"
peer_1=$(af PEER_1_NAME); peer_2=$(af PEER_2_NAME)
[[ $peer_1 =~ ^[A-Za-z0-9_.-]+$ && $peer_2 =~ ^[A-Za-z0-9_.-]+$ \
    && $peer_1 != "$peer_2" && $peer_1 != "$host" && $peer_2 != "$host" \
    && $(af PEER_1_BOOT_ID) =~ ^[0-9a-f-]{36}$ \
    && $(af PEER_2_BOOT_ID) =~ ^[0-9a-f-]{36}$ ]] || fail "peer admission identity"
[[ $(mf WRITES) == 32 && $(mf TOTAL) == "$total_regions" && $(mf INFLIGHT) == 0 ]] \
    || fail "ready checkpoint counters"
[[ $(mf WRITER_JOURNAL_SHA256) =~ ^[0-9a-f]{64}$ \
    && $(sha256sum "$journal" | awk '{print $1}') == "$(mf WRITER_JOURNAL_SHA256)" \
    && $(tail -n1 "$journal") == '{"fsync":true,"state":"COMPLETE","writes":32}' ]] \
    || fail "writer journal durability evidence"
manifest_h=$(mf HYDRATED)
if [[ ! $manifest_h =~ ^[0-9]+$ ]] || ((manifest_h <= 0 || manifest_h >= total_regions)); then
    fail "manifest partial hydration"
fi
expected_sha=$(mf EXPECTED_SHA256)
[[ $expected_sha =~ ^[0-9a-f]{64}$ && $(pf FRONTEND_SHA256) == "$expected_sha" ]] \
    || fail "ready/partial SHA fields"
[[ $(stat -c %s "$expected") -eq $bytes && $(sha256sum "$expected" | awk '{print $1}') == "$expected_sha" ]] \
    || fail "expected-image identity"

[[ $(lv_field "$data_name" lv_uuid) == "$data_uuid" \
    && $(lv_field "$meta_name" lv_uuid) == "$meta_uuid" ]] \
    || fail "LV UUID"
[[ $(lv_field "$data_name" lv_size) == 8589934592 \
    && $(lv_field "$meta_name" lv_size) == 268435456 \
    && $(lv_field "$data_name" lv_skip_activation) == skipactivation \
    && -z $(lv_field "$data_name" lv_autoactivation) \
    && $(lv_field "$meta_name" lv_skip_activation) == skipactivation \
    && -z $(lv_field "$meta_name" lv_autoactivation) ]] || fail "LV size or activation policy"
data_tags=$(lv_field "$data_name" lv_tags); meta_tags=$(lv_field "$meta_name" lv_tags)
for tags in "$data_tags" "$meta_tags"; do
    [[ ,$tags, == *,slt_lazy_lab_v1,* && ,$tags, == *,slt_lazy_tx_$tx,* ]] \
        || fail "LV ownership tags"
done
[[ $data_tags == "$(pf DATA_TAGS)" && $meta_tags == "$(pf META_TAGS)" ]] \
    || fail "LV tag set drift"
vg_uuid_raw=$(vgs --readonly --noheadings -o vg_uuid "$vg" | tr -d '[:space:]')
vg_uuid=${vg_uuid_raw//-/}
expected_data_dm_uuid="LVM-${vg_uuid}${data_uuid//-/}"
expected_meta_dm_uuid="LVM-${vg_uuid}${meta_uuid//-/}"
[[ $(mf VG_UUID) == "$vg_uuid_raw" && $(pf DATA_DM_UUID) == "$expected_data_dm_uuid" \
    && $(pf META_DM_UUID) == "$expected_meta_dm_uuid" ]] || fail "manifest LVM identity"
mapfile -t pv_records < <(pvs --readonly --noheadings --separator '|' \
    -o pv_uuid,pv_name --select "vg_name=$vg")
[[ ${#pv_records[@]} -eq 1 ]] || fail "shared VG exact PV count"
IFS='|' read -r pv_uuid pv_device <<<"${pv_records[0]}"
pv_uuid=${pv_uuid//[[:space:]]/}; pv_device=${pv_device//[[:space:]]/}
[[ $pv_uuid == "$(mf PV_UUID)" && $pv_device == "$(mf PV_DEVICE)" \
    && ${pv_device##*/} == "$(mf WWID)" ]] || fail "new-boot PV/WWID identity"
intent=$(vgs --readonly --noheadings -o vg_tags "$vg" | tr -d '[:space:]')
[[ $intent == *"slt_tg_vgi_tx=$tx"* && $intent == *"slt_tg_vgi_object=lazy-$nonce"* \
    && $intent == *"slt_tg_vgi_op=DM_PIVOT"* ]] || fail "exact VG intent absent"

inventory=$(dmsetup info -c --noheadings --separator '|' -o name,uuid 2>&1) \
    || fail "pre-claim complete kernel inventory"
for forbidden in "$clone" "$delay" "$zero" \
    "${vg//-/--}-${data_name//-/--}" "${vg//-/--}-${meta_name//-/--}"; do
    ! grep -q "^${forbidden}|" <<<"${inventory// /}" || fail "unexpected pre-recovery mapper $forbidden"
done

# dmsetup only reports targets registered in the running kernel.  A reboot can
# leave the signed in-tree modules unloaded even though the exact same kernel
# supports them.  Establish target availability before claiming recovery,
# taking peer holds, or activating either LV; failure here must have no storage
# side effect and must never be repaired by a retry after activation.
for module in dm_clone dm_zero dm_delay; do
    timeout --foreground --kill-after=2s 15s modprobe "$module" \
        || fail "stock device-mapper module $module"
done
targets=$(dmsetup targets 2>&1) || fail "pre-claim device-mapper target inventory"
for target in clone zero delay; do
    grep -q "^${target} " <<<"$targets" || fail "missing stock target $target"
done

set +e
recovery_output=$(timeout --foreground --kill-after=5s 60s sharedlvmthin recovery-check "$storeid" 2>&1)
recovery_rc=$?
set -e
echo "$recovery_output"
[[ $recovery_rc -eq 2 ]] || fail "read-only recovery classification rc=$recovery_rc"
for gate in PATHS_HEALTHY WWID_MATCH PV_UUID_MATCH VG_UUID_MATCH POOL_FLAGS_HEALTHY \
    THICK_ANCHORS_HEALTHY THIN_REFERENCES_HEALTHY THIN_OWNER_STATE NO_RELEVANT_DSTATE \
    NO_RELEVANT_STORAGE_WORKER BOUNDED_LVM_PROBES PVE_STORAGE_HEALTH QUORUM; do
    grep -q "^${gate}=PASS$" <<<"$recovery_output" \
        || fail "read-only recovery classification unrelated failure: $gate"
done
if ! grep -q "^STORAGE_ID=${storeid}$" <<<"$recovery_output" \
    || ! grep -q '^VG_INTENT_CLEAR=FAIL$' <<<"$recovery_output" \
    || ! grep -q '^STATE=RECOVERY_REQUIRED$' <<<"$recovery_output" \
    || ! grep -q '^SAFE_FOR_MUTATION=NO$' <<<"$recovery_output" \
    || ! grep -q 'DETAIL=recovery-required: OPEN DM_PIVOT intent blocks mutation' <<<"$recovery_output"; then
    fail "open-intent read-only classification was not exact"
fi

claim="$root/reboot-recovery.claim"
mkdir -m 0700 "$claim" 2>/dev/null || fail "recovery already claimed; no automatic takeover"
claim_tmp="$claim/claim.tmp"
claim_file="$claim/claim"
{
    printf 'SCHEMA=1\nSTATE=CLAIMED\nMANIFEST_SHA256=%s\nWITNESS_SHA256=%s\nNEW_BOOT_ID=%s\n' \
        "$manifest_sha" "$witness_sha" "$new_boot"
    printf 'RECOVERY_PID=%s\nRECOVERY_STARTTIME=%s\n' "$$" "$(awk '{print $22}' "/proc/$$/stat")"
} >"$claim_tmp"
chmod 0600 "$claim_tmp"
sync -f "$claim_tmp"
mv -n "$claim_tmp" "$claim_file"
sync -f "$claim"

establish_peer_hold "$peer_1" "$(af PEER_1_BOOT_ID)"
establish_peer_hold "$peer_2" "$(af PEER_2_BOOT_ID)"

perl -I/usr/share/perl5 "$lvm_helper" activate --storeid "$storeid" --tx "$tx" \
    --nonce "$nonce" --before "$(mf BEFORE)" --data-uuid "$data_uuid" --meta-uuid "$meta_uuid" \
    --data-gib 8
[[ $(blockdev --getsize64 "$data") -eq $bytes && $(blockdev --getsize64 "$meta") -eq 268435456 ]] \
    || fail "activated LV size"
[[ $(dm_uuid "$data") == "$expected_data_dm_uuid" && $(dm_uuid "$meta") == "$expected_meta_dm_uuid" ]] \
    || fail "new-boot active LVM DM UUID"
data_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$data" | tr -d ' ')
meta_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$meta" | tr -d ' ')
data_seq=$(diskseq "$data_dev"); meta_seq=$(diskseq "$meta_dev")
data_major=${data_dev%:*}; data_minor=${data_dev#*:}
meta_major=${meta_dev%:*}; meta_minor=${meta_dev#*:}
metadata_result=$(python3 -I -B "$io" direct-hash "$meta" \
    "$meta_major" "$meta_minor" "$meta_seq" 268435456)
echo "$metadata_result"
[[ $(json_sha <<<"$metadata_result") == "$(mf METADATA_SHA256)" ]] \
    || fail "persisted clone metadata SHA mismatch"

expected_delay_table="0 $sectors delay $data_dev 0 5 $data_dev 0 5"
expected_zero_table="0 $sectors zero"
dmsetup create "$delay" --uuid "$delay_uuid" --table "$expected_delay_table"
delay_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$delay" | tr -d ' ')
dmsetup create "$zero" --readonly --uuid "$zero_uuid" --table "$expected_zero_table"
zero_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$zero" | tr -d ' ')
expected_clone_table="0 $sectors clone $meta_dev $delay_dev $zero_dev $region_sectors 2 no_hydration no_discard_passdown 2 hydration_threshold 1"
dmsetup create "$clone" --uuid "$clone_uuid" --table "$expected_clone_table"
[[ $(dm_uuid "$clone") == "$clone_uuid" && $(dm_uuid "$delay") == "$delay_uuid" \
    && $(dm_uuid "$zero") == "$zero_uuid" && $(dm_table "$delay") == "$expected_delay_table" \
    && $(dm_table "$zero") == "$expected_zero_table" && $(dm_table "$clone") == "$expected_clone_table" ]] \
    || fail "reconstructed mapper identity/table"
clone_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$clone" | tr -d ' ')
clone_seq=$(diskseq "$clone_dev"); clone_major=${clone_dev%:*}; clone_minor=${clone_dev#*:}
queue="/sys/dev/block/$clone_dev/queue/discard_max_bytes"
[[ -f $queue ]] || fail "reconstructed clone discard queue"
printf '0\n' >"$queue"
[[ $(cat "$queue") == 0 ]] || fail "reconstructed clone discard guard"
current=$(status)
read -r hydrated total inflight <<<"$(progress_fields "$current")"
[[ ${hydrated:-x} == "$manifest_h" && ${total:-x} == "$total_regions" \
    && ${inflight:-x} == 0 ]] || fail "reconstructed partial clone status"
before_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" \
    "$clone_major" "$clone_minor" "$clone_seq" "$bytes")
echo "$before_result"
[[ $(json_sha <<<"$before_result") == "$expected_sha" ]] || fail "new-boot partial frontend SHA"

echo 'INTENT=RESUME_HYDRATION_AFTER_CONTROLLED_REBOOT'
dmsetup message "$clone" 0 enable_hydration
deadline=$((SECONDS+900))
while :; do
    current=$(status)
    read -r hydrated total inflight <<<"$(progress_fields "$current")"
    [[ ${hydrated:-x} =~ ^[0-9]+$ && ${total:-x} == "$total_regions" && ${inflight:-x} =~ ^[0-9]+$ ]] \
        || fail "malformed completion status"
    if ((hydrated == total_regions && inflight == 0)); then break; fi
    ((SECONDS < deadline)) || fail "hydration completion deadline"
    sleep 0.1
done

[[ $(dm_uuid "$data") == "$expected_data_dm_uuid" \
    && $(dmsetup info -c --noheadings --separator ':' -o major,minor "$data" | tr -d ' ') == "$data_dev" \
    && $(devno "$data") == "$data_dev" && $(diskseq "$data_dev") == "$data_seq" ]] \
    || fail "data identity drift before SHA/pivot"
frontend_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" \
    "$clone_major" "$clone_minor" "$clone_seq" "$bytes")
destination_result=$(python3 -I -B "$io" direct-hash "$data" \
    "$data_major" "$data_minor" "$data_seq" "$bytes")
echo "$frontend_result"; echo "$destination_result"
[[ $(json_sha <<<"$frontend_result") == "$expected_sha" \
    && $(json_sha <<<"$destination_result") == "$expected_sha" ]] || fail "completed SHA"

[[ $(dmsetup info -c --noheadings -o open "$clone" | tr -d ' ') == 0 ]] || fail "clone open before pivot"
dmsetup suspend "$clone"
suspended=$(status)
read -r suspended_h suspended_t suspended_f <<<"$(progress_fields "$suspended")"
[[ ${suspended_h:-x} == "$total_regions" && ${suspended_t:-x} == "$total_regions" \
    && ${suspended_f:-x} == 0 ]] || fail "completion drift while suspended"
linear_table="0 $sectors linear $data_dev 0"
dmsetup load "$clone" --table "$linear_table"
[[ $(dmsetup table --inactive "$clone" | sed 's/[[:space:]]*$//') == "$linear_table" ]] \
    || fail "inactive linear table"
dmsetup resume "$clone"
[[ $(dm_table "$clone") == "$linear_table" ]] || fail "active linear table"
pivot_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" \
    "$clone_major" "$clone_minor" "$clone_seq" "$bytes")
echo "$pivot_result"
[[ $(json_sha <<<"$pivot_result") == "$expected_sha" ]] || fail "post-pivot SHA"

for pair in "$clone:$clone_uuid" "$delay:$delay_uuid" "$zero:$zero_uuid"; do
    name=${pair%%:*}; uuid=${pair#*:}
    [[ $(dm_uuid "$name") == "$uuid" ]] || fail "cleanup UUID $name"
    wait_open_zero "$name"
    dmsetup remove "$name"
    inventory=$(dmsetup ls 2>&1) || fail "DM inventory after $name"
    ! grep -q "^${name}[[:space:]]" <<<"$inventory" || fail "mapper remained $name"
done
echo 'CONTROLLED_REBOOT_PARTIAL_CLONE_RECOVERY=PASS'
echo "EXPECTED_SHA256=$expected_sha"
printf 'STATE=COMPLETE\nEXPECTED_SHA256=%s\n' "$expected_sha" >"$claim/complete.tmp"
chmod 0600 "$claim/complete.tmp"
sync -f "$claim/complete.tmp"
mv -n "$claim/complete.tmp" "$claim/complete"
sync -f "$claim"
echo 'RESULT=PASS_LVS_AND_INTENT_RETAINED_FOR_EXACT_CLEANUP'
trap - EXIT
