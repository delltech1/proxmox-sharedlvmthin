#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use Socket qw(AF_UNIX SOCK_STREAM PF_UNSPEC);
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinWatchdog;

sub pair_factory {
    socketpair(my $client, my $server, AF_UNIX, SOCK_STREAM, PF_UNSPEC)
        or die "socketpair: $!";
    $client->autoflush(1);
    $server->autoflush(1);
    return ($client, $server);
}

sub read_byte {
    my ($fh) = @_;
    my $byte = '';
    my $count = sysread($fh, $byte, 1);
    return ($count, $byte);
}

my ($client, $server) = pair_factory();
my $wd = PVE::SharedLvmThinWatchdog->new(socket_factory => sub { return $client; });
eval { $wd->arm({ safe => 0, action => 'NONE' }); };
like($@, qr/not positively safe/, 'unsafe admission cannot open watchdog client');
is($wd->state(), 'DISARMED', 'unsafe admission remains disarmed');

$wd->arm({ safe => 1, action => 'ACTIVATE_EXCLUSIVE' });
is($wd->state(), 'ARMED', 'positive admission arms injected client');
my ($count, $byte) = read_byte($server);
is($count, 1, 'arm writes exactly one byte');
is($byte, "\0", 'arm uses PVE watchdog refresh byte');
$wd->refresh({ safe => 1, action => 'REFRESH_WATCHDOG' });
($count, $byte) = read_byte($server);
is($byte, "\0", 'healthy epoch refreshes with NUL');
eval { $wd->clean_disarm(all_qemu_absent => 1, all_mappers_absent => 1); };
like($@, qr/owner_epochs_released/, 'incomplete stop proof cannot magic-close');
is($wd->state(), 'ARMED', 'failed clean-disarm keeps watchdog armed');
$wd->clean_disarm(
    all_qemu_absent => 1, all_mappers_absent => 1, owner_epochs_released => 1,
);
($count, $byte) = read_byte($server);
is($byte, 'V', 'complete stop proof sends magic close');
is($wd->state(), 'DISARMED', 'complete stop proof disarms');

($client, $server) = pair_factory();
$wd = PVE::SharedLvmThinWatchdog->new(socket_factory => sub { return $client; });
$wd->arm({ safe => 1, action => 'ACTIVATE_EXCLUSIVE' });
read_byte($server);
ok(!$wd->refresh({ safe => 0, action => 'STOP_WATCHDOG_REFRESH' }),
    'runtime uncertainty stops refresh');
is($wd->state(), 'FENCING', 'runtime uncertainty is irreversible fencing state');
ok(!$wd->refresh({ safe => 1, action => 'REFRESH_WATCHDOG' }),
    'later healthy sample cannot resurrect epoch');
eval { $wd->clean_disarm(
    all_qemu_absent => 1, all_mappers_absent => 1, owner_epochs_released => 1,
); };
like($@, qr/cannot cleanly disarm/, 'fencing state cannot send magic close');
$wd->abandon_for_fencing();
($count, $byte) = read_byte($server);
is($count, 0, 'fencing closes injected client without magic byte');
is($wd->state(), 'FENCE_TRIGGERED', 'unsafe epoch reaches terminal fencing state');

my $real = PVE::SharedLvmThinWatchdog->new();
eval { $real->arm({ safe => 1, action => 'ACTIVATE_EXCLUSIVE' }); };
like($@, qr/real watchdog arming is disabled/, 'real watchdog cannot arm by default');
is($real->state(), 'FENCING', 'failed partial arm is fail-closed');

done_testing();
