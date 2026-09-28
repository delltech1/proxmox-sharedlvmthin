#!/usr/bin/perl
use strict;
use warnings;

BEGIN {
    die "candidate fault driver requires PERL5LIB and PERL5OPT to be absent at interpreter start\n"
        if exists($ENV{PERL5LIB}) || exists($ENV{PERL5OPT});
}

use Getopt::Long qw(GetOptions);
use FindBin ();
use lib "$FindBin::Bin/../../usr/share/perl5";
use lib "$FindBin::Bin/lib";
use POSIX ();
use PVE::Storage::Custom::SharedLvmThinPlugin;
use SharedLvmCandidateProvenance qw(verify_candidate_provenance);

my %option;
GetOptions(
    'point=s' => \$option{point},
    'size-bytes=s' => \$option{size_bytes},
    'store-id=s' => \$option{store_id},
    'volume=s' => \$option{volume},
    'vg=s' => \$option{vg},
    'vg-uuid=s' => \$option{vg_uuid},
    'pv-uuid=s' => \$option{pv_uuid},
    'wwid=s' => \$option{wwid},
    'plugin-sha256=s' => \$option{plugin_sha256},
    'candidate-code-sha256=s' => \$option{candidate_code_sha256},
    'ack=s' => \$option{ack},
) or die "invalid resize qualification-driver arguments\n";

die "explicit disposable-data acknowledgement is required\n"
    if ($option{ack} // '') ne 'DISPOSABLE-RESIZE-WILL-BE-LEFT-INCOMPLETE';
die "invalid resize crash point\n" if ($option{point} // '') ne 'R0';
die "invalid resize byte size\n"
    if ($option{size_bytes} // '') !~ /^[1-9][0-9]*$/;
for my $field (qw(store_id volume vg vg_uuid pv_uuid wwid)) {
    die "missing qualification-driver option '$field'\n"
        if !defined($option{$field}) || $option{$field} eq '';
}
verify_candidate_provenance(
    $FindBin::Bin, $option{plugin_sha256}, $option{candidate_code_sha256},
);

{
    no warnings 'redefine';
    *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_fault_point = sub {
        my ($class, $observed, $operation, $store_id, $volume) = @_;
        return if $observed ne $option{point};
        die "unexpected fault-point operation\n" if $operation ne 'RESIZE';
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

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $device = "/dev/mapper/$option{wwid}";
$class->_require_thick_identity_config($option{store_id}, $scfg);
$class->_verify_storage_identity($option{store_id}, $scfg, $device);
$class->_require_no_vg_intent($scfg, $option{vg}, $device);
my $lvs = $class->_thick_list_volumes_scoped($scfg, $option{vg}, $device);
my ($state, $head) =
    $class->_thick_anchor($option{store_id}, $scfg, $option{volume}, $lvs);
die "qualification requires a MATERIALIZED Thick volume\n"
    if ($state->{phase} // '') ne 'MATERIALIZED';
die "qualification requires a strictly larger sector-aligned size\n"
    if !defined($head->{lv_size}) || $head->{lv_size} !~ /^\d+$/
    || $option{size_bytes} <= $head->{lv_size} || $option{size_bytes} % 512;
die "qualification requires an exact published frontend\n"
    if !$class->_thick_frontend_present($scfg, $option{volume});
$class->_thick_verify_frontend(
    $scfg, $option{volume}, $state->{head}, int($head->{lv_size} / 512),
);
my $path = $class->_thick_filesystem_path($scfg, $option{volume}, undef);
die "qualification frontend path is invalid\n"
    if $path !~ m{^/dev/mapper/([A-Za-z0-9+_.-]+)$};
my $mapper = $1;
die "qualification requires an active zero-open frontend\n"
    if $class->_thick_frontend_open_count(
        $mapper, $class->_thick_command_deadline($scfg),
    ) != 0;

$class->_thick_volume_resize(
    $scfg, $option{store_id}, $option{volume}, $option{size_bytes}, 0, undef,
);
die "requested resize crash boundary was not reached\n";
