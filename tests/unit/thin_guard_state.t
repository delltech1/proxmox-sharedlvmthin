#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinGuardState;

sub admission { return {safe => 1, action => 'ACTIVATE_EXCLUSIVE'}; }
sub pool {
    return {
        pool_uuid => 'pool-a', owner_epoch => 'epoch-a', mapper_uuid => 'mapper-a',
        owner_node => 'node-a', locally_active => 0, qemu_reference_exact => 0,
    };
}

my $g = PVE::SharedLvmThinGuardState->new(pending_timeout => 30);
is($g->state(), 'IDLE', 'guardian starts idle');
my $r = $g->prepare(admission => admission(), pool_uuid => 'pool-a',
    owner_epoch => 'epoch-a', mapper_uuid => 'mapper-a', now => 100);
is($r->{state}, 'ARMED_PENDING', 'watchdog is armed before activation');
$r = $g->observe(now => 101, quorum => 1, local_node => 'node-a', pools => [pool()]);
is($r->{action}, 'REFRESH_WATCHDOG', 'bounded pending attach refreshes watchdog');
my $active = pool();
$active->{locally_active} = 1;
$active->{qemu_reference_exact} = 1;
$r = $g->observe(now => 102, quorum => 1, local_node => 'node-a', pools => [$active]);
is($r->{state}, 'PROTECTED', 'exact mapper plus QEMU evidence enters protected state');
$r = $g->release(pool_uuid => 'pool-a', owner_epoch => 'epoch-a',
    qemu_absent => 1, mapper_absent => 1, owner_released => 1);
is($r->{action}, 'CLEAN_DISARM', 'last exact epoch permits clean disarm');

$g = PVE::SharedLvmThinGuardState->new(pending_timeout => 30);
$g->prepare(admission => admission(), pool_uuid => 'pool-a',
    owner_epoch => 'epoch-a', mapper_uuid => 'mapper-a', now => 100);
$r = $g->observe(now => 130, quorum => 1, local_node => 'node-a', pools => [pool()]);
is($r->{state}, 'FENCING', 'expired attach deadline fences');
eval { $g->prepare(admission => admission(), pool_uuid => 'pool-a',
    owner_epoch => 'epoch-a', mapper_uuid => 'mapper-a', now => 131) };
like($@, qr/irreversibly fencing/, 'same guardian epoch cannot self-heal');

for my $mutation (
    ['quorum loss', sub { $_[0]->{quorum} = 0 }],
    ['owner drift', sub { $_[0]->{pools}->[0]->{owner_epoch} = 'epoch-b' }],
    ['mapper drift', sub { $_[0]->{pools}->[0]->{mapper_uuid} = 'mapper-b' }],
    ['node drift', sub { $_[0]->{pools}->[0]->{owner_node} = 'node-b' }],
    ['mapper loss', sub { $_[0]->{pools}->[0]->{locally_active} = 0 }],
    ['QEMU loss', sub { $_[0]->{pools}->[0]->{qemu_reference_exact} = 0 }],
) {
    $g = PVE::SharedLvmThinGuardState->new(pending_timeout => 30);
    $g->prepare(admission => admission(), pool_uuid => 'pool-a',
        owner_epoch => 'epoch-a', mapper_uuid => 'mapper-a', now => 100);
    $g->observe(now => 101, quorum => 1, local_node => 'node-a', pools => [$active]);
    my $sample = {now => 102, quorum => 1, local_node => 'node-a', pools => [{%$active}]};
    $mutation->[1]->($sample);
    $r = $g->observe(%$sample);
    is($r->{action}, 'STOP_WATCHDOG_REFRESH', "$mutation->[0] fails closed");
}

$g = PVE::SharedLvmThinGuardState->new(pending_timeout => 30);
$g->prepare(admission => admission(), pool_uuid => 'pool-a',
    owner_epoch => 'epoch-a', mapper_uuid => 'mapper-a', now => 100);
eval { $g->release(pool_uuid => 'pool-a', owner_epoch => 'epoch-a',
    qemu_absent => 1, mapper_absent => 0, owner_released => 1) };
like($@, qr/mapper_absent/, 'release needs all positive teardown proofs');

done_testing();
