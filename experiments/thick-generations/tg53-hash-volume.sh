#!/usr/bin/env bash
set -euo pipefail

volid=${1:?volume ID required}
bytes=${2:-1048576}
[[ "$bytes" =~ ^[1-9][0-9]*$ && "$bytes" -le 16777216 ]] || {
    echo "sample size must be between 1 and 16777216 bytes" >&2
    exit 64
}

path_file=$(mktemp /run/tg53-hash-volume.XXXXXX)
active=0
cleanup() {
    local rc=$? deactivate_rc=0
    trap - EXIT
    if (( active )); then
        perl -MPVE::Storage -e '
            my $cfg=PVE::Storage::config();
            PVE::Storage::deactivate_volumes($cfg,[$ARGV[0]]);
        ' "$volid" >/dev/null 2>&1 || deactivate_rc=$?
    fi
    rm -f -- "$path_file"
    if (( deactivate_rc != 0 )); then
        echo "VOLUME_DEACTIVATION=UNKNOWN rc=$deactivate_rc" >&2
        (( rc != 0 )) || rc=77
    fi
    exit "$rc"
}
trap cleanup EXIT

perl -MPVE::Storage -e '
    my ($volid) = @ARGV;
    my $cfg = PVE::Storage::config();
    PVE::Storage::activate_volumes($cfg, [$volid]);
    my $path = PVE::Storage::path($cfg, $volid);
    print "$path\n";
' "$volid" >"$path_file"
active=1

path=$(tail -n 1 "$path_file")
[[ "$path" == /dev/* ]] || {
    echo "resolved volume path is not a block device path" >&2
    exit 2
}
size=$(blockdev --getsize64 "$path")
(( size > 0 )) || {
    echo "resolved volume has zero size" >&2
    exit 2
}
(( bytes <= size )) || bytes=$size

middle=$(( (size - bytes) / 2 ))
end=$(( size - bytes ))
sample_hash() {
    local offset=$1
    dd if="$path" iflag=skip_bytes,count_bytes skip="$offset" count="$bytes" \
        status=none | sha256sum | awk '{print $1}'
}

printf 'VOLUME=%s\nPATH=%s\nSIZE=%s\nSAMPLE_BYTES=%s\n' \
    "$volid" "$path" "$size" "$bytes"
printf 'OFFSET_BEGIN=%s\nSHA256_BEGIN=%s\n' 0 "$(sample_hash 0)"
printf 'OFFSET_MIDDLE=%s\nSHA256_MIDDLE=%s\n' "$middle" "$(sample_hash "$middle")"
printf 'OFFSET_END=%s\nSHA256_END=%s\n' "$end" "$(sample_hash "$end")"
echo 'VOLUME_READ_WITNESS=PASS'
