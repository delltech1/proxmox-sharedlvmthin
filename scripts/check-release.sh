#!/bin/sh
# Copyright (C) 2026 Stanislav Baran
# SPDX-License-Identifier: GPL-3.0-only

set -eu

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
