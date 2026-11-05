#!/usr/bin/perl
use strict;
use warnings;
use FindBin;
use File::Temp qw(tempdir);
use Fcntl qw(:DEFAULT :flock);
use lib "$FindBin::Bin/lib";
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::Storage::Custom::SharedLvmThinPlugin;

# Run only inside a disposable mount namespace with a private /run/lock.
# Mapper and LV effects are mocked; neither a guest nor real storage is used.
plan skip_all => 'requires explicit private lock namespace'
    unless $ENV{SLT_TEST_PRIVATE_LOCK_NAMESPACE} && $> == 0;
open(my $mounts, '<', '/proc/self/mountinfo') or die "mountinfo: $!";
my @lock_mount = grep { /\s\/run\/lock\s/ && / - tmpfs / } <$mounts>;
close($mounts);
die "private mount namespace required\n"
    if readlink('/proc/self/ns/mnt') eq readlink('/proc/1/ns/mnt');
die "private tmpfs lock mount required\n" unless @lock_mount >= 1;
open(my $host_mounts, '<', '/proc/1/mountinfo') or die "host mountinfo: $!";
my @host_lock_mount = grep { /\s\/run\/lock\s/ } <$host_mounts>;
close($host_mounts);
my ($test_mount_id) = $lock_mount[-1] =~ /^(\d+) /;
my ($host_mount_id) = @host_lock_mount ? $host_lock_mount[-1] =~ /^(\d+) / : ();
die "lock mount must be independently mounted\n"
    if !defined($test_mount_id) || (defined($host_mount_id) && $test_mount_id eq $host_mount_id);
my @test_dir = stat('/run/lock');
my @host_dir = stat('/proc/1/root/run/lock');
die "cannot prove lock directory isolation\n" unless @test_dir && @host_dir;
die "lock directory shares host filesystem and inode\n"
    if $test_dir[0] == $host_dir[0] && $test_dir[1] == $host_dir[1];

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $dir = tempdir(CLEANUP => 1);
my $maintenance = "$dir/maintenance";
my $guard = "$dir/update-guard";
mkdir($_, 0700) or die "mkdir: $!" for ($maintenance, $guard);
my $store = 'test-lazy';
my $volume = 'vm-901-disk-0';
my $cfg = { 'slt-allocation-mode' => 'thick-generations-lazy',
            'slt-vgname' => 'testvg', 'slt-expected-wwid' => 'testwwid' };
my $state = { v => 6, phase => 'LAZY_ACTIVE', owner_node => 'node',
    owner_boot => 'boot', owner_epoch => 'epoch', tx => 'a' x 32,
    bytes => 1048576, region => 2048, metadata => 'meta', head => 'data',
    data_uuid => 'dataid', metadata_uuid => 'metaid' };
my @effects;
{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub { return ($state, {}, 'anchor') };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_assert_no_active_storage_worker = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_local_identity = sub { return ('node', 'boot') };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_runtime_names = sub { return ('zero', 'clone', 'front') };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_hydration_tuning = sub { return (1, 1) };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_namespace = sub { 'namespace' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_mapper_identity = sub { '1:1' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_active_lv_identity = sub { '2:2' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_command_deadline = sub { 5 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_verify_private_io_guard = sub { '3:3' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_front_pivot_state = sub { 'CLONE_ACTIVE' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_verify_public_io_guard = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_clone_status = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_close_timeout = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_frontend_open_count = sub { 0 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub { push @effects, 'flush' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_remove_mapper_exact = sub { push @effects, $_[2]->[-1] };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_deactivate_exact_lvs = sub { push @effects, 'lvs' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub { $_[3]->() };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_transition_anchor = sub { push @effects, 'anchor' };
    for my $marker ('package-transition.json', 'runtime-qualification-pending.json') {
        open(my $m, '>', "$guard/$marker") or die "marker: $!";
        print {$m} "{}\n"; close($m);
        my $guard_ok = eval { $class->_assert_package_operations_released('volume activation', $maintenance, $guard); 1 };
        ok(!$guard_ok, "$marker refuses guarded operations");
        my $path = "/run/lock/pve-sharedlvmthin-lazy-$store-$volume.lock";
        sysopen(my $lock, $path, O_CREAT | O_RDWR, 0600) or die "lock: $!";
        flock($lock, LOCK_EX | LOCK_NB) or die "flock: $!";
        @effects = ();
        my $ok = eval { $class->deactivate_volume($store, $cfg, $volume, undef, undef); 1 };
        ok(!$ok, "$marker with executor held refuses deactivation");
        like($@, qr/another Lazy Thick executor is still live/, 'actual lock refusal');
        is_deeply(\@effects, [], 'no mocked storage effect while lock held');
        flock($lock, LOCK_UN) or die "unlock: $!";
        ok($class->deactivate_volume($store, $cfg, $volume, undef, undef),
            "$marker with executor free permits simulated teardown");
        is_deeply(\@effects, ['flush', 'front', 'clone', 'zero', 'lvs', 'anchor'],
            'mocked effects occurred once in expected order');
        ok(-f "$guard/$marker", 'marker remains present');
        close($lock); unlink("$guard/$marker") or die "unlink fixture: $!";
    }
}
done_testing();
