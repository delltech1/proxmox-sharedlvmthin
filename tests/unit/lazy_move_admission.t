use strict;
use warnings;
use Test::More;
use lib 'tests/unit/lib', 'usr/share/perl5';
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $sid = 'lazy-test';
my $vol = 'vm-900001-disk-1';
my $cfg = { 'slt-allocation-mode' => 'thick-generations-lazy' };

sub fixture {
    my $storecfg = { ids => { $sid => $cfg } };
    my $source = { vmid => 900001, running => 0, drivename => 'scsi0', snapname => undef,
        drive => { file => 'eager-test:vm-900001-disk-0' }, size => 1024 * 1024 };
    my $dest = { vmid => 900001, drivename => 'scsi0', storage => $sid, format => 'raw' };
    my $vols = [];
    my $callback = sub { };
    my $clone = { sub => 'PVE::QemuServer::clone_disk', package => 'PVE::API2::Qemu',
        file => '/usr/share/perl5/PVE/API2/Qemu.pm',
        args => [$storecfg, $source, $dest, 1, $vols, undef, undef, undef, undef] };
    my @locks = (
        { sub => 'PVE::AbstractConfig::lock_config_full', args => ['PVE::QemuConfig', 900001, 10, $callback] },
        { sub => 'PVE::AbstractConfig::lock_config', args => ['PVE::QemuConfig', 900001, $callback] },
    );
    my $allocate = [
        { sub => $class . '::alloc_image', args => [$class, $sid, $cfg, 900001, 'raw', undef, 1024] },
        { sub => 'PVE::Storage::vdisk_alloc', args => [$storecfg, $sid, 900001, 'raw', undef, 1024] },
        $clone, @locks,
    ];
    my $activate = [
        { sub => $class . '::activate_volume', args => [$class, $sid, $cfg, $vol, undef, {}, undef] },
        { sub => 'PVE::Storage::activate_volumes', args => [$storecfg, ["$sid:$vol"]] },
        $clone, @locks,
    ];
    return { allocate => $allocate, activate => $activate, source => $source, dest => $dest, vols => $vols,
        state => { phase => 'LAZY_DORMANT', publication => 1, generation => 0, bytes => 1024 * 1024,
            tx => '1' x 32, data_uuid => 'data-uuid', metadata_uuid => 'meta-uuid',
            sid => $sid, vol => $vol, op => 'ALLOC', owner_node => 'none', owner_boot => 'none', owner_epoch => 'none' } };
}

sub run_capability {
    my (%opts) = @_;
    my $f = fixture();
    my $frames = $f->{allocate};
    my $now = 100;
    my $policy = 'source-config-sha';
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_call_frames = sub { $frames };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { $now };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_move_source_policy = sub { $policy };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_pve_reference_files = sub {
        return $opts{target_referenced} ? ['unexpected.conf'] : [];
    };
    my ($ok, $error, $second, $second_error);
    eval {
        my $context = $class->_lazy_prepare_move_allocation($sid, $cfg, 900001);
        $class->_lazy_record_move_allocation($sid, $vol, $context, $f->{state}) if !$opts{no_allocation};
        $frames = $f->{activate};
        push @{$f->{vols}}, "$sid:$vol";
        $f->{dest}->{volid} = "$sid:$vol";
        $now = 221 if $opts{expired};
        $policy = 'changed' if $opts{config_drift};
        $opts{mutate}->($f) if $opts{mutate};
        my $cap = $class->_lazy_consume_move_allocation($sid, $cfg, $vol, $f->{state});
        $class->_lazy_recheck_allocation_capability($sid, $cfg, $vol, $f->{state}, $cap);
        $ok = 1;
    };
    $error = $@;
    $second = eval { $class->_lazy_consume_move_allocation($sid, $cfg, $vol, $f->{state}); 1 };
    $second_error = $@;
    return ($ok, $error, $second, $second_error);
}

subtest 'fresh stopped full move has one ephemeral activation authority' => sub {
    my ($ok, $err, $again, $again_err) = run_capability();
    ok($ok, 'qualified allocation then exact activation succeeds') or diag($err);
    ok(!$again, 'capability is one shot');
    like($again_err, qr/absent/, 'cannot replay after consumption');
};

subtest 'failed or mismatched activation consumes authority without effects' => sub {
    for my $case (
        ['no allocation', { no_allocation => 1 }],
        ['expired', { expired => 1 }],
        ['config drift', { config_drift => 1 }],
        ['target referenced', { target_referenced => 1 }],
        ['wrong anchor tx', { mutate => sub { $_[0]->{state}->{tx} = '2' x 32 } }],
        ['wrong backing UUID', { mutate => sub { $_[0]->{state}->{data_uuid} = 'different' } }],
        ['already active', { mutate => sub { $_[0]->{state}->{phase} = 'LAZY_ACTIVE' } }],
        ['running', { mutate => sub { $_[0]->{source}->{running} = 123 } }],
        ['wrong destination', { mutate => sub { $_[0]->{dest}->{volid} = 'other:vm-900001-disk-9' } }],
        ['wrong list', { mutate => sub { push @{$_[0]->{vols}}, 'other:vm-900001-disk-9' } }],
        ['new clone object', { mutate => sub { $_[0]->{activate}->[2]->{args}->[1] = { %{$_[0]->{source}} } } }],
        ['direct activation', { mutate => sub { splice(@{$_[0]->{activate}}, 2) } }],
    ) {
        my ($ok, $err, $again) = run_capability(%{$case->[1]});
        ok(!$ok, "$case->[0] refused") or diag($err);
        ok(!$again, "$case->[0] has no replay authority");
    }
};

subtest 'unqualified copy modes refused before allocation admission' => sub {
    for my $case (
        ['running VM', sub { $_[0]->{source}->{running} = 1 }],
        ['other VM clone', sub { $_[0]->{dest}->{vmid} = 2 }],
        ['snapshot source', sub { $_[0]->{source}->{snapname} = 'snapshot' }],
        ['EFI', sub { $_[0]->{source}->{drivename} = $_[0]->{dest}->{drivename} = 'efidisk0' }],
        ['TPM', sub { $_[0]->{source}->{drivename} = $_[0]->{dest}->{drivename} = 'tpmstate0' }],
        ['cloud-init CD', sub { $_[0]->{source}->{drive}->{media} = 'cdrom' }],
        ['discard', sub { $_[0]->{source}->{drive}->{discard} = 'on' }],
        ['detect zeroes', sub { $_[0]->{source}->{drive}->{detect_zeroes} = 'unmap' }],
        ['API callsite drift', sub { $_[0]->{allocate}->[2]->{file} = '/other/Qemu.pm' }],
        ['signature drift', sub { push @{$_[0]->{allocate}->[2]->{args}}, undef }],
        ['missing VM lock', sub { pop @{$_[0]->{allocate}} }],
        ['foreign VM lock', sub { $_[0]->{allocate}->[4]->{args}->[1] = 2 }],
        ['pre-existing destination', sub { $_[0]->{dest}->{volid} = "$sid:$vol" }],
        ['linked clone', sub { $_[0]->{allocate}->[2]->{args}->[3] = 0 }],
    ) {
        my $f = fixture();
        $case->[1]->($f);
        my $ok = eval { $class->_lazy_move_context($sid, $cfg, 900001, undef, 'allocate', $f->{allocate}); 1 };
        ok(!$ok, "$case->[0] refused");
    }
};

subtest 'source config is exact, canonical and safe before publication' => sub {
    my $f = fixture();
    my $context = $class->_lazy_move_context($sid, $cfg, 900001, undef, 'allocate', $f->{allocate});
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thin_local_node = sub { 'node' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_pve_reference_files = sub { ['source.conf'] };
    my $text = "scsi0: eager-test:vm-900001-disk-0,size=1M\n";
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_owner_reference_text = sub { $text };
    ok($class->_lazy_move_source_policy($context), 'default ignore/zero-off source policy admitted');
    for my $bad (
        "scsi0: eager-test:vm-900001-disk-0,discard=on\n",
        "scsi0: eager-test:vm-900001-disk-0,detect_zeroes=1\n",
        "scsi1: eager-test:vm-900001-disk-0\n",
        "scsi0: eager-test:vm-900001-disk-0\nlock: migrate\n",
        "scsi0: eager-test:vm-900001-disk-0\n[PENDING]\ncores: 2\n",
        "scsi0: eager-test:vm-900001-disk-0\n[snap]\ncores: 2\n",
        "description: eager-test:vm-900001-disk-0\n",
    ) {
        $text = $bad;
        ok(!eval { $class->_lazy_move_source_policy($context); 1 }, 'unsafe/ambiguous source policy refused');
    }
};

done_testing();
