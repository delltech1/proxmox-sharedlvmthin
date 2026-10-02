use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use JSON::PP ();
use Digest::SHA qw(sha256_hex);
use PVE::SharedLvmThinVMDestroyRecoveryDescriptor;

plan skip_all => 'root-only recovery descriptor bridge' if $> != 0;
my $TXID = 'a' x 32;
sub canonical { JSON::PP->new->canonical->ascii->encode($_[0]) }
sub copy { JSON::PP->new->decode(canonical($_[0])) }
sub context {
    return { schema => 'sharedlvmthin-vm-destroy-executor-core/v1', receipt => {
        schema => 1, txid => $TXID, vmid => 123, config => { digest => 'b' x 40 },
        runtime => { status => 'QUALIFIED', boot_id => 'exact-boot' },
        plan => { authority => 'NONE', objects => ['exact-identity'] } } };
}

sub fixture {
    my $state = { calls => [] };
    my $runner = sub {
        my ($argv, $input) = @_;
        push @{$state->{calls}}, [copy($argv), $input];
        die "transport failed\n" if $state->{throw};
        if ($argv->[3] eq '_create') {
            die "exists; never adopt\n" if $state->{descriptor};
            $state->{descriptor} = JSON::PP->new->decode($input);
            die "noncanonical stdin\n" if canonical($state->{descriptor}) . "\n" ne $input;
            die "durable write; lost acknowledgement\n" if $state->{lost_ack};
        } elsif ($argv->[3] eq '_read') {
            die "unexpected read stdin\n" if $input ne '';
            die "missing descriptor\n" if !$state->{descriptor};
        } else {
            die "unexpected operation\n";
        }
        my $descriptor = copy($state->{descriptor});
        my $response = { schema => 'sharedlvmthin-vm-destroy-recovery-result/v1',
            authority => 'NONE', txid => $TXID, durable => JSON::PP::true,
            descriptor => $descriptor, descriptor_sha256 => sha256_hex(canonical($descriptor)) };
        $state->{tamper}->($response) if $state->{tamper};
        return $state->{raw} // (canonical($response) . "\n");
    };
    return (PVE::SharedLvmThinVMDestroyRecoveryDescriptor->new(txid => $TXID, runner => $runner), $state, $runner);
}

subtest 'fixed root-only helper, exact immutable core and durable read' => sub {
    my ($bridge, $state, $runner) = fixture();
    my $created = $bridge->create(context());
    is_deeply($state->{calls}->[0]->[0], ['/usr/bin/python3', '-I',
        '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-recovery', '_create', '--txid', $TXID],
        'fixed argv no shell or configurable path');
    is_deeply($created->{descriptor}->{context}, context(), 'lossless full executor context');
    is($created->{descriptor}->{context_sha256}, sha256_hex(canonical(context())), 'exact context hash');
    is($created->{authority}, 'NONE', 'no mutation authority');
    ok($created->{durable}, 'durability required');
    my $new_process = PVE::SharedLvmThinVMDestroyRecoveryDescriptor->new(txid => $TXID, runner => $runner);
    is_deeply($new_process->read(), $created, 'new process recovers descriptor without caller-supplied context/head');
    is_deeply($state->{calls}->[-1]->[0], ['/usr/bin/python3', '-I',
        '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-recovery', '_read', '--txid', $TXID], 'fixed read argv');
    is($state->{calls}->[-1]->[1], '', 'read has no context supplied via stdin');
    ok(!eval { $bridge->create(context()); 1 }, 'same instance cannot repeat successful create');
    is(scalar(@{$state->{calls}}), 2, 'no duplicate create invocation');
};

subtest 'invalid constructor and incomplete or swapped context have zero IPC' => sub {
    for my $args ({txid => '../bad'}, {txid => ''}, {txid => undef}, {txid => 'A' x 32},
                  {txid => $TXID . "\n"}, {txid => []}, {txid => $TXID, base => '/tmp'},
                  {txid => $TXID, runner => 'shell'}) {
        ok(!eval { PVE::SharedLvmThinVMDestroyRecoveryDescriptor->new(%$args); 1 },
            'constructor refused: ' . canonical($args));
    }
    my @cases = (['undefined', undef], ['empty', {}],
        ['wrong envelope schema', {schema => 'other', receipt => context()->{receipt}}]);
    for my $key (qw(schema txid vmid config runtime plan)) {
        my $bad = context();
        delete $bad->{receipt}->{$key};
        push @cases, ["missing $key", $bad];
    }
    for my $change ({txid => 'b' x 32}, {vmid => JSON::PP::true}, {vmid => '123'},
                    {vmid => 0}, {vmid => -1}, {vmid => 1000000000}, {vmid => 123.5},
                    {schema => '1'}, {evidence => {}}, {config => {}}, {plan => []}) {
        my $bad = context();
        @{$bad->{receipt}}{keys %$change} = values %$change;
        push @cases, ['invalid field ' . canonical($change), $bad];
    }
    for my $case (@cases) {
        my ($label, $bad) = @$case;
        # Fresh bridge for every negative: a previous attempted-create latch
        # must not hide another invalid value accidentally reaching the helper.
        my ($bridge, $state) = fixture();
        ok(!eval { $bridge->create($bad); 1 }, "$label refused");
        is(scalar(@{$state->{calls}}), 0, "$label has zero helper calls");
    }
};

subtest 'lost acknowledgement never retries or adopts; explicit read remains NONE' => sub {
    my ($bridge, $state) = fixture();
    $state->{lost_ack} = 1;
    ok(!eval { $bridge->create(context()); 1 }, 'uncertain create refused');
    ok($state->{descriptor}, 'mock immutable file was written');
    ok(!eval { $bridge->create(context()); 1 }, 'same-process retry blocked');
    is(scalar(@{$state->{calls}}), 1, 'one create attempt only');
    is($bridge->read()->{authority}, 'NONE', 'explicit verified observation is not mutation authorization');
    ok(!eval { $bridge->create(context()); 1 }, 'read never resets create latch');
};

subtest 'fresh-process duplicate create is not interpreted as success' => sub {
    my ($bridge, $state, $runner) = fixture();
    $bridge->create(context());
    my $another = PVE::SharedLvmThinVMDestroyRecoveryDescriptor->new(txid => $TXID, runner => $runner);
    ok(!eval { $another->create(context()); 1 }, 'helper EEXIST must propagate');
    ok(!eval { $another->create(context()); 1 }, 'not retried');
    is(scalar(@{$state->{calls}}), 2, 'only two distinct attempts');
};

subtest 'tampered acknowledgement identity, digest, authority or durability refused' => sub {
    for my $tamper (
        sub { $_[0]->{authority} = 'MUTATE' },
        sub { $_[0]->{txid} = 'b' x 32 },
        sub { $_[0]->{durable} = JSON::PP::false },
        sub { $_[0]->{durable} = 1 },
        sub { $_[0]->{descriptor_sha256} = '0' x 64 },
        sub { $_[0]->{descriptor}->{context_sha256} = '0' x 64 },
        sub { $_[0]->{extra} = 1 },
        sub {
            my ($value) = @_;
            $value->{descriptor}->{context}->{receipt}->{vmid} = 999;
            $value->{descriptor}->{context_sha256} = sha256_hex(canonical($value->{descriptor}->{context}));
            $value->{descriptor_sha256} = sha256_hex(canonical($value->{descriptor}));
        },
    ) {
        my ($bridge, $state) = fixture();
        $state->{tamper} = $tamper;
        ok(!eval { $bridge->create(context()); 1 }, 'tampered create acknowledgement refused');
        ok(!eval { $bridge->create(context()); 1 }, 'no automatic replay after tamper');
        is(scalar(@{$state->{calls}}), 1, 'one invocation');
    }
};

subtest 'durable read rejects invalid stored schema/core/hash and noncanonical response' => sub {
    for my $tamper (
        sub { $_[0]->{descriptor}->{authority} = 'MUTATE' },
        sub { $_[0]->{descriptor}->{context}->{receipt}->{txid} = 'b' x 32 },
        sub { delete $_[0]->{descriptor}->{context}->{receipt}->{runtime} },
        sub { $_[0]->{descriptor}->{context}->{receipt}->{extra} = 'unknown' },
        sub { $_[0]->{descriptor}->{context_sha256} = 'f' x 64 },
    ) {
        my ($bridge, $state) = fixture();
        $bridge->create(context());
        $state->{tamper} = $tamper;
        ok(!eval { $bridge->read(); 1 }, 'invalid durable read refused');
    }
    for my $bytes ('{}', "{\"authority\":\"NONE\",\"authority\":\"NONE\"}\n", 'x' x (1024 * 1024 + 1025)) {
        my ($bridge, $state) = fixture();
        $bridge->create(context());
        $state->{raw} = $bytes;
        ok(!eval { $bridge->read(); 1 }, 'noncanonical/oversized response refused');
    }
};

subtest 'oversized context refuses before IPC; returned data cannot mutate stored core' => sub {
    my ($bridge, $state) = fixture();
    my $large = context();
    $large->{receipt}->{plan}->{padding} = 'x' x (1024 * 1024);
    ok(!eval { $bridge->create($large); 1 }, 'oversized context refused');
    is(scalar(@{$state->{calls}}), 0, 'no publication');
    my $result = $bridge->create(context());
    $result->{descriptor}->{context}->{receipt}->{config}->{digest} = 'tampered caller copy';
    is_deeply($bridge->read()->{descriptor}->{context}, context(), 'readback returns original context');
};

done_testing();
