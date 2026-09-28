#!/bin/sh
# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
EXCLUSIONS="$ROOT/packaging/thick-only/excluded-paths.txt"
[ -s "$EXCLUSIONS" ] || {
    echo "Thick-only exclusion manifest is missing or empty" >&2
    exit 1
}

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
# except for the explicit package-flavor marker and the derived whole-artifact
# identity. Each artifact identity is independently recomputed by
# check-release.sh and must differ because package metadata and payload scope
# differ between profiles.
if ! find "$THICK_ROOT" -type f -print | while IFS= read -r thick_file; do
    relative=${thick_file#"$THICK_ROOT/"}
    case "$relative" in
        usr/share/pve-sharedlvmthin/package-flavor|usr/share/pve-sharedlvmthin/package-artifact-sha256)
            continue
            ;;
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

if ! find "$DUAL_ROOT" -type f -print | while IFS= read -r dual_file; do
    relative=${dual_file#"$DUAL_ROOT/"}
    case "$relative" in
        usr/share/pve-sharedlvmthin/package-flavor|usr/share/pve-sharedlvmthin/package-artifact-sha256)
            continue
            ;;
        usr/share/doc/pve-sharedlvmthin/*)
            doc_relative=${relative#usr/share/doc/pve-sharedlvmthin/}
            [ -f "$THICK_ROOT/usr/share/doc/pve-sharedlvmthin-thick/$doc_relative" ] || {
                echo "Thick-only relocated documentation is missing: $doc_relative" >&2
                exit 1
            }
            continue
            ;;
    esac
    [ ! -f "$THICK_ROOT/$relative" ] || continue
    if ! grep -Fxq "$relative" "$EXCLUSIONS"; then
        echo "unexpected Dual-only payload file: $relative" >&2
        exit 1
    fi
done; then
    exit 1
fi

DUAL_ARTIFACT=$(sed -n '1p' \
    "$DUAL_ROOT/usr/share/pve-sharedlvmthin/package-artifact-sha256")
THICK_ARTIFACT=$(sed -n '1p' \
    "$THICK_ROOT/usr/share/pve-sharedlvmthin/package-artifact-sha256")
case "$DUAL_ARTIFACT:$THICK_ARTIFACT" in
    *[!0-9a-f:]*|:*)
        echo "package-profile artifact identity is malformed" >&2
        exit 1
        ;;
esac
if [ "${#DUAL_ARTIFACT}" -ne 64 ] || [ "${#THICK_ARTIFACT}" -ne 64 ] || \
   [ "$DUAL_ARTIFACT" = "$THICK_ARTIFACT" ]; then
    echo "package-profile artifact identities are invalid or not profile-specific" >&2
    exit 1
fi

echo "dual/Thick-only shared payload parity: PASS"
