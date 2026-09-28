#!/bin/bash
# Inert wrapper: the Python model has no subprocess or storage backend.
set -euo pipefail
script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/bin/python3 -I -B "$script_dir/lvm-sync-io-ab.py" "$@"
