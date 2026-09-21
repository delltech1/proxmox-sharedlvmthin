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
cp -a "$ROOT/DEBIAN" "$ROOT/lib" "$ROOT/usr" "$STAGE/"
cp "$CONTROL" "$STAGE/DEBIAN/control"
mkdir -p "$STAGE/usr/share/pve-sharedlvmthin"
printf '%s\n' "$FLAVOR" >"$STAGE/usr/share/pve-sharedlvmthin/package-flavor"

if [ "$FLAVOR" = "thick-only" ]; then
    rm -f \
        "$STAGE/lib/systemd/system/pve-sharedlvmthin-thin-guard.service" \
        "$STAGE/usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor" \
        "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd" \
        "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory" \
        "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-metadata-check" \
        "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence" \
        "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-import" \
        "$STAGE/usr/sbin/sharedlvmthin-migrate-bridge"
fi

# Ship the same risk/support boundary and project notice inside the binary
# package so the warning remains available after an offline installation.
install -m 0644 "$ROOT/docs/RISK-AND-SUPPORT-BOUNDARY.md" \
    "$STAGE/usr/share/doc/pve-sharedlvmthin/RISK-AND-SUPPORT-BOUNDARY.md"
install -m 0644 "$ROOT/NOTICE" \
    "$STAGE/usr/share/doc/pve-sharedlvmthin/NOTICE"

# Syntax tests can leave bytecode in a development tree. Binary packages
# must be built only from authoritative source files.
find "$STAGE" -type f -name '*.pyc' -delete
find "$STAGE" -depth -type d -name '__pycache__' -exec rmdir {} +

find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
for PROGRAM in \
    "$STAGE/DEBIAN/preinst" \
    "$STAGE/DEBIAN/postinst" \
    "$STAGE/DEBIAN/postrm" \
    "$STAGE/DEBIAN/prerm" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-admission" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-plan" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-import" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-metadata-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize" \
    "$STAGE/usr/share/initramfs-tools/hooks/zz-pve-sharedlvmthin-lvm-prune" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-web" \
    "$STAGE/usr/sbin/sharedlvmthin" \
    "$STAGE/usr/sbin/sharedlvmthin-migrate-bridge" \
    "$STAGE/usr/sbin/sharedlvmthin-web-configure"
do
    [ ! -e "$PROGRAM" ] || chmod 0755 "$PROGRAM"
done

find "$STAGE" -exec touch -d "@$SOURCE_DATE_EPOCH" {} +

# Debian and Ubuntu currently choose different dpkg-deb default compressors.
# Pin the archive format so a release runner and a qualified PVE node produce
# the same bytes from the same source and SOURCE_DATE_EPOCH.
dpkg-deb -Zxz --root-owner-group --build "$STAGE" "$OUT/$PACKAGE"
sh "$ROOT/scripts/check-release.sh" "$OUT/$PACKAGE"
(cd "$OUT" && sha256sum "$PACKAGE" >SHA256SUMS)
printf '%s\n' "$OUT/$PACKAGE"
