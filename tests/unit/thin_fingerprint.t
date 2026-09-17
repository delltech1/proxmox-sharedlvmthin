#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::SharedLvmThinSafety;

sub evidence {
    return (
        pool_uuid => 'pool-uuid-1', metadata_uuid => 'metadata-uuid-1',
        data_uuid => 'data-uuid-1', transaction_id => 42,
        owner_node => 'pve01', owner_epoch => ('a' x 32),
        live_table_hash => ('b' x 64), dependency_hash => ('c' x 64),
        metadata_mode => 'rw',
        @_,
    );
}

subtest 'fingerprint is deterministic and exact' => sub {
    my $first = PVE::SharedLvmThinSafety::build_thin_transaction_fingerprint(evidence());
    my $second = PVE::SharedLvmThinSafety::build_thin_transaction_fingerprint(evidence());
    is($first->{digest}, $second->{digest}, 'same evidence has the same digest');
    like($first->{digest}, qr/^[0-9a-f]{64}$/, 'digest is canonical SHA-256');
    my $decision = PVE::SharedLvmThinSafety::compare_thin_transaction_fingerprint(
        $first, $second,
    );
    is($decision->{status}, 'MATCH', 'exact evidence matches');
    ok(!$decision->{blocks_operation}, 'exact match does not block');
};

subtest 'every safety identity field participates in the digest' => sub {
    my $base = PVE::SharedLvmThinSafety::build_thin_transaction_fingerprint(evidence());
    my %changes = (
        pool_uuid => 'pool-uuid-2', metadata_uuid => 'metadata-uuid-2',
        data_uuid => 'data-uuid-2', transaction_id => 43,
        owner_node => 'pve02', owner_epoch => ('d' x 32),
        live_table_hash => ('e' x 64), dependency_hash => ('f' x 64),
        metadata_mode => 'ro',
    );
    for my $field (sort keys %changes) {
        my $changed = PVE::SharedLvmThinSafety::build_thin_transaction_fingerprint(
            evidence($field => $changes{$field}),
        );
        isnt($changed->{digest}, $base->{digest}, "$field changes the fingerprint");
        my $decision = PVE::SharedLvmThinSafety::compare_thin_transaction_fingerprint(
            $base, $changed,
        );
        is($decision->{status}, 'MISMATCH', "$field mismatch fails closed");
        ok($decision->{blocks_operation}, "$field mismatch blocks mutation");
    }
};

subtest 'missing and malformed evidence is rejected' => sub {
    eval { PVE::SharedLvmThinSafety::build_thin_transaction_fingerprint(evidence(owner_epoch => 'bad')) };
    like($@, qr/invalid owner epoch/, 'malformed owner epoch rejected');
    eval { PVE::SharedLvmThinSafety::build_thin_transaction_fingerprint(evidence(metadata_mode => 'guess')) };
    like($@, qr/invalid metadata mode/, 'guessed metadata mode rejected');
    my $decision = PVE::SharedLvmThinSafety::compare_thin_transaction_fingerprint(undef, {});
    is($decision->{status}, 'UNKNOWN', 'missing fingerprint is unknown');
    ok($decision->{blocks_operation}, 'missing fingerprint blocks');
};

subtest 'read-only metadata validation requires every positive postcondition' => sub {
    my $pass = PVE::SharedLvmThinSafety::evaluate_readonly_metadata_validation(
        snapshot_reserved => 1, thin_check_completed => 1,
        thin_check_ok => 1, snapshot_released => 1,
    );
    is($pass->{status}, 'PASS', 'complete validation passes');
    ok(!$pass->{blocks_operation}, 'complete validation does not block');

    for my $field (qw(snapshot_reserved thin_check_completed snapshot_released)) {
        my %state = (
            snapshot_reserved => 1, thin_check_completed => 1,
            thin_check_ok => 1, snapshot_released => 1,
        );
        $state{$field} = 0;
        my $result = PVE::SharedLvmThinSafety::evaluate_readonly_metadata_validation(%state);
        is($result->{status}, 'UNKNOWN', "$field absence is unknown");
        ok($result->{blocks_operation}, "$field absence blocks");
    }
    my $bad = PVE::SharedLvmThinSafety::evaluate_readonly_metadata_validation(
        snapshot_reserved => 1, thin_check_completed => 1,
        thin_check_ok => 0, snapshot_released => 1,
    );
    is($bad->{status}, 'CRITICAL', 'thin_check failure is critical');
    like($bad->{reason}, qr/no repair attempted/, 'failure never requests automatic repair');
};

subtest 'LeaseGuard activation is opt-in and wholly positive' => sub {
    my $disabled = PVE::SharedLvmThinSafety::evaluate_leaseguard_activation(
        enabled => 0,
    );
    is($disabled->{status}, 'DISABLED', 'existing storage behavior is unchanged');
    ok(!$disabled->{blocks_operation}, 'disabled LeaseGuard adds no gate');

    my %positive = map { $_ => 1 } qw(
        lease_area_identity sanlock_daemon lockspace_joined resource_held
        owner_matches quorum storage_identity fresh_pool_runtime
    );
    my $pass = PVE::SharedLvmThinSafety::evaluate_leaseguard_activation(
        enabled => 1, %positive,
    );
    is($pass->{status}, 'PASS', 'all positive evidence permits activation');
    ok(!$pass->{blocks_operation}, 'positive lease evidence does not block');

    for my $field (sort keys %positive) {
        my %state = %positive;
        $state{$field} = 0;
        my $refused = PVE::SharedLvmThinSafety::evaluate_leaseguard_activation(
            enabled => 1, %state,
        );
        is($refused->{status}, 'REFUSED', "$field failure refuses activation");
        ok($refused->{blocks_operation}, "$field failure is fail-closed");
    }

    delete $positive{resource_held};
    my $unknown = PVE::SharedLvmThinSafety::evaluate_leaseguard_activation(
        enabled => 1, %positive,
    );
    is($unknown->{status}, 'UNKNOWN', 'missing resource evidence is unknown');
    ok($unknown->{blocks_operation}, 'unknown lease state blocks activation');
};

subtest 'PVE-native LeaseGuard requires positive absence from every peer kernel' => sub {
    my $disabled = PVE::SharedLvmThinSafety::evaluate_pve_native_leaseguard(
        enabled => 0,
    );
    is($disabled->{status}, 'DISABLED', 'native guard remains opt-in');
    ok(!$disabled->{blocks_operation}, 'disabled guard preserves existing behavior');

    my %base = (
        enabled => 1, quorum => 1, storage_identity => 1,
        local_runtime_absent => 1,
        remote_nodes => [
            { node => 'pve02', state => 'ABSENT' },
            { node => 'pve03', state => 'ABSENT' },
        ],
    );
    my $pass = PVE::SharedLvmThinSafety::evaluate_pve_native_leaseguard(%base);
    is($pass->{status}, 'PASS', 'exact absence on every peer permits activation');
    ok(!$pass->{blocks_operation}, 'positive peer evidence does not block');

    for my $state (qw(PRESENT UNKNOWN)) {
        my %candidate = %base;
        $candidate{remote_nodes} = [
            { node => 'pve02', state => $state },
            { node => 'pve03', state => 'ABSENT' },
        ];
        my $result = PVE::SharedLvmThinSafety::evaluate_pve_native_leaseguard(%candidate);
        is($result->{status}, $state eq 'PRESENT' ? 'REFUSED' : 'UNKNOWN',
            "$state remote evidence fails closed");
        ok($result->{blocks_operation}, "$state remote evidence blocks activation");
    }

    for my $field (qw(quorum storage_identity local_runtime_absent)) {
        my %candidate = %base;
        $candidate{$field} = 0;
        my $result = PVE::SharedLvmThinSafety::evaluate_pve_native_leaseguard(%candidate);
        is($result->{status}, 'REFUSED', "$field failure refuses activation");
        ok($result->{blocks_operation}, "$field failure blocks activation");
    }

    my %empty = %base;
    $empty{remote_nodes} = [];
    my $unknown = PVE::SharedLvmThinSafety::evaluate_pve_native_leaseguard(%empty);
    is($unknown->{status}, 'UNKNOWN', 'missing peer inventory is unknown');
    ok($unknown->{blocks_operation}, 'missing peer inventory blocks');
};

done_testing();
