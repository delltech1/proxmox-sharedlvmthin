#!/bin/sh
# Copyright (C) 2026 BASTRIX Project Contributors
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
dpkg-deb --control "$PACKAGE" "$TMP/control"

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

for maintscript in preinst postinst prerm postrm; do
    if [ ! -f "$TMP/control/$maintscript" ]; then
        echo "required maintainer script is missing: $maintscript" >&2
        exit 1
    fi
    sh -n "$TMP/control/$maintscript"
done

CANDIDATE_RECOVERY="$TMP/control/sharedlvmthin-candidate-recovery-check"
PAYLOAD_RECOVERY="$TMP/root/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check"
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
python3 -m py_compile "$CANDIDATE_RECOVERY"
grep -Fq 'sharedlvmthin-candidate-recovery-check' "$TMP/control/preinst" || {
    echo "preinst does not invoke the candidate control-archive recovery checker" >&2
    exit 1
}

PACKAGE_NAME=$(dpkg-deb -f "$PACKAGE" Package)
FLAVOR_FILE="$TMP/root/usr/share/pve-sharedlvmthin/package-flavor"
if [ ! -f "$FLAVOR_FILE" ]; then
    echo "package flavor marker is missing" >&2
    exit 1
fi
FLAVOR=$(sed -n '1p' "$FLAVOR_FILE")
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
    usr/share/perl5/PVE/SharedLvmThinThick.pm \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check \
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
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check \
    usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json \
    usr/sbin/sharedlvmthin
do
    if [ ! -x "$TMP/root/$executable_path" ]; then
        echo "required program is not executable: $executable_path" >&2
        exit 1
    fi
done

if [ "$FLAVOR" = "thick-only" ]; then
    if [ -e "$TMP/root/usr/share/doc/pve-sharedlvmthin" ]; then
        echo "dual-package documentation namespace leaked into Thick-only package" >&2
        exit 1
    fi
    for forbidden_path in \
        lib/systemd/system/pve-sharedlvmthin-thin-guard.service \
        usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor \
        usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd \
        usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory \
        usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-metadata-check \
        usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence \
        usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-import \
        usr/sbin/sharedlvmthin-migrate-bridge
    do
        if [ -e "$TMP/root/$forbidden_path" ]; then
            echo "Thin operational path leaked into Thick-only package: $forbidden_path" >&2
            exit 1
        fi
    done

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
