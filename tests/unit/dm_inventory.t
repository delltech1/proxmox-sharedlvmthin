use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib", "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::SharedLvmThinThick qw(mapper_name object_key);
my $class='PVE::Storage::Custom::SharedLvmThinPlugin';
my $rows=[];
no warnings 'redefine';
local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines=sub { $rows };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_namespace=sub { 'testvg' };
my $read=sub { PVE::Storage::Custom::SharedLvmThinPlugin::_dm_kernel_inventory() };
$rows=['foreign-map|','normal-map|LVM-abc'];
is_deeply($read->(),{'foreign-map'=>'','normal-map'=>'LVM-abc'},'UUID-less foreign device is present and tolerated');
for my $bad (['no-separator'],[' |uuid'],['map|uuid|extra'],['map|a','map|b']) {
    $rows=$bad; eval { $read->() }; like($@,qr/malformed|duplicate/,'ambiguous inventory rejected');
}
my $vol='vm-100-disk-0';
my $name=mapper_name('testvg',$vol);
my $uuid='SLT-TG2-'.object_key('testvg',$vol);
$rows=['foreign-map|',"$name|$uuid"];
ok($class->_thick_frontend_present({},$vol),'foreign UUID-less map does not break exact valid frontend');
$rows=["$name|"];
eval { $class->_thick_frontend_present({},$vol) };
like($@,qr/UUID mismatch/,'managed frontend without UUID remains fail-closed');
$rows=['foreign-map|'];
is($class->_thick_frontend_present({},$vol),0,'absence remains exact despite foreign map');

$rows=['test--vg-sltg--head|LVM-head'];
ok(PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists(
    '/dev/mapper/test--vg-sltg--head'),
    'mapper namespace existence comes from kernel inventory');
ok(PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists(
    '/dev/test-vg/sltg-head'),
    'LVM convenience namespace resolves to the exact kernel DM name');
$rows=[];
ok(!PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists(
    '/dev/mapper/test--vg-sltg--head'),
    'missing udev-independent kernel mapping is absent');
done_testing();
