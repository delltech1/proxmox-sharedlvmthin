# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinMobility;

use strict;
use warnings;
use Digest::SHA qw(sha256_hex);
use Exporter qw(import);

our @EXPORT_OK = qw(
    mobility_names build_mobility_fingerprint evaluate_mobility_transition
);

sub mobility_names {
    my (%args) = @_;
    for my $key (qw(vmid disk transaction)) {
        die "missing mobility field $key\n" if !defined($args{$key});
    }
    die "invalid mobility VMID\n" if $args{vmid} !~ /^([1-9][0-9]{2,8})$/;
    my $vmid = $1;
    die "invalid mobility disk index\n" if $args{disk} !~ /^([0-9]{1,3})$/;
    my $disk = $1;
    die "invalid mobility transaction\n"
        if $args{transaction} !~ /^([0-9a-f]{32})$/;
    my $tx = $1;
    my $generation = substr($tx, 0, 12);
    return {
        generation => $generation,
        pool => "sltp-$vmid-m-$generation",
        volume => "vm-$vmid-disk-$disk-m-$generation",
        source_alias => "sltm-src-$generation",
        target_alias => "sltm-dst-$generation",
    };
}

sub build_mobility_fingerprint {
    my (%args) = @_;
    my @keys = qw(
        schema transaction vmid disk source_node target_node
        source_pool_uuid target_pool_uuid source_volume_uuid target_volume_uuid
        phase authority config_volume
    );
    my @canonical;
    for my $key (@keys) {
        die "missing mobility fingerprint field $key\n" if !defined($args{$key});
        die "unsafe mobility fingerprint field $key\n"
            if $args{$key} !~ /^[A-Za-z0-9_.:+~-]+$/;
        push @canonical, "$key=$args{$key}";
    }
    return sha256_hex(join("\n", @canonical) . "\n");
}

# Pure authority/recovery classifier for the Thin Generation Mobility copy
# transaction.  Source and target are independent thin metadata domains, so
# they may be active concurrently while QEMU mirrors between them.  Authority
# changes exactly once at QEMU pivot; a journal phase alone never proves it.
sub evaluate_mobility_transition {
    my (%e) = @_;
    my @required = qw(
        phase journal_integrity quorum identities_distinct
        source_pool target_pool source_qemu target_qemu
        mirror config_authority source_owner target_owner
    );
    for my $key (@required) {
        return _blocked("missing evidence: $key") if !defined($e{$key});
    }
    return _blocked('invalid mobility phase')
        if $e{phase} !~ /^(?:PREPARED|TARGET_ALLOCATED|MIRRORING|MIRROR_READY|PIVOT_COMMITTED|SOURCE_RETIRED|COMPLETED|ABORTED)$/;
    return _blocked('transaction journal integrity is not proven')
        if $e{journal_integrity} ne 'yes';
    return _blocked('cluster quorum is not positively proven')
        if $e{quorum} ne 'yes';
    return _blocked('source and target thin metadata identities are not distinct')
        if $e{identities_distinct} ne 'yes';
    for my $key (qw(source_pool target_pool source_qemu target_qemu)) {
        return _blocked("invalid $key evidence")
            if $e{$key} !~ /^(?:present|absent|unknown)$/;
    }
    return _blocked('invalid mirror evidence')
        if $e{mirror} !~ /^(?:absent|running|ready|completed|failed|unknown)$/;
    for my $key (qw(config_authority source_owner target_owner)) {
        return _blocked("invalid $key evidence")
            if $e{$key} !~ /^(?:source|target|none|unknown)$/;
    }

    my $prepivot = $e{phase} =~ /^(?:PREPARED|TARGET_ALLOCATED|MIRRORING|MIRROR_READY)$/;
    if ($prepivot) {
        return _blocked('pre-pivot configuration no longer names source')
            if $e{config_authority} ne 'source';
        return _blocked('source pool/QEMU ownership is not exact')
            if $e{source_pool} ne 'present' || $e{source_qemu} ne 'present'
            || $e{source_owner} ne 'source';
        return _blocked('target QEMU became authoritative before pivot')
            if $e{target_qemu} eq 'present';

        if ($e{phase} eq 'PREPARED') {
            return _blocked('target objects exist before allocation is recorded')
                if $e{target_pool} ne 'absent' || $e{target_owner} ne 'none';
            return _allow('ALLOCATE_TARGET', 'source remains authoritative');
        }
        return _blocked('target generation is not positively present')
            if $e{target_pool} ne 'present' || $e{target_owner} ne 'target';
        if ($e{phase} eq 'TARGET_ALLOCATED') {
            return _blocked('mirror exists before its start is recorded')
                if $e{mirror} ne 'absent';
            return _allow('START_NATIVE_MIRROR', 'independent target generation is ready');
        }
        if ($e{phase} eq 'MIRRORING') {
            return _blocked('mirror is not positively running')
                if $e{mirror} ne 'running';
            return _allow('WAIT_MIRROR_READY', 'source remains authoritative during copy');
        }
        return _blocked('mirror readiness is not positively proven')
            if $e{mirror} ne 'ready';
        return _allow('ALLOW_PVE_SWITCHOVER', 'active-sync mirror is ready for PVE pivot');
    }

    if ($e{phase} eq 'PIVOT_COMMITTED') {
        return _blocked('post-pivot configuration does not name target')
            if $e{config_authority} ne 'target';
        return _blocked('target pool/QEMU ownership is not exact')
            if $e{target_pool} ne 'present' || $e{target_qemu} ne 'present'
            || $e{target_owner} ne 'target';
        return _blocked('source QEMU still references the retired generation')
            if $e{source_qemu} ne 'absent';
        return _blocked('mirror completion is not positively proven')
            if $e{mirror} ne 'completed';
        return _allow('RETIRE_SOURCE', 'target is authoritative; source data is retained pending cleanup');
    }

    if ($e{phase} eq 'SOURCE_RETIRED') {
        return _blocked('retired source generation still exists')
            if $e{source_pool} ne 'absent' || $e{source_qemu} ne 'absent';
        return _blocked('target authority was lost after source retirement')
            if $e{config_authority} ne 'target' || $e{target_pool} ne 'present'
            || $e{target_qemu} ne 'present' || $e{target_owner} ne 'target';
        return _allow('FINALIZE', 'only the target generation remains');
    }

    if ($e{phase} eq 'COMPLETED') {
        return _blocked('completed transaction is not target-only')
            if $e{source_pool} ne 'absent' || $e{source_qemu} ne 'absent'
            || $e{target_pool} ne 'present' || $e{target_qemu} ne 'present'
            || $e{config_authority} ne 'target' || $e{target_owner} ne 'target';
        return _allow('NONE', 'Thin generation handoff is complete');
    }

    # ABORT is legal only before the PVE/QEMU pivot.  Target cleanup is a
    # separate verified action; the evaluator will not infer it from a failed
    # task or delete a target still referenced by QEMU.
    return _blocked('aborted transaction did not restore source authority')
        if $e{config_authority} ne 'source' || $e{source_pool} ne 'present'
        || $e{source_qemu} ne 'present' || $e{source_owner} ne 'source';
    return _blocked('aborted transaction retains target QEMU reference')
        if $e{target_qemu} ne 'absent';
    return _allow(
        $e{target_pool} eq 'present' ? 'DELETE_UNREFERENCED_TARGET' : 'NONE',
        'pre-pivot transaction safely returned to source',
    );
}

sub _allow {
    my ($action, $reason) = @_;
    return { safe => 1, state => 'SAFE', action => $action, reason => $reason };
}

sub _blocked {
    my ($reason) = @_;
    return { safe => 0, state => 'RECOVERY_REQUIRED', action => 'NONE', reason => $reason };
}

1;
