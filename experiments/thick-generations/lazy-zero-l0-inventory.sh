#!/bin/bash
# Read-only lazy-zero kernel/tool availability inventory. Never loads modules.

set -euo pipefail

expected_host=
output=

usage() {
    cat <<'EOF'
usage: lazy-zero-l0-inventory.sh --expect-host EXACT [--output ABSOLUTE-PATH]

Collects exact running-kernel, dm-clone/dm-zero configuration, module hashes,
available targets and userspace tool versions. It does not create/reload a DM
table, load a module, inspect clone status, or change storage/configuration.
EOF
}

while (($#)); do
    case "$1" in
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --output) output=${2:-}; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

[[ -n "$expected_host" ]] || { usage >&2; exit 64; }
[[ "$(hostname)" == "$expected_host" ]] || {
    echo "exact hostname confirmation failed" >&2
    exit 2
}
if [[ -n "$output" && "$output" != /* ]]; then
    echo "output must be an absolute path" >&2
    exit 64
fi
for command in awk cat dmsetup grep hostname install mktemp modinfo sed sha256sum tr uname; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is unavailable: $command" >&2
        exit 2
    }
done

kernel=$(uname -r)
config="/boot/config-$kernel"
[[ -r "$config" && ! -L "$config" ]] || {
    echo "exact running-kernel configuration is unavailable or unsafe" >&2
    exit 2
}

temp=$(mktemp)
cleanup() { rm -f -- "$temp"; }
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

{
    echo 'SCHEMA=1'
    echo "HOST=$(hostname)"
    echo "BOOT_ID=$(sed -n '1p' /proc/sys/kernel/random/boot_id)"
    echo "KERNEL=$kernel"
    echo "KERNEL_RELEASE=$(uname -v)"
    echo "CONFIG_SHA256=$(sha256sum "$config" | awk '{print $1}')"
    for option in CONFIG_BLK_DEV_DM CONFIG_DM_CLONE CONFIG_DM_ZERO; do
        value=$(sed -n "s/^${option}=//p" "$config")
        [[ -n "$value" ]] || value='unset'
        echo "$option=$value"
    done
    for module in dm-clone dm-zero; do
        module_key=${module//-/_}
        module_key=${module_key^^}
        module_path=$(modinfo -n "$module" 2>/dev/null || true)
        if [[ -n "$module_path" && -f "$module_path" && ! -L "$module_path" ]]; then
            echo "MODULE_${module_key}_PATH=$module_path"
            echo "MODULE_${module_key}_SHA256=$(sha256sum "$module_path" | awk '{print $1}')"
            echo "MODULE_${module_key}_VERMAGIC=$(modinfo -F vermagic "$module" | sed -n '1p')"
        else
            echo "MODULE_${module_key}_PATH=UNAVAILABLE"
        fi
    done
    echo "DMSETUP_VERSION=$(dmsetup version 2>&1 | tr '\n' ';')"
    targets=$(dmsetup targets 2>&1)
    clone_target=$(printf '%s\n' "$targets" | awk '$1 == "clone" { print $2; exit }')
    zero_target=$(printf '%s\n' "$targets" | awk '$1 == "zero" { print $2; exit }')
    echo "TARGET_CLONE=${clone_target:-UNAVAILABLE}"
    echo "TARGET_ZERO=${zero_target:-UNAVAILABLE}"
    command -v qemu-system-x86_64 >/dev/null 2>&1 \
        && echo "QEMU_VERSION=$(qemu-system-x86_64 --version | sed -n '1p')" \
        || echo 'QEMU_VERSION=UNAVAILABLE'
    echo 'MUTATION_PERFORMED=NO'
} >"$temp"

verdict=READY_FOR_L1_DESIGN
for required in \
    '^CONFIG_DM_CLONE=m$' '^CONFIG_DM_ZERO=m$' \
    '^MODULE_DM_CLONE_PATH=/' '^MODULE_DM_ZERO_PATH=/' \
    '^TARGET_CLONE='; do
    grep -q "$required" "$temp" || verdict=BLOCKED
done
grep -q '^TARGET_CLONE=UNAVAILABLE$' "$temp" && verdict=BLOCKED
# An unloaded dm-zero module is recorded but never loaded by this collector.
# The first mutation harness needs separate approval to load it and must then
# rerun L0 before creating any table.
grep -q '^TARGET_ZERO=UNAVAILABLE$' "$temp" \
    && [[ "$verdict" != BLOCKED ]] \
    && verdict=RETEST_AFTER_EXPLICIT_DM_ZERO_LOAD
echo "VERDICT=$verdict" >>"$temp"

if [[ -n "$output" ]]; then
    install -m 0600 -- "$temp" "$output"
    echo "OUTPUT=$output"
    sha256sum "$output"
else
    cat "$temp"
fi

[[ "$verdict" == READY_FOR_L1_DESIGN ]] || exit 2
