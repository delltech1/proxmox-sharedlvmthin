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
select_update_policy=
prior_update_policy=
prior_update_policy_schema=
freeze_package_txid=
direct_package_txid=
clean_env=(env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C)

usage() {
    cat <<'EOF'
usage: package-profile-gate.sh --package /absolute/candidate.deb \
       --sha256 HEX --expect-host EXACT-HOSTNAME \
       --expect-current none|dual|thick-only [--execute] \
       [--select-update-policy freeze|qualified-auto|warn] \
       [--settle-recovery [--transaction-id REPLACEMENT-32-HEX]
                          [--freeze-transaction-id FREEZE-32-HEX]]

Without --execute this performs read-only validation only. The execute mode
installs exactly the supplied local .deb with dpkg; it never downloads
dependencies, changes repositories, reboots the host or advances another node.
Recovery settlement never installs or reconfigures packages. It repeats all read-only checks and
closes only an already-consumed transaction whose exact target is installed.
Same-profile FREEZE recovery supplies only --freeze-transaction-id; a
cross-profile recovery also supplies the distinct --transaction-id.
EOF
}

while (($#)); do
    case "$1" in
        --package) package=${2:-}; shift 2 ;;
        --sha256) expected_hash=${2:-}; shift 2 ;;
        --expect-host) expected_host=${2:-}; shift 2 ;;
        --expect-current) expected_current=${2:-}; shift 2 ;;
        --execute) execute=1; shift ;;
        --select-update-policy) select_update_policy=${2:-}; shift 2 ;;
        --settle-recovery) execute=1; settle_recovery=1; shift ;;
        --transaction-id) transaction_id=${2:-}; shift 2 ;;
        --freeze-transaction-id) freeze_package_txid=${2:-}; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "unknown argument: $1" >&2; usage >&2; exit 64 ;;
    esac
done

if ((settle_recovery == 1)); then
    if ! { [[ -n "$transaction_id" || -n "$freeze_package_txid" ]] &&
        [[ -z "$transaction_id" || "$transaction_id" =~ ^[0-9a-f]{32}$ ]] &&
        [[ -z "$freeze_package_txid" || "$freeze_package_txid" =~ ^[0-9a-f]{32}$ ]]; }; then
        echo "recovery requires exact replacement and/or FREEZE transaction IDs" >&2
        exit 64
    fi
elif [[ -n "$transaction_id" || -n "$freeze_package_txid" ]]; then
    echo "transaction IDs are valid only with --settle-recovery" >&2
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
case "$select_update_policy" in
    ''|freeze|qualified-auto|warn) ;;
    *) echo "invalid update-policy selection" >&2; exit 64 ;;
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
    sharedlvmthin-candidate-update-policy \
    sharedlvmthin_update_policy.py \
    sharedlvmthin-candidate-qualified-tuples.json \
    sharedlvmthin-candidate-package \
    sharedlvmthin-candidate-version \
    sharedlvmthin-candidate-flavor \
    sharedlvmthin-candidate-artifact-sha256; do
    [[ -f "$candidate_control/$control_file" && ! -L "$candidate_control/$control_file" ]] || {
        echo "candidate control archive is missing a safe $control_file" >&2
        exit 2
    }
done
candidate_artifact=$(sed -n '1p' \
    "$candidate_control/sharedlvmthin-candidate-artifact-sha256")
[[ "$candidate_artifact" =~ ^[0-9a-f]{64}$ ]] || {
    echo "candidate artifact identity is malformed" >&2
    exit 2
}
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
        ii*|hi*)
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
if ((settle_recovery == 0)); then
    if [[ -n "$installed_name" && "$installed_name" != "$target_name" ]]; then
        "${clean_env[@]}" dpkg --force-hold --no-act -i "$candidate_deb"
    else
        "${clean_env[@]}" dpkg --no-act -i "$candidate_deb"
    fi
fi

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

if [[ -n "$installed_name" ]] && command -v sharedlvmthin >/dev/null 2>&1; then
    set +e
    prior_policy_output=$("${clean_env[@]}" sharedlvmthin update-policy status 2>/dev/null)
    set -e
    prior_update_policy=$(sed -n 's/^UPDATE_POLICY=//p' <<<"$prior_policy_output")
    prior_update_policy_schema=$(sed -n 's/^UPDATE_POLICY_SCHEMA=//p' <<<"$prior_policy_output")
    [[ $(grep -c '^UPDATE_POLICY=' <<<"$prior_policy_output") -le 1 ]] || {
        echo "installed update-policy identity is ambiguous" >&2
        exit 2
    }
fi

# An already active FREEZE policy is part of the package transaction's safety
# contract, not an optional CLI preference that the caller must repeat.  Keep
# it automatically when no policy switch was requested.  Switching away from
# FREEZE must be a separate, settled administrator action before this gate;
# otherwise dpkg could create baseline drift without a prepared successor.
if ((settle_recovery == 0)) && [[ "$prior_update_policy" == FREEZE ]]; then
    case "$select_update_policy" in
        '') select_update_policy=freeze ;;
        freeze) ;;
        *)
            echo "active FREEZE must be changed in a separate settled policy operation before package upgrade" >&2
            exit 2
            ;;
    esac
fi

# An existing FREEZE baseline is immutable by design.  Prepare one exact,
# durable successor intent from the hash-pinned candidate before dpkg runs.
# The candidate policy helper is extracted from that same private copy; it may
# prepare only the plugin package delta and cannot qualify any PVE/LVM/kernel
# change.  A crash leaves the old baseline plus a pending receipt fail-closed.
if ((settle_recovery == 1)) && [[ "$prior_update_policy" == FREEZE ]]; then
    [[ "$select_update_policy" == freeze && "$freeze_package_txid" =~ ^[0-9a-f]{32}$ ]] || {
        echo "FREEZE recovery requires --select-update-policy freeze and its original --freeze-transaction-id" >&2
        exit 2
    }
fi
if [[ -n "$freeze_package_txid" ]] && \
    [[ "$prior_update_policy" != FREEZE || "$select_update_policy" != freeze ]]; then
    echo "FREEZE recovery transaction cannot be ignored under another policy" >&2
    exit 2
fi
if ((settle_recovery == 0)) && [[ "$prior_update_policy" == FREEZE && "$select_update_policy" == freeze ]]; then
    candidate_root="$temp/candidate-root"
    mkdir -p "$candidate_root"
    dpkg-deb --extract "$candidate_deb" "$candidate_root"
    candidate_policy="$candidate_root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy"
    candidate_manifest="$candidate_root/usr/share/pve-sharedlvmthin/pve-qualified-tuples.json"
    candidate_artifact=$(sed -n '1p' \
        "$candidate_root/usr/share/pve-sharedlvmthin/package-artifact-sha256")
    [[ -f "$candidate_policy" && ! -L "$candidate_policy" \
        && -f "$candidate_manifest" && ! -L "$candidate_manifest" ]] || {
        echo "candidate FREEZE settlement payload is missing or unsafe" >&2
        exit 2
    }
    prepare_output=$("${clean_env[@]}" python3 "$candidate_policy" prepare-freeze-package \
        --source-package "$installed_name" --source-version "$installed_version" \
        --target-package "$target_name" --target-version "$target_version" \
        --target-architecture "$target_arch" --sha256 "$actual_hash" \
        --artifact-sha256 "$candidate_artifact" \
        --candidate-manifest "$candidate_manifest")
    printf '%s\n' "$prepare_output"
    freeze_package_txid=$(sed -n 's/^FREEZE_PACKAGE_TXID=\([0-9a-f]\{32\}\)$/\1/p' \
        <<<"$prepare_output")
    [[ "$freeze_package_txid" =~ ^[0-9a-f]{32}$ ]] || {
        echo "candidate FREEZE prepare receipt is missing or ambiguous" >&2
        exit 2
    }
fi
if ((settle_recovery == 0)) && [[ "$prior_update_policy" != FREEZE ]]; then
    if [[ -z "$installed_name" && -z "$select_update_policy" ]]; then
        echo "first install requires an explicit update-policy selection" >&2
        exit 2
    fi
    candidate_root=${candidate_root:-"$temp/candidate-root"}
    mkdir -p "$candidate_root"
    dpkg-deb --extract "$candidate_deb" "$candidate_root"
    candidate_policy="$candidate_root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy"
    candidate_manifest="$candidate_root/usr/share/pve-sharedlvmthin/pve-qualified-tuples.json"
    [[ -f "$candidate_policy" && ! -L "$candidate_policy" \
        && -f "$candidate_manifest" && ! -L "$candidate_manifest" ]] || {
        echo "candidate direct package transition payload is missing or unsafe" >&2
        exit 2
    }
    direct_source_package=${installed_name:-NONE}
    direct_source_version=${installed_version:-NONE}
    direct_output=$("${clean_env[@]}" python3 "$candidate_policy" prepare-direct-package \
        --source-package "$direct_source_package" --source-version "$direct_source_version" \
        --target-package "$target_name" --target-version "$target_version" \
        --target-architecture "$target_arch" --sha256 "$actual_hash" \
        --artifact-sha256 "$candidate_artifact" \
        --candidate-manifest "$candidate_manifest")
    printf '%s\n' "$direct_output"
    direct_package_txid=$(sed -n 's/^DIRECT_PACKAGE_TXID=\([0-9a-f]\{32\}\)$/\1/p' \
        <<<"$direct_output")
    [[ "$direct_package_txid" =~ ^[0-9a-f]{32}$ ]] || {
        echo "candidate direct package transaction identity is missing" >&2
        exit 2
    }
fi

profile_replacement=0
if ((settle_recovery == 1)); then
    [[ "$installed_name" == "$target_name" ]] || {
        echo "recovery settlement requires the exact target profile installed" >&2
        exit 2
    }
    # Same-profile recovery has only a FREEZE transaction. Cross-profile
    # recovery additionally supplies its distinct replacement transaction.
    [[ -z "$transaction_id" ]] || profile_replacement=1
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
[[ "$status" == ii* || "$status" == hi* ]] || {
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
[[ "$opposite_status" != ii* && "$opposite_status" != hi* ]] || {
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
# A candidate runtime is qualified below while both the package transition and
# runtime-qualification latch keep every mutating entry point closed.  Doctor
# and the ordinary PVE-facing aggregate run only after exact finalization.
# Publish only a boot-bound release candidate while the exact durable package
# transition still closes mutation admission.  This path uses direct read-only
# storage evidence and deliberately does not recurse through pvesm activation.
package_gate_txid=${freeze_package_txid:-$direct_package_txid}
[[ "$package_gate_txid" =~ ^[0-9a-f]{32}$ ]] || {
    echo "package qualification transaction identity is unavailable" >&2
    exit 2
}
"${clean_env[@]}" sharedlvmthin update-policy qualify-runtime \
    --package "$target_name" --version "$target_version" \
    --artifact-sha256 "$candidate_artifact" --plan-digest "$actual_hash" \
    --transaction-id "$package_gate_txid"

# Rotate the immutable package baseline only after the exact candidate runtime
# has passed the read-only gate.  The separate qualification-pending latch
# remains present across this settlement, so no mutating operation can enter.
if [[ "$select_update_policy" == freeze && "$prior_update_policy" == FREEZE ]]; then
    [[ "$freeze_package_txid" =~ ^[0-9a-f]{32}$ ]] || {
        echo "prepared FREEZE package transaction is unavailable" >&2
        exit 2
    }
    freeze_settle_args=()
    if ((profile_replacement == 1)); then
        freeze_settle_args+=(--replacement-txid "$transaction_id")
    fi
    "${clean_env[@]}" sharedlvmthin update-policy settle-freeze-package \
        --package "$target_name" --version "$target_version" \
        --sha256 "$actual_hash" --txid "$freeze_package_txid" "${freeze_settle_args[@]}"
else
    "${clean_env[@]}" sharedlvmthin update-policy settle-direct-package \
        --package "$target_name" --version "$target_version" \
        --sha256 "$actual_hash" --txid "$direct_package_txid"
    if [[ -n "$select_update_policy" ]]; then
        "${clean_env[@]}" sharedlvmthin update-policy select "$select_update_policy"
    fi
fi
if [[ "$prior_update_policy_schema" != 2 && -n "$prior_update_policy" \
    && "$prior_update_policy" != UNSELECTED ]]; then
    "${clean_env[@]}" sharedlvmthin update-policy migrate-policy-schema
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

# Doctor must prove the complete PVE-facing runtime, including exact tuple
# recognition, while the runtime-qualification latch still closes mutation
# admission.  The finalizer independently repeats the in-process tuple proof;
# a direct invocation therefore cannot bypass this shell-level ordering.
"${clean_env[@]}" sharedlvmthin update-policy finalize-runtime \
    --transaction-id "$package_gate_txid"

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
