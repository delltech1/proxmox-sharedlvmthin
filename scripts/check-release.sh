#!/bin/sh
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

if [ "$#" -ne 1 ]; then
    echo "usage: $0 PACKAGE.deb" >&2
    exit 64
fi

PACKAGE=$1
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

dpkg-deb --info "$PACKAGE" >/dev/null
dpkg-deb --contents "$PACKAGE" >"$TMP/contents.txt"
dpkg-deb --extract "$PACKAGE" "$TMP/root"
dpkg-deb --control "$PACKAGE" "$TMP/control"

# Scan both Debian archives after extraction.  This is deliberately separate
# from the source-tree privacy test: a build recipe or maintainer script can
# accidentally inject host evidence even when the tracked source is clean.
python3 "$ROOT/scripts/package-privacy-scan.py" \
    "$TMP/root" "$TMP/control" >/dev/null

# Migration models, tests and local evidence are development-only.  Keep this
# explicit even though build.sh stages only DEBIAN, lib and usr: a future copy
# rule must not silently turn a lab coordinator into shipped runtime code.
if grep -Eq '(^|/)(experiments|tests)/|layout-migration-' "$TMP/contents.txt"; then
    echo "development-only layout migration content leaked into package" >&2
    exit 1
fi

PACKAGE_NAME=$(dpkg-deb -f "$PACKAGE" Package)
PACKAGE_VERSION=$(dpkg-deb -f "$PACKAGE" Version)

# No current runtime path requires a symlink. Rejecting all payload symlinks
# keeps checksum coverage and profile parity exact and prevents an archive from
# redirecting a nominally private path outside its package namespace.
if find "$TMP/root" -type l -print -quit | grep -q .; then
    echo "symbolic links are not permitted in the package payload" >&2
    exit 1
fi

if [ ! -s "$TMP/control/md5sums" ]; then
    echo "package data-file checksum manifest is missing or empty" >&2
    exit 1
fi
(cd "$TMP/root" && find . -type f -printf '%P\n' | LC_ALL=C sort) \
    >"$TMP/payload-files.txt"
cut -c35- "$TMP/control/md5sums" | LC_ALL=C sort >"$TMP/manifest-files.txt"
if ! diff -u "$TMP/payload-files.txt" "$TMP/manifest-files.txt"; then
    echo "package checksum manifest does not cover the exact data payload" >&2
    exit 1
fi
if ! (cd "$TMP/root" && md5sum --strict -c "$TMP/control/md5sums" >/dev/null); then
    echo "package checksum manifest does not match the extracted payload" >&2
    exit 1
fi

DATA_ARTIFACT="$TMP/root/usr/share/pve-sharedlvmthin/package-artifact-sha256"
CONTROL_ARTIFACT="$TMP/control/sharedlvmthin-candidate-artifact-sha256"
CONTROL_PACKAGE="$TMP/control/sharedlvmthin-candidate-package"
CONTROL_VERSION="$TMP/control/sharedlvmthin-candidate-version"
CONTROL_FLAVOR="$TMP/control/sharedlvmthin-candidate-flavor"
identity_is_one_line() {
    identity_file=$1
    [ -f "$identity_file" ] && [ ! -L "$identity_file" ] || return 1
    [ "$(wc -l <"$identity_file")" -eq 1 ] || return 1
    identity_value=$(sed -n '1p' "$identity_file") || return 1
    printf '%s\n' "$identity_value" | cmp -s - "$identity_file"
}
for identity_file in "$DATA_ARTIFACT" "$CONTROL_ARTIFACT" "$CONTROL_PACKAGE" \
    "$CONTROL_VERSION" "$CONTROL_FLAVOR"; do
    if ! identity_is_one_line "$identity_file"; then
        echo "package artifact identity file is missing or malformed: $identity_file" >&2
        exit 1
    fi
done
ARTIFACT_SHA256=$(python3 "$ROOT/scripts/package-artifact-identity.py" \
    "$TMP/root" "$TMP/control")
if [ "$(sed -n '1p' "$DATA_ARTIFACT")" != "$ARTIFACT_SHA256" ] || \
   [ "$(sed -n '1p' "$CONTROL_ARTIFACT")" != "$ARTIFACT_SHA256" ]; then
    echo "package artifact identity does not match exact data/control content" >&2
    exit 1
fi
if [ "$(sed -n '1p' "$CONTROL_PACKAGE")" != "$PACKAGE_NAME" ] || \
   [ "$(sed -n '1p' "$CONTROL_VERSION")" != "$PACKAGE_VERSION" ]; then
    echo "package artifact identity does not match package name/version" >&2
    exit 1
fi

for maintscript in preinst postinst prerm postrm; do
    if [ ! -f "$TMP/control/$maintscript" ]; then
        echo "required maintainer script is missing: $maintscript" >&2
        exit 1
    fi
    sh -n "$TMP/control/$maintscript"
done

CANDIDATE_RECOVERY="$TMP/control/sharedlvmthin-candidate-recovery-check"
PAYLOAD_RECOVERY="$TMP/root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check"
CANDIDATE_INVENTORY="$TMP/control/sharedlvmthin_pve_inventory.py"
PAYLOAD_INVENTORY="$TMP/root/usr/libexec/pve-sharedlvmthin/sharedlvmthin_pve_inventory.py"
if [ ! -x "$CANDIDATE_RECOVERY" ]; then
    echo "candidate control-archive recovery checker is missing or not executable" >&2
    exit 1
fi
if [ ! -x "$PAYLOAD_RECOVERY" ]; then
    echo "payload recovery checker is missing or not executable" >&2
    exit 1
fi
if ! cmp -s "$CANDIDATE_RECOVERY" "$PAYLOAD_RECOVERY"; then
    echo "candidate preinst recovery checker differs from packaged payload checker" >&2
    exit 1
fi
if [ ! -r "$CANDIDATE_INVENTORY" ] || [ ! -r "$PAYLOAD_INVENTORY" ]; then
    echo "candidate or payload PVE inventory parser is missing" >&2
    exit 1
fi
if ! cmp -s "$CANDIDATE_INVENTORY" "$PAYLOAD_INVENTORY"; then
    echo "candidate preinst PVE inventory parser differs from packaged payload" >&2
    exit 1
fi
python3 -m py_compile "$CANDIDATE_RECOVERY" "$CANDIDATE_INVENTORY"
grep -Fq 'sharedlvmthin-candidate-recovery-check' "$TMP/control/preinst" || {
    echo "preinst does not invoke the candidate control-archive recovery checker" >&2
    exit 1
}

FLAVOR_FILE="$TMP/root/usr/share/pve-sharedlvmthin/package-flavor"
if [ ! -f "$FLAVOR_FILE" ]; then
    echo "package flavor marker is missing" >&2
    exit 1
fi
FLAVOR=$(sed -n '1p' "$FLAVOR_FILE")
if [ "$(sed -n '1p' "$CONTROL_FLAVOR")" != "$FLAVOR" ]; then
    echo "candidate control flavor differs from packaged data flavor" >&2
    exit 1
fi
case "$PACKAGE_NAME:$FLAVOR" in
    pve-sharedlvmthin:dual) ;;
    pve-sharedlvmthin-thick:thick-only) ;;
    *) echo "package name/flavor mismatch: $PACKAGE_NAME:$FLAVOR" >&2; exit 1 ;;
esac

DOC_DIR="$TMP/root/usr/share/doc/$PACKAGE_NAME"
if [ ! -f "$DOC_DIR/copyright" ] || \
   [ ! -f "$DOC_DIR/RISK-AND-SUPPORT-BOUNDARY.md" ] || \
   [ ! -f "$DOC_DIR/NOTICE" ]; then
    echo "package documentation is missing from its package namespace: $PACKAGE_NAME" >&2
    exit 1
fi

for required_path in \
    usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm \
    usr/share/perl5/PVE/SharedLvmAdmission.pm \
    usr/share/perl5/PVE/SharedLvmThinThick.pm \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-upstream-inventory \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-candidate-inspect \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-contract-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-gate \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-aggregate \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-lab-evidence-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json \
    usr/sbin/sharedlvmthin
do
    if [ ! -f "$TMP/root/$required_path" ]; then
        echo "required shared Thick component is missing: $required_path" >&2
        exit 1
    fi
done

for executable_path in \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-upstream-inventory \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-candidate-inspect \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-contract-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-gate \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-aggregate \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-lab-evidence-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json \
    usr/sbin/sharedlvmthin
do
    if [ ! -x "$TMP/root/$executable_path" ]; then
        echo "required program is not executable: $executable_path" >&2
        exit 1
    fi
done

if [ ! -f "$TMP/root/usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json" ]; then
    echo "PVE compatibility contract catalogue is missing" >&2
    exit 1
fi
if [ ! -f "$TMP/root/usr/share/pve-sharedlvmthin/pve-lab-scenarios.json" ]; then
    echo "PVE lab scenario registry is missing" >&2
    exit 1
fi
python3 "$TMP/root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-contract-check" \
    --catalogue "$TMP/root/usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json" \
    --scenario-registry "$TMP/root/usr/share/pve-sharedlvmthin/pve-lab-scenarios.json" \
    --source-root "$ROOT" >/dev/null

CONTROL_MAINTENANCE_CHECK="$TMP/control/sharedlvmthin-package-maintenance-check"
if [ ! -x "$CONTROL_MAINTENANCE_CHECK" ]; then
    echo "candidate control-archive maintenance probe is missing or not executable" >&2
    exit 1
fi
python3 - "$CONTROL_MAINTENANCE_CHECK" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
compile(path.read_bytes(), str(path), "exec")
PY

if [ "$FLAVOR" = "dual" ]; then
    MAINTENANCE_CHECK="$TMP/root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check"
    if [ ! -x "$MAINTENANCE_CHECK" ]; then
        echo "DUAL package maintenance transaction verifier is missing or not executable" >&2
        exit 1
    fi
    python3 - "$MAINTENANCE_CHECK" <<'PY'
import pathlib
import sys
path = pathlib.Path(sys.argv[1])
compile(path.read_bytes(), str(path), "exec")
PY
fi

if [ "$FLAVOR" = "thick-only" ]; then
    if [ -e "$TMP/root/usr/share/doc/pve-sharedlvmthin" ]; then
        echo "dual-package documentation namespace leaked into Thick-only package" >&2
        exit 1
    fi
    EXCLUSIONS="$ROOT/packaging/thick-only/excluded-paths.txt"
    [ -s "$EXCLUSIONS" ] || {
        echo "Thick-only exclusion manifest is missing or empty" >&2
        exit 1
    }
    while IFS= read -r forbidden_path || [ -n "$forbidden_path" ]; do
        case "$forbidden_path" in ''|'#'*) continue ;; esac
        if [ -e "$TMP/root/$forbidden_path" ]; then
            echo "Thin operational path leaked into Thick-only package: $forbidden_path" >&2
            exit 1
        fi
    done <"$EXCLUSIONS"

    # The Thick-only artifact intentionally removes both Perl Thin daemons.
    # Its packaged postinst may syntax-check them only inside the exact dual
    # flavor branch. This artifact-level invariant prevents a source/build
    # mismatch from producing a package that builds but cannot configure.
    if ! awk '
        /if \[ "\$PACKAGE_FLAVOR" = "dual" \]; then/ { in_dual=1; next }
        in_dual && /perl -c "\$MONITOR"/ { monitor=1 }
        in_dual && /perl -c "\$THIN_GUARDD"/ { guard=1 }
        in_dual && /^fi$/ { in_dual=0 }
        END { exit (monitor && guard) ? 0 : 1 }
    ' "$TMP/control/postinst"; then
        echo "Thick-only postinst validates an absent Thin daemon outside its flavor guard" >&2
        exit 1
    fi
    if ! grep -Fq 'Thick-only configuration refused: stale Thin component remains' \
        "$TMP/control/postinst" || \
       ! grep -Fq 'ThinGuard runtime state is unavailable' "$TMP/control/postinst"; then
        echo "Thick-only postinst lacks stale Thin payload/runtime refusal" >&2
        exit 1
    fi
fi

for forbidden in \
    '*.key' '*.p12' '*.pfx' 'id_rsa' 'id_ed25519' '*.pyc' '__pycache__'; do
    if find "$TMP/root" -name "$forbidden" -print -quit | grep -q .; then
        echo "forbidden package content matching $forbidden" >&2
        exit 1
    fi
done

if grep -RIlE --exclude='index.html' \
    "(-----BEGIN (OPENSSH |RSA |EC )?PRIVATE KEY-----|password[[:space:]]*=[[:space:]]*['\"][^'\"]+['\"])" \
    "$TMP/root" | grep -q .; then
    echo "possible credential or private key in package" >&2
    exit 1
fi

if grep -Eq '^drwx.*\./(tests|docs)/' "$TMP/contents.txt"; then
    echo "source-only tests/docs leaked into binary package" >&2
    exit 1
fi

echo "package security/content validation: PASS"
