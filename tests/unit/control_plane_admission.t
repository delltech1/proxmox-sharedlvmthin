#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmAdmission qw(
    evaluate_latch_acquire evaluate_latch_close evaluate_prepare_ack
    evaluate_executor_reserve evaluate_executor_bind evaluate_executor_dispatch
    evaluate_executor_finish evaluate_executor_close
);

my $boot_a = '11111111-2222-3333-4444-555555555555';
my $boot_b = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
my $base = {
    schema => 1,
    kind => 'VG_MUTATION',
    transaction => ('a' x 32),
    cluster_id => 'cluster-lab',
    vg_uuid => 'ABCDEF-1234-5678-9abc-def0-1234-ABCDEF',
    storage_set => ('9' x 64),
    node => 'node-a',
    boot_id => $boot_a,
    operation => 'ALLOC',
    executor => 'slt-admission-' . ('a' x 32) . '.service',
};

my $result = evaluate_latch_acquire(
    requested => $base, control_plane_continuity_proven => 1,
);
is($result->{action}, 'ACQUIRE_ATOMIC', 'absent fixed latch permits one atomic acquisition');
ok(!$result->{dispatch_allowed}, 'atomic acquire alone never authorizes dispatch');

$result = evaluate_latch_acquire(requested => $base);
ok(!$result->{allowed}, 'absent latch without continuity proof remains blocked');

$result = evaluate_latch_acquire(existing => {}, requested => $base);
ok(!$result->{allowed}, 'empty acquired latch blocks instead of looking absent');
like($result->{reason}, qr/empty or malformed/, 'empty latch has explicit recovery reason');

$result = evaluate_latch_acquire(existing => $base, requested => $base);
ok(!$result->{allowed}, 'ordinary duplicate invocation cannot redispatch exact transaction');

$result = evaluate_latch_acquire(
    existing => $base, requested => $base, explicit_resume => 1,
);
is($result->{action}, 'INSPECT_EXACT_EXISTING', 'explicit exact resume may inspect existing latch');
ok(!$result->{dispatch_allowed}, 'resume never gains dispatch authority from identity alone');

for my $change (
    [transaction => ('b' x 32)], [node => 'node-b'], [boot_id => $boot_b],
    [operation => 'FREE'], [vg_uuid => 'ZYXWVU-9876-5432-1fed-cba0-9876-ZYXWVU'], [cluster_id => 'cluster-b'],
    [storage_set => ('8' x 64)],
    [executor => 'slt-admission-' . ('b' x 32) . '.service'],
) {
    my $requested = {%$base, $change->[0] => $change->[1]};
    $result = evaluate_latch_acquire(
        existing => $base, requested => $requested, explicit_resume => 1,
    );
    ok(!$result->{allowed}, "existing latch rejects changed $change->[0]");
}

$result = evaluate_latch_acquire(
    requested => {%$base, unexpected_future_field => 'unsafe'},
    control_plane_continuity_proven => 1,
);
ok(!$result->{allowed}, 'unknown future identity field fails closed');

my $close = {
    identity => {%$base},
    node => $base->{node},
    boot_id => $base->{boot_id},
    postcondition_proven => 1,
    executor_terminal => 1,
    executor_result => 'SUCCESS',
};
$result = evaluate_latch_close(record => $base, evidence => $close);
is($result->{action}, 'CLOSE_EXACT_NORMAL', 'exact terminal executor and postcondition permit normal close');

for my $change (
    [postcondition_proven => 0], [executor_terminal => 0],
    [executor_result => 'UNKNOWN'], [node => 'node-b'], [boot_id => $boot_b],
) {
    my $evidence = {%$close, $change->[0] => $change->[1]};
    $result = evaluate_latch_close(record => $base, evidence => $evidence);
    ok(!$result->{allowed}, "normal close rejects $change->[0] ambiguity");
}

for my $field (qw(postcondition_proven executor_terminal)) {
    $result = evaluate_latch_close(
        record => $base, evidence => {%$close, $field => 'UNKNOWN'},
    );
    ok(!$result->{allowed}, "$field truthy string cannot satisfy close proof");
}
$result = evaluate_latch_close(
    record => $base, evidence => {%$close, identity => {%$base, executor => 'slt-admission-' . ('b' x 32) . '.service'}},
);
ok(!$result->{allowed}, 'close rejects a changed executor identity');

$result = evaluate_latch_close(
    record => $base,
    recovery => 1,
    evidence => {%$close, node => 'node-b', boot_id => $boot_b,
        original_node_fenced => 0, storage_reconciled => 1},
);
ok(!$result->{allowed}, 'boot change without fencing cannot close recovery latch');

$result = evaluate_latch_close(
    record => $base,
    recovery => 1,
    evidence => {%$close, node => 'node-b', boot_id => $boot_b,
        original_node_fenced => 1, storage_reconciled => 1},
);
is($result->{action}, 'CLOSE_AFTER_FENCED_RECOVERY', 'fencing plus reconciliation permits explicit recovery close');

my $prepare = {
    %$base,
    kind => 'THICK_PREPARE',
    operation => 'SNAPSHOT',
    volume => 'vm-900001-disk-0',
    object_key => ('c' x 24),
};
my $peer = {
    node => 'node-b', boot_id => $boot_b,
    guard_established => 'YES',
    reservation_transaction => $prepare->{transaction},
    volume => $prepare->{volume},
    object_key => $prepare->{object_key},
    storage_set => $prepare->{storage_set},
    inventory_complete => 1,
    activation_executor => 'ABSENT',
    mapper_present => 0,
    head_path_present => 0,
    clone_dependency_present => 0,
};
my $expected_peer = {node => $peer->{node}, boot_id => $peer->{boot_id}};
$result = evaluate_prepare_ack(
    reservation => $prepare, peer => $peer, expected_peer => $expected_peer,
);
is($result->{action}, 'ACK_GUARDED_ABSENCE', 'complete guarded peer absence may ACK');

for my $change (
    [inventory_complete => 0], [activation_executor => 'RUNNING'],
    [activation_executor => 'UNKNOWN'], [mapper_present => 1],
    [head_path_present => 1], [clone_dependency_present => 1],
) {
    my $evidence = {%$peer, $change->[0] => $change->[1]};
    $result = evaluate_prepare_ack(
        reservation => $prepare, peer => $evidence, expected_peer => $expected_peer,
    );
    ok(!$result->{allowed}, "PREPARE ACK rejects $change->[0] unsafe evidence");
}

for my $missing (qw(inventory_complete mapper_present head_path_present clone_dependency_present guard_established reservation_transaction volume object_key storage_set)) {
    my $evidence = {%$peer};
    delete $evidence->{$missing};
    $result = evaluate_prepare_ack(
        reservation => $prepare, peer => $evidence, expected_peer => $expected_peer,
    );
    ok(!$result->{allowed}, "PREPARE ACK rejects missing $missing evidence");
}

$result = evaluate_latch_acquire(
    requested => {%$base, node => "node-a\n"},
    control_plane_continuity_proven => 1,
);
ok(!$result->{allowed}, 'identity regex rejects a trailing newline');
$result = evaluate_latch_acquire(
    requested => {%$base, operation => 'FUTURE_MUTATION'},
    control_plane_continuity_proven => 1,
);
ok(!$result->{allowed}, 'unknown operation fails the closed enum');

$result = evaluate_prepare_ack(
    reservation => $prepare,
    peer => {%$peer, boot_id => $boot_a},
    expected_peer => $expected_peer,
);
ok(!$result->{allowed}, 'stale or changed peer boot cannot ACK');

my $executor = {
    schema => 1, kind => 'THICK_EXECUTOR',
    enrollment_epoch => ('e' x 64),
    vg_uuid => $base->{vg_uuid},
    volume => 'vm-900001-disk-0', object_key => ('c' x 24),
    transaction => ('d' x 32), attempt => ('1' x 32),
    node => 'node-a', boot_id => $boot_a,
    unit => 'slt-thick-exec-' . ('1' x 32) . '.service',
    code_digest => ('2' x 64), policy_digest => ('3' x 64),
    state => 'RESERVED',
};

my $lab_executor = {
    %$executor,
    kind => 'THICK_EXECUTOR_LAB',
    unit => 'slt-thick-lab-exec-' . ('1' x 32) . '.service',
};
$result = evaluate_executor_reserve(
    requested => $lab_executor, continuity_proven => 1, attempt_fresh_proven => 1,
);
is($result->{action}, 'RESERVE_ATOMIC',
    'explicit lab executor domain accepts only its closed lab unit grammar');
for my $cross_domain (
    {%$executor, unit => $lab_executor->{unit}},
    {%$lab_executor, unit => $executor->{unit}},
    {%$executor, kind => 'THICK_EXECUTOR_FUTURE'},
) {
    $result = evaluate_executor_reserve(
        requested => $cross_domain, continuity_proven => 1,
        attempt_fresh_proven => 1,
    );
    ok(!$result->{allowed}, 'executor kind and unit domain cannot be crossed');
}

$result = evaluate_executor_reserve(
    requested => $executor, continuity_proven => 1, attempt_fresh_proven => 1,
);
is($result->{action}, 'RESERVE_ATOMIC',
    'one per-volume executor attempt may reserve an absent continuous history');
ok(!$result->{dispatch_allowed}, 'reservation alone grants no storage dispatch');

$result = evaluate_executor_reserve(requested => $executor);
ok(!$result->{allowed}, 'missing admission continuity blocks reservation');
$result = evaluate_executor_reserve(
    requested => $executor, continuity_proven => 1,
);
ok(!$result->{allowed},
    'an authority must positively prove that an execution attempt was never used');

my $second_attempt = {
    %$executor, attempt => ('4' x 32),
    unit => 'slt-thick-exec-' . ('4' x 32) . '.service',
};
$result = evaluate_executor_reserve(
    existing => $executor, requested => $second_attempt, continuity_proven => 1,
    attempt_fresh_proven => 1,
);
ok(!$result->{allowed},
    'same storage transaction cannot authorize a second execution attempt');

my $invocation = '55555555555555555555555555555555';
$result = evaluate_executor_bind(
    record => $executor,
    claim => {identity => {%$executor}, invocation_id => $invocation, startup_proven => 1},
);
is($result->{action}, 'BIND_ATOMIC',
    'reserved attempt proposes an atomic bind to one exact systemd invocation');
ok(!$result->{dispatch_allowed}, 'an unpersisted bind proposal grants no dispatch authority');
my $bound = $result->{record};

$result = evaluate_executor_dispatch(
    persisted => $bound, claim => {identity => {%$bound}}, binding_persisted => 1,
);
is($result->{action}, 'DISPATCH_EXACT_INVOCATION',
    'only the exact authoritatively persisted BOUND invocation may dispatch');
ok($result->{dispatch_allowed}, 'confirmed exact BOUND invocation receives dispatch authority');

my $other_bound = {%$bound, invocation_id => ('8' x 32)};
$result = evaluate_executor_dispatch(
    persisted => $bound, claim => {identity => $other_bound}, binding_persisted => 1,
);
ok(!$result->{allowed},
    'a losing concurrent bind proposal cannot dispatch against the persisted winner');
$result = evaluate_executor_dispatch(
    persisted => $bound, claim => {identity => {%$bound}}, binding_persisted => 0,
);
ok(!$result->{allowed}, 'readback without positive persistence confirmation cannot dispatch');

for my $change (
    [attempt => ('4' x 32)], [transaction => ('f' x 32)],
    [boot_id => $boot_b], [code_digest => ('6' x 64)],
) {
    my $identity = {%$executor, $change->[0] => $change->[1]};
    $identity->{unit} = 'slt-thick-exec-' . $identity->{attempt} . '.service'
        if $change->[0] eq 'attempt';
    $result = evaluate_executor_bind(
        record => $executor,
        claim => {identity => $identity, invocation_id => $invocation, startup_proven => 1},
    );
    ok(!$result->{allowed}, "bind rejects changed $change->[0]");
}

$result = evaluate_executor_bind(
    record => $executor,
    claim => {identity => {%$executor}, invocation_id => ('6' x 32), startup_proven => 0},
);
ok(!$result->{allowed}, 'delayed unit without a proven startup handshake cannot dispatch');

my $terminal = {
    identity => {%$bound},
    cgroup_terminal => 1, pending_jobs_absent => 1,
    io_terminal => 1, storage_postcondition_proven => 1,
    executor_result => 'SUCCESS',
};
$result = evaluate_executor_finish(record => $bound, evidence => $terminal);
is($result->{action}, 'MARK_TERMINAL',
    'exact invocation plus terminal I/O and postcondition becomes terminal');
my $finished = $result->{record};

$result = evaluate_executor_finish(
    record => $bound,
    evidence => {%$terminal, identity => {%$finished}},
);
ok(!$result->{allowed},
    'a pre-labelled terminal identity cannot substitute for the exact BOUND record');

for my $change (
    [cgroup_terminal => 0], [pending_jobs_absent => 0],
    [io_terminal => 0], [storage_postcondition_proven => 0],
    [executor_result => 'UNKNOWN'],
) {
    my $evidence = {%$terminal, $change->[0] => $change->[1]};
    $result = evaluate_executor_finish(record => $bound, evidence => $evidence);
    is($result->{action}, 'MARK_UNKNOWN',
        "$change->[0] ambiguity persists a non-dispatchable UNKNOWN reservation");
    ok(!$result->{dispatch_allowed}, 'UNKNOWN never grants another dispatch');
}

for my $missing (qw(cgroup_terminal pending_jobs_absent io_terminal storage_postcondition_proven)) {
    my $evidence = {%$terminal};
    delete $evidence->{$missing};
    $result = evaluate_executor_finish(record => $bound, evidence => $evidence);
    ok(!$result->{allowed}, "missing $missing cannot classify an executor terminal");
}
my $changed_invocation = {%$bound, invocation_id => ('9' x 32)};
$result = evaluate_executor_finish(
    record => $bound,
    evidence => {%$terminal, identity => $changed_invocation},
);
ok(!$result->{allowed}, 'terminal evidence from another InvocationID is rejected');

$result = evaluate_executor_close(current => $finished, expected => $finished);
is($result->{action}, 'CLOSE_ATOMIC_EXACT',
    'only exact terminal attempt and invocation may request atomic close');
ok(!$result->{dispatch_allowed}, 'close proposal grants no storage dispatch');

my $new_finished = {
    %$finished, attempt => ('4' x 32),
    unit => 'slt-thick-exec-' . ('4' x 32) . '.service',
    invocation_id => ('7' x 32),
};
$result = evaluate_executor_close(current => $new_finished, expected => $finished);
ok(!$result->{allowed}, 'delayed closer cannot remove a newer attempt reservation');

$result = evaluate_executor_reserve(
    requested => $executor, continuity_proven => 1, attempt_fresh_proven => 0,
);
ok(!$result->{allowed},
    'a closed execution attempt cannot be replayed even when the transaction is reused');

my $unknown = {%$finished, state => 'UNKNOWN'};
delete $unknown->{executor_result};
$result = evaluate_executor_bind(
    record => $unknown,
    claim => {identity => {%$executor}, invocation_id => $invocation, startup_proven => 1},
);
ok(!$result->{allowed}, 'UNKNOWN reservation cannot bind another invocation');
$result = evaluate_executor_dispatch(
    persisted => $unknown, claim => {identity => {%$unknown}}, binding_persisted => 1,
);
ok(!$result->{allowed}, 'UNKNOWN reservation cannot dispatch');
$result = evaluate_executor_finish(record => $unknown, evidence => $terminal);
ok(!$result->{allowed}, 'UNKNOWN reservation cannot publish a new terminal result');
$result = evaluate_executor_close(current => $unknown, expected => $finished);
ok(!$result->{allowed}, 'UNKNOWN reservation cannot close or become a free slot');

done_testing();
