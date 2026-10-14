use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib", "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::SharedLvmThinThick qw(mapper_name object_key);
my $class='PVE::Storage::Custom::SharedLvmThinPlugin';
my $rows=[];
my $command_lines=\&PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines;
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
subtest 'completed enumeration removal is reobserved without partial rows' => sub {
    my $calls=0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines=sub {
        my ($argv,$error,$observation)=@_;
        $calls++;
        if ($calls==1) {
            $observation->{exitstatus}=1;
            $observation->{stderr}=['Device does not exist.'];
            die "completed read failed\n";
        }
        return ['survivor|exact-uuid'];
    };
    is_deeply($read->(),{'survivor'=>'exact-uuid'},'only successful fresh inventory is authoritative');
    is($calls,2,'exact completed ENODEV retried once');
};

subtest 'native dmsetup terminal summary after ENODEV is reobserved' => sub {
    my $calls=0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines=sub {
        my ($argv,$error,$observation)=@_;
        if (++$calls==1) {
            $observation->{exitstatus}=1;
            $observation->{stderr}=['Device does not exist.','Command failed.'];
            die "completed read failed\n";
        }
        return ['survivor|exact-uuid'];
    };
    is_deeply($read->(),{'survivor'=>'exact-uuid'},'native exact two-line diagnostic reobserved');
    is($calls,2,'fresh read only after confirmed ENODEV');
};

subtest 'ambiguous or non-ENODEV read failures never retry' => sub {
    for my $case (
        [124,['Device does not exist.']],
        [1,['Permission denied']],
        [1,['Command failed.']],
        [1,['Device does not exist.','other error']],
        [undef,[]],
    ) {
        my $calls=0;
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines=sub {
            my ($argv,$error,$observation)=@_;
            $calls++;
            $observation->{exitstatus}=$case->[0];
            $observation->{stderr}=$case->[1];
            die "original read failure\n";
        };
        eval { $read->() };
        like($@,qr/original read failure/,'original unknown/failure remains closed');
        is($calls,1,'no retry of timeout, signal, permission or mixed diagnostics');
    }
};

subtest 'persistent removal churn exhausts bounded reads, never proves absence' => sub {
    my $calls=0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines=sub {
        my ($argv,$error,$observation)=@_;
        $calls++;
        $observation->{exitstatus}=1;
        $observation->{stderr}=['Device does not exist.'];
        die "persistent churn\n";
    };
    eval { $read->() };
    like($@,qr/persistent churn/,'churn remains UNKNOWN rather than empty inventory');
    is($calls,3,'only three completed read attempts permitted');
};

subtest 'optional completed-read observation captures exact exit and stderr' => sub {
    local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command=sub {
        my ($argv,%opts)=@_;
        ok($opts{noerr},'status is collected instead of guessed from exception text');
        ok(!exists($opts{errmsg}),'errmsg cannot override PVE noerr terminal-status collection');
        $opts{outfunc}->('partial|must-not-escape');
        $opts{errfunc}->('Device does not exist.');
        return 1;
    };
    my $observation={};
    eval { $command_lines->(['/sbin/dmsetup','info'],'probe failed',$observation) };
    like($@,qr/read-only probe exited with status 1/,'failed read never returns partial rows');
    is_deeply($observation,{exitstatus=>1,stderr=>['Device does not exist.']},'exact terminal evidence captured');
};
done_testing();
