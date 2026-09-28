#!/bin/bash
# Recover one exact disposable lazy-zero graph after the controller-only crash gate.
set -euo pipefail
umask 077

host='' storeid='' vg='' tx='' nonce='' data_uuid='' meta_uuid='' manifest_sha='' witness_sha=''
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
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
[[ $(hostname) == "$host" && $storeid =~ ^[A-Za-z0-9_.-]+$ && $vg =~ ^[A-Za-z0-9+_.-]+$ \
    && $tx =~ ^[0-9a-f]{32}$ && $nonce =~ ^[0-9a-f]{32}$ \
    && $data_uuid =~ ^[A-Za-z0-9-]+$ && $meta_uuid =~ ^[A-Za-z0-9-]+$ \
    && $manifest_sha =~ ^[0-9a-f]{64}$ && $witness_sha =~ ^[0-9a-f]{64}$ ]] || exit 64
for tool in awk blockdev cat chmod dmsetup grep lsblk lvs mkdir mv python3 sed seq sha256sum sleep sync tail tee timeout tr uname vgs; do
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
manifest="$root/controller-crash.manifest"
witness="$root/controller-crash.observed"
expected="$root/expected.img"
journal="$root/writer.jsonl"
[[ -d $root && ! -L $root && -f $manifest && ! -L $manifest && -f $expected && ! -L $expected \
    && -f $journal && ! -L $journal && -f $witness && ! -L $witness ]] \
    || { echo "unsafe or missing recovery evidence" >&2; exit 2; }
[[ $(sha256sum "$manifest" | awk '{print $1}') == "$manifest_sha" ]] \
    || { echo "manifest SHA mismatch" >&2; exit 2; }
[[ $(sha256sum "$witness" | awk '{print $1}') == "$witness_sha" ]] \
    || { echo "witness SHA mismatch" >&2; exit 2; }
exec > >(tee -a "$root/controller-crash-recovery.log") 2>&1
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
wf() {
    awk -v key="$1" '
        index($0, key "=") == 1 { n++; value=substr($0, length(key) + 2) }
        END { if (n != 1) exit 2; print value }
    ' "$witness"
}
lv_field() { lvs --readonly --noheadings --separator '|' -o "$2" "$vg/$1" | tr -d '[:space:]'; }
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
io="$(dirname "$0")/lazy-zero-live-io.py"
[[ -f $io && ! -L $io ]] || fail "unsafe I/O helper"

[[ $(mf SCHEMA) == 1 && $(mf PHASE) == CONTROLLER_CRASH_READY \
    && $(mf HOST) == "$host" && $(mf BOOT_ID) == "$(cat /proc/sys/kernel/random/boot_id)" \
    && $(mf TX) == "$tx" && $(mf NONCE) == "$nonce" && $(mf VG) == "$vg" \
    && $(mf STOREID) == "$storeid" && $(mf DATA_UUID) == "$data_uuid" \
    && $(mf META_UUID) == "$meta_uuid" && $(mf CLONE_UUID) == "$clone_uuid" \
    && $(mf DELAY_UUID) == "$delay_uuid" && $(mf ZERO_UUID) == "$zero_uuid" ]] \
    || fail "manifest identity"
[[ $(wf SCHEMA) == 1 && $(wf EVENT) == CONTROLLER_SIGKILL_OBSERVED \
    && $(wf MANIFEST_SHA256) == "$manifest_sha" && $(wf WAIT_SIGNAL) == SIGKILL \
    && $(wf CONTROLLER_PID) == "$(mf CONTROLLER_PID)" \
    && $(wf CONTROLLER_STARTTIME) == "$(mf CONTROLLER_STARTTIME)" ]] \
    || fail "independent controller crash witness"
controller_pid=$(mf CONTROLLER_PID)
controller_start=$(mf CONTROLLER_STARTTIME)
[[ $controller_pid =~ ^[0-9]+$ && $controller_start =~ ^[0-9]+$ ]] || fail "controller identity format"
if [[ -e /proc/$controller_pid ]]; then
    observed_start=$(awk '{print $22}' "/proc/$controller_pid/stat" 2>/dev/null || true)
    [[ $observed_start != "$controller_start" ]] || fail "original controller still exists"
fi
[[ $(mf WRITES) == 32 && $(mf TOTAL) == "$total_regions" && $(mf INFLIGHT) == 0 ]] \
    || fail "manifest checkpoint counters"
[[ $(mf WRITER_JOURNAL_SHA256) =~ ^[0-9a-f]{64}$ \
    && $(sha256sum "$journal" | awk '{print $1}') == "$(mf WRITER_JOURNAL_SHA256)" \
    && $(tail -n1 "$journal") == '{"fsync":true,"state":"COMPLETE","writes":32}' ]] \
    || fail "writer journal durability evidence"
manifest_h=$(mf HYDRATED)
if [[ ! $manifest_h =~ ^[0-9]+$ ]] || ((manifest_h <= 0 || manifest_h >= total_regions)); then
    fail "manifest partial hydration"
fi
expected_sha=$(mf EXPECTED_SHA256)
[[ $expected_sha =~ ^[0-9a-f]{64}$ && $(mf FRONTEND_SHA256) == "$expected_sha" ]] \
    || fail "manifest SHA fields"
[[ $(stat -c %s "$expected") -eq $bytes && $(sha256sum "$expected" | awk '{print $1}') == "$expected_sha" ]] \
    || fail "expected-image identity"

[[ $(lv_field "$data_name" lv_uuid) == "$data_uuid" && $(lv_field "$meta_name" lv_uuid) == "$meta_uuid" ]] \
    || fail "LV UUID"
[[ $(blockdev --getsize64 "$data") -eq $bytes && $(blockdev --getsize64 "$meta") -eq 268435456 ]] \
    || fail "LV size"
vg_uuid_raw=$(vgs --readonly --noheadings -o vg_uuid "$vg" | tr -d '[:space:]')
vg_uuid=${vg_uuid_raw//-/}
expected_data_dm_uuid="LVM-${vg_uuid}${data_uuid//-/}"
expected_meta_dm_uuid="LVM-${vg_uuid}${meta_uuid//-/}"
[[ $(mf VG_UUID) == "$vg_uuid_raw" && $(mf DATA_DM_UUID) == "$expected_data_dm_uuid" \
    && $(mf META_DM_UUID) == "$expected_meta_dm_uuid" ]] || fail "manifest LVM identity"
[[ $(dm_uuid "$data") == "$expected_data_dm_uuid" && $(dm_uuid "$meta") == "$expected_meta_dm_uuid" ]] \
    || fail "live LVM DM UUID"
data_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$data" | tr -d ' ')
meta_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$meta" | tr -d ' ')
[[ $data_dev == "$(mf DATA_DEVNO)" && $meta_dev == "$(mf META_DEVNO)" \
    && $(devno "$data") == "$data_dev" && $(devno "$meta") == "$meta_dev" \
    && $(diskseq "$data_dev") == "$(mf DATA_DISKSEQ)" \
    && $(diskseq "$meta_dev") == "$(mf META_DISKSEQ)" ]] || fail "LVM block incarnation"
intent=$(vgs --readonly --noheadings -o vg_tags "$vg" | tr -d '[:space:]')
[[ $intent == *"slt_tg_vgi_tx=$tx"* && $intent == *"slt_tg_vgi_object=lazy-$nonce"* \
    && $intent == *"slt_tg_vgi_op=DM_PIVOT"* ]] || fail "exact VG intent absent"

[[ $(dm_uuid "$clone") == "$clone_uuid" && $(dm_uuid "$delay") == "$delay_uuid" \
    && $(dm_uuid "$zero") == "$zero_uuid" ]] || fail "mapper UUID"
delay_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$delay" | tr -d ' ')
zero_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$zero" | tr -d ' ')
expected_delay_table="0 $sectors delay $data_dev 0 5 $data_dev 0 5"
expected_zero_table="0 $sectors zero"
expected_clone_table="0 $sectors clone $meta_dev $delay_dev $zero_dev $region_sectors 2 no_hydration no_discard_passdown 2 hydration_threshold 1"
[[ $(mf DELAY_TABLE) == "$expected_delay_table" && $(mf ZERO_TABLE) == "$expected_zero_table" \
    && $(mf CLONE_TABLE) == "$expected_clone_table" \
    && $(dm_table "$delay") == "$expected_delay_table" && $(dm_table "$zero") == "$expected_zero_table" \
    && $(dm_table "$clone") == "$expected_clone_table" ]] || fail "exact mapper roles/tables"
clone_dev=$(dmsetup info -c --noheadings --separator ':' -o major,minor "$clone" | tr -d ' ')
[[ $clone_dev == "$(mf CLONE_DEVNO)" && $(devno "/dev/mapper/$clone") == "$clone_dev" \
    && $(diskseq "$clone_dev") == "$(mf CLONE_DISKSEQ)" ]] || fail "surviving clone kernel identity"
clone_seq=$(diskseq "$clone_dev")
clone_major=${clone_dev%:*}; clone_minor=${clone_dev#*:}
queue="/sys/dev/block/$clone_dev/queue/discard_max_bytes"
[[ -f $queue && $(cat "$queue") == 0 ]] || fail "discard guard did not survive"
current=$(status)
read -r hydrated total inflight <<<"$(progress_fields "$current")"
[[ ${hydrated:-x} == "$manifest_h" && ${total:-x} == "$total_regions" && ${inflight:-x} == 0 \
    && $current == "$(mf CLONE_STATUS)" ]] || fail "partial clone status drift"

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

before_result=$(python3 -I -B "$io" direct-hash "/dev/mapper/$clone" \
    "$clone_major" "$clone_minor" "$clone_seq" "$bytes")
echo "$before_result"
[[ $(json_sha <<<"$before_result") == "$expected_sha" ]] || fail "post-crash frontend SHA"

claim="$root/recovery.claim"
mkdir -m 0700 "$claim" 2>/dev/null || fail "recovery already claimed; no automatic takeover"
claim_tmp="$claim/claim.tmp"
claim_file="$claim/claim"
{
    printf 'SCHEMA=1\nSTATE=CLAIMED\nMANIFEST_SHA256=%s\nWITNESS_SHA256=%s\n' \
        "$manifest_sha" "$witness_sha"
    printf 'RECOVERY_PID=%s\nRECOVERY_STARTTIME=%s\n' "$$" "$(awk '{print $22}' "/proc/$$/stat")"
} >"$claim_tmp"
chmod 0600 "$claim_tmp"
sync -f "$claim_tmp"
mv -n "$claim_tmp" "$claim_file"
sync -f "$claim"
echo 'INTENT=RESUME_HYDRATION_AFTER_CONTROLLER_CRASH'
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
    && $(devno "$data") == "$data_dev" && $(diskseq "$data_dev") == "$(mf DATA_DISKSEQ)" ]] \
    || fail "data identity drift before SHA/pivot"
data_seq=$(mf DATA_DISKSEQ); data_major=${data_dev%:*}; data_minor=${data_dev#*:}
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
echo 'CONTROLLER_CRASH_RECOVERY=PASS'
echo "EXPECTED_SHA256=$expected_sha"
printf 'STATE=COMPLETE\nEXPECTED_SHA256=%s\n' "$expected_sha" >"$claim/complete.tmp"
chmod 0600 "$claim/complete.tmp"
sync -f "$claim/complete.tmp"
mv -n "$claim/complete.tmp" "$claim/complete"
sync -f "$claim"
echo 'RESULT=PASS_LVS_AND_INTENT_RETAINED_FOR_EXACT_CLEANUP'
trap - EXIT
