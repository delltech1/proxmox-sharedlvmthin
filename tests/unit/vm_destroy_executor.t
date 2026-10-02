use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use JSON::PP ();
use Digest::SHA qw(sha1_hex sha256_hex);

# All PVE effects are confined to this in-memory fixture. No host or SAN I/O.
BEGIN {
    $INC{"PVE/$_.pm"} = __FILE__ for qw(Cluster QemuConfig QemuServer QemuServer/Network Storage
        HA/Config ReplicationConfig AccessControl Firewall);
}
our ($conf, $locked, $running, $ha, $replication, $failure, $native_calls, $storecfg, $native_throw, $acl_fail,
    $acl_present, $firewall_present, $finalization_unknown, $acl_after_fail, $firewall_after_fail,
    $config_after_fail, $last_executor);
our (@events, %present, $raw, @planned_ids, @records, $cleanup_original_calls, $ipam_original_calls, $cleanup_drift);
{
    package PVE::Cluster;
    sub cfs_update { }
    package PVE::QemuConfig;
    sub lock_config {
        my ($class, $vmid, $code) = @_;
        die "recursive lock\n" if $main::locked;
        local $main::locked = 1;
        return $code->();
    }
    sub load_config { return JSON::PP->new->decode(JSON::PP->new->encode($main::conf)); }
    sub check_lock { die "locked\n" if $_[1]->{lock}; }
    sub check_protection { die "protected\n" if $_[1]->{protection}; }
    sub cleanup_fleecing_images {
        $main::cleanup_original_calls++;
        PVE::QemuConfig->lock_config($_[0], sub { die "unexpected nested cleanup\n" });
    }
    sub parse_volume {
        my ($class, $key, $value) = @_;
        my ($file) = split /,/, $value;
        return { file => $file, ($value =~ /media=cdrom/ ? (media => 'cdrom') : ()) };
    }
    sub destroy_config {
        die "not locked\n" if !$main::locked;
        push @main::events, 'destroy_config';
        $main::conf = undef;
        die "config crash after effect\n" if $main::config_after_fail;
    }
    package PVE::Storage;
    sub config { return $main::storecfg; }
    sub vdisk_free {
        my ($cfg, $volid) = @_;
        push @main::events, "free:$volid";
        die "injected removal failure\n" if $volid eq ($main::failure // '');
        delete $main::present{$volid};
    }
    package PVE::QemuServer;
    sub check_running { return $main::running; }
    sub drive_is_cdrom {
        my ($drive, $exclude_cloudinit) = @_;
        return 0 if $exclude_cloudinit && $drive->{file} =~ /:vm-\d+-cloudinit$/;
        return ($drive->{media} // '') eq 'cdrom';
    }
    sub parse_vm_config {
        my ($filename, $raw, $strict) = @_;
        die "strict parse required\n" if !$strict;
        my $parsed = JSON::PP->new->decode($raw); # fixture serialization only
        $parsed->{digest} = Digest::SHA::sha1_hex($raw);
        return $parsed;
    }
    sub destroy_vm {
        my ($cfg, $vmid, $skiplock, $replacement, $purge) = @_;
        die "invalid native dispatch\n" if !$main::locked || $skiplock || $purge
            || keys(%$replacement) != 1 || $replacement->{lock} ne 'destroyed';
        $main::native_calls++;
        push @main::events, 'native';
        die "API14 direct owner lookup failed\n" if $main::native_throw;
        PVE::QemuConfig::cleanup_fleecing_images($vmid, $cfg);
        # Model the real upstream caught-failure/warn-and-continue boundary.
        for my $volid (sort keys %main::present) {
            eval { PVE::Storage::vdisk_free($cfg, $volid); };
            warn $@ if $@;
        }
        PVE::QemuServer::Network::delete_ifaces_ipams_ips($main::conf, $vmid);
        $main::conf = { %$replacement, snapshots => {}, pending => {}, 'special-sections' => {}, digest => 'b' x 40 };
    }
    package PVE::QemuServer::Network;
    sub delete_ifaces_ipams_ips { $main::ipam_original_calls++ }
    package PVE::HA::Config;
    sub service_is_configured { return $main::ha; }
    package PVE::ReplicationConfig;
    sub new { bless {}, shift }
    sub check_for_existing_jobs { die "replication configured\n" if $main::replication; }
    package PVE::AccessControl;
    sub remove_vm_access { push @main::events, 'acl'; die "ACL failure\n" if $main::acl_fail; $main::acl_present = 0;
        die "ACL crash after effect\n" if $main::acl_after_fail; }
    package PVE::Firewall;
    sub remove_vmfw_conf { push @main::events, 'firewall'; $main::firewall_present = 0;
        die "firewall crash after effect\n" if $main::firewall_after_fail; }
}

use PVE::SharedLvmThinVMDestroy;
plan skip_all => 'root-only executor fixture requires root' if $> != 0;

sub run_case {
    my (%opts) = @_;
    $conf = { scsi0 => 's:vm-101-disk-0', scsi1 => 's:vm-101-disk-1', snapshots => {}, pending => {}, 'special-sections' => {} };
    $storecfg = { ids => { s => { type => 'sharedlvmthin', 'slt-vgname' => 'vg', 'slt-expected-vg-uuid' => 'vg-uuid' } } };
    ($locked, $running, $ha, $replication, $native_calls) = (0, 0, 0, 0, 0);
    $failure = $opts{failure};
    $native_throw = $opts{native_throw};
    $acl_fail = $opts{acl_fail};
    ($acl_present, $firewall_present) = (1, 1);
    $finalization_unknown = $opts{finalization_unknown};
    $acl_after_fail = $opts{acl_after_fail};
    $firewall_after_fail = $opts{firewall_after_fail};
    $config_after_fail = $opts{config_after_fail};
    @events = ();
    @records = ();
    @planned_ids = ();
    ($cleanup_original_calls, $ipam_original_calls) = (0, 0);
    %present = map { $_ => 1 } @{$conf}{qw(scsi0 scsi1)};
    $opts{mutate}->() if $opts{mutate};
    $raw = JSON::PP->new->canonical->encode($conf);
    $conf->{digest} = sha1_hex($raw);
    my (@stages, @warnings);
    my $finalization_observations = 0;
    my $runtime_observations = 0;
    my $executor = PVE::SharedLvmThinVMDestroy->new(
        verify_runtime => sub {
            die "runtime observation outside VM lock\n" if !$locked;
            my $state = { status => 'QUALIFIED', contract_sha256 => 'a' x 64 };
            $opts{runtime_mutate}->($state, ++$runtime_observations) if $opts{runtime_mutate};
            return $state;
        },
        prepare_plan => sub {
            die "unlocked plan\n" if !$locked;
            my ($vmid, $config, $cfg, $volids) = @_;
            my @volumes;
            for my $volid (@$volids) {
                my ($name) = $volid =~ /:(.*)$/;
                push @volumes, { volid => $volid, kind => 'thin',
                    root => { type => 'lvm-vg', name => 'vg', uuid => 'vg-uuid' },
                    objects => [{ type => 'lvm-lv', name => $name, uuid => "uuid-$name",
                        id => "lvm:vg-uuid:uuid-$name", volid => $volid }],
                    runtime_ids => [{ type => 'dm', name => "vg-$name", uuid => "LVM-$name",
                        id => "dm:LVM-$name", volid => $volid }] };
            }
            my $plan = { raw_config => $raw, config_sha256 => sha256_hex($raw),
                parsed_config_sha256 => sha256_hex(JSON::PP->new->canonical->encode($config)),
                volids => $volids, volumes => \@volumes };
            @planned_ids = sort map { map { $_->{id} } (@{$_->{objects}}, @{$_->{runtime_ids}}) } @volumes;
            $opts{plan_mutate}->($plan) if $opts{plan_mutate};
            return $plan;
        },
        observe_plan => sub {
            die "inventory unavailable\n" if $opts{unknown};
            return { status => keys(%present) ? 'PRESENT' : 'ABSENT', foreign_unchanged => 'YES',
                object_ids => $opts{subset} ? [] : [@planned_ids] };
        },
        observe_finalization => sub {
            die "finalization observer unavailable\n" if $finalization_unknown;
            $opts{finalization_mutate}->(++$finalization_observations) if $opts{finalization_mutate};
            return { vmid => '101', acl => $acl_present ? 'PRESENT' : 'ABSENT',
                firewall => $firewall_present ? 'PRESENT' : 'ABSENT',
                config => defined($conf) ? 'PRESENT' : 'ABSENT',
                (defined($conf) ? (config_sha256 => sha256_hex(JSON::PP->new->canonical->encode($conf))) : ()) };
        },
        journal => sub {
            my ($stage, $receipt) = @_;
            push @stages, $stage;
            push @records, [$stage, $receipt];
            die "journal injected\n" if $stage eq ($opts{journal_failure} // '');
            die "receipt lost config\n" if !defined($receipt->{config}->{scsi0});
            return 1;
        },
        read_journal => sub {
            return { durable => 1, records => [map {
                +{ stage => $_->[0], receipt => JSON::PP->new->decode(JSON::PP->new->encode($_->[1])) }
            } @records] };
        },
    );
    $last_executor = $executor;
    local $SIG{__WARN__} = sub { push @warnings, @_ };
    my $result = eval { $executor->execute(101, 'c' x 32) };
    my $error = $@;
    return ($result, $error, \@stages, \@warnings);
}

for my $volume (qw(s:vm-101-disk-0 s:vm-101-disk-1)) {
    subtest "caught failure for $volume retains zombie" => sub {
        my ($result, $error, $stages, $warnings) = run_case(failure => $volume);
        ok(!$result, 'native normal return is not success');
        like($error, qr/PARTIAL_OR_UNKNOWN/, 'explicit unknown result');
        is($native_calls, 1, 'native function dispatched only once');
        is($conf->{lock}, 'destroyed', 'zombie retained');
        ok(@$warnings, 'upstream swallowed warning was exercised');
        is_deeply([grep { /^(?:acl|firewall|destroy_config)$/ } @events], [], 'no finalization');
        is($stages->[-1], 'PARTIAL_OR_UNKNOWN', 'failure journal attempted');
    };
}

for my $network (
    'virtio=02:00:00:00:00:01,bridge=vmbr0',
    'e1000=02:00:00:00:00:02,bridge=vmbr0',
    'virtio=02:00:00:00:00:03,bridge=sdnzone',
    'malformed',
) {
    subtest "network/IPAM boundary refuses before every effect: $network" => sub {
        my ($result, $error, $stages) = run_case(mutate => sub { $conf->{net0} = $network });
        ok(!$result, 'networked VM refused');
        like($error, qr/unjournalled network\/IPAM/, 'exact external-effect boundary');
        is($native_calls, 0, 'native destroy never dispatched');
        is_deeply($stages, [], 'no PREPARED journal or other effect');
        ok(defined($conf->{scsi0}), 'original VM config retained');
    };
}

for my $option (qw(unknown subset)) {
    subtest "$option inventory blocks finalization" => sub {
        my ($result, $error) = run_case($option => 1);
        ok(!$result, 'no success');
        is($conf->{lock}, 'destroyed', 'zombie retained even if deletes completed');
        is_deeply([grep { /^(?:acl|firewall|destroy_config)$/ } @events], [], 'zero finalization');
        is($native_calls, 1, 'no repeat');
    };
}

subtest 'success finalizes once and in order' => sub {
    my ($result, $error, $stages) = run_case();
    is($error, '', 'no error');
    is($result->{status}, 'COMPLETE', 'complete only after finalization');
    is_deeply([grep { /^(?:acl|firewall|destroy_config)$/ } @events], [qw(acl firewall destroy_config)], 'config removed last, once');
    is_deeply($stages, [qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING COMPLETE)], 'durable stages ordered');
    is($native_calls, 1, 'one native call');
    ok(!defined($conf), 'config absent');
};

subtest 'API14 direct lookup exception does not remove original config' => sub {
    my ($result, $error) = run_case(native_throw => 1);
    ok(!$result, 'exception is failure');
    like($error, qr/direct owner lookup failed/, 'original error retained');
    is($conf->{scsi0}, 's:vm-101-disk-0', 'original config not overwritten by adapter');
    is_deeply(\@events, ['native'], 'no cleanup or retry');
};

subtest 'ACL failure retains zombie and prevents later finalization' => sub {
    my ($result) = run_case(acl_fail => 1);
    ok(!$result, 'failure is not complete');
    is($conf->{lock}, 'destroyed', 'zombie remains');
    is_deeply([grep { /^(?:acl|firewall|destroy_config)$/ } @events], ['acl'], 'no later effect');
};

for my $case (
    ['before ACL', 1, []],
    ['after ACL', 2, ['acl']],
    ['after firewall', 4, ['acl', 'firewall']],
) {
    subtest "replacement config $case->[0] prevents remaining VMID-scoped effects" => sub {
        my ($result, $error) = run_case(finalization_mutate => sub {
            my ($observation) = @_;
            $conf = { name => 'new-vm-using-same-id', digest => 'd' x 40 }
                if $observation == $case->[1];
        });
        ok(!$result, 'replacement config never completes old transaction');
        like($error, qr/zombie identity changed/, 'positive identity mismatch blocks effects');
        is_deeply([grep { /^(?:acl|firewall|destroy_config)$/ } @events], $case->[2],
            'no effects after first observed replacement');
        is($conf->{name}, 'new-vm-using-same-id', 'replacement config preserved');
        is($native_calls, 1, 'no storage redispatch');
    };
}

subtest 'absent config never authorizes remaining VMID-scoped cleanup' => sub {
    my ($result, $error) = run_case(finalization_mutate => sub { $conf = undef });
    ok(!$result, 'ambiguous VMID ownership refused');
    like($error, qr/VMID reuse is ambiguous/, 'explicit absent-config boundary');
    is_deeply([grep { /^(?:acl|firewall|destroy_config)$/ } @events], [],
        'ACL/firewall untouched without a present exact zombie');
    is($acl_present, 1, 'ACL preserved');
    is($firewall_present, 1, 'firewall preserved');
};

for my $case (
    ['contract drift', sub { $_[0]->{contract_sha256} = 'd' x 64 }],
    ['qualification revoked', sub { $_[0]->{status} = 'BLOCKED' }],
    ['owner/quorum observation failed', sub { die "runtime quorum or owner unknown\n" }],
) {
    subtest "$case->[0] after durable DISPATCHED remains pre-native" => sub {
        my ($result, $error, $stages) = run_case(runtime_mutate => sub {
            my ($state, $observation) = @_;
            $case->[1]->($state) if $observation == 2;
        });
        ok(!$result && length($error), 'no false qualified result');
        is($native_calls, 0, 'no native call on stale or unknown runtime');
        is_deeply(\@events, [], 'zero storage/ACL/firewall/config effects');
        is($conf->{scsi0}, 's:vm-101-disk-0', 'original config preserved');
        is_deeply($stages, [qw(PREPARED DISPATCHED PARTIAL_OR_UNKNOWN)],
            'dispatch intent is never mistaken for a completed effect');
    };
}

for my $case (
    ['HA', sub { $ha = 1 }], ['replication', sub { $replication = 1 }],
    ['running', sub { $running = 1 }], ['template', sub { $conf->{template} = 1 }],
    ['protection', sub { $conf->{protection} = 1 }],
    ['pending', sub { $conf->{pending} = { memory => 512 } }],
    ['snapstate', sub { $conf->{snapshots}->{s} = { snapstate => 'prepare' } }],
    ['fleecing', sub { $conf->{'fleecing-images'} = 's:vm-101-fleece-0' }],
    ['special', sub { $conf->{'special-sections'} = { mystery => {} } }],
    ['unknown storage', sub { $storecfg->{ids}->{s}->{type} = 'unknown' }],
) {
    subtest "$case->[0] is a pre-effect refusal" => sub {
        my ($result, $error, $stages) = run_case(mutate => $case->[1]);
        ok(!$result && length($error), 'refused');
        is($native_calls, 0, 'no native call');
        is_deeply(\@events, [], 'zero effects');
        is_deeply($stages, [], 'no mutation dispatch record');
    };
}

for my $stage (qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING COMPLETE)) {
    subtest "journal failure at $stage" => sub {
        my ($result, $error) = run_case(journal_failure => $stage);
        ok(!$result && length($error), 'no false success');
        is($native_calls, $stage =~ /^(?:PREPARED|DISPATCHED)$/ ? 0 : 1, 'never redispatched');
        ok(!grep($_ eq 'destroy_config', @events), 'config retained before COMPLETE') if $stage ne 'COMPLETE';
        ok(!defined($conf), 'COMPLETE journal failure does not restore deleted config') if $stage eq 'COMPLETE';
    };
}

for my $case (
    ['raw SHA mismatch', sub { $_[0]->{raw_config} .= ' ' }],
    ['raw bytes do not match locked digest', sub {
        $_[0]->{raw_config} .= ' ';
        $_[0]->{config_sha256} = sha256_hex($_[0]->{raw_config});
    }],
    ['parsed SHA mismatch', sub { $_[0]->{parsed_config_sha256} = '0' x 64 }],
    ['omitted volume', sub { pop @{$_[0]->{volumes}} }],
    ['duplicate volume', sub { push @{$_[0]->{volumes}}, $_[0]->{volumes}->[0] }],
    ['swapped object ownership', sub {
        my $vols = $_[0]->{volumes};
        ($vols->[0]->{objects}, $vols->[1]->{objects}) = ($vols->[1]->{objects}, $vols->[0]->{objects});
    }],
    ['duplicate object ID', sub { push @{$_[0]->{volumes}->[0]->{objects}}, $_[0]->{volumes}->[0]->{objects}->[0] }],
    ['duplicate runtime ID', sub { push @{$_[0]->{volumes}->[0]->{runtime_ids}}, $_[0]->{volumes}->[0]->{runtime_ids}->[0] }],
    ['wrong root', sub { $_[0]->{volumes}->[0]->{root}->{uuid} = 'another-vg' }],
    ['missing object type', sub { delete $_[0]->{volumes}->[0]->{objects}->[0]->{type} }],
) {
    subtest "$case->[0] refuses before dispatch" => sub {
        my ($result, $error) = run_case(plan_mutate => $case->[1]);
        ok(!$result && length($error), 'invalid plan refused');
        is($native_calls, 0, 'no native effects');
    };
}

subtest 'strict parser output must equal the locked parsed config' => sub {
    no warnings 'redefine';
    local *PVE::QemuServer::parse_vm_config = sub { return { digest => $conf->{digest} }; };
    my ($result, $error) = run_case();
    ok(!$result, 'different parse refused');
    like($error, qr/does not parse to locked config/, 'explicit parse binding');
    is($native_calls, 0, 'no mutation');
};

subtest 'optical exclusions preserve cloud-init and exact snapshot vmstate' => sub {
    my ($result, $error) = run_case(mutate => sub {
        $conf->{ide0} = 'local:iso/test.iso,media=cdrom';
        $conf->{ide1} = 'none,media=cdrom';
        $conf->{scsi2} = '/dev/disk/by-id/host-device';
        $conf->{ide2} = 's:vm-101-cloudinit,media=cdrom';
        $conf->{snapshots}->{snapA} = { vmstate => 's:vm-101-state-snapA' };
        $present{'s:vm-101-cloudinit'} = 1;
        $present{'s:vm-101-state-snapA'} = 1;
    });
    is($error, '', 'qualified optical semantics accepted');
    is($result->{status}, 'COMPLETE', 'completed');
    my $volids = $records[0]->[1]->{plan}->{volids};
    is_deeply($volids, [qw(s:vm-101-cloudinit s:vm-101-disk-0 s:vm-101-disk-1 s:vm-101-state-snapA)],
        'cloud-init and vmstate included, ISO/none/passthrough excluded');
    ok(!grep(/free:(?:local:|none|\/dev)/, @events), 'excluded objects never freed');
};

for my $bad (qw(vm-101-state-.bad vm-101-state-_bad vm-102-state-snap vm-101-disk-0)) {
    subtest "invalid vmstate $bad" => sub {
        my ($result) = run_case(mutate => sub { $conf->{snapshots}->{s} = { vmstate => "s:$bad" } });
        ok(!$result, 'invalid exact vmstate refused');
        is($native_calls, 0, 'no effects');
    };
}

subtest 'fleecing and IPAM specializations are scoped verified no-ops' => sub {
    my $original = \&PVE::QemuConfig::cleanup_fleecing_images;
    my $ipam_original = \&PVE::QemuServer::Network::delete_ifaces_ipams_ips;
    my ($result, $error) = run_case();
    is($error, '', 'native destroy does not deadlock on nested cleanup');
    is($cleanup_original_calls, 0, 'effectful native cleanup never invoked');
    is($ipam_original_calls, 0, 'effectful native IPAM cleanup never invoked');
    is(\&PVE::QemuConfig::cleanup_fleecing_images, $original, 'original restored after call');
    is(\&PVE::QemuServer::Network::delete_ifaces_ipams_ips, $ipam_original,
        'IPAM original restored after call');
    my ($complete) = map { $_->[1] } grep { $_->[0] eq 'COMPLETE' } @records;
    is($complete->{evidence}->{fleecing_noop_calls}, 1, 'exactly one qualified no-op call');
    is($complete->{evidence}->{ipam_noop_calls}, 1, 'exactly one qualified IPAM no-op call');
};

subtest 'journal stages carry distinct observed evidence' => sub {
    run_case();
    my %by_stage = map { $_->[0] => $_->[1] } @records;
    is($by_stage{PREPARED}->{evidence}->{native}, 'NOT_DISPATCHED', 'prepared proves no dispatch yet');
    is($by_stage{DISPATCHED}->{evidence}->{native}, 'DISPATCH_INTENT', 'dispatch is intent not success');
    is($by_stage{STORAGE_ABSENT}->{evidence}->{storage_proof}->{status}, 'ABSENT', 'storage observation persisted');
    like($by_stage{STORAGE_ABSENT}->{evidence}->{zombie_config_sha256}, qr/^[a-f0-9]{64}$/, 'zombie digest recorded');
    is_deeply($by_stage{FINALIZING}->{next_effects}, [qw(remove_vm_access remove_vmfw_conf destroy_config)], 'remaining effects explicit');
    is($by_stage{COMPLETE}->{evidence}->{config_remove}, 'ABSENT_VERIFIED',
        'completion records verified absence, not merely a returned call');
    run_case(failure => 's:vm-101-disk-0');
    my $partial = $records[-1]->[1];
    is($partial->{evidence}->{storage_proof}->{status}, 'PRESENT', 'failed absence proof preserved');
    is($partial->{evidence}->{config_remove}, 'NOT_ATTEMPTED', 'failed path records no final removal');
};

for my $case (
    ['ACL', 'acl_after_fail'],
    ['firewall', 'firewall_after_fail'],
    ['config removal', 'config_after_fail'],
) {
    subtest "resumes after crash following $case->[0] effect without storage redispatch" => sub {
        my ($result, $error) = run_case($case->[1] => 1);
        ok(!$result, 'initial finalization is partial');
        like($error, qr/FINALIZING recovery required/, 'recoverable finalization outcome is explicit');
        is($native_calls, 1, 'storage/native destroy dispatched once before crash');
        $acl_after_fail = $firewall_after_fail = $config_after_fail = 0;
        my $resumed = eval { $last_executor->resume_finalizing(101, 'c' x 32) };
        is($@, '', 'resume succeeds from durable proof');
        is($resumed->{status}, 'COMPLETE', 'resume completes');
        is($resumed->{resumed}, 1, 'result identifies recovery path');
        is($native_calls, 1, 'resume never redispatches native/storage destroy');
        ok(!defined($conf), 'configuration is absent');
        is($acl_present, 0, 'ACL absence proven');
        is($firewall_present, 0, 'firewall absence proven');
    };
}

subtest 'resume refuses an unprovable finalization postcondition' => sub {
    my ($result) = run_case(acl_after_fail => 1);
    ok(!$result, 'initial outcome partial');
    $acl_after_fail = 0;
    $finalization_unknown = 1;
    my $resumed = eval { $last_executor->resume_finalizing(101, 'c' x 32) };
    ok(!$resumed, 'no success from unknown observer');
    like($@, qr/finalization observer unavailable|postcondition is UNKNOWN/, 'observer failure is fail-closed');
    is($native_calls, 1, 'no native/storage retry');
};

done_testing();
