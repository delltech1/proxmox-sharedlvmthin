use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use JSON::PP ();
use Digest::SHA qw(sha1_hex sha256_hex);

# Native operations exist ONLY in this in-memory fixture. Production
# bootstrap is never invoked except its refusal-before-load regression.
BEGIN { $INC{"PVE/$_.pm"} = __FILE__ for qw(Cluster QemuConfig QemuServer QemuServer/Network
    Storage HA/Config ReplicationConfig AccessControl Firewall) }
our $F;
{
    package PVE::Cluster; sub cfs_update { }
    package PVE::QemuConfig;
    sub lock_config { my ($class,$vmid,$code)=@_; die 'recursive VM lock' if $main::F->{locked};
        push @{$main::F->{events}}, 'lock'; local $main::F->{locked}=1; return $code->() }
    sub load_config { main::copy($main::F->{conf}) }
    sub check_lock { die 'config locked' if $_[1]->{lock} }
    sub check_protection { die 'protected' if $_[1]->{protection} }
    sub parse_volume { my ($class,$key,$value)=@_; my ($file)=split(/,/,$value); return {file=>$file} }
    sub cleanup_fleecing_images { die 'unqualified real fleecing cleanup' }
    sub destroy_config { die 'unlocked final config removal' if !$main::F->{locked};
        push @{$main::F->{events}}, 'config-remove'; $main::F->{conf}=undef }
    package PVE::Storage; sub config { main::copy($main::F->{cfg}) }
    package PVE::QemuServer;
    sub check_running { $main::F->{running} // 0 }
    sub drive_is_cdrom { 0 }
    sub parse_vm_config { my ($name,$raw,$strict)=@_; die 'non-strict parser' if !$strict;
        my $c=JSON::PP->new->decode($raw); $c->{digest}=Digest::SHA::sha1_hex($raw); return $c }
    sub destroy_vm {
        my ($cfg,$vmid,$skip,$replacement,$purge)=@_;
        die 'invalid native boundary' if !$main::F->{locked} || $skip || $purge;
        push @{$main::F->{events}}, 'native'; $main::F->{native}++;
        PVE::QemuConfig::cleanup_fleecing_images($vmid,$cfg);
        die 'native failure prefix' if $main::F->{native_failure};
        $_->{absent}=1 for values %{$main::F->{scopes}};
        PVE::QemuServer::Network::delete_ifaces_ipams_ips($main::F->{conf},$vmid);
        $main::F->{conf}={%$replacement, digest=>'c' x 40, snapshots=>{}, pending=>{}, 'special-sections'=>{}};
    }
    package PVE::QemuServer::Network; sub delete_ifaces_ipams_ips { die 'real IPAM call forbidden' }
    package PVE::HA::Config; sub service_is_configured { $main::F->{ha} // 0 }
    package PVE::ReplicationConfig; sub new { bless {},shift }
    sub check_for_existing_jobs { die 'replication configured' if $main::F->{replication} }
    package PVE::AccessControl; sub remove_vm_access { push @{$main::F->{events}},'acl-remove'; $main::F->{acl}=0 }
    package PVE::Firewall; sub remove_vmfw_conf { push @{$main::F->{events}},'firewall-remove'; $main::F->{firewall}=0 }
}

use PVE::SharedLvmThinVMDestroyDispatcher;
use PVE::SharedLvmThinVMDestroyRuntime;
use PVE::SharedLvmThinVMDestroyStoragePlan;
use PVE::SharedLvmThinVMDestroyPlanAdapter;
use PVE::SharedLvmThinVMDestroyPlanV3;
use PVE::SharedLvmThinVMDestroy;
plan skip_all => 'root-only dispatcher fixture' if $> != 0 || $< != 0;
my $JSON=JSON::PP->new->canonical;
my $TX='a' x 32;
sub json { $JSON->encode($_[0]) }
sub copy { $JSON->decode(json($_[0])) }
sub hash { sha256_hex(json($_[0])) }
sub refused { my ($code,$pattern,$label)=@_; my $ok=eval { $code->(); 1 }; my $error=$@;
    ok(!$ok,"$label refused"); like($error,$pattern,"$label reason") }

{
    package MockRuntime;
    sub verify { push @{$main::F->{events}}, $main::F->{locked} ? 'runtime-locked' : 'runtime-prelock';
        $main::F->{runtime_reads}++; my $r=main::copy($main::F->{runtime});
        $r->{boot_id}='ffffffff-ffff-ffff-ffff-ffffffffffff'
            if ($main::F->{runtime_drift} && $main::F->{runtime_reads}>1)
            || ($main::F->{runtime_drift_at} && $main::F->{runtime_reads}==$main::F->{runtime_drift_at});
        return $r }
    package MockCollector;
    sub plan {
        my ($self,$volids)=@_; my $scope=$main::F->{scopes}->{$self->{sid}};
        die 'collector outside VM lock' if !$main::F->{locked};
        push @{$main::F->{events}}, "plan:$self->{sid}"; $scope->{plans}++;
        my $inventory=main::copy($scope->{inventory});
        $inventory->{scope}->{boot_id}='bad' if $main::F->{scope_drift};
        my $planner=PVE::SharedLvmThinVMDestroyStoragePlan->new(collector=>sub {$inventory});
        my $storage=$planner->plan(vmid=>101,volumes=>$volids);
        my $foreign=main::copy($scope->{foreign});
        $foreign->{extra}=1 if $main::F->{storage_drift} && $scope->{plans}>1;
        return {authority=>'NONE',inventory=>$inventory, storage_plan=>$storage,bindings=>main::copy($scope->{bindings}),
            foreign_evidence=>$foreign, foreign_evidence_sha256=>main::hash($foreign)};
    }
    sub observe {
        my ($self,$bundle)=@_; my $scope=$main::F->{scopes}->{$self->{sid}};
        push @{$main::F->{events}},"observe:$self->{sid}";
        die 'planned storage remains' if !$scope->{absent} || $main::F->{observe_failure};
        die 'foreign evidence not reconstructed' if main::json($bundle->{foreign_evidence}) ne main::json($scope->{foreign});
        my $after=main::copy($scope->{inventory}); $after->{$_}=[] for qw(roots objects runtimes);
        my $planner=PVE::SharedLvmThinVMDestroyStoragePlan->new(collector=>sub {$after});
        return {%{$planner->observe($bundle->{storage_plan})}, foreign_evidence_sha256=>main::hash($scope->{foreign})};
    }
    package MockJournal;
    sub append { my ($self,$stage,$receipt)=@_;
        die 'journal before descriptor' if !$main::F->{descriptor};
        push @{$main::F->{events}},"journal:$stage";
        die 'journal acknowledgement lost' if ($main::F->{journal_failure}//'') eq $stage;
        push @{$main::F->{records}},[$stage,main::copy($receipt)]; return 1 }
    package MockDescriptor;
    sub create { my ($self,$context)=@_;
        push @{$main::F->{events}},'descriptor:create';
        die 'descriptor already exists' if $main::F->{descriptor};
        $main::F->{descriptor}={schema=>'sharedlvmthin-vm-destroy-recovery/v1',authority=>'NONE',txid=>$self->{txid},
            context=>main::copy($context),context_sha256=>main::hash($context)};
        die 'descriptor fsynced but acknowledgement lost' if $main::F->{descriptor_failure};
        return $self->read();
    }
    sub read { my ($self)=@_; push @{$main::F->{events}},'descriptor:read';
        die 'descriptor missing' if !$main::F->{descriptor};
        return {authority=>'NONE',txid=>$self->{txid},durable=>JSON::PP::true, descriptor=>main::copy($main::F->{descriptor})};
    }
    package MockFinalization;
    sub observe { my ($self,$vmid)=@_; push @{$main::F->{events}},'finalization:observe';
        return {vmid=>"$vmid",acl=>$main::F->{acl}?'PRESENT':'ABSENT',firewall=>$main::F->{firewall}?'PRESENT':'ABSENT',
            config=>defined($main::F->{conf})?'PRESENT':'ABSENT',
            (defined($main::F->{conf}) ? (config_sha256=>main::hash($main::F->{conf})) : ())};
    }
}

sub fixture {
    my ($multi)=@_; $multi //=1;
    $F={locked=>0,events=>[],records=>[],scopes=>{},native=>0,acl=>1,firewall=>1,runtime_reads=>0};
    $F->{cfg}={ids=>{thin=>{type=>'sharedlvmthin',shared=>1,'slt-vgname'=>'vgthin',
        'slt-expected-vg-uuid'=>'vgthinUUID','slt-expected-pv-uuid'=>'pvthinUUID','slt-expected-wwid'=>'60001','slt-allocation-mode'=>'thin'}}};
    $F->{conf}={scsi0=>'thin:vm-101-disk-0',scsi1=>'thin:vm-101-disk-1',snapshots=>{},pending=>{},'special-sections'=>{}};
    if ($multi) {
        $F->{cfg}->{ids}->{thick}={type=>'sharedlvmthin',shared=>1,'slt-vgname'=>'vgthick',
            'slt-expected-vg-uuid'=>'vgthickUUID','slt-expected-pv-uuid'=>'pvthickUUID','slt-expected-wwid'=>'60002','slt-allocation-mode'=>'thick-generations'};
        $F->{conf}->{scsi2}='thick:vm-101-disk-2';
    }
    $F->{raw}=json($F->{conf}); $F->{conf}->{digest}=sha1_hex($F->{raw});
    $F->{runtime}={schema=>'sharedlvmthin-vm-destroy-runtime/v1',status=>'QUALIFIED',authority=>'NONE',vmid=>'101',
        node=>'node-a',boot_id=>'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',contract_sha256=>'b' x 64};
    for my $sid (sort keys %{$F->{cfg}->{ids}}) {
        my $cfg=$F->{cfg}->{ids}->{$sid}; my $thin=$sid eq 'thin';
        my @volids=$thin ? qw(thin:vm-101-disk-0 thin:vm-101-disk-1) : ('thick:vm-101-disk-2');
        my @kinds=$thin ? qw(thin_pool thin_data thin_metadata thin_lv thin_lv snapshot snapshot) : qw(anchor generation);
        my (@objects,%bindings);
        for my $i (0..$#kinds) {
            my $volid=$thin ? ($i<3?'':$volids[($i-3)%2]) : ($i==0?'':$volids[0]);
            my $id="$sid-$i"; my $uuid="$sid-uuid-$i";
            push @objects,{id=>$id,uuid=>$uuid,root_id=>"$sid-0",kind=>$kinds[$i],owner_vmid=>'101',
                namespace=>$cfg->{'slt-expected-vg-uuid'},volume_id=>$volid,origin_uuid=>$thin && $i>=5 ? "$sid-uuid-" . ($i-2) : ''};
            $bindings{$id}={name=>"$sid-lv-$i",uuid=>$uuid};
        }
        my $inventory={schema=>'sharedlvmthin-destroy-inventory/v1',complete=>1,
            scope=>{node=>$F->{runtime}->{node},boot_id=>$F->{runtime}->{boot_id},vg_uuid=>$cfg->{'slt-expected-vg-uuid'},
                pv_uuid=>$cfg->{'slt-expected-pv-uuid'},wwid=>$cfg->{'slt-expected-wwid'},storage_ids=>[$sid],config_sha256=>hash($F->{cfg})},
            roots=>[{id=>"$sid-0",mode=>$thin?'thin':'thick',phase=>$thin?'OWNED_INACTIVE':'MATERIALIZED',owner_vmid=>'101',
                namespace=>$cfg->{'slt-expected-vg-uuid'},object_ids=>[map {$_->{id}} @objects],runtime_ids=>[],volume_ids=>\@volids}],
            objects=>\@objects,runtimes=>[]};
        $F->{scopes}->{$sid}={inventory=>$inventory,bindings=>{objects=>\%bindings,runtimes=>{}},foreign=>{lvs=>{},dm=>[],vg_tags=>'',pv=>{sid=>$sid}},plans=>0};
    }
    return $F;
}

sub dispatcher {
    return PVE::SharedLvmThinVMDestroyDispatcher->new(
        bootstrap=>sub {push @{$F->{events}},'bootstrap'; return {identity=>{schema=>'fixture-load',source_sha256=>'c' x 64},
            check=>sub {push @{$F->{events}},'source-check'; die 'source changed' if $F->{source_drift}; return 1}}},
        runtime_factory=>sub {bless {},'MockRuntime'},
        collector_factory=>sub {my %args=@_; push @{$F->{events}},"collector:$args{storage_id}";
            return bless {sid=>$args{storage_id}},'MockCollector'},
        raw_reader=>sub {die 'raw read outside VM lock' if !$F->{locked}; return $F->{bad_raw} // $F->{raw}},
        journal_factory=>sub {push @{$F->{events}},'journal:new'; bless {},'MockJournal'},
        descriptor_factory=>sub {my %args=@_; bless {txid=>$args{txid}},'MockDescriptor'},
        finalization_factory=>sub {bless {},'MockFinalization'},
        executor_factory=>sub {push @{$F->{events}},'executor:new'; my %args=@_;
            if ($F->{extra_runtime_call}) { my $prepare=$args{prepare_plan};
                $args{prepare_plan}=sub {$args{verify_runtime}->($_[0]); $prepare->(@_)} }
            PVE::SharedLvmThinVMDestroy->new(%args)},
        journal_observer=>sub {push @{$F->{events}},'journal:observe'; return {schema=>'sharedlvmthin-vm-destroy-journal/v1',authority=>'NONE',
            stage=>$F->{records}->[-1]->[0],records=>[map {{request_id=>$TX,context=>copy($F->{descriptor}->{context})}} @{$F->{records}}]}});
}

for my $multi (0,1) {
    my $label = $multi ? 'multi-VG grouped and observed without flattening' : 'single VG still persists full v3 envelope';
    subtest $label => sub {
        fixture($multi); my $d=dispatcher(); my $result=$d->execute(101,$TX);
        is($result->{status},'COMPLETE','complete'); is($F->{native},1,'one native call');
        my $plan=$F->{descriptor}->{context}->{receipt}->{plan};
        is($plan->{schema},'sharedlvmthin-vm-destroy-executor-plan/v3','always lossless v3');
        is(scalar(@{$plan->{scopes}}),$multi?2:1,'exact scope count');
        my ($thin)=grep {$_->{collector_sid} eq 'thin'} @{$plan->{scopes}};
        is(scalar(@{$thin->{executor_v2}->{roots}}),1,'shared Thin pool normalized once');
        is(scalar(@{$thin->{executor_v2}->{roots}->[0]->{objects}}),7,'two heads and snapshots retain structural closure');
        is_deeply([map {$_->[0]} @{$F->{records}}],[qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING COMPLETE)],'existing exact journal stages');
        my %at; for my $i (0..$#{$F->{events}}) {$at{$F->{events}->[$i]} //=$i}
        ok($at{'runtime-prelock'}<$at{lock} && $at{lock}<$at{'runtime-locked'},'first runtime before lock, second under lock');
        is($F->{runtime_reads},5,'prelock, initial locked, two journal boundaries, final native-boundary runtime recheck');
        ok($at{'descriptor:create'}<$at{'journal:PREPARED'} && $at{'journal:PREPARED'}<$at{'journal:DISPATCHED'}
            && $at{'journal:DISPATCHED'}<$at{native},'descriptor durable before PREPARED and dispatch');
        my $calls=scalar(@{$F->{events}});
        refused(sub {$d->execute(101,$TX)},qr/already attempted/,'same-process retry');
        is(scalar(@{$F->{events}}),$calls,'retry has zero callbacks');
    };
}

my @cases=(
    ['runtime drift',sub {$F->{runtime_drift}=1},qr/runtime changed/,0],
    ['out-of-phase runtime callback',sub {$F->{extra_runtime_call}=1},qr/callback phase/,0],
    ['runtime observe-only purpose',sub {$F->{runtime}->{status}='OBSERVED'; $F->{runtime}->{purpose}='FINALIZATION_OBSERVATION_ONLY'},qr/not executable/,0],
    ['source drift',sub {$F->{source_drift}=1},qr/source changed/,0],
    ['collector scope drift',sub {$F->{scope_drift}=1},qr/scope differs|scope|boot/,0],
    ['storage changed before dispatch',sub {$F->{storage_drift}=1},qr/inventory changed before dispatch/,1],
    ['descriptor lost ACK',sub {$F->{descriptor_failure}=1},qr/acknowledgement lost/,0],
    ['journal PREPARED failure',sub {$F->{journal_failure}='PREPARED'},qr/journal acknowledgement lost/,0],
    ['journal DISPATCHED failure',sub {$F->{journal_failure}='DISPATCHED'},qr/journal acknowledgement lost/,1],
    ['bad raw config',sub {$F->{bad_raw}='different'},qr/raw\/locked digest/,0],
    ['running VM',sub {$F->{running}=1},qr/running VM/,0],
    ['HA VM',sub {$F->{ha}=1},qr/HA resource/,0],
    ['replication VM',sub {$F->{replication}=1},qr/replication configured/,0],
    ['current NIC',sub {$F->{conf}->{net0}='virtio,bridge=vmbr0'},qr/network\/IPAM/,0],
    ['snapshot NIC',sub {$F->{conf}->{snapshots}->{old}={net0=>'e1000,bridge=vmbr0'}},qr/network\/IPAM/,0],
    ['pending NIC',sub {$F->{conf}->{pending}->{net0}='virtio'},qr/pending configuration/,0],
    ['unknown fleecing state',sub {$F->{conf}->{'special-sections'}={fleecing=>{}}},qr/special configuration/,0],
);
for my $case (@cases) {
    subtest $case->[0]=>sub {fixture(); $case->[1]->(); my $d=dispatcher();
        refused(sub {$d->execute(101,$TX)},$case->[2],$case->[0]);
        is($F->{native},0,'zero native effects'); is(scalar(@{$F->{records}}),$case->[3],'only expected prior journal records');
        my $calls=scalar(@{$F->{events}}); refused(sub {$d->execute(101,$TX)},qr/already attempted/,'no implicit retry');
        is(scalar(@{$F->{events}}),$calls,'no callbacks after refusal');
    };
}

subtest 'native failure remains terminal unknown, never replayed' => sub {
    fixture(); $F->{native_failure}=1;
    refused(sub {dispatcher()->execute(101,$TX)},qr/PARTIAL_OR_UNKNOWN/,'native failure');
    is($F->{native},1,'one dispatch');
    is($F->{records}->[-1]->[0],'PARTIAL_OR_UNKNOWN','durable unknown stage');
    ok(!grep({$_ eq 'acl-remove' || $_ eq 'firewall-remove' || $_ eq 'config-remove'} @{$F->{events}}),'no finalization');
};

subtest 'final executor runtime recheck remains exact after DISPATCHED' => sub {
    fixture(); $F->{runtime_drift_at}=5;
    refused(sub {dispatcher()->execute(101,$TX)},qr/PARTIAL_OR_UNKNOWN.*runtime changed/s,'late runtime drift');
    is($F->{native},0,'no native call after runtime change');
    is_deeply([map {$_->[0]} @{$F->{records}}],[qw(PREPARED DISPATCHED PARTIAL_OR_UNKNOWN)],'exact no-effect failed-dispatch evidence');
};

subtest 'storage UNKNOWN blocks finalization after native returned' => sub {
    fixture(); $F->{observe_failure}=1;
    refused(sub {dispatcher()->execute(101,$TX)},qr/PARTIAL_OR_UNKNOWN/,'unknown storage result');
    is($F->{conf}->{lock},'destroyed','zombie retained');
    ok($F->{acl} && $F->{firewall},'ACL/firewall retained');
};

subtest 'cross-VG target mapper in raw foreign baseline refuses before publication' => sub {
    fixture();
    my $scope=$F->{scopes}->{thick};
    $scope->{inventory}->{roots}->[0]->{runtime_ids}=['thick-dm'];
    $scope->{inventory}->{runtimes}=[{id=>'thick-dm',dm_uuid=>'thick-DM-UUID',root_id=>'thick-0'}];
    $scope->{bindings}->{runtimes}={'thick-dm'=>{name=>'thick-mapper',uuid=>'thick-DM-UUID'}};
    $F->{scopes}->{thin}->{foreign}->{dm}=[['thick-mapper','thick-DM-UUID',0]];
    refused(sub {dispatcher()->execute(101,$TX)},qr/cross-VG target DM/,'unobservable cross-scope composition');
    is($F->{native},0,'zero native calls'); is_deeply($F->{records},[],'zero journal append');
    ok(!$F->{descriptor},'no descriptor publication');
};

subtest 'observe COMPLETE or FINALIZING/all-ABSENT grants no executor or append authority' => sub {
    for my $stage ('COMPLETE','FINALIZING','PARTIAL_OR_UNKNOWN') {
        fixture(); dispatcher()->execute(101,$TX); $F->{records}->[-1]->[0]=$stage;
        $F->{runtime}->{boot_id}='new boot'; $F->{runtime}->{status}='OBSERVED';
        $F->{conf}=undef; $F->{acl}=0; $F->{firewall}=0; $F->{events}=[];
        my $value=dispatcher()->observe($TX);
        is($value->{authority},'NONE',"$stage no authority"); is($value->{status},'OBSERVE_ONLY',"$stage read-only purpose");
        is_deeply($F->{events},['descriptor:read','journal:observe'],"$stage zero bootstrap/runtime/collector/executor/append effects");
    }
};

subtest 'invalid identities never reach any dependency' => sub {
    fixture(); my $d=dispatcher();
    for my $args ([undef,$TX],['101;id',$TX],["101\n",$TX],[101,'bad'],[101,[]]) {
        refused(sub {$d->execute(@$args)},qr/invalid VM\/transaction/,'invalid execute identity');
    }
    refused(sub {$d->observe('../bad')},qr/invalid transaction/,'invalid observe identity');
    is_deeply($F->{events},[],'zero dependencies');
};

subtest 'production bootstrap refuses preloaded PVE without probes or effects' => sub {
    fixture();
    refused(sub {PVE::SharedLvmThinVMDestroyDispatcher->new()->execute(101,$TX)},qr/PVE modules were loaded/,'not fresh');
    is_deeply($F->{events},[],'zero production effects');
};

done_testing();
