#!/usr/bin/perl
use strict;
use warnings;
use JSON::PP;
use PVE::Storage;
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $cfg = PVE::Storage::config();
for my $sid (qw(example-thick-storage example-lazy-storage example-thin-storage)) {
    my $scfg = $cfg->{ids}->{$sid} // die "missing storage $sid\n";
    my $result = PVE::Storage::Custom::SharedLvmThinPlugin->_scoped_vg_status($scfg);
    print "$sid=", JSON::PP->new->canonical->encode($result), "\n";
}

my %missing = %{$cfg->{ids}->{'example-thick-storage'}};
$missing{'slt-vgname'} = 'tg53-definitely-absent';
delete $missing{'slt-expected-vg-uuid'};
my $result = PVE::Storage::Custom::SharedLvmThinPlugin->_scoped_vg_status(\%missing);
print "ABSENT=", JSON::PP->new->canonical->encode($result), "\n";
