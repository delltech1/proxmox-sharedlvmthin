#!/usr/bin/env bash
set -euo pipefail

observer=${1:-experiments/thick-generations/live-hydration-observer.sh}
[[ -f $observer ]] || { echo "ERROR: observer script not found" >&2; exit 2; }

workdir=$(mktemp -d /var/tmp/sltg-observer-fake.XXXXXXXX)
cleanup() {
    case $workdir in
        /var/tmp/sltg-observer-fake.*) rm -rf -- "$workdir" ;;
        *) echo "ERROR: refusing unexpected cleanup path: $workdir" >&2 ;;
    esac
}
trap cleanup EXIT INT TERM
mkdir "$workdir/bin"

cat >"$workdir/bin/dmsetup" <<'FAKE'
#!/usr/bin/env bash
set -euo pipefail
[[ ${1:-} == status && ${2:-} == --noflush && $# -eq 3 ]] || {
    echo "unexpected dmsetup mutation or arguments" >&2
    exit 90
}
count=0
[[ ! -f $SLTG_FAKE_COUNT ]] || count=$(<"$SLTG_FAKE_COUNT")
count=$((count + 1))
printf '%s\n' "$count" >"$SLTG_FAKE_COUNT"
case $SLTG_FAKE_CASE:$count in
    regression:1) echo '0 8192 clone 8 1/5120 128 10/100 0 1 no_hydration 0 rw' ;;
    regression:*) echo '0 8192 clone 8 1/5120 128 9/100 0 1 no_hydration 0 rw' ;;
    geometry:1) echo '0 8192 clone 8 1/5120 128 10/100 0 1 no_hydration 0 rw' ;;
    geometry:*) echo '0 8192 clone 8 1/5120 256 11/50 0 1 no_hydration 0 rw' ;;
    *) echo "unknown fake case" >&2; exit 91 ;;
esac
FAKE
chmod 0700 "$workdir/bin/dmsetup"

run_case() {
    local case_name expected output rc
    case_name=$1
    expected=$2
    output=$workdir/$case_name.out
    : >"$workdir/count"
    set +e
    PATH="$workdir/bin:$PATH" SLTG_FAKE_CASE="$case_name" \
        SLTG_FAKE_COUNT="$workdir/count" \
        bash "$observer" fake-mapper "$$" 1 0 >"$output" 2>&1
    rc=$?
    set -e
    [[ $rc -eq 4 ]]
    grep -qx "RESULT=$expected" "$output"
    [[ $(<"$workdir/count") -eq 2 ]]
    echo "${case_name^^}=PASS"
}

run_case regression PROGRESS_REGRESSION
run_case geometry GEOMETRY_CHANGED
echo "FAKE_MATRIX=PASS"
