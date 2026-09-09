use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinThick qw(
    anchor_tags decode_anchor_tags generation_tags validate_anchor_transition
    validate_generation_tags vg_intent_tags decode_vg_intent_tags clone_geometry
    transition_tags validate_transition_tags
);

my $tx = '0123456789abcdef0123456789abcdef';
sub state {
    return {
        v => 3, sid => 'store-a', vol => 'vm-100-disk-0',
        phase => 'PREPARED', tx => $tx,
        old => 'g0', new => 'g1', head => 'g0', generation => 0, region => 8,
        @_,
    };
}

my $prepared = state();
my $encoded = anchor_tags(%$prepared);
is_deeply(
    { map { $_ => $prepared->{$_} } keys %$prepared },
    { map { $_ => decode_anchor_tags($encoded)->{$_} } keys %$prepared },
    'anchor round-trip preserves every authoritative field',
);

my @tampered = @$encoded;
$tampered[2] = 'slt_tg_vol=vm-999-disk-0';
eval { decode_anchor_tags(\@tampered) };
like($@, qr/digest mismatch/, 'tampered anchor fails its digest gate');

my @duplicate = (@$encoded, 'slt_tg_head=g0');
eval { decode_anchor_tags(\@duplicate) };
like($@, qr/duplicate/, 'duplicate anchor field fails closed');

my $committed = state(phase => 'COMMITTED', head => 'g1', generation => 1);
ok(validate_anchor_transition($prepared, $committed), 'PREPARED to COMMITTED is valid');
my $complete = state(
    phase => 'HYDRATION_COMPLETE', head => 'g1', generation => 1,
);
ok(validate_anchor_transition($committed, $complete), 'bounded completion shortcut is valid');
my $pivoted = state(phase => 'LINEAR_PIVOTED', head => 'g1', generation => 1);
ok(validate_anchor_transition($complete, $pivoted), 'complete to linear pivot is valid');
my $materialized = state(phase => 'MATERIALIZED', head => 'g1', generation => 1);
ok(validate_anchor_transition($pivoted, $materialized), 'linear pivot to materialized is valid');
my $next_prepared = state(
    phase => 'PREPARED', tx => ('a' x 32), old => 'g1', new => 'g2',
    head => 'g1', generation => 1, region => 16,
);
ok(validate_anchor_transition($materialized, $next_prepared),
    'materialized object starts a fresh transaction with explicit recovery geometry');
eval { validate_anchor_transition($materialized, state(%$next_prepared, tx => $materialized->{tx})) };
like($@, qr/fresh transaction ID/, 'new transition cannot reuse the previous transaction ID');
eval { validate_anchor_transition($materialized, state(%$next_prepared, head => 'g2')) };
like($@, qr/preserve the authoritative old HEAD/, 'PREPARED cannot publish the new HEAD early');
eval { validate_anchor_transition($materialized, state(%$next_prepared, new => 'g1')) };
like($@, qr/distinct destination/, 'new transition requires a distinct destination');

eval { validate_anchor_transition($prepared, $complete) };
like($@, qr/illegal/, 'phase skipping fails closed');
eval { validate_anchor_transition($prepared, state(%$committed, tx => ('f' x 32))) };
like($@, qr/immutable field 'tx'/, 'transaction replacement fails closed');
eval { validate_anchor_transition($prepared, state(%$committed, generation => 2)) };
like($@, qr/advance exactly once/, 'generation skipping fails closed');
eval { validate_anchor_transition($prepared, state(%$committed, region => 16)) };
like($@, qr/immutable field 'region'/, 'recovery geometry cannot drift during transition');

my $initial_prepared = state(old => 'g0', new => 'g0', head => 'g0');
my $initial_done = state(
    phase => 'MATERIALIZED', old => 'g0', new => 'g0', head => 'g0',
);
ok(validate_anchor_transition($initial_prepared, $initial_done),
    'initial allocation may materialize without a clone transition');

my $generation = generation_tags(
    sid => 'store-a', vol => 'vm-100-disk-0', role => 'snapshot',
    generation => 0, snapshot => 'snap1',
);
ok(validate_generation_tags(
    $generation, sid => 'store-a', vol => 'vm-100-disk-0', role => 'snapshot',
    generation => 0, snapshot => 'snap1',
), 'generation ownership proof validates exactly');
eval { validate_generation_tags(
    $generation, sid => 'store-b', vol => 'vm-100-disk-0', role => 'snapshot',
    generation => 0, snapshot => 'snap1',
) };
like($@, qr/ownership proof mismatch/, 'foreign generation is rejected');

my $transition = transition_tags(
    sid => 'store-a', vol => 'vm-100-disk-0', tx => $tx,
    kind => 'metadata', generation => 1, region => 8,
);
ok(validate_transition_tags(
    $transition, sid => 'store-a', vol => 'vm-100-disk-0', tx => $tx,
    kind => 'metadata', generation => 1, region => 8,
), 'persistent transition metadata ownership validates exactly');
eval { validate_transition_tags(
    $transition, sid => 'store-a', vol => 'vm-100-disk-0', tx => ('f' x 32),
    kind => 'metadata', generation => 1, region => 8,
) };
like($@, qr/ownership proof mismatch/, 'foreign transition metadata is rejected');

my $intent = vg_intent_tags(
    tx => $tx, state => 'OPEN', op => 'DM_CUTOVER', object => 'sltg-a-test',
    before => ('a' x 32),
);
is(decode_vg_intent_tags($intent)->{tx}, $tx, 'VG intent round-trip validates');

my $small_geometry = clone_geometry(32 * 1024 * 1024 * 1024);
is($small_geometry->{region_sectors}, 8, 'ordinary VM disk retains 4 KiB regions');
cmp_ok($small_geometry->{metadata_bytes}, '>=', 16 * 1024 * 1024,
    'ordinary VM metadata includes structural headroom');
my $large_geometry = clone_geometry(30 * 1024 * 1024 * 1024 * 1024);
cmp_ok($large_geometry->{region_sectors}, '>', 8,
    'large disk automatically bounds dm-clone region count');
cmp_ok($large_geometry->{regions}, '<=', 134_217_728,
    'large disk in-core bitmap cardinality is bounded');
cmp_ok($large_geometry->{metadata_bytes}, '>', 16 * 1024 * 1024,
    'large disk does not inherit a fixed 16 MiB metadata device');
eval { clone_geometry(513) };
like($@, qr/sector-aligned/, 'unaligned clone geometry fails closed');

done_testing();
