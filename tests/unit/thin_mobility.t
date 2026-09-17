#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinMobility qw(
    mobility_names build_mobility_fingerprint evaluate_mobility_transition
);

my $names = mobility_names(
    vmid => 992700, disk => 0,
    transaction => '0123456789abcdef0123456789abcdef',
);
is($names->{pool}, 'sltp-992700-m-0123456789ab', 'pool generation name is deterministic');
is($names->{volume}, 'vm-992700-disk-0-m-0123456789ab', 'volume generation name is deterministic');
isnt($names->{source_alias}, $names->{target_alias}, 'transaction aliases are distinct');

my %fingerprint = (
    schema => 'v1', transaction => '0123456789abcdef0123456789abcdef',
    vmid => '992700', disk => '0', source_node => 'pve01', target_node => 'pve02',
    source_pool_uuid => 'pool-a', target_pool_uuid => 'pool-b',
    source_volume_uuid => 'vol-a', target_volume_uuid => 'vol-b',
    phase => 'MIRRORING', authority => 'source', config_volume => 'source-vol',
);
my $digest = build_mobility_fingerprint(%fingerprint);
like($digest, qr/^[0-9a-f]{64}$/, 'canonical transaction fingerprint is SHA-256');
$fingerprint{target_pool_uuid} = 'pool-c';
isnt(build_mobility_fingerprint(%fingerprint), $digest, 'target identity changes fingerprint');

sub ev {
    return (
        phase => 'PREPARED', journal_integrity => 'yes', quorum => 'yes',
        identities_distinct => 'yes', source_pool => 'present', target_pool => 'absent',
        source_qemu => 'present', target_qemu => 'absent', mirror => 'absent',
        config_authority => 'source', source_owner => 'source', target_owner => 'none',
    );
}

my $r = evaluate_mobility_transition(ev());
is($r->{action}, 'ALLOCATE_TARGET', 'prepared transaction allocates target');
$r = evaluate_mobility_transition(
    ev(), phase => 'TARGET_ALLOCATED', target_pool => 'present', target_owner => 'target');
is($r->{action}, 'START_NATIVE_MIRROR', 'independent target starts native mirror');
$r = evaluate_mobility_transition(
    ev(), phase => 'MIRRORING', target_pool => 'present', target_owner => 'target',
    mirror => 'running');
is($r->{action}, 'WAIT_MIRROR_READY', 'copy retains source authority');
$r = evaluate_mobility_transition(
    ev(), phase => 'MIRROR_READY', target_pool => 'present', target_owner => 'target',
    mirror => 'ready');
is($r->{action}, 'ALLOW_PVE_SWITCHOVER', 'ready active-sync mirror permits switchover');
$r = evaluate_mobility_transition(
    ev(), phase => 'PIVOT_COMMITTED', target_pool => 'present', target_owner => 'target',
    source_qemu => 'absent', target_qemu => 'present', mirror => 'completed',
    config_authority => 'target');
is($r->{action}, 'RETIRE_SOURCE', 'post-pivot target authority permits exact source retirement');
$r = evaluate_mobility_transition(
    ev(), phase => 'SOURCE_RETIRED', source_pool => 'absent', source_qemu => 'absent',
    target_pool => 'present', target_qemu => 'present', target_owner => 'target',
    config_authority => 'target', mirror => 'completed');
is($r->{action}, 'FINALIZE', 'target-only state finalizes');
$r = evaluate_mobility_transition(
    ev(), phase => 'COMPLETED', source_pool => 'absent', source_qemu => 'absent',
    target_pool => 'present', target_qemu => 'present', target_owner => 'target',
    config_authority => 'target', mirror => 'completed');
ok($r->{safe}, 'completed exact target-only state is safe');

$r = evaluate_mobility_transition(ev(), identities_distinct => 'no');
ok(!$r->{safe}, 'same metadata identity always fails closed');
$r = evaluate_mobility_transition(ev(), quorum => 'unknown');
ok(!$r->{safe}, 'unknown quorum fails closed');
$r = evaluate_mobility_transition(
    ev(), phase => 'MIRRORING', target_pool => 'present', target_owner => 'target',
    target_qemu => 'present', mirror => 'running');
ok(!$r->{safe}, 'target cannot become authoritative before recorded pivot');
$r = evaluate_mobility_transition(
    ev(), phase => 'PIVOT_COMMITTED', target_pool => 'present', target_owner => 'target',
    source_qemu => 'present', target_qemu => 'present', mirror => 'completed',
    config_authority => 'target');
ok(!$r->{safe}, 'source QEMU reference after pivot fails closed');
$r = evaluate_mobility_transition(
    ev(), phase => 'ABORTED', target_pool => 'present', target_owner => 'target');
is($r->{action}, 'DELETE_UNREFERENCED_TARGET', 'pre-pivot abort requests exact target cleanup');

done_testing();
