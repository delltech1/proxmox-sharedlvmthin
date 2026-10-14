#!/usr/bin/perl
# Read-only contract test against real installed or extracted PVE libraries.
# No VM, LVM, mapper, package or service mutation is performed.
use strict;
use warnings;
use FindBin;
BEGIN {
    my $source = "$FindBin::Bin/../usr/share/perl5";
    for my $variable (qw(SLT_TEST_STORAGE_ROOT SLT_TEST_COMMON_ROOT)) {
        next if !defined($ENV{$variable});
        my $path = "$ENV{$variable}/usr/share/perl5";
        die "missing extracted PVE library root: $path\n" if !-d $path;
    }
    my @candidate = map { "$ENV{$_}/usr/share/perl5" }
        grep { defined($ENV{$_}) } qw(SLT_TEST_COMMON_ROOT SLT_TEST_STORAGE_ROOT);
    @INC = ($source, @candidate, @INC);
}
use Test::More;
use Cwd qw(abs_path);
use PVE::Storage;
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $runtime = PVE::Storage::APIVER();
my $expected = $ENV{SLT_TEST_EXPECT_API};
die "SLT_TEST_EXPECT_API must be 14, 15 or 16\n"
    if !defined($expected) || $expected !~ /^(14|15|16)$/;
is($runtime, int($expected), 'real Storage API is the requested test target');
is($class->api(), $runtime, 'candidate plugin registers on exact real host API');
is(abs_path($INC{'PVE/Storage/Custom/SharedLvmThinPlugin.pm'}),
   abs_path("$FindBin::Bin/../usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"),
   'source plugin was loaded, not an installed older plugin');
if (defined($ENV{SLT_TEST_STORAGE_ROOT})) {
    for my $module (qw(PVE/Storage.pm PVE/Storage/Plugin.pm PVE/Storage/LVMPlugin.pm)) {
        is(abs_path($INC{$module}),
           abs_path("$ENV{SLT_TEST_STORAGE_ROOT}/usr/share/perl5/$module"),
           "exact extracted source loaded: $module");
    }
}
for my $hook (qw(type plugindata properties options status list_images alloc_image free_image
    activate_storage deactivate_storage activate_volume deactivate_volume volume_size_info
    volume_resize volume_snapshot volume_snapshot_delete volume_snapshot_rollback
    volume_snapshot_needs_fsfreeze volume_has_feature path parse_volname filesystem_path
    clone_image volume_rollback_is_possible)) {
    ok($class->can($hook), "required hook exists: $hook");
}
is_deeply($class->FORMAT_EXTENSION(), { raw => '' }, 'raw-only block format policy');
for my $name (qw(vm-100-disk-0 base-100-disk-0 vm-100-state-save.1 vm-100-fleece-0 vm-100-cloudinit)) {
    is($class->get_parsed_format($name), 'raw', "raw name parse: $name");
    is($class->volname_with_format($name, 'raw'), $name, "LV name retained: $name");
    if ($runtime == 16) {
        for my $strict (0, 1) {
            is($class->volname_for_format($name, 'raw', $strict), $name,
               "API16 format reconciliation preserves name ($strict): $name");
        }
    }
}
for my $format (qw(qcow2 vmdk subvol)) {
    my $ok = eval { $class->volname_with_format('vm-100-disk-0', $format); 1 };
    ok(!$ok, "non-raw naming refused: $format");
    if ($runtime == 16) {
        ok(!$class->is_valid_format($format), "API16 generic format policy refuses $format");
        $ok = eval { $class->volname_for_format('vm-100-disk-0', $format, 0); 1 };
        ok(!$ok, "API16 reconciliation refuses $format");
    }
}
for my $name ('../vm-100-disk-0', 'vm-100/disk-0', 'not-a-volume', '') {
    my $ok = eval { $class->volname_with_format($name, 'raw'); 1 };
    ok(!$ok, 'malformed raw name refused without storage mutation');
}
done_testing();
