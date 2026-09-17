#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::SharedLvmThinSafety;

my $gib = 1024 * 1024 * 1024;

subtest 'effective reserve is maximum of percentage and fixed GiB' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_autogrow_reserve(
        vg_size => 1000 * $gib, vg_free => 300 * $gib,
        pool_size => 100 * $gib, growth_percent => 10,
        reserve_percent => 5, reserve_gib => 100,
    );
    is($r->{reserve_bytes}, 100 * $gib, 'fixed 100 GiB wins over 5%');
    is($r->{growth_bytes}, 10 * $gib, 'growth calculated');
    ok($r->{allowed}, 'growth leaves reserve intact');
};

subtest 'small VG uses percentage when fixed reserve is explicitly zero' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_autogrow_reserve(
        vg_size => 40 * $gib, vg_free => 8 * $gib,
        pool_size => 10 * $gib, growth_percent => 10,
        reserve_percent => 5, reserve_gib => 0,
    );
    is($r->{reserve_bytes}, 2 * $gib, 'small-VG reserve is 5%');
    ok($r->{allowed}, 'small VG can grow without crossing reserve');
};

subtest 'crossing reserve is refused before lvextend' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_autogrow_reserve(
        vg_size => 1000 * $gib, vg_free => 105 * $gib,
        pool_size => 100 * $gib, growth_percent => 10,
        reserve_percent => 5, reserve_gib => 100,
    );
    ok(!$r->{allowed}, 'growth refused');
    is($r->{free_after_bytes}, 95 * $gib, 'projected free space reported');
    is($r->{usable_free_bytes}, 5 * $gib, 'usable free above reserve reported');
};

subtest 'exact reserve boundary is allowed deterministically' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_autogrow_reserve(
        vg_size => 1000 * $gib, vg_free => 110 * $gib,
        pool_size => 100 * $gib, growth_percent => 10,
        reserve_percent => 5, reserve_gib => 100,
    );
    ok($r->{allowed}, 'exact boundary allowed');
    is($r->{free_after_bytes}, $r->{reserve_bytes}, 'boundary calculation exact');
};

subtest 'capacity governor predicts data exhaustion before the reaction horizon' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_capacity_stability(
        data_free_bytes => 600, metadata_free_units => 10_000,
        data_rate_bytes_per_second => 10, metadata_rate_units_per_second => 1,
        extend_latency_p99_seconds => 20, lock_latency_p99_seconds => 10,
        reaction_margin_seconds => 40,
    );
    is($r->{status}, 'GROW_REQUIRED', 'growth is requested before exhaustion');
    is($r->{limiting_dimension}, 'DATA', 'data is the limiting dimension');
    is($r->{data_runway_seconds}, 60, 'data runway is exact');
    is($r->{safety_horizon_seconds}, 70, 'latencies and margin form the horizon');
    ok($r->{safe}, 'decision was made from complete valid evidence');
};

subtest 'capacity governor evaluates metadata independently' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_capacity_stability(
        data_free_bytes => 100_000, metadata_free_units => 50,
        data_rate_bytes_per_second => 10, metadata_rate_units_per_second => 1,
        extend_latency_p99_seconds => 10, lock_latency_p99_seconds => 10,
        reaction_margin_seconds => 30,
    );
    is($r->{status}, 'GROW_REQUIRED', 'metadata exhaustion requests growth');
    is($r->{limiting_dimension}, 'METADATA', 'metadata is reported as limiting');
};

subtest 'positively observed zero rate is unbounded, not guessed' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_capacity_stability(
        data_free_bytes => 1, metadata_free_units => 1,
        data_rate_bytes_per_second => 0, metadata_rate_units_per_second => 0,
        extend_latency_p99_seconds => 10, lock_latency_p99_seconds => 10,
        reaction_margin_seconds => 10,
    );
    is($r->{status}, 'STABLE', 'zero observed consumption does not trigger growth');
    is($r->{data_runway_state}, 'UNBOUNDED', 'data is explicitly unbounded');
    is($r->{metadata_runway_state}, 'UNBOUNDED', 'metadata is explicitly unbounded');
    ok(!defined($r->{data_runway_seconds}), 'no artificial infinity is returned');
};

subtest 'unknown rates fail closed' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_capacity_stability(
        data_free_bytes => 1000, metadata_free_units => 1000,
        data_rate_bytes_per_second => 0, metadata_rate_units_per_second => 0,
        extend_latency_p99_seconds => 10, lock_latency_p99_seconds => 10,
        reaction_margin_seconds => 10, rates_known => 0,
    );
    is($r->{status}, 'UNKNOWN', 'unknown observations are not treated as stable');
    ok($r->{blocks_operation}, 'unknown evidence blocks a safety-sensitive use');
    ok(!$r->{safe}, 'unknown state never emits SAFE');
};

subtest 'invalid input and zero horizon fail closed' => sub {
    my $bad = PVE::SharedLvmThinSafety::evaluate_capacity_stability(
        data_free_bytes => 'broken', metadata_free_units => 1,
        data_rate_bytes_per_second => 1, metadata_rate_units_per_second => 1,
        extend_latency_p99_seconds => 1, lock_latency_p99_seconds => 1,
        reaction_margin_seconds => 1,
    );
    is($bad->{status}, 'UNKNOWN', 'invalid numeric input is unknown');
    ok($bad->{blocks_operation}, 'invalid numeric input blocks');

    my $zero = PVE::SharedLvmThinSafety::evaluate_capacity_stability(
        data_free_bytes => 1, metadata_free_units => 1,
        data_rate_bytes_per_second => 1, metadata_rate_units_per_second => 1,
        extend_latency_p99_seconds => 0, lock_latency_p99_seconds => 0,
        reaction_margin_seconds => 0,
    );
    is($zero->{status}, 'UNKNOWN', 'zero safety horizon is rejected');
};

done_testing();
