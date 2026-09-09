#!/usr/bin/perl
use strict;
use warnings;

use Getopt::Long qw(GetOptions);
use PVE::SharedLvmThinThick qw(
    object_key validate_generation_tags validate_transition_tags
);
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::Tools qw(run_command);

my %option;
GetOptions(
    'store-id=s' => \$option{store_id}, 'volume=s' => \$option{volume},
    'vg=s' => \$option{vg}, 'vg-uuid=s' => \$option{vg_uuid},
    'pv-uuid=s' => \$option{pv_uuid}, 'wwid=s' => \$option{wwid},
    'ack=s' => \$option{ack},
) or die "invalid recovery arguments\n";

die "explicit unrecorded-prepare recovery acknowledgement is required\n"
    if ($option{ack} // '') ne 'REMOVE-ONLY-PROVEN-UNRECORDED-PREPARE';
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
    die "intent is not an unrecorded prepare\n" if $intent->{tx} eq $state->{tx};
    $class->_thick_verify_frontend(
        $scfg, $option{volume}, $state->{head}, int($head->{lv_size} / 512),
    );

    my $generation = int($state->{generation}) + 1;
    my $key = object_key(lc($option{vg_uuid}), $option{volume});
    my $new = sprintf('sltg-g-%s-%08d', $key, $generation);
    my $meta = sprintf('sltg-m-%s-%08d', $key, $generation);
    die "exact unrecorded transition object set is incomplete\n"
        if !$lvs->{$option{vg}}->{$new} || !$lvs->{$option{vg}}->{$meta};
    validate_generation_tags(
        $lvs->{$option{vg}}->{$new}->{tags} // '', sid => $option{store_id},
        vol => $option{volume}, role => 'head', generation => $generation,
    );
    validate_transition_tags(
        $lvs->{$option{vg}}->{$meta}->{tags} // '', sid => $option{store_id},
        vol => $option{volume}, tx => $intent->{tx}, kind => 'metadata',
        generation => $generation, region => $state->{region},
    );
    $class->_verify_autoactivation_disabled($option{vg}, $new);
    $class->_verify_autoactivation_disabled($option{vg}, $meta);
    die "unrecorded transition object is active; refusing cleanup\n"
        if -b "/dev/$option{vg}/$new" || -b "/dev/$option{vg}/$meta";

    run_command(
        ['/sbin/lvremove', '-f', "$option{vg}/$meta", "$option{vg}/$new"],
        errmsg => 'removing exact unrecorded prepare objects failed',
    );
    my $after = PVE::Storage::LVMPlugin::lvm_list_volumes($option{vg});
    die "unrecorded prepare cleanup postcondition failed\n"
        if $after->{$option{vg}}->{$new} || $after->{$option{vg}}->{$meta};
    $class->_clear_vg_intent($option{vg}, %$intent);
    return;
});

print "RECOVERY=UNRECORDED_PREPARE_REMOVED\n";
