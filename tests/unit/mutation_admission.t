use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib", "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $cfg = { shared => 1, 'slt-vgname' => 'testvg',
    'slt-expected-vg-uuid' => 'test-uuid',
    'slt-mutation-admission-timeout' => 10 };
our $depth;
my ($now, $reads, $calls, $checks, $sleeps);
my ($intent, $read_error, $identity_error, $quorum_error, $lock_error, $clear_after);
my ($advance_lock, $change_tx);
my $run = sub {
    return $class->_with_mutation_lock('thin', $cfg, sub { $calls++; return 'done' });
};
sub reset_state {
    ($now, $depth, $reads, $calls, $checks, $sleeps) = (0,0,0,0,0,0);
    ($intent, $read_error, $identity_error, $quorum_error, $lock_error, $clear_after) = (undef) x 6;
    ($advance_lock, $change_tx) = (0,0);
}
no warnings 'redefine';
local *PVE::Storage::Custom::SharedLvmThinPlugin::_admission_now = sub { $now };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_outer_lock_yield = sub {
    is($depth, 0, 'wait never holds canonical VG lock');
    $now += $_[1]/1000; $sleeps++;
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::_canonical_vg_lock_id = sub { 'vg-lock' };
local *PVE::Storage::Custom::SharedLvmThinPlugin::cluster_lock_storage = sub {
    die "lock failed\n" if $lock_error;
    cmp_ok($_[3], '>=', 1, 'lock acquire timeout never rounds to disabled alarm');
    $now += $advance_lock;
    local $depth = $depth + 1;
    return $_[4]->();
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub {
    die "quorum lost\n" if $quorum_error && $reads;
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub {
    $checks++;
    die "identity lost\n" if $identity_error && $reads;
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_same_vg_alias_configuration = sub { 1 };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub {
    $reads++;
    die "malformed intent\n" if $read_error;
    $intent = { %$intent, tx => sprintf('%032x', $reads) } if $change_tx;
    return undef if defined($clear_after) && $reads > $clear_after;
    return $intent;
};
local $SIG{__WARN__} = sub { };

reset_state();
is($run->(), 'done', 'clean VG executes');
is($calls, 1, 'callback once'); is($sleeps, 0, 'clean fast path no sleep');
for my $op (qw(DM_CUTOVER DM_PIVOT)) {
    reset_state(); $intent={state=>'OPEN',op=>$op,tx=>'a'x32}; $clear_after=3;
    is($run->(), 'done', "$op clears and waiter succeeds");
    is($calls, 1, 'callback once after intent clears');
    is($checks, 4, 'identity checked on every acquisition');
}
reset_state(); $intent={state=>'OPEN',op=>'DM_CUTOVER',tx=>'a'x32};
eval { $run->() }; like($@, qr/admission timed out/, 'stale intent expires');
is($calls,0,'timeout never mutates'); cmp_ok($now,'<=',10,'absolute budget bounded');
is($intent->{tx},'a'x32,'stale intent untouched');
for my $failure (qw(read identity quorum lock)) {
    reset_state(); $intent={state=>'OPEN',op=>'DM_CUTOVER',tx=>'a'x32};
    $read_error=1 if $failure eq 'read'; $identity_error=1 if $failure eq 'identity';
    $quorum_error=1 if $failure eq 'quorum'; $lock_error=1 if $failure eq 'lock';
    eval { $run->() }; like($@, qr/malformed|lost|lock failed/, "$failure failure propagated");
    is($calls,0,"$failure prevents mutation");
}
reset_state(); $intent={state=>'OPEN',op=>'ALLOC',tx=>'a'x32};
eval { $run->() }; like($@,qr/unresolved transaction/,'non-transition intent fails immediately');
is($sleeps,0,'ALLOC not retried');
reset_state();
eval { $class->_with_mutation_lock('thin',$cfg,sub { $calls++; die "partial mutation\n" }) };
like($@,qr/partial mutation/,'callback error preserved'); is($calls,1,'partial callback never replayed');
reset_state();
is($class->_with_mutation_lock('thin',$cfg,sub { $calls++; return undef }),undef,'undef callback return supported');
is($calls,1,'undef result does not replay');
reset_state(); $advance_lock=11;
eval { $run->() }; like($@,qr/admission timed out/,'budget rechecked after lock acquisition');
is($calls,0,'late acquisition never starts callback');
reset_state(); $intent={state=>'OPEN',op=>'DM_CUTOVER',tx=>'a'x32}; $change_tx=1;
eval { $run->() }; like($@,qr/admission timed out/,'successive transactions cannot reset deadline');
is($calls,0,'transaction churn never bypasses intent');
reset_state();
eval { $class->_with_mutation_lock('thin',{%$cfg,'slt-mutation-admission-timeout'=>0},sub { $calls++ }) };
like($@,qr/invalid mutation admission timeout/,'invalid budget rejected');
is($calls,0,'invalid config never mutates');
done_testing();
