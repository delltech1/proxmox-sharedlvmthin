use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinThick qw(
    anchor_tags decode_anchor_tags generation_tags validate_anchor_transition
    validate_generation_tags vg_intent_tags decode_vg_intent_tags clone_geometry
    transition_tags validate_transition_tags materialized_rebase_state
    classify_recovery
);

my $tx = '0123456789abcdef0123456789abcdef';
sub state {
    return {
        v => 5, sid => 'store-a', vol => 'vm-100-disk-0',
        phase => 'PREPARED', tx => $tx,
        op => 'SNAPSHOT', snapshot => 'snap1', source => 'g0',
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
eval { anchor_tags(%$prepared, head => 'g1') };
like($@, qr/old generation authoritative/, 'internally inconsistent PREPARED state is rejected');
eval { anchor_tags(%$prepared, snapshot => 'none') };
like($@, qr/persist an exact snapshot name/,
    'snapshot transition cannot omit its persistent snapshot identity');
eval { anchor_tags(%$prepared, v => 4) };
like($@, qr/unsupported/, 'pre-recovery anchor schema is rejected explicitly');

my $source_ready = state(phase => 'SOURCE_READY');
ok(validate_anchor_transition($prepared, $source_ready), 'PREPARED to SOURCE_READY is valid');
my $committed = state(phase => 'COMMITTED', head => 'g1', generation => 1);
ok(validate_anchor_transition($source_ready, $committed), 'SOURCE_READY to COMMITTED is valid');
eval { validate_anchor_transition($source_ready, { %$committed, snapshot => 'other' }) };
like($@, qr/immutable field 'snapshot'/,
    'snapshot identity cannot drift after PREPARED');
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
    head => 'g1', generation => 1, region => 16, source => 'g1',
);
ok(validate_anchor_transition($materialized, $next_prepared),
    'materialized object starts a fresh transaction with explicit recovery geometry');
my $rebased = materialized_rebase_state($materialized, ('c' x 32));
is($rebased->{phase}, 'MATERIALIZED', 'snapshot deletion rebase remains materialized');
is($rebased->{tx}, ('c' x 32), 'snapshot deletion rebase uses its fresh intent transaction');
is($rebased->{op}, 'ALLOC', 'snapshot deletion rebase returns to canonical operation');
is($rebased->{snapshot}, 'none', 'snapshot deletion rebase removes historical snapshot identity');
is($rebased->{source}, $rebased->{head}, 'rebased source is the authoritative HEAD');
is($rebased->{old}, $rebased->{head}, 'rebased old edge is the authoritative HEAD');
is($rebased->{new}, $rebased->{head}, 'rebased new edge is the authoritative HEAD');
eval { materialized_rebase_state($materialized, $materialized->{tx}) };
like($@, qr/fresh transaction ID/, 'snapshot deletion rebase rejects transaction reuse');
eval { materialized_rebase_state($prepared, ('d' x 32)) };
like($@, qr/only a MATERIALIZED/, 'snapshot deletion rebase rejects an active transition');
my $rollback_prepared = state(
    phase => 'PREPARED', tx => ('b' x 32), op => 'ROLLBACK',
    snapshot => 'snap1', source => 'g0', old => 'g1', new => 'g2', head => 'g1',
    generation => 1, region => 8,
);
ok(validate_anchor_transition($materialized, $rollback_prepared),
    'rollback persists the retained snapshot as a source distinct from old HEAD');
my $rollback_source_ready = {
    %$rollback_prepared, phase => 'SOURCE_READY',
};
ok(validate_anchor_transition($rollback_prepared, $rollback_source_ready),
    'rollback persists exact source readiness before cutover');
my $rollback_committed = {
    %$rollback_prepared, phase => 'COMMITTED', head => 'g2', generation => 2,
};
ok(validate_anchor_transition($rollback_source_ready, $rollback_committed),
    'rollback commit advances HEAD exactly once');
eval { anchor_tags(%$rollback_prepared, source => 'g1') };
like($@, qr/retained snapshot, not the previous HEAD/,
    'rollback cannot silently clone the current HEAD');
eval { validate_anchor_transition($materialized, state(%$next_prepared, tx => $materialized->{tx})) };
like($@, qr/fresh transaction ID/, 'new transition cannot reuse the previous transaction ID');
eval { validate_anchor_transition($materialized, state(%$next_prepared, head => 'g2')) };
like($@, qr/old generation authoritative|preserve the authoritative old HEAD/,
    'PREPARED cannot publish the new HEAD early');
eval { validate_anchor_transition($materialized, state(%$next_prepared, new => 'g1')) };
like($@, qr/distinct (?:old and new generations|destination)/,
    'new transition requires a distinct destination');

eval { validate_anchor_transition($prepared, $complete) };
like($@, qr/illegal/, 'phase skipping fails closed');
eval { validate_anchor_transition($prepared, $committed) };
like($@, qr/illegal/, 'PREPARED cannot publish COMMITTED before SOURCE_READY');
eval { validate_anchor_transition($source_ready, state(%$committed, tx => ('f' x 32))) };
like($@, qr/immutable field 'tx'/, 'transaction replacement fails closed');
eval { validate_anchor_transition($source_ready, state(%$committed, generation => 2)) };
like($@, qr/advance exactly once/, 'generation skipping fails closed');
eval { validate_anchor_transition($source_ready, state(%$committed, region => 16)) };
like($@, qr/immutable field 'region'/, 'recovery geometry cannot drift during transition');

my $initial_prepared = state(
    old => 'g0', new => 'g0', head => 'g0', op => 'ALLOC', snapshot => 'none', source => 'g0',
);
my $initial_done = state(
    phase => 'MATERIALIZED', old => 'g0', new => 'g0', head => 'g0',
    op => 'ALLOC', snapshot => 'none', source => 'g0',
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
my $remove_snapshot_intent = vg_intent_tags(
    tx => $tx, state => 'OPEN', op => 'REMOVE_SNAPSHOT', object => 'sltg-g-test',
    before => ('b' x 32),
);
is(decode_vg_intent_tags($remove_snapshot_intent)->{op}, 'REMOVE_SNAPSHOT',
    'snapshot deletion has an explicit transaction intent');

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
my $pib = 2 ** 50;
for my $size_pib (1, 16, 128) {
    my $geometry = clone_geometry($size_pib * $pib);
    cmp_ok($geometry->{regions}, '<=', 134_217_728,
        "$size_pib PiB geometry keeps the in-core bitmap bounded");
    cmp_ok($geometry->{metadata_bytes}, '<=', 16 * 1024 * 1024 * 1024,
        "$size_pib PiB geometry keeps persistent metadata within the implementation limit");
    ok(($geometry->{region_sectors} & ($geometry->{region_sectors} - 1)) == 0,
        "$size_pib PiB geometry retains a power-of-two region size");
}
eval { clone_geometry(128 * $pib + 512) };
like($@, qr/exceeds supported dm-clone geometry/,
    'geometry above the exact 128 PiB boundary fails closed without integer wrap');
eval { clone_geometry(513) };
like($@, qr/sector-aligned/, 'unaligned clone geometry fails closed');

my $anchor_object = 'sltg-a-recoverytest';
my $old_tx = '1' x 32;
my $new_tx = '2' x 32;
my $recovery_materialized = {
    v => 5, sid => 'store-a', vol => 'vm-100-disk-0', phase => 'MATERIALIZED',
    tx => $old_tx, old => 'g0', new => 'g0', head => 'g0', generation => 0,
    region => 8, op => 'ALLOC', snapshot => 'none', source => 'g0',
};
my $cutover_intent = {
    v => 1, tx => $new_tx, state => 'OPEN', op => 'DM_CUTOVER',
    object => $anchor_object, before => ('c' x 32),
};
my $recovery_prepared = {
    %$recovery_materialized, phase => 'PREPARED', tx => $new_tx,
    old => 'g0', new => 'g1', head => 'g0', op => 'SNAPSHOT', snapshot => 'snap2', source => 'g0',
};
my %transition_objects = (head => 1, source => 1, old => 1, new => 1, meta => 1);
my @crash_matrix = (
    ['C0', $recovery_materialized, undef, { head => 1 }, 'absent', 'none', 'COMMITTED'],
    ['C1', $recovery_materialized, $cutover_intent, { head => 1 }, 'absent', 'none', 'PREPARE_INCOMPLETE'],
    ['C2', $recovery_materialized, $cutover_intent,
        { %transition_objects, candidate_new => 1 }, 'linear-head', 'none', 'PREPARE_UNRECORDED'],
    ['C3', $recovery_prepared, $cutover_intent, { %transition_objects }, 'absent', 'none', 'PREPARED'],
    ['C4', { %$recovery_prepared, phase => 'SOURCE_READY' }, $cutover_intent,
        { %transition_objects }, 'linear-head', 'none', 'SOURCE_READY'],
    ['C5', { %$recovery_prepared, phase => 'SOURCE_READY' }, $cutover_intent,
        { %transition_objects }, 'linear-old', 'none', 'SOURCE_READY'],
    ['C6', { %$recovery_prepared, phase => 'COMMITTED', head => 'g1', generation => 1 },
        $cutover_intent, { %transition_objects }, 'absent', 'none', 'COMMITTED'],
    ['C7', { %$recovery_prepared, phase => 'COMMITTED', head => 'g1', generation => 1 },
        $cutover_intent, { %transition_objects }, 'clone', 'incomplete', 'COMMITTED', 'source'],
    ['C8', { %$recovery_prepared, phase => 'HYDRATING', head => 'g1', generation => 1 },
        $cutover_intent, { %transition_objects }, 'clone', 'incomplete', 'HYDRATING', 'source'],
    ['C9', { %$recovery_prepared, phase => 'HYDRATION_COMPLETE', head => 'g1', generation => 1 },
        $cutover_intent, { %transition_objects }, 'clone', 'complete', 'HYDRATION_COMPLETE', 'source'],
);
for my $case (@crash_matrix) {
    my ($point, $anchor_state, $open_intent, $objects, $runtime, $status, $transaction, $clone_source) = @$case;
    my $classification = classify_recovery(
        anchor => $anchor_state, intent => $open_intent, objects => $objects,
        runtime => $runtime, clone_status => $status,
        clone_source => ($clone_source // 'none'), expected_anchor => $anchor_object,
    );
    is($classification->{transaction_state}, $transaction, "$point has deterministic transaction classification");
    is($classification->{safe_for_mutation}, ($point eq 'C0' ? 1 : 0),
        "$point mutation policy is fail-closed until fully healthy");
}
my $remove_snapshot_open = {
    v => 1, tx => ('c' x 32), state => 'OPEN', op => 'REMOVE_SNAPSHOT',
    object => 'g0', before => ('d' x 32),
};
my $delete_prepared = classify_recovery(
    anchor => $materialized, intent => $remove_snapshot_open,
    objects => { head => 1, source => 1, old => 1, new => 1, meta => 0 },
    runtime => 'linear-head', intent_object_present => 1,
);
is($delete_prepared->{transaction_state}, 'SNAPSHOT_DELETE_PREPARED',
    'snapshot delete before anchor rebase is classified exactly');
is($delete_prepared->{safe_for_mutation}, 0,
    'snapshot delete before anchor rebase remains fail-closed');
my $canonical_before_delete = materialized_rebase_state(
    $materialized, ('e' x 32),
);
my $canonical_delete_prepared = classify_recovery(
    anchor => $canonical_before_delete, intent => $remove_snapshot_open,
    objects => { head => 1, source => 1, old => 1, new => 1, meta => 0 },
    runtime => 'linear-head', intent_object_present => 1,
);
is($canonical_delete_prepared->{transaction_state}, 'SNAPSHOT_DELETE_PREPARED',
    'a canonical ALLOC anchor is a valid pre-rebase snapshot-delete state');
my $delete_ready = classify_recovery(
    anchor => $rebased, intent => $remove_snapshot_open,
    objects => { head => 1, source => 1, old => 1, new => 1, meta => 0 },
    runtime => 'linear-head', intent_object_present => 1,
);
is($delete_ready->{transaction_state}, 'SNAPSHOT_DELETE_READY',
    'rebased anchor and retained exact snapshot are retryable evidence');
my $delete_finalize = classify_recovery(
    anchor => $rebased, intent => $remove_snapshot_open,
    objects => { head => 1, source => 1, old => 1, new => 1, meta => 0 },
    runtime => 'linear-head', intent_object_present => 0,
);
is($delete_finalize->{transaction_state}, 'SNAPSHOT_DELETE_FINALIZE',
    'missing exact snapshot after rebase is an explicit finalize state');
is($delete_finalize->{safe_for_mutation}, 0,
    'snapshot delete finalize requires explicit intent cleanup');
my $c6_suspended = classify_recovery(
    anchor => { %$recovery_prepared, phase => 'COMMITTED', head => 'g1', generation => 1 },
    intent => $cutover_intent, objects => { %transition_objects },
    runtime => 'linear-old', runtime_suspended => 1, clone_status => 'none',
    expected_anchor => $anchor_object,
);
is($c6_suspended->{materialization_state}, 'PUBLISH_REQUIRED',
    'C6 suspended old-generation runtime is an exact resumable publication state');
is($c6_suspended->{safe_for_mutation}, 0,
    'C6 publication recovery remains fail-closed');
my $c6_active = classify_recovery(
    anchor => { %$recovery_prepared, phase => 'COMMITTED', head => 'g1', generation => 1 },
    intent => $cutover_intent, objects => { %transition_objects },
    runtime => 'linear-old', runtime_suspended => 0, clone_status => 'none',
    expected_anchor => $anchor_object,
);
is($c6_active->{data_state}, 'AMBIGUOUS',
    'an active old-generation frontend after COMMITTED fails closed');
my $ambiguous_recovery = classify_recovery(
    anchor => { %$recovery_prepared, phase => 'COMMITTED', head => 'g1', generation => 1 },
    intent => $cutover_intent, objects => { %transition_objects },
    runtime => 'linear-old', clone_status => 'none', expected_anchor => $anchor_object,
);
is($ambiguous_recovery->{data_state}, 'AMBIGUOUS',
    'contradictory committed runtime evidence is never guessed');
is($ambiguous_recovery->{safe_for_mutation}, 0,
    'contradictory recovery evidence blocks mutation');

my $rollback_intent = {
    %$cutover_intent, op => 'DM_PIVOT', tx => ('3' x 32),
};
my $rollback_recovery = {
    %$recovery_prepared, tx => $rollback_intent->{tx}, op => 'ROLLBACK',
    snapshot => 'snap1', source => 'g-snapshot', old => 'g1', new => 'g2', head => 'g1', generation => 1,
};
my %rollback_objects = (head => 1, source => 1, old => 1, new => 1, meta => 1);
for my $case (
    ['PREPARED', $rollback_recovery, 'linear-old', 'none', 'none'],
    ['COMMITTED', { %$rollback_recovery, phase => 'COMMITTED', head => 'g2', generation => 2 },
        'clone', 'incomplete', 'source'],
    ['HYDRATING', { %$rollback_recovery, phase => 'HYDRATING', head => 'g2', generation => 2 },
        'clone', 'incomplete', 'source'],
    ['HYDRATION_COMPLETE', { %$rollback_recovery, phase => 'HYDRATION_COMPLETE', head => 'g2', generation => 2 },
        'clone', 'complete', 'source'],
    ['LINEAR_PIVOTED', { %$rollback_recovery, phase => 'LINEAR_PIVOTED', head => 'g2', generation => 2 },
        'linear-new', 'none', 'none'],
) {
    my ($phase, $anchor_state, $runtime, $status, $source) = @$case;
    my $classification = classify_recovery(
        anchor => $anchor_state, intent => $rollback_intent,
        objects => { %rollback_objects }, runtime => $runtime,
        clone_status => $status, clone_source => $source,
        expected_anchor => $anchor_object,
    );
    is($classification->{data_state}, 'VALID', "rollback $phase has deterministic data authority");
    is($classification->{safe_for_mutation}, 0, "rollback $phase remains fail-closed");
}

my $rollback_materialized = {
    %$rollback_recovery, phase => 'MATERIALIZED', head => 'g2', generation => 2,
};
my $rollback_healthy = classify_recovery(
    anchor => $rollback_materialized, intent => undef,
    objects => { head => 1, source => 1 }, runtime => 'linear-head',
    expected_anchor => $anchor_object,
);
is($rollback_healthy->{state}, 'HEALTHY',
    'materialized rollback is healthy only after superseded HEAD and metadata are gone');

my $wrong_rollback_source = classify_recovery(
    anchor => { %$rollback_recovery, phase => 'COMMITTED', head => 'g2', generation => 2 },
    intent => $rollback_intent, objects => { %rollback_objects }, runtime => 'clone',
    clone_status => 'incomplete', clone_source => 'old', expected_anchor => $anchor_object,
);
is($wrong_rollback_source->{data_state}, 'AMBIGUOUS',
    'rollback clone depending on old HEAD instead of signed snapshot fails closed');

my $unclean_rollback = classify_recovery(
    anchor => $rollback_materialized, intent => undef,
    objects => { head => 1, source => 1, old => 1 }, runtime => 'linear-head',
    expected_anchor => $anchor_object,
);
is($unclean_rollback->{state}, 'RECOVERY_REQUIRED',
    'materialized rollback cannot hide a retained superseded HEAD');

my $snapshot_healthy = classify_recovery(
    anchor => $materialized, intent => undef,
    objects => { head => 1, source => 1, old => 1 }, runtime => 'linear-head',
    expected_anchor => $anchor_object,
);
is($snapshot_healthy->{state}, 'HEALTHY',
    'materialized snapshot retains its immutable source generation');

my $snapshot_source_missing = classify_recovery(
    anchor => $materialized, intent => undef,
    objects => { head => 1 }, runtime => 'linear-head',
    expected_anchor => $anchor_object,
);
is($snapshot_source_missing->{state}, 'RECOVERY_REQUIRED',
    'materialized snapshot with a missing source fails closed');

my $suspended_materialized = classify_recovery(
    anchor => $materialized, intent => undef,
    objects => { head => 1, source => 1, old => 1 }, runtime => 'linear-head',
    runtime_suspended => 1, expected_anchor => $anchor_object,
);
is($suspended_materialized->{state}, 'RECOVERY_REQUIRED',
    'a suspended materialized frontend is never declared healthy');

done_testing();
