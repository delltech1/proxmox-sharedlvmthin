#!/usr/bin/perl
use strict;
use warnings;

use Getopt::Long qw(GetOptions);
use PVE::SharedLvmThinThick qw(anchor_name decode_generation_tags object_key);
use PVE::Storage::Custom::SharedLvmThinPlugin;

my %option;
GetOptions(
    'store-id=s' => \$option{store_id}, 'volume=s' => \$option{volume},
    'vg=s' => \$option{vg}, 'vg-uuid=s' => \$option{vg_uuid},
    'pv-uuid=s' => \$option{pv_uuid}, 'wwid=s' => \$option{wwid},
    'ack=s' => \$option{ack},
) or die "invalid recovery arguments\n";

die "explicit exact-intent recovery acknowledgement is required\n"
    if ($option{ack} // '') ne 'CLEAR-ONLY-PROVEN-PREPARE-INCOMPLETE';
for my $field (qw(store_id volume vg vg_uuid pv_uuid wwid)) {
    die "missing recovery option '$field'\n"
        if !defined($option{$field}) || $option{$field} eq '';
}

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $scfg = {
    shared => 1, 'slt-allocation-mode' => 'thick-generations',
    'slt-vgname' => $option{vg},
    'slt-expected-vg-uuid' => $option{vg_uuid},
    'slt-expected-pv-uuid' => $option{pv_uuid},
    'slt-expected-wwid' => $option{wwid},
    'slt-vg-reserve-percent' => 5,
};

$class->_require_thick_identity_config($option{store_id}, $scfg);
$class->_with_vg_lock($option{store_id}, $scfg, sub {
    my $intent = $class->_read_vg_intent($option{vg});
    die "no OPEN intent exists\n" if !$intent;
    die "OPEN intent is not a Thick Generations transition\n"
        if $intent->{op} ne 'DM_CUTOVER' && $intent->{op} ne 'DM_PIVOT';

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($option{vg});
    my ($state, $head, $anchor) = $class->_thick_anchor(
        $option{store_id}, $scfg, $option{volume}, $lvs,
    );
    die "OPEN intent refers to another anchor\n" if $intent->{object} ne $anchor;
    die "intent is not PREPARE_INCOMPLETE\n" if $intent->{tx} eq $state->{tx};

    my $key = object_key(lc($option{vg_uuid}), $option{volume});
    for my $name (keys %{$lvs->{$option{vg}}}) {
        die "transition metadata exists; refusing intent-only recovery\n"
            if $name =~ /^sltg-m-\Q$key\E-/;
        next if $name !~ /^sltg-g-\Q$key\E-/ || $name eq $state->{head};
        my $generation = eval {
            decode_generation_tags($lvs->{$option{vg}}->{$name}->{tags} // '')
        };
        next if !$generation;
        die "a second HEAD exists; refusing intent-only recovery\n"
            if $generation->{sid} eq $option{store_id}
            && $generation->{vol} eq $option{volume}
            && $generation->{role} eq 'head';
    }

    $class->_thick_verify_frontend(
        $scfg, $option{volume}, $state->{head}, int($head->{lv_size} / 512),
    );
    $class->_clear_vg_intent($option{vg}, %$intent);
    return;
});

print "RECOVERY=PREPARE_INCOMPLETE_INTENT_CLEARED\n";
