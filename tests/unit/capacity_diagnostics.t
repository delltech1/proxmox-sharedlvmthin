#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::SharedLvmThinSafety;

subtest 'metadata thresholds are diagnostic and independent' => sub {
    for my $case (
        [0, 74.99, 'HEALTHY'],
        [0, 75, 'WARN'],
        [0, 89.99, 'WARN'],
        [0, 90, 'CRITICAL'],
        [0, 100, 'CRITICAL'],
    ) {
        my ($unused, $percent, $expected) = @$case;
        my $r = PVE::SharedLvmThinSafety::evaluate_metadata_health(
            active => 1, metadata_percent => $percent,
        );
        is($r->{status}, $expected, "$percent% classified $expected");
        is($r->{blocks_operation}, 0, 'health severity is not an operation gate');
    }
};

subtest 'inactive and unavailable metadata do not imply repair' => sub {
    my $inactive = PVE::SharedLvmThinSafety::evaluate_metadata_health(
        active => 0, metadata_percent => undef,
    );
    is($inactive->{status}, 'N_A', 'inactive pool is N/A');
    is($inactive->{blocks_operation}, 0, 'inactive diagnostic performs no mutation');

    my $unknown = PVE::SharedLvmThinSafety::evaluate_metadata_health(
        active => 1, metadata_percent => undef,
    );
    is($unknown->{status}, 'UNKNOWN', 'active unreadable metadata is unknown');
    is($unknown->{blocks_operation}, 0, 'unknown diagnostic is not automatic repair');
};

subtest 'chunk geometry reports reason without changing storage' => sub {
    my $tib = 1024 * 1024 * 1024 * 1024;
    my $kib = 1024;

    my $healthy = PVE::SharedLvmThinSafety::evaluate_chunk_geometry(
        pool_size => 0.5 * $tib, chunk_size => 64 * $kib,
    );
    is($healthy->{status}, 'HEALTHY', '512 GiB/64 KiB is within ceiling');
    like($healthy->{reason}, qr/within/, 'healthy reason included');

    my $warn = PVE::SharedLvmThinSafety::evaluate_chunk_geometry(
        pool_size => 20 * $tib, chunk_size => 64 * $kib,
    );
    is($warn->{status}, 'WARN', '20 TiB/64 KiB exceeds ceiling');
    like($warn->{reason}, qr/exceeds/, 'warning reason included');
    is($warn->{blocks_operation}, 0, 'geometry warning is diagnostic only');

    my $unknown = PVE::SharedLvmThinSafety::evaluate_chunk_geometry(
        pool_size => 20 * $tib, chunk_size => 0,
    );
    is($unknown->{status}, 'UNKNOWN', 'invalid geometry is unknown');
};

subtest 'thin-pool health flags fail closed without repair semantics' => sub {
    for my $case (
        ['twi-aotz--', '', '', 0, 'healthy writable pool'],
        ['twi-cotz--', 'needs_check', 'check_needed', 1, 'needs_check'],
        ['twi-aotzM-', '', '', 1, 'metadata read-only'],
        ['twi-aotz--', 'needs_check', '', 1, 'health status'],
        ['twi-aotz--', '', 'yes', 1, 'explicit check-needed field'],
        ['unexpected', '', '', 1, 'unexpected attributes'],
        [undef, undef, undef, 1, 'unavailable attributes'],
    ) {
        my ($attr, $health, $check_needed, $blocked, $name) = @$case;
        my $result = PVE::SharedLvmThinSafety::evaluate_pool_health(
            lv_attr => $attr,
            health_status => $health,
            check_needed => $check_needed,
        );
        is($result->{blocks_operation}, $blocked, "$name block policy");
        is($result->{status}, $blocked ? ($attr ? 'CRITICAL' : 'UNKNOWN') : 'HEALTHY', "$name status");
    }
};

done_testing();
