#!/bin/bash
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

# Functional test for the read-only remote Thin evidence helper.  The real
# dmsetup binary is replaced only inside a private mount namespace.  No LVM,
# device-mapper, PVE, or storage state is mutated.
set -euo pipefail
export LC_ALL=C

root="$(cd "$(dirname "$0")/.." && pwd)"
helper="$root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence"
mapper='test--vg-sltp--900001-tpool'
uuid='LVM-abcdef0123456789-tpool'
work="$(mktemp -d /var/tmp/slt-fix2-h3-alias.XXXXXX)"
trap 'rm -rf -- "$work"' EXIT

test -r "$helper"
test -x /usr/sbin/dmsetup
command -v unshare >/dev/null
command -v mount >/dev/null

cat >"$work/dmsetup" <<'EOF'
#!/bin/bash
set -eu
case "${1:-}" in
    ls)
        case "${SLT_H3_SCENARIO:-}" in
            absent)    printf 'unrelated-pool\t(253:10)\n' ;;
            canonical|mismatch) printf 'test--vg-sltp--900001-tpool\t(253:11)\n' ;;
            alias)     printf 'legacy-renamed-pool\t(253:12)\n' ;;
            *) exit 90 ;;
        esac
        ;;
    info)
        name="${*: -1}"
        case "${SLT_H3_SCENARIO:-}:$name" in
            absent:unrelated-pool) printf 'LVM-unrelated-tpool\n' ;;
            canonical:test--vg-sltp--900001-tpool) printf 'LVM-abcdef0123456789-tpool\n' ;;
            mismatch:test--vg-sltp--900001-tpool) printf 'LVM-wrongidentity-tpool\n' ;;
            alias:legacy-renamed-pool) printf 'LVM-abcdef0123456789-tpool\n' ;;
            *) exit 91 ;;
        esac
        ;;
    *) exit 92 ;;
esac
EOF
chmod 0755 "$work/dmsetup"

run_case() {
    local scenario="$1"
    unshare --mount --propagation private \
        env SLT_H3_SCENARIO="$scenario" \
        bash -c 'set -eu; mount --bind "$1" /usr/sbin/dmsetup; exec /bin/bash "$2" "$3" "$4"' \
        bash "$work/dmsetup" "$helper" "$mapper" "$uuid"
}

expected_absent="BASTRIX_REMOTE_THIN_V1|ABSENT|$mapper|$uuid"
expected_present="BASTRIX_REMOTE_THIN_V1|PRESENT|$mapper|$uuid"

test "$(run_case absent)" = "$expected_absent"
test "$(run_case canonical)" = "$expected_present"
test "$(run_case alias)" = "$expected_present"

set +e
mismatch_output="$(run_case mismatch 2>&1)"
mismatch_rc=$?
set -e
test "$mismatch_rc" -eq 73
test -z "$mismatch_output"

printf '%s\n' \
    'H3_CANONICAL_ABSENT=PASS' \
    'H3_CANONICAL_PRESENT=PASS' \
    'H3_UUID_ALIAS_PRESENT=PASS' \
    'H3_CANONICAL_UUID_MISMATCH_FAIL_CLOSED=PASS'
