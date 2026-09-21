#!/bin/bash
# Disposable-lab package install/replacement gate. Dry-run is the default.

set -euo pipefail

execute=0
package=
expected_hash=
expected_host=

usage() {
    cat <<'EOF'
usage: package-profile-gate.sh --package /absolute/candidate.deb \
       --sha256 HEX --expect-host EXACT-HOSTNAME [--execute]

Without --execute this performs read-only validation only. The execute mode
installs exactly the supplied local .deb with dpkg; it never downloads
dependencies, changes repositories, reboots the host or advances another node.
EOF
}

while (($#)); do
    case "$1" in
        --package) package=${2:-}; shift 2 ;;
        --sha256) expected_hash=${2:-}; shift 2 ;;
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --execute) execute=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

[[ -n "$package" && -n "$expected_hash" && -n "$expected_host" ]] || {
    usage >&2
    exit 64
}
[[ "$package" = /* && -f "$package" && ! -L "$package" ]] || {
    echo "package must be an absolute path to a regular, non-symlink file" >&2
    exit 64
}
[[ "$expected_hash" =~ ^[0-9a-fA-F]{64}$ ]] || {
    echo "expected SHA-256 must contain exactly 64 hexadecimal characters" >&2
    exit 64
}
[[ "$(hostname)" == "$expected_host" ]] || {
    echo "exact hostname confirmation failed" >&2
    exit 2
}

for command in awk sha256sum dpkg dpkg-deb dpkg-query systemctl timeout; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is unavailable: $command" >&2
        exit 2
    }
done

actual_hash=$(sha256sum "$package" | awk '{print $1}')
[[ "${actual_hash,,}" == "${expected_hash,,}" ]] || {
    echo "candidate SHA-256 mismatch" >&2
    exit 2
}

target_name=$(dpkg-deb -f "$package" Package)
target_version=$(dpkg-deb -f "$package" Version)
target_arch=$(dpkg-deb -f "$package" Architecture)
case "$target_name" in
    pve-sharedlvmthin|pve-sharedlvmthin-thick) ;;
    *) echo "unexpected package identity: $target_name" >&2; exit 2 ;;
esac
[[ "$target_arch" == "all" ]] || {
    echo "unexpected package architecture: $target_arch" >&2
    exit 2
}
[[ -n "$target_version" ]] || {
    echo "candidate package version is empty" >&2
    exit 2
}

installed_name=
installed_version=
for candidate in pve-sharedlvmthin pve-sharedlvmthin-thick; do
    status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$candidate" 2>/dev/null || true)
    case "$status" in
        ii*)
            [[ -z "$installed_name" ]] || {
                echo "both mutually exclusive package profiles appear installed" >&2
                exit 2
            }
            installed_name=$candidate
            installed_version=$(dpkg-query -W -f='${Version}' "$candidate")
            ;;
    esac
done

if [[ -n "$installed_name" ]]; then
    if [[ "$installed_name" == "$target_name" ]]; then
        dpkg --compare-versions "$target_version" ge "$installed_version" || {
            echo "package downgrade is refused by the qualification gate" >&2
            exit 2
        }
    else
        [[ "$target_version" == "$installed_version" ]] || {
            echo "profile replacement requires identical package versions" >&2
            exit 2
        }
    fi

    timeout --foreground --kill-after=10 1800 sharedlvmthin upgrade-check
fi

if ! thick_units=$(systemctl list-units --all --no-legend \
    --state=active,activating,deactivating,failed \
    'pve-sharedlvmthin-tg-*' 2>/dev/null); then
    echo "transient Thick unit inventory is unavailable" >&2
    exit 2
fi
[[ -z "$thick_units" ]] || {
    echo "pending, active or failed Thick transaction unit blocks package work" >&2
    exit 2
}

if ! audit=$(dpkg --audit); then
    echo "dpkg database audit failed" >&2
    exit 2
fi
[[ -z "$audit" ]] || {
    echo "dpkg database reports unfinished or inconsistent package state" >&2
    exit 2
}
dpkg --no-act -i "$package"

echo "CANDIDATE_PACKAGE=$target_name"
echo "CANDIDATE_VERSION=$target_version"
echo "CANDIDATE_SHA256=$actual_hash"
if [[ -n "$installed_name" ]]; then
    echo "CURRENT_PACKAGE=$installed_name"
    echo "CURRENT_VERSION=$installed_version"
else
    echo "CURRENT_PACKAGE=none"
fi

if ((execute == 0)); then
    echo "RESULT=DRY_RUN_PASS"
    echo "No package, service, storage or reboot state was changed."
    exit 0
fi

[[ $EUID -eq 0 ]] || {
    echo "--execute requires root" >&2
    exit 2
}

dpkg -i "$package"

status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$target_name" 2>/dev/null || true)
[[ "$status" == ii* ]] || {
    echo "target package is not fully installed after dpkg" >&2
    exit 2
}
observed_version=$(dpkg-query -W -f='${Version}' "$target_name")
[[ "$observed_version" == "$target_version" ]] || {
    echo "installed package version does not match candidate" >&2
    exit 2
}
dpkg -V "$target_name"
timeout --foreground --kill-after=10 300 sharedlvmthin compat-check
timeout --foreground --kill-after=10 300 sharedlvmthin doctor --quick
timeout --foreground --kill-after=10 1800 sharedlvmthin upgrade-check

echo "RESULT=EXECUTE_PASS"
echo "Reboot was not performed. Reboot this one lab node under change control,"
echo "then verify package identity, storage health and guest I/O before advancing."
