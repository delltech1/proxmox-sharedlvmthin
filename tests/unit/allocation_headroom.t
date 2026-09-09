#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinSafety;

my $gib = 1024 * 1024 * 1024;

sub target {
    return PVE::SharedLvmThinSafety::evaluate_allocation_target(@_);
}

subtest 'fixed mode preserves legacy physical sizing' => sub {
    my $r = target(
        mode => 'fixed', fixed_gib => 4, requested_kib => 64 * 1024 * 1024,
        used_bytes => 0, current_pool_bytes => 0,
    );
    is($r->{target_bytes}, 4 * $gib, '64 GiB disk still creates the configured 4 GiB pool');
    is($r->{burst_guarantee}, 'NONE', 'fixed policy does not overclaim a guarantee');
};

subtest 'proportional mode uses usage plus bounded disk headroom' => sub {
    my $first = target(
        mode => 'proportional', fixed_gib => 4, percent => 50,
        requested_kib => 64 * 1024 * 1024, used_bytes => 0,
        current_pool_bytes => 0,
    );
    is($first->{target_bytes}, 32 * $gib, 'first 64 GiB disk gets 32 GiB target');

    my $second = target(
        mode => 'proportional', fixed_gib => 4, percent => 50,
        requested_kib => 500 * 1024 * 1024, used_bytes => 27 * $gib,
        current_pool_bytes => 32 * $gib,
    );
    is($second->{target_bytes}, 277 * $gib, 'second disk uses live usage plus its own headroom');

    my $capped = target(
        mode => 'proportional', fixed_gib => 4, percent => 50, max_gib => 128,
        requested_kib => 500 * 1024 * 1024, used_bytes => 27 * $gib,
        current_pool_bytes => 32 * $gib,
    );
    is($capped->{target_bytes}, 128 * $gib, 'explicit proportional ceiling is honored');

    my $ok = eval {
        target(
            mode => 'proportional', fixed_gib => 8, percent => 50, max_gib => 4,
            requested_kib => 64 * 1024 * 1024, used_bytes => 0,
            current_pool_bytes => 0,
        );
        1;
    };
    ok(!$ok, 'proportional ceiling below fixed minimum is rejected');
    like($@, qr/below fixed minimum/, 'contradictory policy is explicit');
};

subtest 'full mode covers live usage plus the entire new disk' => sub {
    my $small_first = target(
        mode => 'full', fixed_gib => 4,
        requested_kib => 4096, used_bytes => 0, current_pool_bytes => 0,
    );
    is($small_first->{target_bytes}, 4 * $gib, 'small first EFI/TPM-style LV honors fixed pool minimum');

    my $r = target(
        mode => 'full', fixed_gib => 4,
        requested_kib => 64 * 1024 * 1024, used_bytes => 27 * $gib,
        current_pool_bytes => 32 * $gib,
    );
    is($r->{target_bytes}, 91 * $gib, 'full admission target is exact');
    is($r->{burst_guarantee}, 'FULL_AT_ADMISSION', 'scope of guarantee is explicit');

    my $ok = eval {
        target(
            mode => 'full', fixed_gib => 4, max_gib => 64,
            requested_kib => 64 * 1024 * 1024, used_bytes => 27 * $gib,
            current_pool_bytes => 32 * $gib,
        );
        1;
    };
    ok(!$ok, 'full mode never silently applies an insufficient maximum');
    like($@, qr/exceeds configured maximum/, 'configuration conflict is explicit');
};

subtest 'elastic mode uses absolute headroom independent of virtual size' => sub {
    my $efi = target(
        mode => 'elastic', fixed_gib => 16, headroom_gib => 64, requested_kib => 4096,
        used_bytes => 0, current_pool_bytes => 0,
    );
    is($efi->{target_bytes}, 1 * $gib, 'EFI-sized first allocation uses bootstrap, not guest floor');

    my $disk32 = target(
        mode => 'elastic', fixed_gib => 16, headroom_gib => 64, requested_kib => 32 * 1024 * 1024,
        used_bytes => 512 * 1024, current_pool_bytes => 1 * $gib,
    );
    is($disk32->{target_bytes}, 32 * $gib + 512 * 1024, '32 GiB disk never reserves more than its virtual size');

    my $disk30t = target(
        mode => 'elastic', fixed_gib => 16, headroom_gib => 64,
        requested_kib => 30 * 1024 * 1024 * 1024,
        used_bytes => 10 * $gib, current_pool_bytes => 16 * $gib,
    );
    is($disk30t->{target_bytes}, 74 * $gib, '30 TiB disk adds only capped 64 GiB headroom');

    my $later = target(
        mode => 'elastic', fixed_gib => 16, headroom_gib => 64,
        requested_kib => 2 * 1024 * 1024 * 1024,
        used_bytes => 80 * $gib, current_pool_bytes => 96 * $gib,
    );
    is($later->{target_bytes}, 144 * $gib, 'ceiling applies to new headroom, not total pool size');
};

subtest 'inactive-pool fallback can conservatively use current size as used' => sub {
    my $r = target(
        mode => 'proportional', fixed_gib => 4, percent => 50,
        requested_kib => 20 * 1024 * 1024,
        used_bytes => 8 * $gib,
        current_pool_bytes => 8 * $gib,
    );
    is($r->{target_bytes}, 18 * $gib, 'full current pool plus new disk headroom is reserved');
};

subtest 'invalid percentages and modes fail closed' => sub {
    for my $percent (0, 101, -1, 'x') {
        my $ok = eval {
            target(
                mode => 'proportional', fixed_gib => 4, percent => $percent,
                requested_kib => 1024, used_bytes => 0, current_pool_bytes => 0,
            );
            1;
        };
        ok(!$ok, "percent '$percent' rejected");
    }
    my $ok = eval {
        target(
            mode => 'guess', fixed_gib => 4,
            requested_kib => 1024, used_bytes => 0, current_pool_bytes => 0,
        );
        1;
    };
    ok(!$ok, 'unknown mode rejected');
};

subtest 'reserve calculation includes overhead and extent rounding' => sub {
    my $r = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => 100 * $gib,
        vg_free => 20 * $gib,
        growth_bytes => 8 * $gib + 1,
        overhead_bytes => 1 * $gib,
        extent_bytes => 4 * 1024 * 1024,
        reserve_percent => 5,
        reserve_gib => 10,
    );
    is($r->{required_physical_bytes}, 9 * $gib + 4 * 1024 * 1024, 'physical need rounded up to an extent');
    ok($r->{allowed}, 'projected free remains above protected reserve');

    my $blocked = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => 100 * $gib,
        vg_free => 19 * $gib,
        growth_bytes => 8 * $gib + 1,
        overhead_bytes => 1 * $gib,
        extent_bytes => 4 * 1024 * 1024,
        reserve_percent => 5,
        reserve_gib => 10,
    );
    ok(!$blocked->{allowed}, 'extent-rounded allocation crossing reserve is blocked');
};

done_testing();
