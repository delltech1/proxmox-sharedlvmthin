#!/usr/bin/perl

use strict;
use warnings;

use lib '/usr/share/perl5';
use PVE::AbstractConfig ();
use PVE::QemuConfig ();
use PVE::QemuServer ();
use Test::More;

my $vmid = 999_997;
our $network_fixture_mtu;
my $real_get_nets_host_mtu = \&PVE::QemuServer::Network::get_nets_host_mtu;
my $qemu_config_module = $INC{'PVE/QemuConfig.pm'} // '';
die "loaded QemuConfig source is not identifiable\n"
    if $qemu_config_module eq '' || !-f $qemu_config_module;
open(my $source_fh, '<', $qemu_config_module)
    or die "cannot read $qemu_config_module: $!\n";
my $qemu_config_source;
{
    local $/;
    $qemu_config_source = <$source_fh>;
}
close($source_fh) or die "cannot close $qemu_config_module: $!\n";
my ($save_vmstate_source) = $qemu_config_source =~ m{
    sub\s+__snapshot_save_vmstate\s*\{(.*?)
    ^sub\s+__snapshot_activate_storages\s*\{
}msx;
die "cannot locate __snapshot_save_vmstate source\n" if !defined($save_vmstate_source);
my $allocation_position = index($save_vmstate_source, 'PVE::Storage::vdisk_alloc');
my @query_positions = map { index($save_vmstate_source, $_) } (
    'get_current_qemu_machine', 'get_cpu_from_running_vm', 'get_nets_host_mtu',
);
die "cannot classify vmstate allocation/query order\n"
    if $allocation_position < 0 || grep { $_ < 0 } @query_positions;
my $allocation_before_runtime_queries =
    (grep { $allocation_position < $_ } @query_positions) ? 1 : 0;

{
    package FixtureSnapshotClass;
    our @ISA = ('PVE::AbstractConfig');
    our (@effects, $deleted_force, $deleted_drivehash, $committed);

    sub reset {
        @effects = ();
        $deleted_force = undef;
        $deleted_drivehash = undef;
        $committed = 0;
    }
    sub __snapshot_prepare {
        push @effects, 'prepare';
        return { vmstate => 'state:vm-999997-state-snap', scsi0 => {}, scsi1 => {} };
    }
    sub load_config { return {} }
    sub __snapshot_check_freeze_needed { return (1, 0) }
    sub __snapshot_activate_storages { push @effects, 'activate-storages' }
    sub __snapshot_create_vol_snapshots_hook {
        my ($class, $vmid, $snap, $running, $hook) = @_;
        push @effects, "hook:$hook";
    }
    sub foreach_volume {
        my ($class, $snap, $callback) = @_;
        $callback->('scsi0', $snap->{scsi0});
        $callback->('scsi1', $snap->{scsi1});
    }
    sub __snapshot_create_vol_snapshot {
        my ($class, $vmid, $disk) = @_;
        push @effects, "snapshot:$disk";
        die "second-disk-injected\n" if $disk eq 'scsi1';
    }
    sub snapshot_delete {
        my ($class, $vmid, $snapname, $force, $drivehash) = @_;
        push @effects, 'cleanup';
        $deleted_force = $force;
        $deleted_drivehash = { %$drivehash };
    }
    sub __snapshot_commit {
        $committed++;
        push @effects, 'commit';
    }
}

sub save_vmstate_case {
    my ($name, $failure_point, $expected_error, $expected_allocations) = @_;

    my $allocations = 0;
    my $conf = {
        memory => 64,
        snapshots => { snap => {} },
    };
    my $storecfg = { ids => { state => { path => undef } } };

    no warnings 'redefine';
    local *PVE::QemuConfig::find_vmstate_storage = sub { return 'state' };
    local *PVE::QemuConfig::get_current_memory = sub { return 64 };
    local *PVE::Storage::storage_config = sub { return {} };
    local *PVE::Storage::vdisk_alloc = sub {
        $allocations++;
        return 'state:vm-999997-state-snap';
    };
    local *PVE::QemuServer::Machine::get_current_qemu_machine = sub {
        die "machine-query-injected\n" if $failure_point eq 'machine';
        return 'pc-q35-9.2';
    };
    local *PVE::QemuServer::Helpers::vm_running_locally = sub { return 4242 };
    local *PVE::QemuServer::CPUConfig::get_cpu_from_running_vm = sub {
        die "cpu-query-injected\n" if $failure_point eq 'cpu';
        return 'x86-64-v2-AES';
    };
    local *PVE::QemuServer::Network::get_nets_host_mtu = sub {
        die "mtu-query-injected\n" if $failure_point eq 'mtu';
        return '' if $failure_point eq 'empty-mtu';
        return 'net0=1500';
    };

    my $result = eval {
        PVE::QemuConfig->__snapshot_save_vmstate(
            $vmid, $conf, 'snap', $storecfg, 'state', 0,
        );
    };
    my $error = $@;

    is($allocations, $expected_allocations, "$name allocation count");
    if (defined($expected_error)) {
        like($error, qr/\Q$expected_error\E/, "$name preserves query error");
        ok(!defined($result), "$name has no state volume result");
        ok(!exists($conf->{snapshots}->{snap}->{vmstate}), "$name has no vmstate reference");
    } else {
        is($error, '', "$name completes");
        is($result, 'state:vm-999997-state-snap', "$name returns exact state volume");
        is($conf->{snapshots}->{snap}->{'running-nets-host-mtu'}, '',
            "$name records the upstream empty MTU value");
    }
}

my $failure_allocation_count = $allocation_before_runtime_queries ? 1 : 0;
save_vmstate_case(
    'machine-query-failure', 'machine', 'machine-query-injected', $failure_allocation_count,
);
save_vmstate_case(
    'cpu-query-failure', 'cpu', 'cpu-query-injected', $failure_allocation_count,
);
save_vmstate_case(
    'mtu-query-failure', 'mtu', 'mtu-query-injected', $failure_allocation_count,
);
save_vmstate_case('empty-mtu-roundtrip-prefix', 'empty-mtu', undef, 1);
diag('VMSTATE_QUERY_ORDER=' . (
    $allocation_before_runtime_queries ? 'ALLOCATE_BEFORE_QUERY' : 'QUERY_BEFORE_ALLOCATE'
));

sub save_vmstate_network_case {
    my ($name, $netconf, $answers, $expected_mtu, $expected_warnings) = @_;
    my $allocations = 0;
    my @warnings;
    my $conf = {
        memory => 64,
        %$netconf,
        snapshots => { snap => {} },
    };
    my $storecfg = { ids => { state => { path => undef } } };

    no warnings 'redefine';
    my $observed_mtu;
    {
        local *PVE::QemuServer::Network::log_warn = sub { push @warnings, $_[0] };
        local *PVE::QemuServer::Network::mon_cmd = sub {
            my ($observed_vmid, $command, %args) = @_;
            die "unexpected QMP command\n"
                if $observed_vmid != $vmid || $command ne 'qom-get';
            my ($net) = ($args{path} // '') =~ m{/machine/peripheral/(net\d+)$};
            die "unexpected QMP path\n" if !defined($net) || !exists($answers->{$net});
            my $answer = $answers->{$net};
            die "$answer->{error}\n" if exists($answer->{error});
            return $answer->{value};
        };
        $observed_mtu = $real_get_nets_host_mtu->($vmid, $conf);
    }
    local *PVE::QemuConfig::find_vmstate_storage = sub { return 'state' };
    local *PVE::QemuConfig::get_current_memory = sub { return 64 };
    local *PVE::Storage::storage_config = sub { return {} };
    local *PVE::Storage::vdisk_alloc = sub {
        $allocations++;
        return 'state:vm-999997-state-snap';
    };
    # Feed the exact output from the real helper into the real save-vmstate
    # branch.  This avoids re-entering unrelated /proc machine discovery while
    # preserving the actual Network.pm exception/undefined/partial semantics.
    $network_fixture_mtu = $observed_mtu;

    my $result = eval {
        PVE::QemuConfig->__snapshot_save_vmstate(
            $vmid, $conf, 'snap', $storecfg, 'state', 0,
        );
    };
    is($@, '', "$name QMP failure is contained by the real MTU helper");
    is($result, 'state:vm-999997-state-snap', "$name returns exact state volume");
    is($allocations, 1, "$name allocates exactly one vmstate volume");
    is(
        $conf->{snapshots}->{snap}->{'running-nets-host-mtu'},
        $expected_mtu,
        "$name records the exact real-helper MTU result",
    );
    is(scalar(@warnings), $expected_warnings, "$name warning count");
    my $config_name = "/qemu-server/$vmid.conf";
    my $raw = PVE::QemuServer::write_vm_config($config_name, $conf);
    my $parsed = PVE::QemuServer::parse_vm_config($config_name, $raw, 1);
    if ($expected_mtu eq '') {
        like(
            $parsed->{snapshots}->{snap}->{'running-nets-host-mtu'},
            qr/^\s*$/,
            "$name native roundtrip preserves unsafe empty MTU semantics",
        );
    } else {
        is(
            $parsed->{snapshots}->{snap}->{'running-nets-host-mtu'},
            $expected_mtu,
            "$name exact native serializer/parser preserves MTU evidence",
        );
    }
    is(
        $parsed->{snapshots}->{snap}->{vmstate},
        'state:vm-999997-state-snap',
        "$name exact native serializer/parser preserves vmstate identity",
    );
}

my $virtio0 = 'virtio=02:00:00:00:10:10,bridge=vmbr0';
my $virtio1 = 'virtio=02:00:00:00:10:11,bridge=vmbr1';
my $e1000 = 'e1000=02:00:00:00:10:12,bridge=vmbr0';
{
    # The direct fully-qualified calls in QemuConfig may retain the first CV
    # observed by this process.  Install stable fixture-wide runtime helpers;
    # only the MTU scalar changes between cases and comes from the real helper.
    no warnings 'redefine';
    *PVE::QemuServer::Machine::get_current_qemu_machine = sub { return 'pc-q35-9.2' };
    *PVE::QemuServer::Helpers::vm_running_locally = sub { return 4242 };
    *PVE::QemuServer::CPUConfig::get_cpu_from_running_vm = sub { return 'x86-64-v2-AES' };
    *PVE::QemuServer::Network::get_nets_host_mtu = sub { return $network_fixture_mtu };
}
save_vmstate_network_case('real-no-nic', {}, {}, '', 0);
save_vmstate_network_case('real-e1000-only', { net0 => $e1000 }, {}, '', 0);
save_vmstate_network_case(
    'real-virtio-qmp-error', { net0 => $virtio0 },
    { net0 => { error => 'injected-qmp-error' } }, '', 1,
);
save_vmstate_network_case(
    'real-virtio-undefined', { net0 => $virtio0 },
    { net0 => { value => undef } }, '', 1,
);
save_vmstate_network_case(
    'real-two-virtio-partial', { net0 => $virtio0, net1 => $virtio1 },
    { net0 => { value => 1500 }, net1 => { error => 'injected-qmp-error' } },
    'net0=1500', 1,
);
save_vmstate_network_case(
    'real-two-virtio-complete', { net0 => $virtio0, net1 => $virtio1 },
    { net0 => { value => 1500 }, net1 => { value => 9000 } },
    'net0=1500,net1=9000', 0,
);

{
    my @effects;
    my $snap = { vmstate => 'state:vm-999997-state-snap' };
    no warnings 'redefine';
    local *PVE::Storage::config = sub { return {} };
    local *PVE::QemuConfig::mon_cmd = sub {
        my ($observed_vmid, $command) = @_;
        is($observed_vmid, $vmid, 'after hook uses exact VM');
        push @effects, "qmp:$command";
        die "savevm-end-injected\n" if $command eq 'savevm-end';
        return {};
    };
    local *PVE::Storage::deactivate_volumes = sub {
        push @effects, 'deactivate';
    };
    my @warnings;
    local $SIG{__WARN__} = sub { push @warnings, @_ };

    my $error = eval {
        PVE::QemuConfig->__snapshot_create_vol_snapshots_hook(
            $vmid, $snap, 1, 'after',
        );
        return '';
    };
    is($@, '', 'after hook converts savevm-end exception to warning');
    is_deeply(\@effects, ['qmp:savevm-end'],
        'upstream savevm-end failure skips vmstate deactivation');
    like(join('', @warnings), qr/savevm-end-injected/,
        'upstream after hook exposes the original warning');
}

{
    FixtureSnapshotClass::reset();
    eval { FixtureSnapshotClass->snapshot_create($vmid, 'snap', 1, 'fixture') };
    like($@, qr/second-disk-injected/, 'caller preserves second-disk failure');
    is_deeply(
        \@FixtureSnapshotClass::effects,
        [
            'prepare', 'activate-storages', 'hook:before', 'snapshot:scsi0',
            'snapshot:scsi1', 'hook:after', 'hook:after-unfreeze', 'cleanup',
        ],
        'actual AbstractConfig caller finalizes QMP before prefix cleanup',
    );
    is($FixtureSnapshotClass::deleted_force, 1, 'partial snapshot cleanup is forced');
    is_deeply(
        $FixtureSnapshotClass::deleted_drivehash,
        { scsi0 => 1 },
        'cleanup receives only the disk whose snapshot completed',
    );
    is($FixtureSnapshotClass::committed, 0, 'failed multi-disk snapshot is not committed');
}

{
    my $agent_checks = 0;
    no warnings 'redefine';
    local *PVE::QemuConfig::__snapshot_check_running = sub { return 1 };
    local *PVE::QemuServer::Agent::guest_fs_freeze_applicable = sub {
        $agent_checks++;
        return 1;
    };
    my ($running, $freeze) = PVE::QemuConfig->__snapshot_check_freeze_needed(
        $vmid, { agent => 'enabled=1' }, 'state:vm-999997-state-snap',
    );
    is_deeply([$running, $freeze, $agent_checks], [1, 0, 0],
        'RAM snapshot does not invoke guest-agent filesystem freeze');

    ($running, $freeze) = PVE::QemuConfig->__snapshot_check_freeze_needed(
        $vmid, { agent => 'enabled=1' }, undef,
    );
    is_deeply([$running, $freeze, $agent_checks], [1, 1, 1],
        'running disk-only snapshot uses the applicable guest-agent freeze');
}

{
    my @calls;
    my @warnings;
    no warnings 'redefine';
    local *PVE::QemuServer::Agent::guest_fs_freeze = sub {
        push @calls, 'freeze';
        die "freeze-injected\n";
    };
    local *PVE::QemuServer::Agent::guest_fs_thaw = sub {
        push @calls, 'thaw';
        die "thaw-injected\n";
    };
    local $SIG{__WARN__} = sub { push @warnings, @_ };
    my $freeze_ok = eval { PVE::QemuConfig->__snapshot_freeze($vmid, 0); 1 };
    my $thaw_ok = eval { PVE::QemuConfig->__snapshot_freeze($vmid, 1); 1 };
    ok($freeze_ok && $thaw_ok, 'guest-agent freeze and thaw exceptions are warning-only upstream');
    is_deeply(\@calls, ['freeze', 'thaw'], 'freeze and thaw effects are both attempted');
    like(join('', @warnings), qr/freeze-injected.*thaw-injected/s,
        'both guest-agent errors remain visible as warnings');
}

{
    my @effects;
    my $snap = { vmstate => 'state:vm-999997-state-snap' };
    no warnings 'redefine';
    local *PVE::Storage::config = sub { return {} };
    local *PVE::Storage::path = sub { return '/dev/mock-state' };
    local *PVE::Storage::activate_volumes = sub { push @effects, 'activate' };
    local *PVE::Storage::parse_volume_id = sub { return 'state' };
    local *PVE::QemuMigrate::Helpers::set_migration_caps = sub {
        push @effects, 'migration-caps';
    };
    local *PVE::QemuConfig::mon_cmd = sub {
        my ($observed_vmid, $command) = @_;
        push @effects, "qmp:$command";
        return { status => 'failed', error => 'query-savevm-injected' }
            if $command eq 'query-savevm';
        return {};
    };

    eval {
        PVE::QemuConfig->__snapshot_create_vol_snapshots_hook(
            $vmid, $snap, 1, 'before',
        );
    };
    like($@, qr/query-savevm-injected/, 'before hook preserves QMP failure');
    is_deeply(
        \@effects,
        ['activate', 'migration-caps', 'qmp:savevm-start', 'qmp:query-savevm'],
        'before-hook failure prefix is exact and performs no local deactivation',
    );
}

{
    ok($qemu_config_module ne '' && -f $qemu_config_module,
        'loaded QemuConfig source is identifiable');
    my ($body) = $qemu_config_source =~ m{
        sub\s+__snapshot_create_vol_snapshots_hook\s*\{(.*?)
        ^sub\s+__snapshot_create_vol_snapshot\s*\{
    }msx;
    ok(defined($body), 'snapshot hook source boundary is found');
    like($body // '', qr/after-(?:un)?freeze.*?for\s*\(;;\)/s,
        'finalization hook contains an unbounded polling loop');
    unlike($body // '', qr/after-(?:un)?freeze.*?(?:deadline|timeout)/s,
        'finalization hook has no explicit deadline in this upstream tuple');
    diag("QEMU_CONFIG_SOURCE=$qemu_config_module");
}

done_testing();
