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
    'slt-allocation-mode' => 'thick-generations-lazy',
    'slt-mutation-admission-timeout' => 10,
};
my $tx = 'c' x 32;
my $anchor = 'sltg-a-test';
my $old = 'sltg-g-test-00000001';
my $new = 'sltg-g-test-00000002';
my $target = 'sltg-g-test-00000000';
my $lvs = { testvg => {
    $anchor => { lv_uuid => 'anchor-uuid' },
    $old => { lv_uuid => 'old-uuid' },
    $new => { lv_uuid => 'new-uuid' },
    $target => { lv_uuid => 'target-uuid' },
} };

sub transition_state {
    my ($phase) = @_;
    return {
        v => 5, phase => $phase, tx => $tx, op => 'SNAPSHOT',
        snapshot => 'newer-snapshot', old => $old, new => $new,
        source => $old, head => $new, generation => 2,
    };
}

sub materialized_state {
    return {
        v => 5, phase => 'MATERIALIZED', tx => $tx, op => 'ALLOC',
        snapshot => 'none', old => $new, new => $new, source => $new,
        head => $new, generation => 2,
    };
}

sub install_common_mocks {
    no warnings 'redefine';
    *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub {
        return ($target, 0, $lvs->{testvg}->{$target});
    };
}

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub {
        return ($target, 0, $lvs->{testvg}->{$target});
    };
    my @states = (transition_state('LINEAR_PIVOTED'), materialized_state());
    my $index = 0;
    my $pauses = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($states[$index], $lvs->{testvg}->{$new}, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_observation_pause = sub {
        $pauses++;
        $index = 1;
        return 1;
    };
    ok($class->_thick_wait_rollback_admission(
        $scfg, 'store', 'vm-100-disk-0', 'older-target'),
        'rollback waiter accepts the exact transition only after MATERIALIZED');
    is($pauses, 1, 'waiter observes without dispatching a worker or mutation');
}

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub {
        return ($target, 0, $lvs->{testvg}->{$target});
    };
    my @states = (transition_state('HYDRATING'), transition_state('HYDRATING'));
    $states[1]->{tx} = 'd' x 32;
    my $index = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($states[$index], $lvs->{testvg}->{$new}, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_observation_pause = sub {
        $index = 1;
        return 1;
    };
    my $ok = eval {
        $class->_thick_wait_rollback_admission(
            $scfg, 'store', 'vm-100-disk-0', 'older-target');
        1;
    };
    ok(!$ok, 'transaction replacement is refused');
    like($@, qr/identity changed while waiting/, 'transaction refusal is explicit');
}

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub {
        return ($target, 0, $lvs->{testvg}->{$target});
    };
    my $state = transition_state('HYDRATING');
    $state->{v} = 6;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($state, $lvs->{testvg}->{$new}, $anchor);
    };
    my $ok = eval {
        $class->_thick_wait_rollback_admission(
            $scfg, 'store', 'vm-100-disk-0', 'older-target');
        1;
    };
    ok(!$ok, 'v6 Lazy object is never adopted as a v5 transition');
    like($@, qr/requires completed materialization/, 'v6 refusal names the boundary');
}

done_testing();
