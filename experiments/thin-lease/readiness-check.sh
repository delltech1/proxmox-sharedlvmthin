#!/bin/sh
# Read-only qualification probe for the experimental per-pool LeaseGuard.
# This script never installs packages, starts services, opens a lockspace,
# writes a lease, changes a watchdog, or activates an LV.
set -eu

failures=""

result() {
    printf '%s=%s\n' "$1" "$2"
}

fail() {
    failures="${failures}${failures:+,}$1"
    result "$1" FAIL
}

pass() {
    result "$1" PASS
}

if [ "$(id -u)" -eq 0 ]; then
    pass RUNNING_AS_ROOT
else
    fail RUNNING_AS_ROOT
fi

if [ -c /dev/watchdog ] || [ -c /dev/watchdog0 ]; then
    pass WATCHDOG_DEVICE_PRESENT
else
    fail WATCHDOG_DEVICE_PRESENT
fi

if command -v sanlock >/dev/null 2>&1; then
    pass SANLOCK_CLIENT_PRESENT
else
    fail SANLOCK_CLIENT_PRESENT
fi

if command -v wdmd >/dev/null 2>&1; then
    pass WDMD_PRESENT
else
    fail WDMD_PRESENT
fi

if command -v lvm >/dev/null 2>&1 &&
   lvm version 2>/dev/null | grep -q -- '--enable-lvmlockd-sanlock'; then
    pass LVM_SANLOCK_SUPPORT
else
    fail LVM_SANLOCK_SUPPORT
fi

if command -v pvecm >/dev/null 2>&1 &&
   pvecm status 2>/dev/null | grep -Eq '^Quorate:[[:space:]]+Yes$'; then
    pass PVE_QUORUM
else
    fail PVE_QUORUM
fi

if command -v dmsetup >/dev/null 2>&1; then
    pass DEVICE_MAPPER_PRESENT
else
    fail DEVICE_MAPPER_PRESENT
fi

if [ -r /etc/machine-id ] && [ -s /etc/machine-id ]; then
    pass UNIQUE_HOST_ID_SOURCE
else
    fail UNIQUE_HOST_ID_SOURCE
fi

result MUTATION_ATTEMPTED NO

if [ -n "$failures" ]; then
    result LEASEGUARD_READINESS NO
    result REASONS "$failures"
    exit 1
fi

result LEASEGUARD_READINESS YES
result REASONS none

