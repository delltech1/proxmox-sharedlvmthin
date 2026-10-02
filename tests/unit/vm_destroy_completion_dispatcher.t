use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use JSON::PP ();
use Digest::SHA qw(sha256_hex);
BEGIN { $INC{"PVE/$_.pm"} = __FILE__ for qw(Cluster QemuConfig) }
our ($F, @effects);
sub forbidden { push @effects, $_[0]; die "forbidden $_[0]\n" }
{
    package PVE::Cluster; sub cfs_update { }
    package PVE::QemuConfig;
    sub lock_config { my ($class,$vmid,$code)=@_; die 'nested/wrong lock' if $main::F->{locked} || $vmid != 101;
        local $main::F->{locked}=1; $main::F->{locks}++; return $code->() }
    sub destroy_config { main::forbidden('destroy_config') }
    sub load_config { main::forbidden('load_config') }
    package PVE::QemuServer; sub destroy_vm { main::forbidden('native destroy') }
    package PVE::Storage; sub config { main::forbidden('storage') }
    package PVE::AccessControl; sub remove_vm_access { main::forbidden('ACL') }
    package PVE::Firewall; sub remove_vmfw_conf { main::forbidden('firewall') }
}
use PVE::SharedLvmThinVMDestroyDispatcher;
use PVE::SharedLvmThinVMDestroyJournal;
use PVE::SharedLvmThinVMDestroyRuntime;
use PVE::SharedLvmThinVMDestroy;
plan skip_all => 'root-only dispatcher integration fixture' if $> != 0 || $< != 0;
my $TXID = 'a' x 32;
sub json { JSON::PP->new->canonical->ascii->encode($_[0]) }
sub copy { JSON::PP->new->decode(json($_[0])) }
sub hash { sha256_hex(json($_[0])) }

sub observation {
    my $last=$F->{rows}->[-1];
    return {schema=>'sharedlvmthin-vm-destroy-journal/v1',authority=>'NONE',durable=>JSON::PP::true,
        stage=>$last->{stage},last_sha256=>hash($last),records=>copy($F->{rows})};
}
{
    package CompletionDescriptor;
    sub create { main::forbidden('descriptor create') }
    sub read { $main::F->{descriptor_reads}++; return {authority=>'NONE',txid=>$main::F->{txid},durable=>JSON::PP::true,
        descriptor=>{context=>main::copy($main::F->{context})}} }
    package CompletionFinalization;
    sub observe {
        die 'finalization outside VM lock' if !$main::F->{locked};
        $main::F->{finalization_reads}++;
        return {vmid=>'101',config=>$main::F->{vm_reused}?'PRESENT':'ABSENT',
            acl=>$main::F->{acl_present}?'PRESENT':'ABSENT',firewall=>'ABSENT'};
    }
    package CompletionRuntime;
    sub verify { main::forbidden('ordinary runtime qualification') }
    sub verify_finalizing_absent {
        my ($self,$vmid,%args)=@_;
        die 'runtime outside VM lock' if !$main::F->{locked};
        $main::F->{runtime_reads}++;
        die 'owner reused' if $main::F->{owner_reused};
        my $ctx=$args{expected_context};
        my $fresh=$self->{fresh_process_reader}->();
        die 'fresh identity mismatch' if main::json($fresh) ne main::json($ctx->{receipt}->{runtime}->{fresh_process});
        my $journal=$self->{journal_reader}->($args{txid});
        my $head=PVE::SharedLvmThinVMDestroyRuntime::_finalizing_head($journal,$args{txid},$ctx);
        PVE::SharedLvmThinVMDestroyRuntime::_all_finalization_absent($self->{finalization_reader}->($vmid),$vmid);
        die 'stale boot' if $main::F->{boot_drift};
        $main::F->{source_changed}=1 if $main::F->{source_during_probe};
        my $proof={%{main::copy($ctx->{receipt}->{runtime})},status=>'OBSERVED',purpose=>'FINALIZATION_OBSERVATION_ONLY',
            recovery=>{txid=>$args{txid},context_sha256=>main::hash($ctx),journal_head_sha256=>$head}};
        $proof->{status}='QUALIFIED' if $main::F->{promote};
        $proof->{recovery}->{journal_head_sha256}='0' x 64 if $main::F->{stale_proof};
        return $proof;
    }
}

sub fixture {
    @effects=();
    my $fresh={schema=>'sharedlvmthin-fresh-pve-load/v1',source_sha256=>'b' x 64,module_count=>50,
        launcher_sha256=>'c' x 64,dispatcher_sha256=>'d' x 64};
    my $context={schema=>'sharedlvmthin-vm-destroy-executor-core/v1',receipt=>{
        schema=>1,txid=>$TXID,vmid=>101,config=>{digest=>'e' x 40},plan=>{authority=>'NONE',schema=>'v3'},
        runtime=>{schema=>'sharedlvmthin-vm-destroy-runtime/v1',status=>'QUALIFIED',authority=>'NONE',vmid=>'101',
            node=>'node-1',boot_id=>'exact-boot',contract_sha256=>'f' x 64,fresh_process=>$fresh}}};
    $F={txid=>$TXID,fresh=>copy($fresh),context=>$context,rows=>[],appends=>0,locks=>0,helper_calls=>[],runtime_reads=>0};
    my $previous;
    for my $stage (qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING)) {
        my $row={schema=>'sharedlvmthin-vm-destroy-journal/v1',authority=>'NONE',sequence=>scalar(@{$F->{rows}})+1,
            request_id=>$TXID,stage=>$stage,previous_sha256=>$previous,context=>copy($context),context_sha256=>hash($context),
            evidence=>{evidence=>{stage=>$stage}}};
        push @{$F->{rows}},$row; $previous=hash($row);
    }
    return $F;
}

sub helper {
    my ($argv,$input)=@_;
    push @{$F->{helper_calls}},copy($argv);
    die 'not fixed isolated Python' if $argv->[0] ne '/usr/bin/python3' || $argv->[1] ne '-I';
    my $op=$argv->[3];
    return json(observation())."\n" if $op eq '_observe-durable';
    die 'unexpected helper operation' if $op ne '_append';
    die 'append outside VM lock' if !$F->{locked};
    $F->{appends}++;
    my $req=JSON::PP->new->decode($input);
    die 'non-COMPLETE stage forbidden' if $req->{stage} ne 'COMPLETE';
    if ($F->{concurrent_cas}) {
        my $next=copy($F->{rows}->[-1]);
        $next->{sequence}++; $next->{previous_sha256}=hash($F->{rows}->[-1]);
        $next->{evidence}={evidence=>{concurrent=>1}}; push @{$F->{rows}},$next;
    }
    die 'CAS mismatch' if $req->{expected_previous} ne hash($F->{rows}->[-1]);
    my $row={schema=>'sharedlvmthin-vm-destroy-journal/v1',authority=>'NONE',sequence=>scalar(@{$F->{rows}})+1,
        request_id=>$TXID,stage=>'COMPLETE',previous_sha256=>$req->{expected_previous},context=>$req->{context},
        context_sha256=>hash($req->{context}),evidence=>$req->{evidence}};
    push @{$F->{rows}},$row;
    die 'durable COMPLETE lost ACK' if $F->{lost_ack};
    return json({schema=>'sharedlvmthin-vm-destroy-append-result/v1',authority=>'NONE',request_id=>$TXID,stage=>'COMPLETE',
        previous_sha256=>$req->{expected_previous},request_sha256=>hash($req),last_sha256=>hash($row)})."\n";
}

sub dispatcher {
    return PVE::SharedLvmThinVMDestroyDispatcher->new(
        bootstrap=>sub { $F->{bootstraps}++; return {identity=>copy($F->{fresh}),check=>sub {
            die 'loaded source changed' if $F->{source_changed}; return 1}} },
        descriptor_factory=>sub {bless {},'CompletionDescriptor'},
        journal_recovery_factory=>sub {PVE::SharedLvmThinVMDestroyJournal->open_existing(@_,runner=>\&helper)},
        durable_journal_observer=>sub {die 'raw journal outside lock' if !$F->{locked}; return observation()},
        finalization_factory=>sub {bless {},'CompletionFinalization'},
        runtime_factory=>sub {my %deps=@_; die 'missing trusted readers' if
            join(',',sort keys %deps) ne 'finalization_reader,fresh_process_reader,journal_reader';
            return bless \%deps,'CompletionRuntime'},
        executor_factory=>sub {PVE::SharedLvmThinVMDestroy->new(@_)},
        collector_factory=>sub {forbidden('collector factory')},raw_reader=>sub {forbidden('raw config reader')},
        journal_factory=>sub {forbidden('fresh journal factory')},
    );
}

subtest 'real Executor and Journal bridge compose journal-only completion' => sub {
    fixture(); my $d=dispatcher(); my $result=$d->complete_finalizing_absent(101,$TXID);
    is($result->{status},'COMPLETE','acknowledged completion');
    is($result->{journal_only},1,'no effectful resume');
    is($F->{locks},1,'one canonical VM lock');
    is($F->{runtime_reads},2,'absent-only verifier called twice under lock');
    is($F->{appends},1,'one private COMPLETE CAS');
    is($F->{rows}->[-1]->{stage},'COMPLETE','durable terminal record');
    is_deeply(\@effects,[],'zero storage/native/ACL/fw/config/execute effects');
    my $calls=scalar(@{$F->{helper_calls}});
    ok(!eval {$d->complete_finalizing_absent(101,$TXID);1},'same-process repeat refused');
    ok(!eval {$d->execute(101,$TXID);1},'execute fallback on used dispatcher refused');
    is(scalar(@{$F->{helper_calls}}),$calls,'no repeated helper dispatch');
};

for my $case (
    ['source before probes','source_changed'], ['source during absent probe','source_during_probe'],
    ['VMID reused','vm_reused'], ['owner reused','owner_reused'], ['remaining ACL','acl_present'],
    ['stale boot','boot_drift'], ['stale proof head','stale_proof'], ['purpose promotion','promote'],
) {
    subtest "$case->[0] never publishes COMPLETE or invokes effects" => sub {
        fixture(); $F->{$case->[1]}=1; my $d=dispatcher();
        ok(!eval {$d->complete_finalizing_absent(101,$TXID);1},'refused');
        is($F->{appends},0,'no journal append');
        is_deeply(\@effects,[],'zero effect callbacks');
    };
}

subtest 'exact launcher/dispatcher identity in immutable descriptor cannot drift' => sub {
    for my $field (qw(launcher_sha256 dispatcher_sha256 source_sha256)) {
        fixture(); $F->{fresh}->{$field}='0' x 64;
        ok(!eval {dispatcher()->complete_finalizing_absent(101,$TXID);1},"$field drift refused");
        is($F->{appends},0,'no append'); is_deeply(\@effects,[],'zero effects');
    }
};

subtest 'last-moment journal concurrency loses exact CAS without any replay' => sub {
    fixture(); $F->{concurrent_cas}=1;
    ok(!eval {dispatcher()->complete_finalizing_absent(101,$TXID);1},'concurrent writer refused');
    is($F->{appends},1,'one CAS attempt');
    is($F->{rows}->[-1]->{stage},'FINALIZING','no COMPLETE appended');
    is_deeply(\@effects,[],'zero effects');
};

subtest 'durable COMPLETE with lost ACK is never retried or converted to execute' => sub {
    fixture(); $F->{lost_ack}=1; my $d=dispatcher();
    ok(!eval {$d->complete_finalizing_absent(101,$TXID);1},'lost ACK is not false success');
    is($F->{rows}->[-1]->{stage},'COMPLETE','durable terminal retained');
    is($F->{appends},1,'one publication');
    ok(!eval {dispatcher()->complete_finalizing_absent(101,$TXID);1},'fresh process refuses already terminal');
    is($F->{appends},1,'no replay after observation of COMPLETE');
    is_deeply(\@effects,[],'zero effects');
};

done_testing();
