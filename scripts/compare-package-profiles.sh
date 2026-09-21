#!/bin/sh
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

set -eu

if [ "$#" -ne 2 ]; then
    echo "usage: $0 DUAL.deb THICK-ONLY.deb" >&2
    exit 64
fi

DUAL_PACKAGE=$1
THICK_PACKAGE=$2
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM
DUAL_ROOT="$TMP/dual"
THICK_ROOT="$TMP/thick"
mkdir -p "$DUAL_ROOT" "$THICK_ROOT"
dpkg-deb --extract "$DUAL_PACKAGE" "$DUAL_ROOT"
dpkg-deb --extract "$THICK_PACKAGE" "$THICK_ROOT"

# Thick-only is a restricted build profile, not a fork. Every non-document
# regular file it retains must be byte-identical to the dual-mode artifact,
# except for the explicit package-flavor marker.
if ! find "$THICK_ROOT" -type f -print | while IFS= read -r thick_file; do
    relative=${thick_file#"$THICK_ROOT/"}
    case "$relative" in
        usr/share/pve-sharedlvmthin/package-flavor) continue ;;
        usr/share/doc/pve-sharedlvmthin-thick/*)
            doc_relative=${relative#usr/share/doc/pve-sharedlvmthin-thick/}
            dual_file="$DUAL_ROOT/usr/share/doc/pve-sharedlvmthin/$doc_relative"
            ;;
        *) dual_file="$DUAL_ROOT/$relative" ;;
    esac

    if [ ! -f "$dual_file" ]; then
        echo "Thick-only file has no dual-mode source counterpart: $relative" >&2
        exit 1
    fi
    if ! cmp -s "$dual_file" "$thick_file"; then
        echo "shared package-profile file content differs: $relative" >&2
        exit 1
    fi
    dual_mode=$(stat -c '%a' "$dual_file")
    thick_mode=$(stat -c '%a' "$thick_file")
    if [ "$dual_mode" != "$thick_mode" ]; then
        echo "shared package-profile file mode differs: $relative ($dual_mode != $thick_mode)" >&2
        exit 1
    fi
done; then
    exit 1
fi

echo "dual/Thick-only shared payload parity: PASS"
