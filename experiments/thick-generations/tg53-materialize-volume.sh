#!/usr/bin/env bash
set -Eeuo pipefail

STORAGE="${1:?usage: $0 STORAGE VOLUME EVIDENCE}"
VOLUME="${2:?usage: $0 STORAGE VOLUME EVIDENCE}"
EVIDENCE="${3:?usage: $0 STORAGE VOLUME EVIDENCE}"
VOLID="$STORAGE:$VOLUME"

[[ "$STORAGE" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]]
[[ "$VOLUME" =~ ^vm-[1-9][0-9]{2,8}-disk-[0-9]+$ ]]

exec > >(tee -a "$EVIDENCE") 2>&1
echo "TEST=tg53-materialize-volume"
echo "START_UTC=$(date -u +%FT%TZ)"
echo "NODE=$(hostname)"
echo "VOLID=$VOLID"

deactivate() {
    perl -MPVE::Storage -e '
        my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
        PVE::Storage::deactivate_volumes($cfg,[$volid]);
    ' "$VOLID"
}

# The materializing process is the storage worker.  It must never be killed by
# an observer deadline, and an interrupted/unknown outcome must not trigger a
# blind deactivation.  Leave the exact volume active for explicit recovery.
trap 'echo "RESULT=INTERRUPTED_UNKNOWN" >&2; echo "RECOVERY_REQUIRED=YES" >&2' HUP INT TERM

perl -MPVE::Storage -e '
    my ($volid)=@ARGV; my $cfg=PVE::Storage::config();
    PVE::Storage::activate_volumes($cfg,[$volid]);
' "$VOLID"

/usr/sbin/sharedlvmthin thick-lazy-materialize "$STORAGE" "$VOLUME"

deactivate
trap - HUP INT TERM
echo "END_UTC=$(date -u +%FT%TZ)"
echo "TG53_MATERIALIZE_VOLUME=PASS"
