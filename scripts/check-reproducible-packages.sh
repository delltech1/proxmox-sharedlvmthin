#!/bin/sh
# Prove byte-for-byte reproducibility of both binary package profiles.

set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT HUP INT TERM

build_pair() {
    flavor=$1
    first="$TMP/$flavor-first"
    second="$TMP/$flavor-second"

    sh "$ROOT/scripts/build.sh" "$first" "$flavor" >/dev/null
    sh "$ROOT/scripts/build.sh" "$second" "$flavor" >/dev/null

    set -- "$first"/*.deb
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "$flavor first build did not produce exactly one package candidate" >&2
        exit 1
    fi
    first_deb=$1
    set -- "$second"/*.deb
    if [ "$#" -ne 1 ] || [ ! -f "$1" ]; then
        echo "$flavor second build did not produce exactly one package candidate" >&2
        exit 1
    fi
    second_deb=$1

    if ! cmp -s "$first_deb" "$second_deb"; then
        echo "$flavor package is not byte-for-byte reproducible" >&2
        exit 1
    fi
    if ! cmp -s "$first/SHA256SUMS" "$second/SHA256SUMS"; then
        echo "$flavor checksum manifest is not reproducible" >&2
        exit 1
    fi
}

build_pair dual
build_pair thick-only
echo "dual/Thick-only reproducible package builds: PASS"
