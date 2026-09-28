#!/bin/bash
# Disposable-lab package install/replacement gate. Dry-run is the default.

set -euo pipefail

temp=$(mktemp -d)
cleanup() {
    rm -rf -- "$temp"
}
trap cleanup EXIT
trap 'exit 130' HUP INT TERM

execute=0
settle_recovery=0
transaction_id=
package=
expected_hash=
expected_host=
expected_current=
clean_env=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C)

usage() {
    cat <<'EOF'
usage: package-profile-gate.sh --package /absolute/candidate.deb \
       --sha256 HEX --expect-host EXACT-HOSTNAME \
       --expect-current none|dual|thick-only [--execute] \
       [--settle-recovery --transaction-id 32-HEX]

Without --execute this performs read-only validation only. The execute mode
installs exactly the supplied local .deb with dpkg; it never downloads
dependencies, changes repositories, reboots the host or advances another node.
Recovery settlement never invokes dpkg. It repeats all read-only checks and
closes only an already-consumed transaction whose exact target is installed.
EOF
}

while (($#)); do
    case "$1" in
        --package) package=${2:-}; shift 2 ;;
        --sha256) expected_hash=${2:-}; shift 2 ;;
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --expect-current) expected_current=${2:-}; shift 2 ;;
        --execute) execute=1; shift ;;
        --settle-recovery) execute=1; settle_recovery=1; shift ;;
        --transaction-id) transaction_id=${2:-}; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

if ((settle_recovery == 1)); then
    [[ "$transaction_id" =~ ^[0-9a-f]{32}$ ]] || {
        echo "recovery settlement requires the exact 32-hex transaction ID" >&2
        exit 64
    }
elif [[ -n "$transaction_id" ]]; then
    echo "--transaction-id is valid only with --settle-recovery" >&2
    exit 64
fi

[[ -n "$package" && -n "$expected_hash" && -n "$expected_host" && -n "$expected_current" ]] || {
    usage >&2
    exit 64
}
case "$expected_current" in
    none|dual|thick-only) ;;
    *) echo "expected current profile must be none, dual or thick-only" >&2; exit 64 ;;
esac
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

for command in awk cp env grep sed sha256sum dpkg dpkg-deb dpkg-query systemctl timeout; do
    command -v "$command" >/dev/null 2>&1 || {
        echo "required command is unavailable: $command" >&2
        exit 2
    }
done

# Pin one private byte-for-byte candidate before trusting or executing any of
# its content. From this point onward the caller-controlled pathname is never
# reopened, closing the hash-to-preinst/dpkg replacement window.
candidate_deb="$temp/candidate.deb"
cp -- "$package" "$candidate_deb"
chmod 0400 "$candidate_deb"
actual_hash=$(sha256sum "$candidate_deb" | awk '{print $1}')
[[ "${actual_hash,,}" == "${expected_hash,,}" ]] || {
    echo "candidate SHA-256 mismatch" >&2
    exit 2
}

# Extract only the candidate control archive. The caller supplies the exact
# accepted package SHA and the release pipeline separately runs the complete
# content/identity checker. The installed TG32
# CLI predates some candidate recovery options, so a preflight through the old
# `sharedlvmthin upgrade-check` can reject a valid candidate or, worse, apply
# weaker old semantics. The candidate preinst carries its exact read-only
# recovery checker and is the authoritative before-unpack gate.
candidate_control="$temp/control"
mkdir -p "$candidate_control"
dpkg-deb --control "$candidate_deb" "$candidate_control"
target_name=$(dpkg-deb -f "$candidate_deb" Package)
target_version=$(dpkg-deb -f "$candidate_deb" Version)
target_arch=$(dpkg-deb -f "$candidate_deb" Architecture)
for control_file in \
    preinst \
    sharedlvmthin-candidate-recovery-check \
    sharedlvmthin_pve_inventory.py \
    sharedlvmthin-candidate-package \
    sharedlvmthin-candidate-version \
    sharedlvmthin-candidate-flavor \
    sharedlvmthin-candidate-artifact-sha256; do
    [[ -f "$candidate_control/$control_file" && ! -L "$candidate_control/$control_file" ]] || {
        echo "candidate control archive is missing a safe $control_file" >&2
        exit 2
    }
done
case "$target_name" in
    pve-sharedlvmthin) target_flavor=dual; opposite_name=pve-sharedlvmthin-thick ;;
    pve-sharedlvmthin-thick) target_flavor=thick-only; opposite_name=pve-sharedlvmthin ;;
    *) echo "unexpected package identity: $target_name" >&2; exit 2 ;;
esac
if [[ "$target_flavor" == dual ]] && \
   [[ ! -x "$candidate_control/sharedlvmthin-package-maintenance-check" ]]; then
    echo "DUAL candidate control archive lacks the maintenance verifier" >&2
    exit 2
fi
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

case "$installed_name" in
    '') observed_current=none ;;
    pve-sharedlvmthin) observed_current=dual ;;
    pve-sharedlvmthin-thick) observed_current=thick-only ;;
    *) echo "internal current-profile classification failed" >&2; exit 2 ;;
esac
[[ "$observed_current" == "$expected_current" ]] || {
    echo "current package profile mismatch: expected $expected_current, observed $observed_current" >&2
    exit 2
}

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

fi

if [[ "$installed_name" == "$target_name" ]]; then
    preinst_action=(upgrade "$installed_version" "$target_version")
else
    # First install and a Conflicts/Replaces profile transition both invoke the
    # new package's preinst as install. The installed flavor marker, when
    # present, keeps a cross-profile transition out of preinstall semantics.
    preinst_action=(install)
fi
"${clean_env[@]}" \
    DPKG_MAINTSCRIPT_PACKAGE="$target_name" \
    timeout --foreground --kill-after=10 1800 \
    "$candidate_control/preinst" preflight "${preinst_action[@]}"

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
"${clean_env[@]}" dpkg --no-act -i "$candidate_deb"

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

profile_replacement=0
if ((settle_recovery == 1)); then
    [[ "$installed_name" == "$target_name" ]] || {
        echo "recovery settlement requires the exact target profile installed" >&2
        exit 2
    }
    profile_replacement=1
elif [[ -n "$installed_name" && "$installed_name" != "$target_name" ]]; then
    profile_replacement=1
    replacement_helper=/usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement
    [[ -x "$replacement_helper" ]] || {
        echo "installed profile is not replacement-protocol aware; first install this version's matching profile" >&2
        exit 2
    }
    # The helper owns the only authorized dpkg process.  READY is bound to
    # that exact PID/starttime/executable/argv before the child is released.
    set +e
    transaction_output=$("${clean_env[@]}" "$replacement_helper" execute \
        --candidate "$candidate_deb" --sha256 "$actual_hash" \
        --source-package "$installed_name" --source-version "$installed_version" \
        --target-package "$target_name" --target-version "$target_version" 2>&1)
    transaction_rc=$?
    set -e
    printf '%s\n' "$transaction_output"
    if [[ $transaction_rc -ne 0 ]]; then
        echo "exact profile replacement transaction failed" >&2
        exit 2
    fi
    transaction_id=$(sed -n 's/^PROFILE_REPLACEMENT_TXID=\([0-9a-f]\{32\}\)$/\1/p' \
        <<<"$transaction_output")
    [[ "$transaction_id" =~ ^[0-9a-f]{32}$ ]] || {
        echo "exact profile replacement transaction identity is missing or ambiguous" >&2
        exit 2
    }
else
    "${clean_env[@]}" dpkg -i "$candidate_deb"
fi

# dpkg-query expands these literal field expressions, not the shell.
# shellcheck disable=SC2016
# dpkg-query expands these literal field expressions, not the shell.
# shellcheck disable=SC2016
status=$("${clean_env[@]}" dpkg-query -W -f='${db:Status-Abbrev}' "$target_name" 2>/dev/null || true)
[[ "$status" == ii* ]] || {
    echo "target package is not fully installed after dpkg" >&2
    exit 2
}
# shellcheck disable=SC2016
# shellcheck disable=SC2016
observed_version=$("${clean_env[@]}" dpkg-query -W -f='${Version}' "$target_name")
[[ "$observed_version" == "$target_version" ]] || {
    echo "installed package version does not match candidate" >&2
    exit 2
}
# shellcheck disable=SC2016
# shellcheck disable=SC2016
opposite_status=$("${clean_env[@]}" dpkg-query -W -f='${db:Status-Abbrev}' "$opposite_name" 2>/dev/null || true)
[[ "$opposite_status" != ii* ]] || {
    echo "opposite package profile remains installed after replacement" >&2
    exit 2
}
flavor_file=/usr/share/pve-sharedlvmthin/package-flavor
[[ -f "$flavor_file" && ! -L "$flavor_file" ]] || {
    echo "installed package flavor marker is missing or unsafe" >&2
    exit 2
}
observed_flavor=$(sed -n '1p' "$flavor_file")
[[ "$observed_flavor" == "$target_flavor" ]] || {
    echo "installed package flavor marker does not match package identity" >&2
    exit 2
}
if ! verify_output=$("${clean_env[@]}" dpkg --verify-format=rpm --verify "$target_name"); then
    echo "installed package integrity verification failed" >&2
    exit 2
fi
[[ -z "$verify_output" ]] || {
    echo "installed package files differ from the candidate manifest" >&2
    exit 2
}
"${clean_env[@]}" timeout --foreground --kill-after=10 300 sharedlvmthin compat-check
# The CLI deliberately returns EX_USAGE (64) after printing its command surface.
# Capture and validate that contract instead of letting `set -e` abort a healthy
# execute gate before its profile and post-upgrade checks run.
set +e
help_output=$("${clean_env[@]}" sharedlvmthin help 2>&1)
help_rc=$?
set -e
[[ $help_rc -eq 64 ]] || {
    echo "installed CLI help returned unexpected status: $help_rc" >&2
    exit 2
}
grep -Fq 'thick-recover-prepare <storage-id> <volume>' <<<"$help_output" || {
    echo "installed CLI is missing thick-recover-prepare" >&2
    exit 2
}
if [[ "$target_flavor" == dual ]]; then
    grep -Fq 'thin-adopt-owner-model <storage-id> <volume> ALL-NODES-INACTIVE' <<<"$help_output" || {
        echo "installed Dual CLI is missing thin-adopt-owner-model" >&2
        exit 2
    }
    for required_path in \
        /usr/sbin/sharedlvmthin-migrate-bridge \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd \
        /lib/systemd/system/pve-sharedlvmthin-thin-guard.service; do
        [[ -f "$required_path" && ! -L "$required_path" ]] || {
            echo "installed Dual profile is missing required payload: $required_path" >&2
            exit 2
        }
    done
else
    ! grep -Fq 'thin-adopt-owner-model' <<<"$help_output" || {
        echo "installed Thick-only CLI unexpectedly exposes Thin commands" >&2
        exit 2
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
            echo "installed Thick-only profile retains excluded payload: $excluded_path" >&2
            exit 2
        }
    done
    if ! thin_guard_state=$("${clean_env[@]}" systemctl show --property ActiveState --value \
        pve-sharedlvmthin-thin-guard.service 2>/dev/null); then
        echo "installed Thick-only profile cannot prove ThinGuard runtime state" >&2
        exit 2
    fi
    case "$thin_guard_state" in
        inactive|failed) ;;
        *) echo "installed Thick-only profile retains active ThinGuard state: $thin_guard_state" >&2; exit 2 ;;
    esac
    if "${clean_env[@]}" systemctl is-enabled --quiet pve-sharedlvmthin-thin-guard.service; then
        echo "installed Thick-only profile retains enabled ThinGuard state" >&2
        exit 2
    fi
fi
doctor_output="$temp/doctor.out"
set +e
"${clean_env[@]}" timeout --foreground --kill-after=10 300 sharedlvmthin doctor --quick \
    > >(tee "$doctor_output") 2>&1
doctor_rc=$?
set -e
if [[ $doctor_rc -ne 0 && $doctor_rc -ne 1 ]] \
    || ! grep -q 'FAIL: 0' "$doctor_output" \
    || ! grep -Eq 'RESULT: PASS( WITH WARNINGS)?' "$doctor_output"; then
    echo "installed Doctor did not prove a zero-failure result (rc=$doctor_rc)" >&2
    exit 2
fi
"${clean_env[@]}" timeout --foreground --kill-after=10 1800 sharedlvmthin upgrade-check

# Consume the durable replacement evidence only after every package identity,
# payload, runtime, Doctor and upgrade postcondition above has passed.
if ((profile_replacement == 1)); then
    finalize_args=(finalize --target-package "$target_name" \
        --target-version "$target_version" --candidate-sha256 "$actual_hash" \
        --txid "$transaction_id")
    if ((settle_recovery == 1)); then
        finalize_args+=(--recovery)
    fi
    "${clean_env[@]}" \
        /usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement \
        "${finalize_args[@]}"
fi

echo "RESULT=EXECUTE_PASS"
echo "Reboot was not performed. Reboot this one lab node under change control,"
echo "then verify package identity, storage health and guest I/O before advancing."
