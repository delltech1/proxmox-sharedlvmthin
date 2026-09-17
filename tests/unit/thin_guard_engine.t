#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinGuardEngine;

{
    package FakeWatchdog;
    sub new { bless({state => 'DISARMED', writes => []}, shift) }
    sub state { $_[0]->{state} }
    sub arm { $_[0]->{state} = 'ARMED'; push @{$_[0]->{writes}}, 'ARM'; 1 }
    sub refresh {
        my ($self, $d) = @_;
        if (!$d->{safe}) { $self->{state} = 'FENCING'; push @{$self->{writes}}, 'STOP'; return 0; }
        push @{$self->{writes}}, 'REFRESH'; return 1;
    }
    sub clean_disarm { $_[0]->{state} = 'DISARMED'; push @{$_[0]->{writes}}, 'V'; 1 }
}

sub admission {
    return {
        journal_integrity => 1, quorum => 1, storage_identity => 1,
        local_runtime_absent => 1, remote_runtime_absent => 1,
        watchdog_available => 1, watchdog_armed => 1,
        owner_epoch_exact => 1, predecessor_fenced => 0,
        predecessor => 'none', requested_owner => 'self',
    };
}

my $active = 0;
my $released = 0;
my $bad_quorum = 0;
my $pool = {
    pool_uuid => 'pool-a', owner_epoch => ('a' x 32), mapper_uuid => 'LVM-abcdef-tpool',
    owner_node => 'node-a', locally_active => 0, qemu_reference_exact => 0,
};
my $provider = sub {
    my (%query) = @_;
    if ($query{op} eq 'RELEASE') {
        return {inventory_ok => 1, release_proof => {
            qemu_absent => $released, mapper_absent => $released, owner_released => $released,
        }};
    }
    return {
        inventory_ok => 1, quorum => $bad_quorum ? 0 : 1, local_node => 'node-a',
        pools => [{%$pool, locally_active => $active, qemu_reference_exact => $active}],
        admission => admission(),
    };
};

my $watchdog = FakeWatchdog->new();
my $engine = PVE::SharedLvmThinGuardEngine->new(
    inventory_provider => $provider, watchdog => $watchdog, pending_timeout => 30,
);
my $request = {storage_id => 'slt-thin', pool_uuid => 'pool-a',
    owner_epoch => ('a' x 32), mapper_uuid => 'LVM-abcdef-tpool'};
my $r = $engine->prepare($request, 100);
is($r->{action}, 'ACK_PREPARED', 'PREPARE acknowledges only after watchdog arm');
is_deeply($watchdog->{writes}, ['ARM'], 'first epoch arms one aggregate watchdog client');
$engine->observe(101);
is_deeply($watchdog->{writes}, ['ARM', 'REFRESH'], 'pending attach is bounded but refreshed');
$active = 1;
$r = $engine->observe(102);
is($r->{state}, 'PROTECTED', 'runtime evidence protects epoch');
$active = 0;
$released = 1;
$r = $engine->release({pool_uuid => 'pool-a', owner_epoch => ('a' x 32)});
is($r->{action}, 'CLEAN_DISARM', 'last independently proven release cleanly disarms');
is_deeply($watchdog->{writes}, ['ARM', 'REFRESH', 'REFRESH', 'V'], 'magic close occurs only at terminal proof');

$watchdog = FakeWatchdog->new();
$engine = PVE::SharedLvmThinGuardEngine->new(
    inventory_provider => $provider, watchdog => $watchdog, pending_timeout => 30,
);
$released = 0;
$active = 0;
$engine->prepare($request, 100);
$bad_quorum = 1;
$r = $engine->observe(101);
is($r->{action}, 'STOP_WATCHDOG_REFRESH', 'quorum loss stops watchdog refresh');
is($watchdog->state(), 'FENCING', 'watchdog enters irreversible fencing');

$bad_quorum = 0;
$watchdog = FakeWatchdog->new();
$engine = PVE::SharedLvmThinGuardEngine->new(
    inventory_provider => $provider, watchdog => $watchdog, pending_timeout => 30,
);
my $wrong = {%$request, owner_epoch => ('b' x 32)};
eval { $engine->prepare($wrong, 100) };
like($@, qr/owner epoch mismatch/, 'caller cannot substitute a different owner epoch');
is($watchdog->state(), 'DISARMED', 'invalid request cannot arm watchdog');

done_testing();
