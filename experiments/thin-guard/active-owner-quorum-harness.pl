#!/usr/bin/perl

# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only
#
# Destructive qualification harness for an already-active owned Thin pool.
# It is deliberately not packaged. The exact QEMU, mapper and owner epoch must
# be positive before the real watchdog client is opened.

use strict;
use warnings;
use Getopt::Long qw(GetOptions);
use Time::HiRes qw(sleep);
use lib '/tmp';

use PVE::SharedLvmThinWatchdog;

my ($vmid, $vg, $pool, $confirmation, $interval, $writer_pid) = ('', '', '', '', 2, '');
GetOptions(
    'vmid=s' => \$vmid,
    'vg=s' => \$vg,
    'pool=s' => \$pool,
    'confirm=s' => \$confirmation,
    'interval=f' => \$interval,
    'writer-pid=s' => \$writer_pid,
) or die "invalid arguments\n";

die "invalid VMID\n" if $vmid !~ /^[1-9][0-9]{2,8}$/;
die "invalid VG\n" if $vg !~ /^[A-Za-z0-9][A-Za-z0-9_.+-]*$/;
die "invalid pool\n" if $pool !~ /^sltp-$vmid$/;
die "confirmation must be ACTIVE-TEST-OWNER-FENCE\n"
    if $confirmation ne 'ACTIVE-TEST-OWNER-FENCE';
die "invalid interval\n" if $interval < 0.2 || $interval > 10;

my $node = qx{/bin/hostname};
chomp($node);
die "invalid local node\n" if $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

if ($writer_pid ne '') {
    die "invalid canary writer PID\n" if $writer_pid !~ /^[1-9][0-9]*$/;
    die "canary writer process is not live\n" if !-d "/proc/$writer_pid";
    open(my $cmdline, '<', "/proc/$writer_pid/cmdline")
        or die "cannot inspect canary writer process\n";
    local $/;
    my $command = <$cmdline> // '';
    close($cmdline);
    $command =~ s/\0/ /g;
    die "writer PID is not the Thin I/O canary\n"
        if $command !~ /thin-io-canary\.py\s+write\b/;
} else {
    my $status = qx{/usr/sbin/qm status $vmid 2>&1};
    die "qualification VM is not positively running\n"
        if $? != 0 || $status !~ /^status:\s+running\s*$/m;
}

my $tags = qx{/sbin/lvs --readonly --noheadings -o lv_tags $vg/$pool 2>&1};
die "cannot read exact pool tags\n" if $? != 0;
$tags =~ s/^\s+|\s+$//g;
die "pool owner does not match local node\n"
    if $tags !~ /(?:^|,)pve-slt-owner-node-\Q$node\E(?:,|\s|$)/;
die "pool has no exact owner epoch\n"
    if $tags !~ /(?:^|,)pve-slt-owner-epoch-[0-9a-f]{32}(?:,|\s|$)/;
my @owners = $tags =~ /pve-slt-owner-node-([A-Za-z0-9][A-Za-z0-9_.-]*)/g;
die "pool owner tags are ambiguous\n" if @owners != 1;

my $vg_dm = $vg;
my $pool_dm = $pool;
$vg_dm =~ s/-/--/g;
$pool_dm =~ s/-/--/g;
my $mapper = "$vg_dm-$pool_dm-tpool";
my $dm_uuid = qx{/sbin/dmsetup info -c --noheadings -o uuid $mapper 2>&1};
die "exact Thin pool mapper is absent or malformed\n"
    if $? != 0 || $dm_uuid !~ /^\s*LVM-[A-Za-z0-9-]+-tpool\s*$/;

sub quorum_positive {
    my $cluster = qx{/usr/bin/pvecm status 2>&1};
    return $? == 0 && $cluster =~ /^Quorate:\s+Yes\s*$/m;
}

die "initial cluster quorum is not positively proven\n" if !quorum_positive();

my $watchdog = PVE::SharedLvmThinWatchdog->new(allow_real_watchdog => 1);
$watchdog->arm({ safe => 1, action => 'ACTIVATE_EXCLUSIVE' });
$| = 1;
print "THINGUARD_ACTIVE_OWNER_ARMED vmid=$vmid pool=$vg/$pool node=$node\n";

while (1) {
    if (!quorum_positive()) {
        $watchdog->refresh({ safe => 0, action => 'STOP_WATCHDOG_REFRESH' });
        print "THINGUARD_ACTIVE_OWNER_QUORUM_LOST_REFRESH_STOPPED\n";
        sleep(3600) while 1;
    }
    die "healthy watchdog refresh failed\n"
        if !$watchdog->refresh({ safe => 1, action => 'REFRESH_WATCHDOG' });
    sleep($interval);
}
