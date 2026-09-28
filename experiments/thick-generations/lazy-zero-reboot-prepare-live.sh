#!/bin/bash
# Create one disposable fixture and drive it only to witnessed reboot-ready state.
set -euo pipefail
umask 077

host='' storeid='' vg='' tx='' nonce=''
while (($#)); do
    case $1 in
        --expect-host) host=${2:-}; shift 2 ;;
        --storeid) storeid=${2:-}; shift 2 ;;
        --vg) vg=${2:-}; shift 2 ;;
        --tx) tx=${2:-}; shift 2 ;;
        --nonce) nonce=${2:-}; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 64 ;;
    esac
done
[[ $(hostname) == "$host" && $storeid =~ ^[A-Za-z0-9_.-]+$ \
    && $vg =~ ^[A-Za-z0-9+_.-]+$ && $tx =~ ^[0-9a-f]{32}$ \
    && $nonce =~ ^[0-9a-f]{32}$ ]] || exit 64

here=$(dirname "$0")
lvm_helper="$here/lazy-zero-shared-lvm-lab.pl"
launcher="$here/lazy-zero-reboot-prepare.py"
[[ -f $lvm_helper && ! -L $lvm_helper && -f $launcher && ! -L $launcher ]] \
    || { echo "unsafe controlled reboot helper set" >&2; exit 2; }

root="/var/tmp/slt-lazy-shared-$tx"
setup_log="/var/tmp/slt-lazy-reboot-setup-$tx.log"
setup_json="/var/tmp/slt-lazy-reboot-setup-$tx.json"
[[ ! -e $root && ! -e $setup_log && ! -e $setup_json ]] \
    || { echo "controlled reboot fixture collision" >&2; exit 2; }

perl -I/usr/share/perl5 "$lvm_helper" setup --storeid "$storeid" \
    --tx "$tx" --nonce "$nonce" --data-gib 8 | tee "$setup_log"
tail -n1 "$setup_log" >"$setup_json.tmp"
chmod 0600 "$setup_json.tmp"
python3 -I -B - "$setup_json.tmp" "$tx" "$nonce" <<'PY'
import json, pathlib, sys
path = pathlib.Path(sys.argv[1])
value = json.loads(path.read_text(encoding="utf-8"))
if (value.get("classification") != "LAB_LVS_CREATED_INTENT_HELD"
        or value.get("tx") != sys.argv[2]
        or value.get("object") != "lazy-" + sys.argv[3]):
    raise SystemExit("setup result identity mismatch")
PY
sync -f "$setup_json.tmp"
mv -n "$setup_json.tmp" "$setup_json"
sync -f /var/tmp

readarray -t fields < <(python3 -I -B - "$setup_json" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
for path in (("before",), ("data", "uuid"), ("metadata", "uuid")):
    item = value
    for key in path:
        item = item[key]
    print(item)
PY
)
[[ ${#fields[@]} -eq 3 ]] || { echo "setup result field count" >&2; exit 2; }
before=${fields[0]}; data_uuid=${fields[1]}; meta_uuid=${fields[2]}

python3 -I -B "$launcher" --expect-host "$host" --storeid "$storeid" \
    --vg "$vg" --tx "$tx" --nonce "$nonce" --before "$before" \
    --data-uuid "$data_uuid" --meta-uuid "$meta_uuid"
printf 'TX=%s\nNONCE=%s\nBEFORE=%s\nDATA_UUID=%s\nMETA_UUID=%s\n' \
    "$tx" "$nonce" "$before" "$data_uuid" "$meta_uuid"
echo 'RESULT=CONTROLLED_REBOOT_FIXTURE_PREPARED_NO_REBOOT_DISPATCHED'
