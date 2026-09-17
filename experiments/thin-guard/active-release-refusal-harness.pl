#!/usr/bin/perl
# SPDX-License-Identifier: GPL-3.0-only
#
# Non-destructive qualification helper.  It asks the packaged guardian to
# release an epoch that is still backed by an active mapper and QEMU process.
# A safe guardian must refuse and remain armed.  The helper never changes LVM,
# QEMU, watchdog or cluster state itself.

use strict;
use warnings;

use JSON::PP qw(decode_json);
use PVE::SharedLvmThinGuardClient;

die "Usage: $0 <storage-id> <vmid>\n" if @ARGV != 2;
my ($storage_id, $vmid) = @ARGV;
die "invalid storage ID\n" if $storage_id !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
die "invalid VMID\n" if $vmid !~ /^\d+$/;

my $helper = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory';
open(my $fh, '-|', $helper, '--storage-id', $storage_id)
    or die "cannot execute exact inventory helper: $!\n";
local $/;
my $raw = <$fh>;
close($fh) or die "exact inventory helper refused runtime state\n";
my $inventory = eval { decode_json($raw) };
die "invalid exact inventory JSON\n" if $@ || ref($inventory) ne 'HASH';

my @matching = grep {
    ($_->{vmid} // '') eq $vmid
} @{$inventory->{pools} // []};
die "expected exactly one pool for VMID $vmid\n" if @matching != 1;
my $pool = $matching[0];
die "qualification requires an active exact mapper\n" if !$pool->{locally_active};
die "qualification requires an exact live QEMU reference\n"
    if !$pool->{qemu_reference_exact};
die "qualification requires a persistent owner epoch\n"
    if ($pool->{owner_epoch} // '') !~ /^[a-f0-9]{32}$/;
die "qualification requires an exact pool UUID\n"
    if ($pool->{pool_uuid} // '') !~ /^[A-Za-z0-9-]+$/;

my $request_id = join('', map { sprintf('%x', int(rand(16))) } 1 .. 32);
my $client = PVE::SharedLvmThinGuardClient->new(allow_real_socket => 1);
my $ok = eval {
    $client->request({
        version => 1,
        op => 'RELEASE',
        request_id => $request_id,
        pool_uuid => $pool->{pool_uuid},
        owner_epoch => $pool->{owner_epoch},
    }, ['CLEAN_DISARM', 'REFRESH_WATCHDOG']);
    1;
};
die "UNSAFE: guardian accepted RELEASE for an active QEMU/mapper epoch\n" if $ok;
my $error = $@;
die "unexpected guardian failure: $error"
    if $error !~ /refused|teardown proof|QEMU|mapper|owner/i;

print "ACTIVE_RELEASE_REFUSAL=PASS storage=$storage_id vmid=$vmid\n";
