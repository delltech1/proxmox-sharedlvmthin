# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinGuard;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(
    evaluate_guard_activation evaluate_guard_runtime evaluate_guard_handoff
    evaluate_guard_node evaluate_guard_topology
);

sub _decision {
    my ($safe, $state, $action, $reason) = @_;
    return {
        safe => $safe ? 1 : 0,
        state => $state,
        action => $action,
        reason => $reason,
    };
}

sub _bool {
    my ($e, $name) = @_;
    return undef if !defined($e->{$name}) || $e->{$name} !~ /^(?:0|1)$/;
    return 0 + $e->{$name};
}

# Quorum-Fenced Activation Lease (QFAL) admission classifier.  It never
# guesses from a stale owner tag.  A takeover requires either no predecessor
# or positive fencing evidence for the exact predecessor epoch.
sub evaluate_guard_activation {
    my (%e) = @_;
    for my $name (qw(
        journal_integrity quorum storage_identity local_runtime_absent
        remote_runtime_absent watchdog_available watchdog_armed
        owner_epoch_exact predecessor_fenced
    )) {
        my $value = _bool(\%e, $name);
        return _decision(0, 'RECOVERY_REQUIRED', 'NONE', "missing or malformed evidence: $name")
            if !defined($value);
    }
    return _decision(0, 'RECOVERY_REQUIRED', 'NONE', 'invalid predecessor state')
        if !defined($e{predecessor}) || $e{predecessor} !~ /^(?:none|self|other)$/;
    return _decision(0, 'RECOVERY_REQUIRED', 'NONE', 'invalid requested owner relation')
        if !defined($e{requested_owner}) || $e{requested_owner} !~ /^(?:self|other)$/;

    for my $name (qw(journal_integrity quorum storage_identity owner_epoch_exact)) {
        return _decision(0, 'RECOVERY_REQUIRED', 'NONE', "$name is not positively proven")
            if !$e{$name};
    }
    return _decision(0, 'REFUSED', 'NONE', 'lease request does not name this node')
        if $e{requested_owner} ne 'self';
    return _decision(0, 'REFUSED', 'NONE', 'local thin-pool runtime already exists before admission')
        if !$e{local_runtime_absent};
    return _decision(0, 'REFUSED', 'NONE', 'peer thin-pool runtime absence is unproven')
        if !$e{remote_runtime_absent};
    return _decision(0, 'REFUSED', 'NONE', 'PVE watchdog-mux protection is unavailable')
        if !$e{watchdog_available};

    if ($e{predecessor} eq 'other' && !$e{predecessor_fenced}) {
        return _decision(0, 'WAIT_FENCE', 'NONE',
            'previous owner epoch is not positively fenced; activation remains forbidden');
    }
    return _decision(0, 'REFUSED', 'NONE', 'watchdog client is not armed before activation')
        if !$e{watchdog_armed};

    return _decision(1, 'ADMITTED', 'ACTIVATE_EXCLUSIVE',
        'quorum epoch, storage identity, remote absence and fencing evidence are exact');
}

# Runtime guard classifier.  The caller keeps an irreversible uncertainty
# latch for an activation epoch.  Once set, fresh-looking observations cannot
# resume I/O: an explicit stop/fence and a new epoch are required.
sub evaluate_guard_runtime {
    my (%e) = @_;
    for my $name (qw(
        uncertainty_latched quorum storage_identity owner_epoch_exact
        local_mapper_exact qemu_reference_exact remote_conflict watchdog_armed
    )) {
        my $value = _bool(\%e, $name);
        return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
            "missing or malformed runtime evidence: $name") if !defined($value);
    }

    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'activation epoch was already marked uncertain') if $e{uncertainty_latched};
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'watchdog protection disappeared while thin metadata is active') if !$e{watchdog_armed};
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'cluster quorum was lost while thin metadata is active') if !$e{quorum};
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'storage identity or owner epoch changed while active')
        if !$e{storage_identity} || !$e{owner_epoch_exact};
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'a peer reports the same thin-pool runtime active') if $e{remote_conflict};
    return _decision(0, 'RECOVERY_REQUIRED', 'PAUSE_VM_AND_WITHDRAW',
        'local mapper or QEMU dependency no longer matches the admitted epoch')
        if !$e{local_mapper_exact} || !$e{qemu_reference_exact};

    return _decision(1, 'HEALTHY', 'REFRESH_WATCHDOG',
        'all runtime authority evidence remains positively verified');
}

# One watchdog-mux client protects every admitted Thin pool on a node.  PVE's
# multiplexer has a finite client table, so per-pool clients would not scale.
# Aggregation is intentionally strict: one uncertain active pool prevents the
# node client from refreshing and therefore protects all metadata domains from
# a partially failed guardian.
sub evaluate_guard_node {
    my (%e) = @_;
    my $daemon_healthy = _bool(\%e, 'daemon_healthy');
    my $watchdog_connected = _bool(\%e, 'watchdog_connected');
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'guardian daemon health is missing or negative')
        if !defined($daemon_healthy) || !$daemon_healthy;
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'aggregate watchdog connection is missing or negative')
        if !defined($watchdog_connected) || !$watchdog_connected;
    return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
        'active pool evidence is unavailable') if ref($e{pools}) ne 'ARRAY';

    my %seen;
    for my $pool (@{$e{pools}}) {
        return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
            'active pool evidence is malformed')
            if ref($pool) ne 'HASH'
            || ($pool->{pool_uuid} // '') !~ /^[A-Za-z0-9_.:+-]+$/
            || $seen{$pool->{pool_uuid}}++;
        my $decision = evaluate_guard_runtime(%$pool);
        return _decision(0, 'FENCE_REQUIRED', 'STOP_WATCHDOG_REFRESH',
            "pool $pool->{pool_uuid}: $decision->{reason}") if !$decision->{safe};
    }

    return _decision(1, 'HEALTHY', 'REFRESH_WATCHDOG',
        @{$e{pools}}
            ? 'all active Thin authority epochs are positively verified'
            : 'no active Thin pools; watchdog client may close cleanly');
}

# Topology policy is separate from per-pool authority. Two-node clusters are
# supported for normal and planned operation, but without a third vote they
# cannot safely infer which side of a partition may take over. An explicit
# external fence plus restored quorum can authorize a manual takeover.
sub evaluate_guard_topology {
    my (%e) = @_;
    for my $name (qw(configured_nodes online_nodes)) {
        return _decision(0, 'RECOVERY_REQUIRED', 'NONE', "invalid topology field: $name")
            if !defined($e{$name}) || $e{$name} !~ /^\d+$/;
    }
    return _decision(0, 'UNSUPPORTED', 'NONE', 'a cluster requires at least two configured nodes')
        if $e{configured_nodes} < 2;
    return _decision(0, 'RECOVERY_REQUIRED', 'NONE', 'online node count exceeds configured topology')
        if $e{online_nodes} > $e{configured_nodes};
    for my $name (qw(quorum qdevice external_fence)) {
        my $value = _bool(\%e, $name);
        return _decision(0, 'RECOVERY_REQUIRED', 'NONE', "missing or malformed topology field: $name")
            if !defined($value);
    }

    if ($e{configured_nodes} == 2 && !$e{qdevice}) {
        if ($e{online_nodes} == 2 && $e{quorum}) {
            my $result = _decision(1, 'SUPPORTED_GUARDED', 'ALLOW_PLANNED_OPERATIONS',
                'two-node cluster is healthy; automatic single-survivor failover is unavailable without a third vote');
            $result->{automatic_failover} = 0;
            $result->{warning} = 'NO_THIRD_VOTE';
            return $result;
        }
        if ($e{online_nodes} == 1 && $e{quorum} && $e{external_fence}) {
            my $result = _decision(1, 'MANUAL_FENCED_TAKEOVER', 'ALLOW_EXPLICIT_TAKEOVER',
                'single survivor has restored quorum and positive external fencing evidence');
            $result->{automatic_failover} = 0;
            $result->{warning} = 'OPERATOR_FENCING_ASSERTION_REQUIRED';
            return $result;
        }
        my $result = _decision(0, 'RECOVERY_REQUIRED', 'NONE',
            'two-node survivor lacks a third vote or positive external-fence plus restored-quorum evidence');
        $result->{automatic_failover} = 0;
        $result->{warning} = 'NO_THIRD_VOTE';
        return $result;
    }

    return _decision(0, 'RECOVERY_REQUIRED', 'NONE', 'cluster quorum is not positively proven')
        if !$e{quorum};
    my $result = _decision(1, 'SUPPORTED', 'ALLOW_FENCED_OPERATIONS',
        'cluster topology has positive quorum; per-pool fencing evidence remains mandatory');
    $result->{automatic_failover} = 1;
    return $result;
}

# Planned handoff deliberately has a no-authority gap.  It never permits the
# target to overlap the source metadata runtime.  Live mobility must instead
# use SharedLvmThinMobility with two independent metadata UUIDs.
sub evaluate_guard_handoff {
    my (%e) = @_;
    for my $name (qw(
        source_qemu_absent source_mapper_absent source_watchdog_released
        new_epoch_committed target_watchdog_armed target_runtime_absent
        quorum storage_identity
    )) {
        my $value = _bool(\%e, $name);
        return _decision(0, 'RECOVERY_REQUIRED', 'NONE',
            "missing or malformed handoff evidence: $name") if !defined($value);
    }
    for my $name (qw(
        source_qemu_absent source_mapper_absent source_watchdog_released
        new_epoch_committed target_watchdog_armed target_runtime_absent
        quorum storage_identity
    )) {
        return _decision(0, 'HANDOFF_BLOCKED', 'NONE', "$name is not positively proven")
            if !$e{$name};
    }
    return _decision(1, 'HANDOFF_READY', 'ACTIVATE_TARGET',
        'source is drained and a new fenced target epoch is committed');
}

1;
