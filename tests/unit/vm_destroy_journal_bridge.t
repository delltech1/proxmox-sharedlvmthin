use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use JSON::PP ();
use Digest::SHA qw(sha256_hex);
use PVE::SharedLvmThinVMDestroyJournal;

plan skip_all => 'root-only bridge contract' if $> != 0;
my $ID = 'a' x 32;
sub canonical { JSON::PP->new->canonical->ascii->encode($_[0]) }
sub copy { JSON::PP->new->decode(canonical($_[0])) }
sub receipt {
    return { schema => 1, txid => $ID, vmid => 123,
        config => { raw_sha256 => 'b' x 64 }, runtime => { boot_id => 'exact-boot' },
        plan => { authority => 'NONE', objects => ['exact-volume'] },
        evidence => { at => 'preflight' } };
}

sub fixture {
    my $state = { rows => [], calls => [] };
    my $runner = sub {
        my ($argv, $input) = @_;
        push @{$state->{calls}}, [copy($argv), $input];
        die "transport lost after invocation\n" if $state->{throw};
        if ($argv->[3] eq '_observe-durable') {
            die "unexpected observe input\n" if $input ne '';
            my $last = $state->{rows}->[-1];
            my $value = { schema => 'sharedlvmthin-vm-destroy-journal/v1', authority => 'NONE',
                durable => JSON::PP::true, records => $state->{rows},
                stage => $last ? $last->{stage} : 'EMPTY',
                last_sha256 => $last ? sha256_hex(canonical($last)) : undef };
            $state->{tamper_observe}->($value) if $state->{tamper_observe};
            return canonical($value) . "\n";
        }
        die "unexpected operation\n" if $argv->[3] ne '_append';
        my $request = JSON::PP->new->decode($input);
        die "noncanonical input\n" if $input ne canonical($request) . "\n";
        my $last = $state->{rows}->[-1];
        die "CAS mismatch\n" if canonical($request->{expected_previous}) ne
            canonical($last ? sha256_hex(canonical($last)) : undef);
        my $row = { schema => 'sharedlvmthin-vm-destroy-journal/v1', authority => 'NONE',
            sequence => scalar(@{$state->{rows}}) + 1, request_id => $request->{request_id},
            stage => $request->{stage}, previous_sha256 => $request->{expected_previous},
            context => $request->{context}, context_sha256 => sha256_hex(canonical($request->{context})),
            evidence => $request->{evidence} };
        push @{$state->{rows}}, $row;
        die "durable publication lost acknowledgement\n" if $state->{lost_ack};
        my $ack = { schema => 'sharedlvmthin-vm-destroy-append-result/v1', authority => 'NONE',
            request_id => $request->{request_id}, stage => $request->{stage},
            previous_sha256 => $request->{expected_previous}, last_sha256 => sha256_hex(canonical($row)),
            request_sha256 => sha256_hex(canonical($request)) };
        $state->{tamper_ack}->($ack) if $state->{tamper_ack};
        return $state->{raw_ack} // (canonical($ack) . "\n");
    };
    return (PVE::SharedLvmThinVMDestroyJournal->new(request_id => $ID, runner => $runner), $state, $runner);
}

sub prepare_finalizing {
    my ($bridge) = @_;
    for my $stage (qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING)) {
        my $receipt = receipt();
        $receipt->{evidence} = { at => $stage };
        $bridge->append($stage, $receipt);
    }
}

subtest 'fixed private argv, immutable core and per-stage evidence' => sub {
    my ($bridge, $state) = fixture();
    my $journal = $bridge->callback();
    is($journal->('PREPARED', receipt()), 1, 'callback success exact 1');
    is_deeply($state->{calls}->[0]->[0], ['/usr/bin/python3', '-I',
        '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy', '_append', '--request-id', $ID],
        'fixed executable/operation, no shell or configurable path');
    my $r = receipt();
    $r->{evidence}->{at} = 'native-dispatch';
    is($journal->('DISPATCHED', $r), 1, 'second append with current CAS');
    is_deeply($state->{rows}->[0]->{context}, $state->{rows}->[1]->{context}, 'core unchanged');
    isnt(canonical($state->{rows}->[0]->{evidence}), canonical($state->{rows}->[1]->{evidence}), 'actual stage evidence retained');
    is($state->{rows}->[1]->{previous_sha256}, sha256_hex(canonical($state->{rows}->[0])), 'exact prior hash');
    is($state->{rows}->[0]->{authority}, 'NONE', 'never grants storage authority');
};

subtest 'invalid receipt, skip, replay and changed immutable core never dispatch' => sub {
    my ($bridge, $state) = fixture();
    $bridge->append('PREPARED', receipt());
    for my $case (
        ['PREPARED', receipt()], ['COMPLETE', receipt()],
        ['DISPATCHED', { %{receipt()}, vmid => 999 }],
        ['DISPATCHED', { %{receipt()}, authority => 'MUTATE' }],
        ['DISPATCHED', { %{receipt()}, evidence => [] }],
        ['DISPATCHED', { %{receipt()}, evidence => { large => 'x' x (1024 * 1024) } }],
    ) {
        my $ok = eval { $bridge->append(@$case); 1 };
        ok(!$ok, 'invalid input refused');
    }
    is(scalar(@{$state->{calls}}), 1, 'zero subsequent helper invocations');
};

subtest 'durable lost acknowledgement poisons instance, no retry including UNKNOWN' => sub {
    my ($bridge, $state) = fixture();
    $state->{lost_ack} = 1;
    ok(!eval { $bridge->append('PREPARED', receipt()); 1 }, 'lost ack refused');
    is(scalar(@{$state->{rows}}), 1, 'mock durable record exists');
    for my $stage ('PREPARED', 'PARTIAL_OR_UNKNOWN') {
        ok(!eval { $bridge->append($stage, receipt()); 1 }, 'uncertainty cannot redispatch');
    }
    is(scalar(@{$state->{calls}}), 1, 'only one invocation');
};

subtest 'ack schema, authority, hash and noncanonical bytes fail closed' => sub {
    for my $change ({authority => 'MUTATE'}, {last_sha256 => '0' x 64},
                    {request_sha256 => '1' x 64}, {unexpected => 1}) {
        my ($bridge, $state) = fixture();
        $state->{tamper_ack} = sub { my ($ack) = @_; @$ack{keys %$change} = values %$change; };
        ok(!eval { $bridge->append('PREPARED', receipt()); 1 }, 'tampered ack refused');
        ok(!eval { $bridge->append('PREPARED', receipt()); 1 }, 'not retried');
        is(scalar(@{$state->{calls}}), 1, 'only one invocation');
    }
    my ($bridge, $state) = fixture();
    $state->{raw_ack} = "{\"authority\":\"NONE\",\"authority\":\"NONE\"}\n";
    ok(!eval { $bridge->append('PREPARED', receipt()); 1 }, 'duplicate-key ack not canonical');
};

subtest 'new process hydrates exact durable FINALIZING chain and maps executor receipts' => sub {
    my ($bridge, $state, $runner) = fixture();
    prepare_finalizing($bridge);
    my $context = copy($state->{rows}->[0]->{context});
    my $reopened = PVE::SharedLvmThinVMDestroyJournal->open_existing(
        request_id => $ID, expected_context => $context, runner => $runner);
    my $reader = $reopened->reader();
    my $history = $reader->($ID);
    is($history->{durable}, 1, 'durability explicitly established by private helper');
    is(scalar(@{$history->{records}}), 4, 'entire validated chain returned');
    is($history->{last_sha256}, sha256_hex(canonical($state->{rows}->[-1])), 'verified exact head exposed to completion CAS');
    is($history->{context_sha256}, sha256_hex(canonical($context)), 'verified context digest exposed');
    is($history->{request_id}, $ID, 'verified transaction identity exposed');
    is($history->{stage}, 'FINALIZING', 'verified stage exposed');
    is($history->{records}->[-1]->{receipt}->{evidence}->{at}, 'FINALIZING', 'last stage evidence mapped');
    is_deeply($history->{records}->[0]->{receipt}->{plan}, receipt()->{plan}, 'immutable planner core mapped');
    my $prior = sha256_hex(canonical($state->{rows}->[-1]));
    my $r = receipt();
    $r->{evidence} = { at => 'acl-absent' };
    $r->{current_effect} = 'delete-firewall';
    $r->{resume} = 1;
    $r->{recovery_required} = 0;
    is($reopened->append('FINALIZING', $r), 1, 'legal evidence checkpoint after reopening');
    is($state->{rows}->[-1]->{previous_sha256}, $prior, 'observed head used, not caller supplied');
    is($state->{rows}->[-1]->{sequence}, 5, 'sequence restored');
    is($reopened->append('COMPLETE', $r), 1, 'final terminal receipt');
    ok(!eval { $reopened->append('FINALIZING', $r); 1 }, 'terminal cannot reopen locally');
};

subtest 'recovery refuses context drift, malformed chain, wrong head and terminal states' => sub {
    for my $tamper (
        sub { $_[0]->{durable} = JSON::PP::false },
        sub { $_[0]->{last_sha256} = '0' x 64 },
        sub { $_[0]->{records}->[1]->{previous_sha256} = '0' x 64 },
        sub { $_[0]->{records}->[1]->{request_id} = 'b' x 32 },
        sub { $_[0]->{records}->[1]->{sequence} = 7 },
        sub { $_[0]->{records}->[1]->{context}->{receipt}->{vmid} = 999 },
        sub { $_[0]->{records}->[1]->{evidence}->{authority} = 'MUTATE' },
    ) {
        my ($bridge, $state, $runner) = fixture();
        prepare_finalizing($bridge);
        my $context = copy($state->{rows}->[0]->{context});
        $state->{tamper_observe} = $tamper;
        ok(!eval { PVE::SharedLvmThinVMDestroyJournal->open_existing(
            request_id => $ID, expected_context => $context, runner => $runner); 1 }, 'tampered durable observation refused');
        is(scalar(@{$state->{rows}}), 4, 'no record publication');
    }
    for my $terminal (qw(COMPLETE PARTIAL_OR_UNKNOWN)) {
        my ($bridge, $state, $runner) = fixture();
        prepare_finalizing($bridge);
        $bridge->append($terminal, receipt());
        ok(!eval { PVE::SharedLvmThinVMDestroyJournal->open_existing(
            request_id => $ID, expected_context => $state->{rows}->[0]->{context}, runner => $runner); 1 },
            "$terminal cannot authorize recovery");
    }
};

subtest 'explicit completion CAS cannot silently adopt another observed head' => sub {
    my ($bridge, $state) = fixture();
    prepare_finalizing($bridge);
    my $head = sha256_hex(canonical($state->{rows}->[-1]));
    my $cas = $bridge->cas_callback();
    my $before = scalar(@{$state->{calls}});
    for my $invalid (undef, '../head', 'f' x 64) {
        ok(!eval { $cas->('COMPLETE', receipt(), $invalid); 1 }, 'invalid/stale explicit CAS refused');
    }
    is(scalar(@{$state->{calls}}), $before, 'invalid CAS never invokes helper');
    is($cas->('COMPLETE', receipt(), $head), 1, 'exact observed FINALIZING head accepted');
    is($state->{rows}->[-1]->{previous_sha256}, $head, 'helper request retains explicit CAS');
    is($state->{rows}->[-1]->{stage}, 'COMPLETE', 'only requested terminal stage published');
};

done_testing();
