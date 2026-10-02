#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib";
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $scfg = {
    'slt-vgname' => 'testvg',
    'slt-expected-vg-uuid' => 'vg-uuid',
    'slt-expected-wwid' => '6000a',
    'slt-allocation-mode' => 'thick-generations',
    'slt-mutation-admission-timeout' => 10,
};
my $tx = 'a' x 32;
my $anchor = 'sltg-a-test';
my $old = 'sltg-g-test-00000001';
my $new = 'sltg-g-test-00000002';
my $snapshot = $old;
my $lvs = {
    testvg => {
        $anchor => { lv_uuid => 'anchor-uuid' },
        $old => { lv_uuid => 'snapshot-uuid', tags => '' },
        $new => { lv_uuid => 'head-uuid', tags => '' },
    },
};

sub transition_state {
    my ($phase) = @_;
    return {
        phase => $phase, tx => $tx, op => 'SNAPSHOT', snapshot => 'snap1',
        old => $old, new => $new, source => $old, head => $new,
        generation => 2,
    };
}

sub materialized_state {
    return {
        phase => 'MATERIALIZED', tx => $tx, op => 'ALLOC', snapshot => 'none',
        old => $new, new => $new, source => $new, head => $new,
        generation => 2,
    };
}

{
    no warnings 'redefine';
    my @states = (transition_state('HYDRATING'),
        transition_state('HYDRATION_COMPLETE'), materialized_state());
    my $index = 0;
    my $pauses = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($states[$index], $lvs->{testvg}->{$new}, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub {
        return ($snapshot, 1, $lvs->{testvg}->{$snapshot});
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_observation_pause = sub {
        $pauses++;
        $index++ if $index < $#states;
        return 1;
    };
    my $receipt = $class->_thick_wait_snapshot_delete_admission(
        $scfg, 'store', 'vm-100-disk-0', 'snap1');
    is($pauses, 2, 'waiter observes the existing transition without dispatching work');
    is($receipt->{tx}, $tx, 'receipt pins the completing transaction');
    is($receipt->{anchor_uuid}, 'anchor-uuid', 'receipt pins anchor identity');
    is($receipt->{snapshot_uuid}, 'snapshot-uuid', 'receipt pins snapshot identity');
    is($receipt->{head}, $new, 'receipt pins the materialized HEAD');
}

{
    no warnings 'redefine';
    my @states = (transition_state('HYDRATING'), transition_state('HYDRATING'));
    $states[1]->{tx} = 'b' x 32;
    my $index = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($states[$index], $lvs->{testvg}->{$new}, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_observation_pause = sub {
        $index = 1;
        return 1;
    };
    my $ok = eval {
        $class->_thick_wait_snapshot_delete_admission(
            $scfg, 'store', 'vm-100-disk-0', 'snap1');
        1;
    };
    ok(!$ok, 'changed transaction is refused');
    like($@, qr/identity changed while waiting/, 'identity-change refusal is explicit');
}

{
    no warnings 'redefine';
    my $lazy = {
        v => 6, phase => 'LAZY_DORMANT', tx => $tx, op => 'ALLOC', snapshot => 'none',
        old => $new, new => $new, source => $new, head => $new, generation => 2,
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($lazy, $lvs->{testvg}->{$new}, $anchor);
    };
    my $ok = eval {
        $class->_thick_wait_snapshot_delete_admission(
            $scfg, 'store', 'vm-100-disk-0', 'snap1');
        1;
    };
    ok(!$ok, 'unmaterialized v6 Lazy object is not adopted as a snapshot transition');
    like($@, qr/requires completed materialization/, 'Lazy refusal names the integration boundary');
}

done_testing();
