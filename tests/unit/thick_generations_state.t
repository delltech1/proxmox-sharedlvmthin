use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinThick qw(
    anchor_tags decode_anchor_tags generation_tags validate_anchor_transition
    validate_generation_tags vg_intent_tags decode_vg_intent_tags
);

my $tx = '0123456789abcdef0123456789abcdef';
sub state {
    return {
        v => 2, sid => 'store-a', vol => 'vm-100-disk-0',
        phase => 'PREPARED', tx => $tx,
        old => 'g0', new => 'g1', head => 'g0', generation => 0,
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

eval { validate_anchor_transition($prepared, $complete) };
like($@, qr/illegal/, 'phase skipping fails closed');
eval { validate_anchor_transition($prepared, state(%$committed, tx => ('f' x 32))) };
like($@, qr/immutable field 'tx'/, 'transaction replacement fails closed');
eval { validate_anchor_transition($prepared, state(%$committed, generation => 2)) };
like($@, qr/advance exactly once/, 'generation skipping fails closed');

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

my $intent = vg_intent_tags(
    tx => $tx, state => 'OPEN', op => 'DM_CUTOVER', object => 'sltg-a-test',
    before => ('a' x 32),
);
is(decode_vg_intent_tags($intent)->{tx}, $tx, 'VG intent round-trip validates');

done_testing();
