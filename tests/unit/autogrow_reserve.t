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

done_testing();
