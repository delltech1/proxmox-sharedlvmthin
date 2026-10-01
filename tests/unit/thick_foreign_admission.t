#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib";
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $tx = 'a' x 32;
my $foreign_anchor = 'sltg-a-foreign';
my $requested_anchor = 'sltg-a-requested';
my $scfg = {
    type => 'sharedlvmthin', shared => 1,
    'slt-vgname' => 'testvg',
    'slt-expected-vg-uuid' => 'vg-uuid',
    'slt-expected-pv-uuid' => 'pv-uuid',
    'slt-expected-wwid' => '6000a',
    'slt-vg-reserve-gib' => 1,
    'slt-allocation-mode' => 'thick-generations',
};
my $foreign = { %$scfg, 'slt-allocation-mode' => 'thick-generations-lazy' };
my $cfg = { ids => { request => $scfg, foreign => $foreign } };
my $lvs = { testvg => {
    $foreign_anchor => { tags => 'anchor-tags', lv_uuid => 'anchor-uuid' },
} };

sub foreign_state {
    my ($phase, $foreign_tx) = @_;
    return {
        v => 5, sid => 'foreign', vol => 'vm-200-disk-0',
        tx => ($foreign_tx // $tx), phase => $phase, op => 'SNAPSHOT',
        snapshot => 'snap1', head => 'head2', old => 'head1', new => 'head2',
        source => 'head1', generation => 2,
    };
}

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub { undef };
    my $result = $class->_thick_foreign_intent_admission(
        'request', $scfg, $requested_anchor, undef,
    );
    is($result->{action}, 'GRANT', 'absence of an intent grants admission');
}

{
    no warnings 'redefine';
    my $state = foreign_state('HYDRATING');
    my $resume_calls = 0;
    my $persistent_only;
    local *PVE::Storage::config = sub { $cfg };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub {{
        tx => $tx, state => 'OPEN', op => 'DM_CUTOVER',
        object => $foreign_anchor, before => ('0' x 32),
    }};
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::decode_anchor_tags = sub { $state };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($state, {}, $foreign_anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_resume_transition = sub {
        $persistent_only = $_[8];
        $resume_calls++;
        return {};
    };
    my $result = $class->_thick_foreign_intent_admission(
        'request', $scfg, $requested_anchor, undef,
    );
    is($result->{action}, 'WAIT_EXACT_FOREIGN', 'exact foreign transition is waitable');
    is($result->{receipt}->{tx}, $tx, 'wait receipt pins the transaction');
    is($result->{receipt}->{sid}, 'foreign', 'wait receipt pins the sibling alias');
    is($resume_calls, 1, 'deep transition proof is executed read-only');
    is($persistent_only, 1, 'foreign admission requests persistent-only proof');
}

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub {{
        tx => $tx, state => 'OPEN', op => 'DM_CUTOVER',
        object => $requested_anchor, before => ('0' x 32),
    }};
    my $ok = eval {
        $class->_thick_foreign_intent_admission(
            'request', $scfg, $requested_anchor, undef,
        );
        1;
    };
    ok(!$ok, 'own unresolved transition is never treated as a new request');
    like($@, qr/implicit retry refused/, 'own-intent refusal is explicit');
}

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub {{
        tx => $tx, state => 'OPEN', op => 'OTHER',
        object => $foreign_anchor, before => ('0' x 32),
    }};
    my $ok = eval {
        $class->_thick_foreign_intent_admission(
            'request', $scfg, $requested_anchor, undef,
        );
        1;
    };
    ok(!$ok, 'unsupported intent is not waitable');
    like($@, qr/not an exact foreign Thick transition/, 'malformed intent fails closed');
}

{
    no warnings 'redefine';
    my $state = foreign_state('HYDRATING', 'b' x 32);
    my $old_state = foreign_state('PREPARED', $tx);
    my $anchor_reads = 0;
    local *PVE::Storage::config = sub { $cfg };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub {{
        tx => ('b' x 32), state => 'OPEN', op => 'DM_CUTOVER',
        object => $foreign_anchor, before => ('1' x 32),
    }};
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::decode_anchor_tags = sub { $state };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        $anchor_reads++;
        return $anchor_reads == 1
            ? ($state, {}, $foreign_anchor)
            : ($old_state, {}, $foreign_anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_resume_transition = sub { {} };
    my $ok = eval {
        $class->_thick_foreign_intent_admission(
            'request', $scfg, $requested_anchor,
            { tx => $tx, sid => 'foreign', vol => 'vm-200-disk-0', anchor => $foreign_anchor },
        );
        1;
    };
    ok(!$ok, 'replacement of a PREPARED blocker is refused');
    like($@, qr/not a proven anchor handoff/, 'replacement requires durable handoff evidence');
}

{
    no warnings 'redefine';
    my @decisions = (
        { action => 'WAIT_EXACT_FOREIGN', receipt => {
            tx => $tx, sid => 'foreign', vol => 'vm-200-disk-0',
            anchor => $foreign_anchor, phase => 'HYDRATING',
        } },
        { action => 'GRANT' },
    );
    my ($callback_calls, $lock_calls, $pause_calls) = (0, 0, 0);
    my @clock = (100, 100, 100, 101);
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_admission_now = sub {
        return shift(@clock) // 101;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_observation_pause = sub {
        $pause_calls++;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my ($class, $sid, $candidate, $code, $device, $budget, $bypass, $classifier) = @_;
        $lock_calls++;
        my $decision = shift @decisions;
        return $decision if $decision->{action} eq 'WAIT_EXACT_FOREIGN';
        $classifier->();
        return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_foreign_intent_admission = sub {
        return { action => 'GRANT' };
    };
    my $result = $class->_with_thick_allocation_admission(
        'request', { %$scfg, 'slt-mutation-admission-timeout' => 30 },
        'sltg-a-' . ('1' x 24), sub { $callback_calls++; return 'ALLOCATED'; },
    );
    is($result, 'ALLOCATED', 'allocation runs after exact foreign settlement');
    is($callback_calls, 1, 'mutating allocation callback runs exactly once');
    is($lock_calls, 2, 'foreign wait reacquires the canonical lock');
    is($pause_calls, 1, 'foreign wait yields outside the canonical lock');
}

{
    no warnings 'redefine';
    my @clock = (200, 200, 211);
    my $callback_calls = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_admission_now = sub {
        return shift(@clock) // 211;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_observation_pause = sub { };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {{
        action => 'WAIT_EXACT_FOREIGN', receipt => {
            tx => $tx, sid => 'foreign', vol => 'vm-200-disk-0',
            anchor => $foreign_anchor, phase => 'HYDRATING',
        },
    }};
    my $ok = eval {
        $class->_with_thick_allocation_admission(
            'request', { %$scfg, 'slt-mutation-admission-timeout' => 10 },
            'sltg-a-' . ('2' x 24), sub { $callback_calls++ },
        );
        1;
    };
    ok(!$ok, 'allocation admission stops at its one monotonic deadline');
    like($@, qr/no allocation mutation was issued/, 'deadline refusal is explicit');
    is($callback_calls, 0, 'deadline performs no allocation effect');
}

{
    no warnings 'redefine';
    my $callback_calls = 0;
    my @clock = (300, 300);
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_admission_now = sub {
        return shift(@clock) // 300;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my ($class, $sid, $candidate, $code, $device, $budget, $bypass, $classifier) = @_;
        my $decision = $classifier->();
        return $decision if ($decision->{action} // '') ne 'GRANT';
        return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_foreign_intent_admission = sub {{
        action => 'GRANT',
    }};
    my $ok = eval {
        $class->_with_thick_allocation_admission(
            'request', { %$scfg, 'slt-mutation-admission-timeout' => 30 },
            'sltg-a-' . ('3' x 24),
            sub { $callback_calls++; die "injected first-effect failure\n" },
        );
        1;
    };
    ok(!$ok, 'allocation callback failure is propagated');
    like($@, qr/injected first-effect failure/, 'original callback failure is preserved');
    is($callback_calls, 1, 'failing mutating callback is never replayed');
}

{
    no warnings 'redefine';
    my $callback_calls = 0;
    my @clock = (400, 400);
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_admission_now = sub {
        return shift(@clock) // 400;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my ($class, $sid, $candidate, $code, $device, $budget, $bypass, $classifier) = @_;
        return $classifier->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_foreign_intent_admission = sub {
        die "unsupported foreign mutation intent\n";
    };
    my $ok = eval {
        $class->_with_thick_allocation_admission(
            'request', { %$scfg, 'slt-mutation-admission-timeout' => 30 },
            'sltg-a-' . ('4' x 24), sub { $callback_calls++ },
        );
        1;
    };
    ok(!$ok, 'unsupported foreign intent is refused by the wrapper');
    like($@, qr/unsupported foreign mutation intent/, 'classifier refusal is preserved');
    is($callback_calls, 0, 'classifier refusal performs no allocation effect');
}

done_testing();
