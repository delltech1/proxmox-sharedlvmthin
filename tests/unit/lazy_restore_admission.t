use strict;
use warnings;
use Test::More;
use File::Temp qw(tempfile);
use lib 'tests/unit/lib', 'usr/share/perl5';
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $sid = 'lazy-test';
my $vmid = 910001;
my $vol = "vm-$vmid-disk-0";
my $cfg = { 'slt-allocation-mode' => 'thick-generations-lazy' };

sub fixture {
    my $storecfg = { ids => { $sid => $cfg } };
    my $rows = {
        scsi0 => {
            storeid => $sid, format => 'raw', virtdev => 'scsi0',
            devname => 'drive-scsi0', size => 1024 * 1024,
        },
    };
    my $opts = { storage => $sid, live => 0, override_conf => {} };
    my $lock_callback = sub { };
    my $archive = '/var/lib/vz/dump/test.vma.zst';
    my $restore = {
        sub => 'PVE::QemuServer::restore_vma_archive', package => 'PVE::QemuServer',
        file => '/usr/share/perl5/PVE/QemuServer.pm',
        args => [$archive, $vmid, 'root@pam', $opts, 'zst'],
    };
    my $restore_file = {
        sub => 'PVE::QemuServer::restore_file_archive', package => 'PVE::API2::Qemu',
        file => '/usr/share/perl5/PVE/API2/Qemu.pm',
        args => [$archive, $vmid, 'root@pam', $opts],
    };
    my $lock = {
        sub => 'PVE::AbstractConfig::lock_config_full', package => 'PVE::API2::Qemu',
        file => '/usr/share/perl5/PVE/AbstractConfig.pm',
        args => ['PVE::QemuConfig', $vmid, 1, $lock_callback],
    };
    my $allocator = {
        sub => 'PVE::QemuServer::__ANON__', package => 'PVE::QemuServer',
        file => '/usr/share/perl5/PVE/QemuServer.pm',
        args => [$storecfg, $rows, $vmid],
    };
    my $allocate = [
        { sub => "$class\::alloc_image", args => [$class, $sid, $cfg, $vmid, 'raw', undef, 1024] },
        { sub => 'PVE::Storage::vdisk_alloc', args => [$storecfg, $sid, $vmid, 'raw', undef, 1024] },
        $allocator, $restore, $restore_file, $lock,
    ];
    my $activate = [
        { sub => "$class\::activate_volume", args => [$class, $sid, $cfg, $vol, undef, {}, undef] },
        { sub => 'PVE::Storage::activate_volumes', args => [$storecfg, ["$sid:$vol"]] },
        $allocator, $restore, $restore_file, $lock,
    ];
    my $state = {
        phase => 'LAZY_DORMANT', publication => 1, generation => 0,
        bytes => 1024 * 1024, tx => '1' x 32,
        data_uuid => 'data-uuid', metadata_uuid => 'meta-uuid',
        sid => $sid, vol => $vol, op => 'ALLOC',
        owner_node => 'none', owner_boot => 'none', owner_epoch => 'none',
    };
    return {
        storecfg => $storecfg, rows => $rows, opts => $opts,
        allocate => $allocate, activate => $activate, state => $state,
        allocator => $allocator, restore => $restore,
        restore_file => $restore_file, lock => $lock,
    };
}

sub run_capability {
    my (%opts) = @_;
    my $f = fixture();
    my ($fh, $path) = tempfile();
    print {$fh} "#qmdump#map:scsi0:drive-scsi0:source:raw:\n";
    print {$fh} "scsi0: source:vm-1-disk-0,iothread=1,size=1M\n";
    close($fh);
    my $frames = $f->{allocate};
    my $now = 100;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_call_frames = sub { $frames };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { $now };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_restore_config_path = sub { $path };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_pve_reference_files = sub {
        return $opts{referenced} ? ['unexpected.conf'] : [];
    };
    my ($ok, $error, $again, $again_error);
    eval {
        $opts{before_prepare}->($f, $path) if $opts{before_prepare};
        my $context = $class->_lazy_prepare_restore_allocation($sid, $cfg, $vmid);
        $class->_lazy_record_restore_allocation($sid, $vol, $context, $f->{state});
        $f->{rows}->{scsi0}->{volid} = "$sid:$vol";
        $frames = $f->{activate};
        $now = 221 if $opts{expired};
        $opts{before_consume}->($f, $path) if $opts{before_consume};
        my $cap = $class->_lazy_consume_unreferenced_allocation(
            $sid, $cfg, $vol, $f->{state},
        );
        $class->_lazy_recheck_allocation_capability(
            $sid, $cfg, $vol, $f->{state}, $cap,
        );
        $ok = 1;
    };
    $error = $@;
    $again = eval {
        $class->_lazy_consume_unreferenced_allocation($sid, $cfg, $vol, $f->{state});
        1;
    };
    $again_error = $@;
    return ($ok, $error, $again, $again_error);
}

subtest 'native VMA restore gets one exact activation authority' => sub {
    my ($ok, $error, $again, $again_error) = run_capability();
    ok($ok, 'allocation and immediate activation match') or diag($error);
    ok(!$again, 'authority is one-shot');
    like($again_error, qr/absent/, 'replay has no authority');
};

subtest 'restore capability fails closed on caller and policy drift' => sub {
    my @cases = (
        ['live restore', before_prepare => sub { $_[0]->{opts}->{live} = 1 }],
        ['implicit target', before_prepare => sub { delete $_[0]->{opts}->{storage} }],
        ['pipe archive', before_prepare => sub {
            $_[0]->{restore}->{args}->[0] = $_[0]->{restore_file}->{args}->[0] = '-';
        }],
        ['drive override', before_prepare => sub { $_[0]->{opts}->{override_conf}->{scsi0} = 'x' }],
        ['wrong allocator package', before_prepare => sub { $_[0]->{allocator}->{package} = 'Other' }],
        ['wrong restore file', before_prepare => sub { $_[0]->{restore_file}->{file} = '/tmp/Qemu.pm' }],
        ['wrong lock timeout', before_prepare => sub { $_[0]->{lock}->{args}->[2] = 10 }],
        ['unsafe discard', before_prepare => sub {
            open(my $fh, '>', $_[1]) or die $!;
            print {$fh} "#qmdump#map:scsi0:drive-scsi0:source:raw:\n";
            print {$fh} "scsi0: source:vm-1-disk-0,discard=on\n";
            close($fh);
        }],
        ['expired', expired => 1],
        ['referenced', referenced => 1],
        ['row replacement', before_consume => sub {
            $_[0]->{rows}->{scsi0} = { %{$_[0]->{rows}->{scsi0}} };
        }],
        ['config drift', before_consume => sub {
            open(my $fh, '>>', $_[1]) or die $!;
            print {$fh} "description: changed\n";
            close($fh);
        }],
        ['wrong target', before_consume => sub {
            $_[0]->{rows}->{scsi0}->{volid} = "$sid:vm-$vmid-disk-9";
        }],
    );
    for my $case (@cases) {
        my ($name, $key, $value) = @$case;
        my ($ok, $error, $again) = run_capability($key => $value);
        ok(!$ok, "$name refused") or diag($error);
        ok(!$again, "$name cannot replay");
    }
};

subtest 'three equal-size rows advance only in lexical restore order' => sub {
    my $f = fixture();
    $f->{rows}->{scsi1} = { %{$f->{rows}->{scsi0}}, virtdev => 'scsi1', devname => 'drive-scsi1' };
    $f->{rows}->{scsi2} = { %{$f->{rows}->{scsi0}}, virtdev => 'scsi2', devname => 'drive-scsi2' };
    my ($fh, $path) = tempfile();
    print {$fh} "#qmdump#map:scsi0:drive-scsi0:source:raw:\nscsi0: source:vm-1-disk-0\n";
    close($fh);
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_call_frames = sub { $f->{allocate} };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_restore_config_path = sub { $path };
    my $context = $class->_lazy_prepare_restore_allocation($sid, $cfg, $vmid);
    is($context->{row_key}, 'scsi0', 'first unallocated lexical row selected');
    $f->{rows}->{scsi0}->{volid} = "$sid:$vol";
    open($fh, '>', $path) or die $!;
    print {$fh} "#qmdump#map:scsi1:drive-scsi1:source:raw:\nscsi1: source:vm-1-disk-1\n";
    close($fh);
    $context = $class->_lazy_prepare_restore_allocation($sid, $cfg, $vmid);
    is($context->{row_key}, 'scsi1', 'prior completed row retained and next row selected');
};

done_testing();
