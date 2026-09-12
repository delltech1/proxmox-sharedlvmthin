#!/bin/sh
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
VERSION=$(sed -n 's/^Version:[[:space:]]*//p' "$ROOT/DEBIAN/control")
ARCH=$(sed -n 's/^Architecture:[[:space:]]*//p' "$ROOT/DEBIAN/control")
OUT=${1:-"$ROOT/dist"}
PACKAGE="pve-sharedlvmthin_${VERSION}_${ARCH}.deb"
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-1788231600}
export SOURCE_DATE_EPOCH
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT HUP INT TERM

mkdir -p "$OUT"
cp -a "$ROOT/DEBIAN" "$ROOT/lib" "$ROOT/usr" "$STAGE/"

# Syntax tests can leave bytecode in a development tree. Binary packages
# must be built only from authoritative source files.
find "$STAGE" -type f -name '*.pyc' -delete
find "$STAGE" -depth -type d -name '__pycache__' -exec rmdir {} +

find "$STAGE" -type d -exec chmod 0755 {} +
find "$STAGE" -type f -exec chmod 0644 {} +
chmod 0755 \
    "$STAGE/DEBIAN/postinst" \
    "$STAGE/DEBIAN/postrm" \
    "$STAGE/DEBIAN/prerm" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize" \
    "$STAGE/usr/libexec/pve-sharedlvmthin/sharedlvmthin-web" \
    "$STAGE/usr/sbin/sharedlvmthin" \
    "$STAGE/usr/sbin/sharedlvmthin-web-configure"

find "$STAGE" -exec touch -d "@$SOURCE_DATE_EPOCH" {} +

# Debian and Ubuntu currently choose different dpkg-deb default compressors.
# Pin the archive format so a release runner and a qualified PVE node produce
# the same bytes from the same source and SOURCE_DATE_EPOCH.
dpkg-deb -Zxz --root-owner-group --build "$STAGE" "$OUT/$PACKAGE"
sh "$ROOT/scripts/check-release.sh" "$OUT/$PACKAGE"
(cd "$OUT" && sha256sum "$PACKAGE" >SHA256SUMS)
printf '%s\n' "$OUT/$PACKAGE"
