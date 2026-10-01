#!/bin/sh
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
OUT=${1:-"$ROOT/dist"}
FLAVOR=${2:-${PACKAGE_FLAVOR:-dual}}
case "$FLAVOR" in
    dual) CONTROL="$ROOT/DEBIAN/control" ;;
    thick-only) CONTROL="$ROOT/packaging/thick-only/control" ;;
    *) echo "unknown package flavor: $FLAVOR" >&2; exit 64 ;;
esac
VERSION=$(sed -n 's/^Version:[[:space:]]*//p' "$CONTROL")
ARCH=$(sed -n 's/^Architecture:[[:space:]]*//p' "$CONTROL")
PACKAGE_NAME=$(sed -n 's/^Package:[[:space:]]*//p' "$CONTROL")
PACKAGE="${PACKAGE_NAME}_${VERSION}_${ARCH}.deb"
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-1788231600}
export SOURCE_DATE_EPOCH
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM

mkdir -p "$OUT"
cp -a "$ROOT/DEBIAN" "$ROOT/etc" "$ROOT/lib" "$ROOT/usr" "$STAGE/"
cp "$CONTROL" "$STAGE/DEBIAN/control"
# preinst runs before dpkg unpacks the payload and cannot safely rely on the
# older installed recovery checker. Ship the exact candidate checker inside
# the control archive so disabled-storage state is audited with candidate
# semantics before any package file is replaced.
cp "$ROOT/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check" \
    "$STAGE/DEBIAN/sharedlvmthin-candidate-recovery-check"
cp "$ROOT/usr/libexec/pve-sharedlvmthin/sharedlvmthin_pve_inventory.py" \
    "$STAGE/DEBIAN/sharedlvmthin_pve_inventory.py"
cp "$ROOT/usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy" \
    "$STAGE/DEBIAN/sharedlvmthin-candidate-update-policy"
cp "$ROOT/usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py" \
    "$STAGE/DEBIAN/sharedlvmthin_update_policy.py"
cp "$ROOT/usr/share/pve-sharedlvmthin/pve-qualified-tuples.json" \
    "$STAGE/DEBIAN/sharedlvmthin-candidate-qualified-tuples.json"
# Both profiles need the exact candidate probe before unpack/configure.  The
# Thick-only data archive still excludes this DUAL-only runtime helper.
cp "$ROOT/usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check" \
    "$STAGE/DEBIAN/sharedlvmthin-package-maintenance-check"
mkdir -p "$STAGE/usr/share/pve-sharedlvmthin"
printf '%s\n' "$FLAVOR" >"$STAGE/usr/share/pve-sharedlvmthin/package-flavor"
printf '%s\n' "$PACKAGE_NAME" >"$STAGE/DEBIAN/sharedlvmthin-candidate-package"
printf '%s\n' "$VERSION" >"$STAGE/DEBIAN/sharedlvmthin-candidate-version"
printf '%s\n' "$FLAVOR" >"$STAGE/DEBIAN/sharedlvmthin-candidate-flavor"
printf '%s\n' "$ARCH" >"$STAGE/DEBIAN/sharedlvmthin-candidate-architecture"

if [ "$FLAVOR" = "thick-only" ]; then
    EXCLUSIONS="$ROOT/packaging/thick-only/excluded-paths.txt"
    [ -s "$EXCLUSIONS" ] || {
        echo "Thick-only exclusion manifest is missing or empty" >&2
        exit 1
    }
    while IFS= read -r relative || [ -n "$relative" ]; do
        case "$relative" in
            ''|'#'*) continue ;;
            /*|*..*) echo "unsafe Thick-only exclusion: $relative" >&2; exit 1 ;;
        esac
        [ -e "$STAGE/$relative" ] || {
            echo "stale Thick-only exclusion does not exist in Dual payload: $relative" >&2
            exit 1
        }
        rm -f -- "$STAGE/$relative"
    done <"$EXCLUSIONS"

    # Debian package documentation belongs under the binary package name.
    # Move the shared source documentation rather than duplicating it, so a
    # flavor replacement cannot leave files owned under the other profile.
    mkdir -p "$STAGE/usr/share/doc/$PACKAGE_NAME"
    cp -a "$STAGE/usr/share/doc/pve-sharedlvmthin/." \
        "$STAGE/usr/share/doc/$PACKAGE_NAME/"
    rm -rf "$STAGE/usr/share/doc/pve-sharedlvmthin"
fi

# Bind mutation admission to the code identity actually loaded by a PVE
# worker.  The digest is computed from the staged Perl template set after
# profile exclusions, then embedded into the plugin and installed as an
# independently verified root-owned marker.  It intentionally precedes the
# whole-package artifact digest to avoid a circular identity.
RUNTIME_BUILD_ID=$(
    find "$STAGE/usr/share/perl5/PVE" -type f -name '*.pm' -print |
        LC_ALL=C sort |
        while IFS= read -r path; do
            relative=${path#"$STAGE/"}
            printf '%s  %s\n' "$(sha256sum "$path" | awk '{print $1}')" "$relative"
        done |
        sha256sum | awk '{print $1}'
)
case "$RUNTIME_BUILD_ID" in
    *[!0-9a-f]*|'') echo "invalid runtime build identity" >&2; exit 1 ;;
esac
[ "${#RUNTIME_BUILD_ID}" -eq 64 ] || {
    echo "invalid runtime build identity length" >&2
    exit 1
}
PLUGIN_PM="$STAGE/usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
grep -q "__SLT_RUNTIME_BUILD_ID__" "$PLUGIN_PM" || {
    echo "runtime build identity template is missing" >&2
    exit 1
}
sed "s/__SLT_RUNTIME_BUILD_ID__/$RUNTIME_BUILD_ID/g" "$PLUGIN_PM" >"$PLUGIN_PM.new"
mv "$PLUGIN_PM.new" "$PLUGIN_PM"
printf '%s\n' "$RUNTIME_BUILD_ID" \
    >"$STAGE/usr/share/pve-sharedlvmthin/runtime-build-id"

DOC_DIR="$STAGE/usr/share/doc/$PACKAGE_NAME"
mkdir -p "$DOC_DIR"

# Ship the same risk/support boundary and project notice inside the binary
# package so the warning remains available after an offline installation.
install -m 0644 "$ROOT/docs/RISK-AND-SUPPORT-BOUNDARY.md" \
    "$DOC_DIR/RISK-AND-SUPPORT-BOUNDARY.md"
install -m 0644 "$ROOT/NOTICE" \
    "$DOC_DIR/NOTICE"

# Syntax tests can leave bytecode in a development tree. Binary packages
# must be built only from authoritative source files.
find "$STAGE" -type f -name '*.pyc' -delete
find "$STAGE" -depth -type d -name '__pycache__' -exec rmdir {} +

find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
for PROGRAM in \
    "$STAGE/DEBIAN/preinst" \
    "$STAGE/DEBIAN/sharedlvmthin-candidate-recovery-check" \
    "$STAGE/DEBIAN/sharedlvmthin-candidate-update-policy" \
    "$STAGE/DEBIAN/sharedlvmthin-package-maintenance-check" \
    "$STAGE/DEBIAN/postinst" \
    "$STAGE/DEBIAN/postrm" \
    "$STAGE/DEBIAN/prerm" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-upstream-inventory" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-plan" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-snapshot-observe" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-migration-preflight" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-storage-move-preflight" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-apt-guard" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-candidate-inspect" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-contract-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-gate" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-aggregate" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-lab-evidence-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-readonly-lab-observe" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-readonly-lab-oracle" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-admission" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-plan" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-topology" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-import" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-metadata-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-snapshot-preflight" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize" \
    "$STAGE/usr/share/initramfs-tools/hooks/zz-pve-sharedlvmthin-lvm-prune" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-web" \
    "$STAGE/usr/sbin/sharedlvmthin" \
    "$STAGE/usr/sbin/sharedlvmthin-migrate-bridge" \
    "$STAGE/usr/sbin/sharedlvmthin-web-configure"
do
    [ ! -e "$PROGRAM" ] || chmod 0755 "$PROGRAM"
done

# Bind both the data archive and every security-relevant control-archive file
# to one logical artifact identity. The two identity files and the derived
# dpkg md5sums are deliberately excluded to avoid a circular digest. This is
# an accidental substitution/reinstall guard, not an attestation of installed
# bytes and not a replacement for dpkg --verify or the runtime profile gate.
ARTIFACT_SHA256=$(python3 "$ROOT/scripts/package-artifact-identity.py" \
    "$STAGE" "$STAGE/DEBIAN")
printf '%s\n' "$ARTIFACT_SHA256" \
    >"$STAGE/usr/share/pve-sharedlvmthin/package-artifact-sha256"
printf '%s\n' "$ARTIFACT_SHA256" \
    >"$STAGE/DEBIAN/sharedlvmthin-candidate-artifact-sha256"
chmod 0644 \
    "$STAGE/usr/share/pve-sharedlvmthin/package-artifact-sha256" \
    "$STAGE/DEBIAN/sharedlvmthin-candidate-artifact-sha256"

# dpkg --verify can check only paths recorded in the package's md5sums control
# file. Because this package is assembled directly with dpkg-deb rather than
# debhelper, generate that manifest explicitly and deterministically for every
# data-archive file in both package profiles.
(
    cd "$STAGE"
    find . -type f ! -path './DEBIAN/*' -print | LC_ALL=C sort |
        while IFS= read -r path; do md5sum "$path"; done |
        sed 's#  \./#  #' >DEBIAN/md5sums
)
chmod 0644 "$STAGE/DEBIAN/md5sums"

find "$STAGE" -exec touch -d "@$SOURCE_DATE_EPOCH" {} +

# Debian and Ubuntu currently choose different dpkg-deb default compressors.
# Pin the archive format so a release runner and a qualified PVE node produce
# the same bytes from the same source and SOURCE_DATE_EPOCH.
dpkg-deb -Zxz --root-owner-group --build "$STAGE" "$OUT/$PACKAGE"
sh "$ROOT/scripts/check-release.sh" "$OUT/$PACKAGE"
(cd "$OUT" && sha256sum "$PACKAGE" >SHA256SUMS)
printf '%s\n' "$OUT/$PACKAGE"
