#!/bin/bash
# Read-only proof that one package-qualified lab node crossed a real reboot.
set -euo pipefail

expected_host=
expected_profile=
expected_version=
previous_boot_id=
health_file=
doctor_file=
settle_runtime=0

cleanup() {
    [[ -z "$health_file" ]] || rm -f -- "$health_file"
    [[ -z "$doctor_file" ]] || rm -f -- "$doctor_file"
}
trap cleanup EXIT
trap 'cleanup; exit 130' HUP INT TERM

usage() {
    cat <<'EOF'
usage: package-post-reboot-gate.sh --expect-host EXACT-HOSTNAME \
       --expect-profile dual|thick-only --expect-version DEBIAN-VERSION \
       --previous-boot-id UUID [--settle-runtime]

By default this is read-only. --settle-runtime publishes only the new
boot-bound runtime receipt after every check passes. It never installs,
removes, restarts or reboots anything.
EOF
}

while (($#)); do
    case "$1" in
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --expect-profile) expected_profile=${2:-}; shift 2 ;;
        --expect-version) expected_version=${2:-}; shift 2 ;;
        --previous-boot-id) previous_boot_id=${2:-}; shift 2 ;;
        --settle-runtime) settle_runtime=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

[[ -n "$expected_host" && -n "$expected_profile" && \
   -n "$expected_version" && -n "$previous_boot_id" ]] || { usage >&2; exit 64; }
case "$expected_profile" in
    dual) expected_package=pve-sharedlvmthin ;;
    thick-only) expected_package=pve-sharedlvmthin-thick ;;
    *) echo "expected profile must be dual or thick-only" >&2; exit 64 ;;
esac
uuid_re='^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
[[ "$previous_boot_id" =~ $uuid_re ]] || {
    echo "previous boot ID must be a canonical UUID" >&2; exit 64;
}
[[ "$(hostname)" == "$expected_host" ]] || {
    echo "exact hostname confirmation failed" >&2; exit 2;
}
for command in grep dpkg dpkg-query mktemp python3 sed systemctl timeout; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is unavailable: $command" >&2; exit 2;
    }
done

boot_id_file=/proc/sys/kernel/random/boot_id
[[ -r "$boot_id_file" ]] || { echo "current kernel boot ID is unavailable" >&2; exit 2; }
current_boot_id=$(sed -n '1p' "$boot_id_file")
[[ "$current_boot_id" =~ $uuid_re ]] || {
    echo "current kernel boot ID is malformed" >&2; exit 2;
}
[[ "${current_boot_id,,}" != "${previous_boot_id,,}" ]] || {
    echo "reboot is unproven: the kernel boot ID did not change" >&2; exit 2;
}

installed_count=0
installed_package=
installed_version=
for candidate in pve-sharedlvmthin pve-sharedlvmthin-thick; do
    status=$(dpkg-query -W -f='${db:Status-Abbrev}' "$candidate" 2>/dev/null || true)
    case "$status" in
        ii*|hi*)
            installed_count=$((installed_count + 1))
            installed_package=$candidate
            installed_version=$(dpkg-query -W -f='${Version}' "$candidate")
            ;;
    esac
done
[[ "$installed_count" -eq 1 && "$installed_package" == "$expected_package" ]] || {
    echo "installed package does not match the expected exclusive profile" >&2; exit 2;
}
[[ "$installed_version" == "$expected_version" ]] || {
    echo "installed package version does not match the qualified candidate" >&2; exit 2;
}
if ! dpkg_audit=$(dpkg --audit); then
    echo "dpkg database audit failed after reboot" >&2
    exit 2
fi
[[ -z "$dpkg_audit" ]] || {
    echo "dpkg database reports unfinished or inconsistent package state after reboot" >&2
    exit 2
}
if ! verify_output=$(dpkg --verify-format=rpm --verify "$installed_package"); then
    echo "installed package integrity verification failed after reboot" >&2
    exit 2
fi
[[ -z "$verify_output" ]] || {
    echo "installed package files differ from their dpkg manifest after reboot" >&2
    exit 2
}

flavor_file=/usr/share/pve-sharedlvmthin/package-flavor
[[ -f "$flavor_file" && ! -L "$flavor_file" ]] || {
    echo "installed package flavor marker is missing or unsafe" >&2; exit 2;
}
observed_profile=$(sed -n '1p' "$flavor_file")
[[ "$observed_profile" == "$expected_profile" ]] || {
    echo "installed package flavor marker does not match expectation" >&2; exit 2;
}

if ! thick_units=$(systemctl list-units --all --no-legend \
    --state=active,activating,deactivating,failed 'pve-sharedlvmthin-tg-*' 2>/dev/null); then
    echo "transient Thick unit inventory is unavailable" >&2; exit 2;
fi
[[ -z "$thick_units" ]] || {
    echo "pending, active or failed Thick transaction exists after reboot" >&2; exit 2;
}

set +e
help_output=$(sharedlvmthin help 2>&1)
help_rc=$?
set -e
[[ $help_rc -eq 64 ]] || {
    echo "post-reboot CLI help returned unexpected status: $help_rc" >&2
    exit 2
}
grep -Fq 'thick-recover-prepare <storage-id> <volume>' <<<"$help_output" || {
    echo "installed CLI is missing Thick recovery commands" >&2; exit 2;
}
if [[ "$expected_profile" == dual ]]; then
    grep -Fq 'thin-adopt-owner-model <storage-id> <volume> ALL-NODES-INACTIVE' <<<"$help_output" || {
        echo "installed Dual CLI is missing Thin ownership commands" >&2; exit 2;
    }
    for required_path in /usr/sbin/sharedlvmthin-migrate-bridge \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd \
        /lib/systemd/system/pve-sharedlvmthin-thin-guard.service; do
        [[ -f "$required_path" && ! -L "$required_path" ]] || {
            echo "installed Dual profile is missing required payload: $required_path" >&2; exit 2;
        }
    done
else
    ! grep -Fq 'thin-adopt-owner-model' <<<"$help_output" || {
        echo "installed Thick-only CLI unexpectedly exposes Thin commands" >&2; exit 2;
    }
    for excluded_path in \
        /lib/systemd/system/pve-sharedlvmthin-thin-guard.service \
        /usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-metadata-check \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-import \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-admission \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-plan \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-topology \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check \
        /usr/share/perl5/PVE/SharedLvmThinGuard.pm \
        /usr/share/perl5/PVE/SharedLvmThinGuardClient.pm \
        /usr/share/perl5/PVE/SharedLvmThinGuardEngine.pm \
        /usr/share/perl5/PVE/SharedLvmThinGuardInventory.pm \
        /usr/share/perl5/PVE/SharedLvmThinGuardProtocol.pm \
        /usr/share/perl5/PVE/SharedLvmThinGuardState.pm \
        /usr/share/perl5/PVE/SharedLvmThinMobility.pm \
        /usr/share/perl5/PVE/SharedLvmThinPeerAudit.pm \
        /usr/share/perl5/PVE/SharedLvmThinRelay.pm \
        /usr/share/perl5/PVE/SharedLvmThinWatchdog.pm \
        /usr/sbin/sharedlvmthin-migrate-bridge; do
        [[ ! -e "$excluded_path" && ! -L "$excluded_path" ]] || {
            echo "installed Thick-only profile retains excluded payload: $excluded_path" >&2; exit 2;
        }
    done
    if ! thin_guard_state=$(systemctl show --property ActiveState --value \
        pve-sharedlvmthin-thin-guard.service 2>/dev/null); then
        echo "Thick-only post-reboot gate cannot prove ThinGuard runtime state" >&2
        exit 2
    fi
    case "$thin_guard_state" in
        inactive|failed) ;;
        *) echo "Thick-only post-reboot gate found active ThinGuard state: $thin_guard_state" >&2; exit 2 ;;
    esac
    if systemctl is-enabled --quiet pve-sharedlvmthin-thin-guard.service; then
        echo "Thick-only post-reboot gate found enabled ThinGuard state" >&2
        exit 2
    fi
fi

timeout --foreground --kill-after=10 300 sharedlvmthin compat-check
doctor_file=$(mktemp)
set +e
timeout --foreground --kill-after=10 300 sharedlvmthin doctor --quick \
    > >(tee "$doctor_file") 2>&1
doctor_rc=$?
set -e
if [[ $doctor_rc -ne 0 && $doctor_rc -ne 1 ]] \
    || ! grep -q 'FAIL: 0' "$doctor_file" \
    || ! grep -Eq 'RESULT: PASS( WITH WARNINGS)?' "$doctor_file"; then
    echo "post-reboot Doctor did not prove a zero-failure result (rc=$doctor_rc)" >&2
    exit 2
fi
rm -f -- "$doctor_file"
doctor_file=
timeout --foreground --kill-after=10 1800 sharedlvmthin upgrade-check
health_file=$(mktemp)
if ! timeout --foreground --kill-after=10 300 \
    /usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json >"$health_file"; then
    echo "health JSON collection failed after reboot" >&2
    exit 2
fi
python3 - "$health_file" "$expected_profile" "$installed_package" "$installed_version" <<'PY'
import json
import sys

path, expected_profile, expected_package, expected_version = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as stream:
        doc = json.load(stream)
except (OSError, UnicodeError, json.JSONDecodeError) as exc:
    raise SystemExit(f"health JSON is unreadable or malformed: {exc}")

if doc.get("result") not in ("PASS", "WARN"):
    raise SystemExit(f"health JSON result is unsafe: {doc.get('result')!r}")
checks = doc.get("checks")
if not isinstance(checks, list) or not checks or any(
    not isinstance(item, dict) or item.get("status") not in ("PASS", "WARN")
    for item in checks
):
    raise SystemExit("health JSON does not prove a complete zero-failure check set")
platform = doc.get("platform")
if not isinstance(platform, dict):
    raise SystemExit("health JSON has no platform object")
expected = {
    "package_flavor": expected_profile,
    "plugin_package": expected_package,
    "plugin_version": expected_version,
}
for field, value in expected.items():
    if platform.get(field) != value:
        raise SystemExit(
            f"health JSON {field} mismatch: expected {value!r}, observed {platform.get(field)!r}"
        )
storages = doc.get("storages")
if not isinstance(storages, list):
    raise SystemExit("health JSON storages is not a list")
if expected_profile == "thick-only":
    invalid = []
    for item in storages:
        if not isinstance(item, dict):
            invalid.append("<malformed>")
        elif not isinstance(item.get("node_applicable"), bool):
            invalid.append(str(item.get("id", "<unknown>")) + " (unknown node scope)")
        elif item["node_applicable"] and item.get("allocation_mode", "thin") not in (
            "thick-generations", "thick-generations-lazy"
        ):
            invalid.append(str(item.get("id", "<unknown>")))
    if invalid:
        raise SystemExit(
            "Thick-only health JSON contains non-Thick storage: " + ", ".join(invalid)
        )
PY
rm -f -- "$health_file"
health_file=

if ((settle_runtime == 1)); then
    sharedlvmthin update-policy requalify-boot
fi

echo "HOST=$expected_host"
echo "PREVIOUS_BOOT_ID=${previous_boot_id,,}"
echo "CURRENT_BOOT_ID=${current_boot_id,,}"
echo "PACKAGE=$installed_package"
echo "VERSION=$installed_version"
echo "PROFILE=$observed_profile"
echo "HEALTH_JSON=PASS"
echo "RUNTIME_SETTLED=$([[ $settle_runtime -eq 1 ]] && echo YES || echo NO)"
echo "RESULT=POST_REBOOT_PASS"
echo "Guest-I/O validation remains workload-specific and must be recorded separately."
