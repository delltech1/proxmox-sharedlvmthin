#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinGuard qw(
    evaluate_guard_activation evaluate_guard_runtime evaluate_guard_handoff
    evaluate_guard_node
);

sub admission {
    return (
        journal_integrity => 1, quorum => 1, storage_identity => 1,
        local_runtime_absent => 1, remote_runtime_absent => 1,
        watchdog_available => 1, watchdog_armed => 1,
        owner_epoch_exact => 1, predecessor_fenced => 0,
        predecessor => 'none', requested_owner => 'self',
    );
}

my $r = evaluate_guard_activation(admission());
is($r->{action}, 'ACTIVATE_EXCLUSIVE', 'fresh fenced epoch admits exclusive activation');
$r = evaluate_guard_activation(admission(), predecessor => 'other');
is($r->{state}, 'WAIT_FENCE', 'new owner waits for positive predecessor fencing');
$r = evaluate_guard_activation(admission(), predecessor => 'other', predecessor_fenced => 1);
ok($r->{safe}, 'positively fenced predecessor permits takeover');
for my $field (qw(quorum storage_identity remote_runtime_absent watchdog_available watchdog_armed owner_epoch_exact)) {
    $r = evaluate_guard_activation(admission(), $field => 0);
    ok(!$r->{safe}, "negative $field fails closed");
}
$r = evaluate_guard_activation(admission(), requested_owner => 'other');
ok(!$r->{safe}, 'wrong requested owner is refused');
$r = evaluate_guard_activation(admission(), quorum => 'unknown');
is($r->{state}, 'RECOVERY_REQUIRED', 'malformed evidence is not interpreted');

sub runtime {
    return (
        uncertainty_latched => 0, quorum => 1, storage_identity => 1,
        owner_epoch_exact => 1, local_mapper_exact => 1,
        qemu_reference_exact => 1, remote_conflict => 0, watchdog_armed => 1,
    );
}

$r = evaluate_guard_runtime(runtime());
is($r->{action}, 'REFRESH_WATCHDOG', 'only wholly positive runtime evidence refreshes watchdog');
for my $field (qw(quorum storage_identity owner_epoch_exact watchdog_armed)) {
    $r = evaluate_guard_runtime(runtime(), $field => 0);
    is($r->{action}, 'STOP_WATCHDOG_REFRESH', "$field loss requests fencing");
}
$r = evaluate_guard_runtime(runtime(), remote_conflict => 1);
is($r->{state}, 'FENCE_REQUIRED', 'remote conflict requests fencing');
$r = evaluate_guard_runtime(runtime(), local_mapper_exact => 0);
is($r->{action}, 'PAUSE_VM_AND_WITHDRAW', 'local dependency mismatch requests controlled withdrawal');
$r = evaluate_guard_runtime(runtime(), uncertainty_latched => 1);
is($r->{action}, 'STOP_WATCHDOG_REFRESH', 'uncertainty latch cannot self-heal in the same epoch');

my @pools = (
    { pool_uuid => 'pool-a', runtime() },
    { pool_uuid => 'pool-b', runtime() },
);
$r = evaluate_guard_node(daemon_healthy => 1, watchdog_connected => 1, pools => \@pools);
is($r->{action}, 'REFRESH_WATCHDOG', 'one node client aggregates healthy pool epochs');
$pools[1]->{owner_epoch_exact} = 0;
$r = evaluate_guard_node(daemon_healthy => 1, watchdog_connected => 1, pools => \@pools);
is($r->{action}, 'STOP_WATCHDOG_REFRESH', 'one uncertain pool fences aggregate node client');
like($r->{reason}, qr/pool-b/, 'aggregate refusal names the exact pool');
$r = evaluate_guard_node(daemon_healthy => 1, watchdog_connected => 1, pools => []);
ok($r->{safe}, 'empty node may close aggregate client cleanly');
$r = evaluate_guard_node(daemon_healthy => 0, watchdog_connected => 1, pools => []);
is($r->{state}, 'FENCE_REQUIRED', 'guardian failure never refreshes watchdog');

# Exhaust the entire Boolean runtime evidence space.  There must be exactly
# one refresh state: every required proof true and no conflict/latch.
my @runtime_fields = qw(
    uncertainty_latched quorum storage_identity owner_epoch_exact
    local_mapper_exact qemu_reference_exact remote_conflict watchdog_armed
);
my $refresh_states = 0;
for my $mask (0 .. (2 ** @runtime_fields) - 1) {
    my %candidate;
    for my $index (0 .. $#runtime_fields) {
        $candidate{$runtime_fields[$index]} = ($mask >> $index) & 1;
    }
    my $decision = evaluate_guard_runtime(%candidate);
    $refresh_states++ if $decision->{action} eq 'REFRESH_WATCHDOG';
    my $expected = !$candidate{uncertainty_latched}
        && $candidate{quorum} && $candidate{storage_identity}
        && $candidate{owner_epoch_exact} && $candidate{local_mapper_exact}
        && $candidate{qemu_reference_exact} && !$candidate{remote_conflict}
        && $candidate{watchdog_armed};
    is($decision->{safe} ? 1 : 0, $expected ? 1 : 0,
        "runtime truth table mask $mask is fail-closed");
}
is($refresh_states, 1, 'exactly one of 256 runtime evidence states may refresh');

sub handoff {
    return (
        source_qemu_absent => 1, source_mapper_absent => 1,
        source_watchdog_released => 1, new_epoch_committed => 1,
        target_watchdog_armed => 1, target_runtime_absent => 1,
        quorum => 1, storage_identity => 1,
    );
}

$r = evaluate_guard_handoff(handoff());
is($r->{action}, 'ACTIVATE_TARGET', 'exact no-overlap handoff permits target activation');
for my $field (qw(source_qemu_absent source_mapper_absent source_watchdog_released new_epoch_committed target_watchdog_armed)) {
    $r = evaluate_guard_handoff(handoff(), $field => 0);
    ok(!$r->{safe}, "handoff blocks without $field");
}

done_testing();
