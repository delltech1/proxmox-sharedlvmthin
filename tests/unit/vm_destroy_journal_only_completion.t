use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use JSON::PP ();
use Digest::SHA qw(sha256_hex);

BEGIN { $INC{"PVE/$_.pm"} = __FILE__ for qw(Cluster QemuConfig) }
our ($locked, @effects);
sub forbidden { push @effects, $_[0]; die "forbidden effect $_[0]\n" }
{
    package PVE::Cluster;
    sub cfs_update { }
    package PVE::QemuConfig;
    sub lock_config {
        my ($class, $vmid, $code) = @_;
        die "nested or wrong VM lock\n" if $main::locked || $vmid != 101;
        local $main::locked = 1;
        return $code->();
    }
    sub destroy_config { main::forbidden('destroy_config') }
    sub load_config { main::forbidden('direct config load') }
    package PVE::Storage;
    sub config { main::forbidden('storage config') }
    sub vdisk_free { main::forbidden('storage free') }
    package PVE::QemuServer;
    sub destroy_vm { main::forbidden('native destroy') }
    package PVE::AccessControl;
    sub remove_vm_access { main::forbidden('ACL removal') }
    package PVE::Firewall;
    sub remove_vmfw_conf { main::forbidden('firewall removal') }
}
use PVE::SharedLvmThinVMDestroy;
plan skip_all => 'root-only completion fixture' if $> != 0 || $< != 0;

my $TXID = 'c' x 32;
sub canonical { JSON::PP->new->canonical->ascii->encode($_[0]) }
sub copy { JSON::PP->new->decode(canonical($_[0])) }

sub fixture {
    @effects = ();
    $locked = 0;
    my $core = { schema => 1, txid => $TXID, vmid => 101, config => {digest => 'a' x 40},
        plan => {authority => 'NONE', schema => 'fixture'},
        runtime => { schema => 'sharedlvmthin-vm-destroy-runtime/v1', status => 'QUALIFIED',
            authority => 'NONE', vmid => '101', node => 'node-1', boot_id => 'exact-boot',
            contract_sha256 => 'b' x 64 } };
    my $context = {schema => 'sharedlvmthin-vm-destroy-executor-core/v1', receipt => $core};
    my $state = { reads => 0, probes => 0, appends => 0, writes => 0,
        context => $context, history => { durable => 1, request_id => $TXID,
            stage => 'FINALIZING', last_sha256 => 'd' x 64,
            context_sha256 => sha256_hex(canonical($context)),
            records => [map { +{ stage => $_, receipt => { %$core, evidence => {at => $_} } } }
                qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING)] } };
    my %deps = (
        map { my $name = $_; ($name => sub { forbidden($name) }) }
            qw(verify_runtime prepare_plan observe_plan observe_finalization journal)
    );
    my $executor = PVE::SharedLvmThinVMDestroy->new(%deps,
        read_journal => sub {
            die "unlocked/wrong journal read\n" if !$locked || $_[0] ne $TXID;
            $state->{reads}++;
            $state->{on_read}->($state, $state->{reads}) if $state->{on_read};
            return copy($state->{history});
        },
        verify_finalizing_absent => sub {
            my ($vmid, %args) = @_;
            die "unlocked/wrong absent proof\n" if !$locked || $vmid != 101 || $args{txid} ne $TXID;
            die "unexpected recovery context\n" if canonical($args{expected_context}) ne canonical($state->{context});
            $state->{probes}++;
            # The real Runtime callback additionally reads/brackets journal,
            # all-ABSENT state and current tuple; this fixture tests consumption.
            my $proof = { %{$core->{runtime}}, status => 'OBSERVED',
                purpose => 'FINALIZATION_OBSERVATION_ONLY', recovery => {
                    txid => $TXID, context_sha256 => $state->{history}->{context_sha256},
                    journal_head_sha256 => $state->{history}->{last_sha256} } };
            $state->{on_probe}->($state, $proof, $state->{probes}) if $state->{on_probe};
            return copy($proof);
        },
        journal_cas => sub {
            my ($stage, $receipt, $expected) = @_;
            die "unlocked or non-COMPLETE journal append\n" if !$locked || $stage ne 'COMPLETE';
            $state->{appends}++;
            $state->{before_cas}->($state) if $state->{before_cas};
            die "CAS mismatch\n" if $expected ne $state->{history}->{last_sha256};
            die "crash before write\n" if $state->{crash_before};
            $state->{writes}++;
            $state->{written_receipt} = copy($receipt);
            $state->{history}->{stage} = 'COMPLETE';
            push @{$state->{history}->{records}}, {stage => 'COMPLETE', receipt => copy($receipt)};
            $state->{history}->{last_sha256} = sha256_hex(canonical($receipt));
            die "crash after durable COMPLETE; lost acknowledgement\n" if $state->{crash_after};
            return $state->{bad_ack} ? 0 : 1;
        },
    );
    return ($executor, $state);
}

sub complete { $_[0]->complete_finalizing_absent(101, $TXID) }

subtest 'only exact bracketed FINALIZING head appends COMPLETE' => sub {
    my ($executor, $state) = fixture();
    my $result = complete($executor);
    is($result->{status}, 'COMPLETE', 'completion acknowledged');
    is($result->{journal_only}, 1, 'explicit journal-only API/result');
    is($result->{previous_head_sha256}, 'd' x 64, 'exact original FINALIZING head');
    is($state->{reads}, 2, 'journal bracket under one VM lock');
    is($state->{probes}, 2, 'absent proof bracket repeated');
    is($state->{appends}, 1, 'one explicit CAS append');
    is($state->{writes}, 1, 'one record publication');
    is($state->{written_receipt}->{evidence}->{journal_only_completion}->{status}, 'OBSERVED',
        'actual narrow observation persisted, never promoted');
    is_deeply(\@effects, [], 'zero storage/native/ACL/firewall/config effects');
};

for my $case (
    ['UNKNOWN terminal', sub { $_[0]->{history}->{stage} = 'PARTIAL_OR_UNKNOWN' }],
    ['COMPLETE terminal', sub { $_[0]->{history}->{stage} = 'COMPLETE' }],
    ['wrong txid', sub { $_[0]->{history}->{request_id} = 'a' x 32 }],
    ['nondurable history', sub { $_[0]->{history}->{durable} = 0 }],
    ['tampered context', sub { $_[0]->{history}->{context_sha256} = '0' x 64 }],
    ['swapped record core', sub { $_[0]->{history}->{records}->[1]->{receipt}->{vmid} = 999 }],
    ['missing STORAGE_ABSENT', sub { splice @{$_[0]->{history}->{records}}, 2, 1 }],
) {
    subtest "$case->[0] never reaches completion callback" => sub {
        my ($executor, $state) = fixture();
        $case->[1]->($state);
        ok(!eval { complete($executor); 1 }, 'refused');
        is($state->{appends}, 0, 'no journal append');
        is_deeply(\@effects, [], 'zero mutation effects');
    };
}

for my $case (
    ['QUALIFIED status', sub { $_[1]->{status} = 'QUALIFIED' }],
    ['wrong purpose', sub { $_[1]->{purpose} = 'EXECUTE' }],
    ['authority promotion', sub { $_[1]->{authority} = 'MUTATE' }],
    ['stale observation head', sub { $_[1]->{recovery}->{journal_head_sha256} = '0' x 64 }],
    ['stale observation context', sub { $_[1]->{recovery}->{context_sha256} = '0' x 64 }],
    ['other boot', sub { $_[1]->{boot_id} = 'new-boot' }],
    ['other node', sub { $_[1]->{node} = 'node-2' }],
    ['VMID reused', sub { die "VMID was reused\n" }],
    ['UNKNOWN helper result', sub { die "finalization unavailable\n" }],
) {
    subtest "$case->[0] cannot finish" => sub {
        my ($executor, $state) = fixture();
        $state->{on_probe} = $case->[1];
        ok(!eval { complete($executor); 1 }, 'refused');
        is($state->{appends}, 0, 'no completion publication');
        is_deeply(\@effects, [], 'zero mutation effects');
    };
}

subtest 'new VMID during bracket and concurrent journal writer block completion' => sub {
    my ($executor, $state) = fixture();
    $state->{on_probe} = sub { die "VMID reused during bracket\n" if $_[2] == 2 };
    ok(!eval { complete($executor); 1 }, 'second probe catches reuse');
    is($state->{appends}, 0, 'no append');
    ($executor, $state) = fixture();
    $state->{on_read} = sub { $_[0]->{history}->{last_sha256} = 'e' x 64 if $_[1] == 2 };
    ok(!eval { complete($executor); 1 }, 'read bracket catches changed head');
    is($state->{appends}, 0, 'changed head never adopted');
    ($executor, $state) = fixture();
    $state->{before_cas} = sub { $_[0]->{history}->{last_sha256} = 'e' x 64 };
    ok(!eval { complete($executor); 1 }, 'race after last observation loses exact CAS');
    is($state->{appends}, 1, 'one CAS attempt only');
    is($state->{writes}, 0, 'no publication on stale head');
    is_deeply(\@effects, [], 'no compensating storage/finalization effects');
};

for my $fault (qw(crash_before crash_after bad_ack)) {
    subtest "$fault never retries or journals a fallback state" => sub {
        my ($executor, $state) = fixture();
        $state->{$fault} = 1;
        ok(!eval { complete($executor); 1 }, 'uncertain completion not reported successful');
        is($state->{appends}, 1, 'exactly one append invocation');
        is($state->{writes}, $fault eq 'crash_before' ? 0 : 1, 'expected crash prefix');
        is_deeply(\@effects, [], 'zero mutation effects');
        if ($fault ne 'crash_before') {
            ok(!eval { complete($executor); 1 }, 'durable terminal cannot be replayed');
            is($state->{appends}, 1, 'no second COMPLETE or UNKNOWN append');
        }
    };
}

done_testing();
