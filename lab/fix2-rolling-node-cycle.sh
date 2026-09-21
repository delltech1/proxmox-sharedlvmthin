#!/usr/bin/env bash
set -euo pipefail

# Disposable-lab rolling-cycle helper. Dry-run is the default. This script
# deliberately does not select disposable guests by name or ID.

usage() {
    cat <<'EOF'
Usage:
  fix2-rolling-node-cycle.sh snapshot --state-dir DIR
  fix2-rolling-node-cycle.sh stop-list --state-dir DIR --vmid-file FILE [--execute --confirm-node NODE]
  fix2-rolling-node-cycle.sh restore --state-dir DIR [--execute --confirm-node NODE]
  fix2-rolling-node-cycle.sh verify --state-dir DIR
  fix2-rolling-node-cycle.sh reboot-ready --state-dir DIR

Mutating actions are dry-run unless both --execute and --confirm-node matching
the local hostname are supplied. stop-list accepts only an explicit VMID file;
it never guesses which guests are disposable. The helper never runs reboot,
kills a PVE task at an arbitrary wall-clock deadline, or retries an ambiguous
mutation. Native PVE start/stop completion is awaited and then re-read.
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }
log() { printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"; }

action=${1:-}
[[ -n "$action" ]] || { usage; exit 2; }
shift

state_dir=
vmid_file=
execute=0
confirm_node=

while (($#)); do
    case "$1" in
        --state-dir) state_dir=${2:?}; shift 2 ;;
        --vmid-file) vmid_file=${2:?}; shift 2 ;;
        --execute) execute=1; shift ;;
        --confirm-node) confirm_node=${2:?}; shift 2 ;;
        -h|--help) usage; exit 0 ;;
        *) die "unknown argument: $1" ;;
    esac
done

[[ -n "$state_dir" ]] || die '--state-dir is required'
[[ "$state_dir" = /* ]] || die '--state-dir must be an absolute path'

node=$(hostname)
manifest="$state_dir/running-before.txt"
progress="$state_dir/progress.log"

require_cluster_health() {
    pvecm status | grep -q 'Quorate:[[:space:]]*Yes' || die 'cluster is not quorate'
    systemctl is-active --quiet pve-cluster corosync pvedaemon pvestatd || die 'required PVE service is not active'
    [[ $(pvesh get "/nodes/$node/tasks" --source active --output-format json) = '[]' ]] || die 'active PVE tasks exist'
}

require_execute() {
    if ((execute == 0)); then
        log "DRY-RUN: mutation suppressed; rerun with --execute --confirm-node $node"
        return 1
    fi
    [[ "$confirm_node" = "$node" ]] || die "--confirm-node must exactly equal $node"
    return 0
}

validate_vmid_file() {
    local file=$1
    [[ -s "$file" ]] || die "missing or empty VMID file: $file"
    awk '!/^[0-9]+$/ {bad=1} END {exit bad}' "$file" || die "invalid VMID in $file"
    [[ $(sort -n "$file" | uniq -d | wc -l) -eq 0 ]] || die "duplicate VMID in $file"
}

snapshot() {
    require_cluster_health
    mkdir -p -m 0700 "$state_dir"
    local tmp="$state_dir/.running-before.$$"
    qm list | awk 'NR > 1 && $3 == "running" {print $1}' | sort -n >"$tmp"
    [[ -s "$tmp" ]] || { rm -f "$tmp"; die 'no running VMs found; refusing ambiguous snapshot'; }
    mv "$tmp" "$manifest"
    chmod 0600 "$manifest"
    : >"$progress"
    chmod 0600 "$progress"
    log "SNAPSHOT node=$node running=$(wc -l <"$manifest") manifest=$manifest" | tee -a "$progress"
}

stop_list() {
    require_cluster_health
    validate_vmid_file "$manifest"
    [[ -n "$vmid_file" ]] || die '--vmid-file is required for stop-list'
    validate_vmid_file "$vmid_file"
    while read -r vmid; do
        grep -qx "$vmid" "$manifest" || die "VM $vmid was not running at snapshot"
    done <"$vmid_file"
    if ! require_execute; then
        while read -r vmid; do log "DRY-RUN STOP vmid=$vmid"; done <"$vmid_file"
        return 0
    fi
    while read -r vmid; do
        [[ $(qm status "$vmid" | awk '{print $2}') = running ]] || { log "SKIP already-stopped vmid=$vmid" | tee -a "$progress"; continue; }
        log "STOP_BEGIN vmid=$vmid wait=native-pve-task" | tee -a "$progress"
        qm stop "$vmid"
        [[ $(qm status "$vmid" | awk '{print $2}') = stopped ]] || die "VM $vmid did not stop"
        log "STOP_PASS vmid=$vmid" | tee -a "$progress"
    done <"$vmid_file"
}

restore() {
    require_cluster_health
    validate_vmid_file "$manifest"
    if ! require_execute; then
        while read -r vmid; do
            [[ $(qm status "$vmid" | awk '{print $2}') = running ]] || log "DRY-RUN START vmid=$vmid"
        done <"$manifest"
        return 0
    fi
    while read -r vmid; do
        [[ $(qm status "$vmid" | awk '{print $2}') = running ]] && { log "SKIP already-running vmid=$vmid" | tee -a "$progress"; continue; }
        log "START_BEGIN vmid=$vmid wait=native-pve-task" | tee -a "$progress"
        qm start "$vmid"
        [[ $(qm status "$vmid" | awk '{print $2}') = running ]] || die "VM $vmid did not start"
        log "START_PASS vmid=$vmid" | tee -a "$progress"
    done <"$manifest"
}

verify() {
    require_cluster_health
    validate_vmid_file "$manifest"
    local now="$state_dir/running-now.txt"
    qm list | awk 'NR > 1 && $3 == "running" {print $1}' | sort -n >"$now"
    diff -u "$manifest" "$now" || die 'running VM set differs from snapshot'
    systemctl --failed --no-legend | grep -q . && die 'failed systemd units exist'
    log "VERIFY_PASS node=$node running=$(wc -l <"$now")"
}

reboot_ready() {
    require_cluster_health
    validate_vmid_file "$manifest"
    log "REBOOT_READY node=$node running_now=$(qm list | awk 'NR > 1 && $3 == "running" {n++} END {print n+0}')"
    log 'This helper intentionally does not issue reboot; use the site change-control procedure.'
}

case "$action" in
    snapshot) snapshot ;;
    stop-list) stop_list ;;
    restore) restore ;;
    verify) verify ;;
    reboot-ready) reboot_ready ;;
    *) usage; die "unknown action: $action" ;;
esac
