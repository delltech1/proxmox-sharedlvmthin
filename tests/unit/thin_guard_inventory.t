#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinGuardInventory qw(build_guard_inventory dm_mapper_name dm_pool_uuid storage_identity_matches);

my $pin = { 'slt-expected-vg-uuid' => 'Vg-123', 'slt-expected-pv-uuid' => 'Pv-123', 'slt-expected-wwid' => '3600ab' };
ok(storage_identity_matches($pin, " Vg-123\n", " Pv-123 | /dev/mapper/3600ab\n"), 'exact scoped physical identity accepted');
ok(!storage_identity_matches($pin, 'vg-123', 'Pv-123|/dev/mapper/3600ab'), 'case-only VG mismatch refused');
ok(!storage_identity_matches($pin, 'Vg-123', 'pv-123|/dev/mapper/3600ab'), 'case-only PV mismatch refused');
ok(!storage_identity_matches($pin, 'Vg-123', "Pv-123|/dev/mapper/3600ab\nPv-123|/dev/sda"), 'ambiguous PV evidence refused');
ok(!storage_identity_matches($pin, 'Vg-123', 'Pv-123|/dev/sda'), 'incorrect backing device refused');

my $vg_uuid = 'Test02-T002-T002-T002-T002-T002-Test02';
my $lv_uuid = 'Fake01-F001-F001-F001-F001-F001-Fake01';
my $mapper = dm_mapper_name('example-vg-03', 'sltp-992700');
my $dm_uuid = dm_pool_uuid($vg_uuid, $lv_uuid);

is($mapper, 'example--vg--03-sltp--992700-tpool', 'DM hyphen escaping is exact');
is($dm_uuid, 'LVM-Test02T002T002T002T002T002Test02Fake01F001F001F001F001F001Fake01-tpool',
    'DM UUID is derived from exact LVM UUIDs');

sub evidence {
    return (
        local_node => 'node-b', quorum => 1,
        storages => {
            'example-storage-04' => {
                allocation_mode => 'thin', vg_name => 'example-vg-03', vg_uuid => $vg_uuid,
            },
        },
        lvs => [{
            vg_name => 'example-vg-03', lv_name => 'sltp-992700', lv_attr => 'twi-aotz--',
            lv_tags => 'pve-slt-owner-v1,pve-slt-sid-example-storage-04,pve-slt-owner-node-node-b,pve-slt-owner-epoch-40596e5c394d4b8796d196154eec9ec2',
            lv_uuid => $lv_uuid, vg_uuid => $vg_uuid,
        }],
        dm => {$mapper => $dm_uuid},
        qemu_refs => {992700 => ['example-storage-04:vm-992700-disk-0']},
    );
}

my $inventory = build_guard_inventory(evidence());
is($inventory->{schema}, 'BASTRIX_THIN_GUARD_INVENTORY_V1', 'schema is explicit');
is(scalar($inventory->{pools}->@*), 1, 'one exact pool collected');
ok($inventory->{pools}->[0]->{locally_active}, 'local mapper is recognized');
ok($inventory->{pools}->[0]->{qemu_reference_exact}, 'exact QEMU reference is recognized');

my @bad = (
    ['missing owner', sub { my %e = evidence(); $e{lvs}->[0]->{lv_tags} =~ s/,pve-slt-owner-node-node-b//; build_guard_inventory(%e) }],
    ['duplicate epoch', sub { my %e = evidence(); $e{lvs}->[0]->{lv_tags} .= ',pve-slt-owner-epoch-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'; build_guard_inventory(%e) }],
    ['wrong DM UUID', sub { my %e = evidence(); $e{dm}->{$mapper} = 'LVM-wrong-tpool'; build_guard_inventory(%e) }],
    ['wrong owner', sub { my %e = evidence(); $e{lvs}->[0]->{lv_tags} =~ s/node-b/node-a/; build_guard_inventory(%e) }],
    ['no QEMU reference', sub { my %e = evidence(); $e{qemu_refs} = {}; build_guard_inventory(%e) }],
    ['wrong VG UUID', sub { my %e = evidence(); $e{storages}->{'example-storage-04'}->{vg_uuid} = 'other'; build_guard_inventory(%e) }],
    ['unknown storage', sub { my %e = evidence(); $e{storages} = {}; build_guard_inventory(%e) }],
    ['duplicate pool', sub { my %e = evidence(); push $e{lvs}->@*, {%{$e{lvs}->[0]}}; build_guard_inventory(%e) }],
);
for my $case (@bad) {
    my ($name, $code) = @$case;
    my $ok = eval { $code->(); 1 };
    ok(!$ok, "$name fails closed");
}

my %inactive = evidence();
$inactive{dm} = {};
$inactive{qemu_refs} = {};
$inventory = build_guard_inventory(%inactive);
ok(!$inventory->{pools}->[0]->{locally_active}, 'inactive pool does not require local QEMU reference');

done_testing();
