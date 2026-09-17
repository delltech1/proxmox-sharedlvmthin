#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinRelay qw(evaluate_relay_handoff);

sub evidence {
    return (
        phase => 'PREPARED',
        quorum => 'yes',
        source_fenced => 'no',
        source_mapper => 'present',
        target_mapper => 'absent',
        source_qemu => 'present',
        target_qemu => 'absent',
        source_flushed => 'no',
        relay_connected => 'no',
        journal_integrity => 'yes',
        owner => 'source',
    );
}

my $r = evaluate_relay_handoff(evidence());
is($r->{action}, 'ESTABLISH_RELAY', 'prepared transition establishes relay');

$r = evaluate_relay_handoff(evidence(), phase => 'RELAY_READY', relay_connected => 'yes');
is($r->{action}, 'QUIESCE_SOURCE', 'ready relay advances to quiesce');

$r = evaluate_relay_handoff(
    evidence(), phase => 'QUIESCED', relay_connected => 'yes', source_flushed => 'yes');
is($r->{action}, 'CLOSE_SOURCE', 'flushed quiesced source may close');

$r = evaluate_relay_handoff(
    evidence(), phase => 'SOURCE_CLOSED', source_mapper => 'absent', source_qemu => 'absent',
    source_flushed => 'yes', relay_connected => 'yes');
is($r->{action}, 'COMMIT_TARGET_OWNERSHIP', 'closed source may commit target ownership');

$r = evaluate_relay_handoff(
    evidence(), phase => 'OWNERSHIP_COMMITTED', owner => 'target', source_mapper => 'absent',
    source_qemu => 'absent', source_flushed => 'yes', relay_connected => 'yes');
is($r->{action}, 'ACTIVATE_TARGET', 'committed target may activate');

$r = evaluate_relay_handoff(
    evidence(), phase => 'TARGET_ACTIVE', owner => 'target', source_mapper => 'absent',
    source_qemu => 'absent', target_mapper => 'present', relay_connected => 'yes');
is($r->{action}, 'PIVOT_TARGET_LOCAL', 'sole active target may pivot from relay');

$r = evaluate_relay_handoff(
    evidence(), phase => 'COMPLETED', owner => 'target', source_mapper => 'absent',
    source_qemu => 'absent', target_mapper => 'present', target_qemu => 'present');
ok($r->{safe}, 'completed exact target state is safe');

$r = evaluate_relay_handoff(evidence(), target_mapper => 'present');
is($r->{state}, 'DUAL_ACTIVATION', 'dual activation always blocks');

$r = evaluate_relay_handoff(evidence(), quorum => 'unknown');
is($r->{state}, 'QUORUM_LOST', 'unknown quorum fails closed');

$r = evaluate_relay_handoff(evidence(), journal_integrity => 'unknown');
is($r->{state}, 'RECOVERY_REQUIRED', 'unknown journal integrity fails closed');

$r = evaluate_relay_handoff(
    evidence(), phase => 'SOURCE_CLOSED', source_mapper => 'absent', source_qemu => 'unknown',
    source_flushed => 'yes');
ok(!$r->{safe}, 'unfenced source with unknown QEMU state cannot commit');

$r = evaluate_relay_handoff(
    evidence(), phase => 'SOURCE_CLOSED', source_mapper => 'absent', source_qemu => 'unknown',
    source_fenced => 'yes', source_flushed => 'yes');
is($r->{action}, 'COMMIT_TARGET_OWNERSHIP', 'positive fencing substitutes for source QEMU absence');

$r = evaluate_relay_handoff(
    evidence(), phase => 'ABORTED', source_qemu => 'present', target_mapper => 'absent');
ok($r->{safe}, 'pre-commit abort restored to source is safe');

$r = evaluate_relay_handoff(
    evidence(), phase => 'ABORTED', owner => 'target', source_mapper => 'absent',
    source_qemu => 'absent', target_mapper => 'present');
ok(!$r->{safe}, 'post-commit state cannot be relabelled as aborted');

done_testing();
