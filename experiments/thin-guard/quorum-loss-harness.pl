#!/usr/bin/perl

# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only
#
# Destructive qualification harness. This is intentionally outside usr/ and
# is never installed by the DEB. It arms the real PVE watchdog only with the
# exact confirmation string and only when no dm-thin-pool target is active.

use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use Time::HiRes qw(sleep);
use lib '/tmp';

use PVE::SharedLvmThinWatchdog;

my ($arm_real, $confirmation, $interval) = (0, '', 2);
GetOptions(
    'arm-real!' => \$arm_real,
    'confirm=s' => \$confirmation,
    'interval=f' => \$interval,
) or die "invalid arguments\n";

die "real watchdog qualification requires --arm-real\n" if !$arm_real;
die "confirmation must be DISPOSABLE-NODE-NO-ACTIVE-THIN\n"
    if $confirmation ne 'DISPOSABLE-NODE-NO-ACTIVE-THIN';
die "invalid interval\n" if $interval < 0.2 || $interval > 10;

my $dm = qx{/sbin/dmsetup ls --target thin-pool 2>/dev/null};
die "active thin-pool mapper present; refusing destructive watchdog test\n"
    if $dm =~ /\S/;

sub quorum_positive {
    my $status = qx{/usr/bin/pvecm status 2>&1};
    return $? == 0 && $status =~ /^Quorate:\s+Yes\s*$/m;
}

die "initial cluster quorum is not positively proven\n" if !quorum_positive();

my $watchdog = PVE::SharedLvmThinWatchdog->new(allow_real_watchdog => 1);
$watchdog->arm({ safe => 1, action => 'ACTIVATE_EXCLUSIVE' });
$| = 1;
print "THINGUARD_QUORUM_HARNESS_ARMED\n";

while (1) {
    if (!quorum_positive()) {
        $watchdog->refresh({ safe => 0, action => 'STOP_WATCHDOG_REFRESH' });
        print "THINGUARD_QUORUM_LOST_REFRESH_STOPPED\n";
        # Keep the socket open but never refresh. watchdog-mux must expire the
        # client using its own authoritative timeout and reset the node.
        sleep(3600) while 1;
    }
    die "healthy watchdog refresh failed\n"
        if !$watchdog->refresh({ safe => 1, action => 'REFRESH_WATCHDOG' });
    sleep($interval);
}
