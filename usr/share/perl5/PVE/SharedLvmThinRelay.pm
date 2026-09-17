# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinRelay;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(evaluate_relay_handoff);

# Pure recovery/admission evaluator for a zero-copy Thin ownership handoff.
# It deliberately does not perform fencing, QMP, SSH, or LVM operations.  The
# coordinator must gather positive evidence for every input and execute only
# the returned action.  UNKNOWN is never treated as absence.
sub evaluate_relay_handoff {
    my (%e) = @_;

    my @required = qw(
        phase quorum source_fenced source_mapper target_mapper
        source_qemu target_qemu source_flushed relay_connected
        journal_integrity owner
    );
    for my $key (@required) {
        return _blocked('RECOVERY_REQUIRED', "missing evidence: $key")
            if !defined($e{$key});
    }

    return _blocked('RECOVERY_REQUIRED', 'invalid phase')
        if $e{phase} !~ /^(?:PREPARED|RELAY_READY|QUIESCED|SOURCE_CLOSED|OWNERSHIP_COMMITTED|TARGET_ACTIVE|COMPLETED|ABORTED)$/;
    return _blocked('RECOVERY_REQUIRED', 'invalid quorum evidence')
        if $e{quorum} !~ /^(?:yes|no|unknown)$/;
    return _blocked('RECOVERY_REQUIRED', 'invalid source fencing evidence')
        if $e{source_fenced} !~ /^(?:yes|no|unknown)$/;
    for my $key (qw(source_mapper target_mapper source_qemu target_qemu)) {
        return _blocked('RECOVERY_REQUIRED', "invalid $key evidence")
            if $e{$key} !~ /^(?:present|absent|unknown)$/;
    }
    for my $key (qw(source_flushed relay_connected journal_integrity)) {
        return _blocked('RECOVERY_REQUIRED', "invalid $key evidence")
            if $e{$key} !~ /^(?:yes|no|unknown)$/;
    }
    return _blocked('RECOVERY_REQUIRED', 'invalid owner evidence')
        if $e{owner} !~ /^(?:source|target|none|unknown)$/;

    return _blocked('RECOVERY_REQUIRED', 'transaction journal integrity is not proven')
        if $e{journal_integrity} ne 'yes';
    return _blocked('QUORUM_LOST', 'cluster quorum is not positively proven')
        if $e{quorum} ne 'yes';

    # This is the non-negotiable dm-thin invariant.
    return _blocked('DUAL_ACTIVATION', 'the same thin metadata domain is active on source and target')
        if $e{source_mapper} eq 'present' && $e{target_mapper} eq 'present';

    my $precommit = $e{phase} =~ /^(?:PREPARED|RELAY_READY|QUIESCED|SOURCE_CLOSED)$/;
    if ($precommit) {
        return _blocked('RECOVERY_REQUIRED', 'pre-commit journal cannot name target as owner')
            if $e{owner} eq 'target';
        return _blocked('RECOVERY_REQUIRED', 'target mapper exists before ownership commit')
            if $e{target_mapper} ne 'absent';

        if ($e{phase} eq 'PREPARED') {
            return _blocked('RECOVERY_REQUIRED', 'source ownership or mapper is not proven')
                if $e{owner} ne 'source' || $e{source_mapper} ne 'present';
            return _allow('ESTABLISH_RELAY', 'source remains the sole dm-thin owner');
        }

        if ($e{phase} eq 'RELAY_READY') {
            return _blocked('RECOVERY_REQUIRED', 'relay is not positively connected')
                if $e{relay_connected} ne 'yes';
            return _blocked('RECOVERY_REQUIRED', 'source QEMU or mapper is not present')
                if $e{source_qemu} ne 'present' || $e{source_mapper} ne 'present';
            return _allow('QUIESCE_SOURCE', 'relay is ready and source remains authoritative');
        }

        if ($e{phase} eq 'QUIESCED') {
            return _blocked('RECOVERY_REQUIRED', 'source flush is not positively proven')
                if $e{source_flushed} ne 'yes';
            return _blocked('RECOVERY_REQUIRED', 'source mapper disappeared before close was recorded')
                if $e{source_mapper} ne 'present';
            return _allow('CLOSE_SOURCE', 'writes are quiesced and flushed');
        }

        # SOURCE_CLOSED is the only pre-commit phase in which the source
        # mapper may be absent.  A live source host must prove QEMU no longer
        # owns the device; a fenced host is acceptable evidence as well.
        return _blocked('RECOVERY_REQUIRED', 'source mapper is not positively absent')
            if $e{source_mapper} ne 'absent';
        return _blocked('RECOVERY_REQUIRED', 'source may still issue I/O')
            if $e{source_qemu} ne 'absent' && $e{source_fenced} ne 'yes';
        return _allow('COMMIT_TARGET_OWNERSHIP', 'source is closed; ownership CAS may proceed');
    }

    if ($e{phase} eq 'OWNERSHIP_COMMITTED') {
        return _blocked('RECOVERY_REQUIRED', 'committed transaction does not name target owner')
            if $e{owner} ne 'target';
        return _blocked('RECOVERY_REQUIRED', 'source mapper survived ownership commit')
            if $e{source_mapper} ne 'absent';
        return _blocked('RECOVERY_REQUIRED', 'source may still issue I/O after ownership commit')
            if $e{source_qemu} ne 'absent' && $e{source_fenced} ne 'yes';
        return _blocked('RECOVERY_REQUIRED', 'target mapper state is ambiguous')
            if $e{target_mapper} eq 'unknown';
        return _allow(
            $e{target_mapper} eq 'present' ? 'VERIFY_TARGET' : 'ACTIVATE_TARGET',
            'target is the committed sole owner',
        );
    }

    if ($e{phase} eq 'TARGET_ACTIVE') {
        return _blocked('RECOVERY_REQUIRED', 'target ownership/runtime is not exact')
            if $e{owner} ne 'target' || $e{target_mapper} ne 'present';
        return _blocked('RECOVERY_REQUIRED', 'source runtime reappeared after cutover')
            if $e{source_mapper} ne 'absent';
        return _allow('PIVOT_TARGET_LOCAL', 'target owns the only active thin metadata instance');
    }

    if ($e{phase} eq 'COMPLETED') {
        return _blocked('RECOVERY_REQUIRED', 'completed target is not positively healthy')
            if $e{owner} ne 'target' || $e{target_mapper} ne 'present'
            || $e{target_qemu} ne 'present' || $e{source_mapper} ne 'absent';
        return _allow('NONE', 'handoff is complete and target is authoritative');
    }

    # ABORTED is valid only before ownership transfer.  Recovery must never
    # reinterpret a target-owned transaction as an abort.
    return _blocked('RECOVERY_REQUIRED', 'aborted transaction retained target ownership')
        if $e{owner} eq 'target';
    return _blocked('RECOVERY_REQUIRED', 'aborted transaction has target runtime')
        if $e{target_mapper} ne 'absent';
    return _blocked('RECOVERY_REQUIRED', 'aborted source is not restored')
        if $e{owner} ne 'source' || $e{source_mapper} ne 'present';
    return _allow('NONE', 'pre-commit handoff was safely aborted to source');
}

sub _allow {
    my ($action, $reason) = @_;
    return {
        safe => 1,
        state => 'SAFE',
        action => $action,
        reason => $reason,
    };
}

sub _blocked {
    my ($state, $reason) = @_;
    return {
        safe => 0,
        state => $state,
        action => 'NONE',
        reason => $reason,
    };
}

1;
