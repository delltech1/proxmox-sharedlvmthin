#!/usr/bin/perl
use strict;
use warnings;

use Getopt::Long qw(GetOptions);
use POSIX ();
use PVE::Storage::Custom::SharedLvmThinPlugin;

my %option;
GetOptions(
    'point=s' => \$option{point},
    'snapshot=s' => \$option{snapshot},
    'store-id=s' => \$option{store_id},
    'volume=s' => \$option{volume},
    'vg=s' => \$option{vg},
    'vg-uuid=s' => \$option{vg_uuid},
    'pv-uuid=s' => \$option{pv_uuid},
    'wwid=s' => \$option{wwid},
    'ack=s' => \$option{ack},
) or die "invalid snapshot-delete qualification-driver arguments\n";

die "explicit disposable-data acknowledgement is required\n"
    if ($option{ack} // '') ne 'DISPOSABLE-SNAPSHOT-WILL-BE-LEFT-INCOMPLETE';
die "invalid snapshot-delete crash point\n"
    if ($option{point} // '') !~ /^D[0-4]$/;
for my $field (qw(snapshot store_id volume vg vg_uuid pv_uuid wwid)) {
    die "missing qualification-driver option '$field'\n"
        if !defined($option{$field}) || $option{$field} eq '';
}

{
    no warnings 'redefine';
    *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_fault_point = sub {
        my ($class, $observed, $operation, $store_id, $volume) = @_;
        return if $observed ne $option{point};
        die "unexpected fault-point operation\n" if $operation ne 'REMOVE_SNAPSHOT';
        print STDERR "FAULT_POINT=$observed\nOPERATION=$operation\n"
            . "STORE_ID=$store_id\nVOLUME=$volume\n";
        POSIX::_exit(137);
    };
}

my $scfg = {
    shared => 1,
    'slt-allocation-mode' => 'thick-generations',
    'slt-vgname' => $option{vg},
    'slt-expected-vg-uuid' => $option{vg_uuid},
    'slt-expected-pv-uuid' => $option{pv_uuid},
    'slt-expected-wwid' => $option{wwid},
    'slt-vg-reserve-percent' => 5,
    'slt-tg-hydration-timeout' => 3600,
};

PVE::Storage::Custom::SharedLvmThinPlugin->_thick_volume_snapshot_delete(
    $scfg, $option{store_id}, $option{volume}, $option{snapshot},
);
die "requested snapshot-delete crash boundary was not reached\n";
