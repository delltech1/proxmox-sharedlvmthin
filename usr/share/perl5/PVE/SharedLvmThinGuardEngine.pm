# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinGuardEngine;

use strict;
use warnings;

use PVE::SharedLvmThinGuard qw(evaluate_guard_activation);
use PVE::SharedLvmThinGuardState;

sub new {
    my ($class, %args) = @_;
    die "guardian engine needs an inventory provider\n"
        if ref($args{inventory_provider}) ne 'CODE';
    die "guardian engine needs a watchdog adapter\n"
        if !defined($args{watchdog}) || !$args{watchdog}->can('arm')
        || !$args{watchdog}->can('refresh') || !$args{watchdog}->can('clean_disarm');
    my $state = PVE::SharedLvmThinGuardState->new(
        pending_timeout => $args{pending_timeout} // 45,
    );
    return bless({
        inventory_provider => $args{inventory_provider},
        watchdog => $args{watchdog},
        state => $state,
        storage_by_pool => {},
        mapper_by_pool => {},
    }, $class);
}

sub state { return $_[0]->{state}->state(); }

sub status {
    my ($self) = @_;
    return {
        state => $self->{state}->state(),
        reason => $self->{state}->reason(),
        epochs => $self->{state}->epochs(),
        watchdog_state => $self->{watchdog}->state(),
    };
}

sub _inventory {
    my ($self, %query) = @_;
    my $inventory = $self->{inventory_provider}->(%query);
    die "guardian inventory provider returned no positive inventory\n"
        if ref($inventory) ne 'HASH' || !$inventory->{inventory_ok};
    return $inventory;
}

sub prepare {
    my ($self, $request, $now) = @_;
    die "guardian PREPARE request is malformed\n" if ref($request) ne 'HASH';
    my $inventory = $self->_inventory(
        op => 'PREPARE', storage_id => $request->{storage_id},
        pool_uuid => $request->{pool_uuid}, owner_epoch => $request->{owner_epoch},
        mapper_uuid => $request->{mapper_uuid},
    );
    my @matches = grep {
        ($_->{pool_uuid} // '') eq ($request->{pool_uuid} // '')
    } @{$inventory->{pools} // []};
    die "guardian PREPARE needs exactly one inventory pool\n" if @matches != 1;
    my $pool = $matches[0];
    die "guardian PREPARE owner epoch mismatch\n"
        if ($pool->{owner_epoch} // '') ne ($request->{owner_epoch} // '');
    die "guardian PREPARE mapper UUID mismatch\n"
        if ($pool->{mapper_uuid} // '') ne ($request->{mapper_uuid} // '');
    die "guardian PREPARE requires an inactive local mapper and no QEMU reference\n"
        if $pool->{locally_active} || $pool->{qemu_reference_exact};

    my $admission_evidence = $inventory->{admission};
    die "guardian PREPARE lacks independently collected admission evidence\n"
        if ref($admission_evidence) ne 'HASH';
    my %admission = (%$admission_evidence,
        # The daemon process owns both this state and the watchdog descriptor.
        # A crash drops the descriptor without magic close and fences the host;
        # a new process may not inherit or reconstruct an active epoch.
        journal_integrity => $self->{state}->state() ne 'FENCING' ? 1 : 0,
        watchdog_armed => 1,
    );
    my $decision = evaluate_guard_activation(%admission);
    die "guardian PREPARE refused: $decision->{reason}\n" if !$decision->{safe};

    my $result = $self->{state}->prepare(
        admission => $decision,
        pool_uuid => $pool->{pool_uuid},
        owner_epoch => $pool->{owner_epoch},
        mapper_uuid => $pool->{mapper_uuid},
        now => $now,
    );
    if ($self->{watchdog}->state() eq 'DISARMED') {
        eval { $self->{watchdog}->arm($decision); };
        die "guardian watchdog arm failed after epoch registration: $@" if $@;
    } elsif ($self->{watchdog}->state() ne 'ARMED') {
        die "guardian watchdog is not in an armable state\n";
    }
    $self->{storage_by_pool}->{$pool->{pool_uuid}} = $request->{storage_id};
    $self->{mapper_by_pool}->{$pool->{pool_uuid}} = $pool->{mapper_uuid};
    return $result;
}

sub observe {
    my ($self, $now) = @_;
    my %storage = map { $_ => 1 } values %{$self->{storage_by_pool}};
    my $inventory = $self->_inventory(op => 'OBSERVE', storage_ids => [sort keys %storage]);
    my $decision = $self->{state}->observe(
        now => $now,
        quorum => $inventory->{quorum},
        local_node => $inventory->{local_node},
        pools => $inventory->{pools},
    );
    if ($decision->{action} eq 'REFRESH_WATCHDOG') {
        my $ok = $self->{watchdog}->refresh({safe => 1, action => 'REFRESH_WATCHDOG'});
        die "guardian watchdog refresh failed\n" if !$ok;
    } elsif ($decision->{action} eq 'STOP_WATCHDOG_REFRESH') {
        $self->{watchdog}->refresh({safe => 0, action => 'STOP_WATCHDOG_REFRESH'});
    }
    return $decision;
}

sub release {
    my ($self, $request) = @_;
    die "guardian RELEASE request is malformed\n" if ref($request) ne 'HASH';
    my $storage_id = $self->{storage_by_pool}->{$request->{pool_uuid}}
        or die "guardian RELEASE has no storage binding for pool\n";
    my $mapper_uuid = $self->{mapper_by_pool}->{$request->{pool_uuid}}
        or die "guardian RELEASE has no mapper binding for pool\n";
    my $inventory = $self->_inventory(
        op => 'RELEASE', pool_uuid => $request->{pool_uuid},
        owner_epoch => $request->{owner_epoch}, storage_id => $storage_id,
        mapper_uuid => $mapper_uuid,
    );
    my $proof = $inventory->{release_proof};
    die "guardian RELEASE lacks independently collected teardown proof\n"
        if ref($proof) ne 'HASH';
    my $decision = $self->{state}->release(
        pool_uuid => $request->{pool_uuid},
        owner_epoch => $request->{owner_epoch},
        map { $_ => $proof->{$_} } qw(qemu_absent mapper_absent owner_released),
    );
    if ($decision->{action} eq 'CLEAN_DISARM') {
        $self->{watchdog}->clean_disarm(
            all_qemu_absent => 1, all_mappers_absent => 1, owner_epochs_released => 1,
        );
    }
    delete $self->{storage_by_pool}->{$request->{pool_uuid}};
    delete $self->{mapper_by_pool}->{$request->{pool_uuid}};
    return $decision;
}

1;
