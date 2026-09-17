# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinGuardState;

use strict;
use warnings;

sub new {
    my ($class, %args) = @_;
    die "pending deadline must be between 10 and 300 seconds\n"
        if !defined($args{pending_timeout}) || $args{pending_timeout} !~ /^\d+$/
        || $args{pending_timeout} < 10 || $args{pending_timeout} > 300;
    return bless({
        state => 'IDLE',
        pending_timeout => 0 + $args{pending_timeout},
        epochs => {},
        uncertainty_latched => 0,
        reason => 'no protected Thin epochs',
    }, $class);
}

sub state { return $_[0]->{state}; }
sub reason { return $_[0]->{reason}; }
sub epochs { return [sort keys %{$_[0]->{epochs}}]; }

sub _fence {
    my ($self, $reason) = @_;
    $self->{state} = 'FENCING';
    $self->{uncertainty_latched} = 1;
    $self->{reason} = $reason;
    return {action => 'STOP_WATCHDOG_REFRESH', state => $self->{state}, reason => $reason};
}

sub prepare {
    my ($self, %request) = @_;
    die "guardian is irreversibly fencing\n" if $self->{uncertainty_latched};
    die "guardian admission is malformed\n"
        if ref($request{admission}) ne 'HASH'
        || !$request{admission}->{safe}
        || ($request{admission}->{action} // '') ne 'ACTIVATE_EXCLUSIVE';
    for my $name (qw(pool_uuid owner_epoch mapper_uuid)) {
        die "guardian request field '$name' is malformed\n"
            if !defined($request{$name}) || $request{$name} !~ /^[A-Za-z0-9_.:+-]+$/;
    }
    die "guardian time is malformed\n"
        if !defined($request{now}) || $request{now} !~ /^\d+(?:\.\d+)?$/;
    die "pool already has a different protected epoch\n"
        if exists($self->{epochs}->{$request{pool_uuid}})
        && $self->{epochs}->{$request{pool_uuid}}->{owner_epoch} ne $request{owner_epoch};

    $self->{epochs}->{$request{pool_uuid}} = {
        owner_epoch => $request{owner_epoch},
        mapper_uuid => $request{mapper_uuid},
        phase => 'PENDING_ATTACH',
        deadline => $request{now} + $self->{pending_timeout},
    };
    $self->{state} = 'ARMED_PENDING';
    $self->{reason} = 'watchdog armed before local thin-pool activation';
    return {action => 'ACK_PREPARED', state => $self->{state}};
}

sub observe {
    my ($self, %sample) = @_;
    return $self->_fence('runtime sample is malformed')
        if ref($sample{pools}) ne 'ARRAY'
        || !defined($sample{now}) || $sample{now} !~ /^\d+(?:\.\d+)?$/
        || !defined($sample{quorum}) || $sample{quorum} !~ /^(?:0|1)$/;
    return $self->_fence('cluster quorum was lost') if !$sample{quorum};
    return $self->_fence('guardian was already marked uncertain')
        if $self->{uncertainty_latched};

    my %observed;
    for my $pool (@{$sample{pools}}) {
        return $self->_fence('runtime pool sample is malformed')
            if ref($pool) ne 'HASH' || !defined($pool->{pool_uuid})
            || $observed{$pool->{pool_uuid}}++;
    }

    my $all_protected = 1;
    for my $pool_uuid (keys %{$self->{epochs}}) {
        my $epoch = $self->{epochs}->{$pool_uuid};
        my ($pool) = grep { ($_->{pool_uuid} // '') eq $pool_uuid } @{$sample{pools}};
        return $self->_fence("protected pool $pool_uuid disappeared from inventory")
            if !defined($pool);
        return $self->_fence("owner epoch changed for pool $pool_uuid")
            if ($pool->{owner_epoch} // '') ne $epoch->{owner_epoch};
        return $self->_fence("mapper UUID changed for pool $pool_uuid")
            if ($pool->{mapper_uuid} // '') ne $epoch->{mapper_uuid};
        return $self->_fence("pool $pool_uuid is owned by another node")
            if ($pool->{owner_node} // '') ne ($sample{local_node} // '');

        if ($epoch->{phase} eq 'PENDING_ATTACH') {
            if ($pool->{locally_active} && $pool->{qemu_reference_exact}) {
                $epoch->{phase} = 'PROTECTED';
            } elsif ($sample{now} >= $epoch->{deadline}) {
                return $self->_fence("QEMU attach deadline expired for pool $pool_uuid");
            } elsif ($pool->{qemu_reference_exact} && !$pool->{locally_active}) {
                return $self->_fence("QEMU references pool $pool_uuid without its exact mapper");
            }
        }
        if ($epoch->{phase} eq 'PROTECTED') {
            return $self->_fence("protected runtime disappeared for pool $pool_uuid")
                if !$pool->{locally_active} || !$pool->{qemu_reference_exact};
        } else {
            $all_protected = 0;
        }
    }

    $self->{state} = $all_protected && keys(%{$self->{epochs}})
        ? 'PROTECTED' : keys(%{$self->{epochs}}) ? 'ARMED_PENDING' : 'IDLE';
    $self->{reason} = $self->{state} eq 'PROTECTED'
        ? 'all Thin epochs have exact mapper and QEMU evidence'
        : $self->{state} eq 'ARMED_PENDING'
            ? 'waiting for bounded QEMU attach'
            : 'no protected Thin epochs';
    return {action => keys(%{$self->{epochs}}) ? 'REFRESH_WATCHDOG' : 'NONE', state => $self->{state}};
}

sub release {
    my ($self, %proof) = @_;
    die "guardian cannot release while fencing\n" if $self->{uncertainty_latched};
    my $pool_uuid = $proof{pool_uuid} // '';
    my $epoch = $self->{epochs}->{$pool_uuid}
        or die "guardian has no exact protected pool epoch\n";
    die "guardian release epoch mismatch\n"
        if ($proof{owner_epoch} // '') ne $epoch->{owner_epoch};
    for my $field (qw(qemu_absent mapper_absent owner_released)) {
        die "guardian release lacks positive $field proof\n"
            if !defined($proof{$field}) || $proof{$field} ne '1';
    }
    delete $self->{epochs}->{$pool_uuid};
    $self->{state} = keys(%{$self->{epochs}}) ? 'PROTECTED' : 'IDLE';
    $self->{reason} = keys(%{$self->{epochs}})
        ? 'other Thin epochs remain protected' : 'all Thin epochs released';
    return {action => keys(%{$self->{epochs}}) ? 'REFRESH_WATCHDOG' : 'CLEAN_DISARM', state => $self->{state}};
}

1;
