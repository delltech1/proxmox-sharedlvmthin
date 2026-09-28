#!/bin/bash
# PRE-LIVE: plan-only by default. The live backend is deliberately unavailable.
set -euo pipefail
umask 077
script_path=$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")
script_dir=${script_path%/*}
exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C \
    /usr/bin/python3 -I -B "$script_dir/lazy-zero-loop-lab.py" "$@"
