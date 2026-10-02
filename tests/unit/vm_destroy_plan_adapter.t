use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use JSON::PP;
use Digest::SHA qw(sha1_hex);
use PVE::SharedLvmThinVMDestroyStoragePlan;
use PVE::SharedLvmThinVMDestroyPlanAdapter;
use PVE::SharedLvmThinVMDestroy;

my $adapter = 'PVE::SharedLvmThinVMDestroyPlanAdapter';
my $json = JSON::PP->new->canonical;
our $locked_config;
{ package PVE::QemuServer; sub parse_vm_config { return $main::locked_config; } }
sub copy { $json->decode($json->encode($_[0])) }

sub fixture {
    my ($mode) = @_;
    my $vol = 'store:vm-101-disk-0';
    my $doc = { schema => 'sharedlvmthin-destroy-inventory/v1', complete => 1,
        scope => { node => 'node-a', boot_id => 'boot-a', vg_uuid => 'vg-uuid', pv_uuid => 'pv-uuid',
            wwid => '60001', storage_ids => ['store'], config_sha256 => 'a' x 64 },
        roots => [], objects => [], runtimes => [] };
    my @kinds = $mode eq 'thin' ? qw(thin_pool thin_data thin_metadata thin_lv)
        : $mode eq 'lazy' ? qw(anchor lazy_data lazy_metadata) : qw(anchor generation);
    my $bindings = { objects => {}, runtimes => {} };
    for my $i (0 .. $#kinds) {
        my $id = "opaque:$i";
        push @{$doc->{objects}}, { id => $id, uuid => "uuid-$i", root_id => 'opaque:0',
            kind => $kinds[$i], owner_vmid => '101', namespace => 'ns',
            volume_id => $i == $#kinds ? $vol : '', origin_uuid => '' };
        $bindings->{objects}->{$id} = { name => "real-lv-$i", uuid => "uuid-$i" };
    }
    push @{$doc->{runtimes}}, { id => 'opaque:dm', dm_uuid => 'DM-UUID', root_id => 'opaque:0' };
    $bindings->{runtimes}->{'opaque:dm'} = { name => 'real-mapper', uuid => 'DM-UUID' };
    push @{$doc->{roots}}, { id => 'opaque:0', mode => $mode, owner_vmid => '101', namespace => 'ns',
        phase => $mode eq 'thin' ? 'OWNED_INACTIVE' : $mode eq 'lazy' ? 'LAZY_DORMANT' : 'MATERIALIZED',
        object_ids => [map { $_->{id} } @{$doc->{objects}}], runtime_ids => ['opaque:dm'], volume_ids => [$vol] };
    my $current = copy($doc);
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $current });
    my $plan = $planner->plan(vmid => 101, volumes => [$vol]);
    my $raw = "scsi0: $vol\n";
    $locked_config = { scsi0 => $vol, digest => sha1_hex($raw) };
    my %args = (storage_plan => $plan, raw_config => $raw, locked_config => copy($locked_config),
        expected_scope => copy($doc->{scope}), bindings => $bindings,
        storecfg => { ids => { store => { type => 'sharedlvmthin', 'slt-vgname' => 'vg',
            'slt-expected-vg-uuid' => 'vg-uuid', 'slt-expected-pv-uuid' => 'pv-uuid', 'slt-expected-wwid' => '60001',
            'slt-allocation-mode' => $mode eq 'thin' ? 'thin' : $mode eq 'lazy' ? 'thick-generations-lazy' : 'thick-generations' } } });
    return (\%args, $planner, \$current);
}

for my $mode (qw(thin thick lazy)) {
    subtest "$mode adapter composes planner and executor" => sub {
        my ($args, $planner, $current) = fixture($mode);
        my $adapted = $adapter->adapt_plan(%$args);
        is($adapted->{authority}, 'NONE', 'diagnostic only');
        is($adapted->{volumes}->[0]->{objects}->[0]->{name}, 'real-lv-0', 'name is supplied, never guessed from opaque ID');
        my $ids = PVE::SharedLvmThinVMDestroy::_validate_plan(101, $locked_config,
            $args->{storecfg}, ['store:vm-101-disk-0'], $adapted);
        is_deeply($ids, $adapted->{object_ids}, 'executor sees the exact complete typed identity set');
        my $ok = eval { $adapter->observe($planner, $adapted); 1 };
        ok(!$ok, 'present inventory never proves absence');
        ${$current}->{$_} = [] for qw(roots objects runtimes);
        my $proof = $adapter->observe($planner, $adapted);
        is($proof->{authority}, 'NONE', 'observation still carries no authority');
        is($proof->{status}, 'ABSENT', 'exact planner absence translated');
        is_deeply($proof->{object_ids}, $ids, 'persistent and runtime closure preserved');
        $adapted->{volumes}->[0]->{objects}->[0]->{name} = 'forged';
        $ok = eval { $adapter->observe($planner, $adapted); 1 };
        ok(!$ok, 'adapted plan tamper refused');
    };
    subtest "$mode single-root explicit v2 remains exact" => sub {
        my ($args) = fixture($mode);
        $args->{plan_version} = 2;
        my $adapted = $adapter->adapt_plan(%$args);
        my $ids = PVE::SharedLvmThinVMDestroy::_validate_plan(101, $locked_config,
            $args->{storecfg}, ['store:vm-101-disk-0'], $adapted);
        is_deeply($ids, $adapted->{object_ids}, 'same complete set in normalized form');
        is(scalar(@{$adapted->{roots}}), 1, 'one root');
    };
}

for my $case (
    ['authority promotion', sub { $_[0]->{storage_plan}->{authority} = 'GRANT' }],
    ['raw mismatch', sub { $_[0]->{raw_config} .= 'changed' }],
    ['scope drift', sub { $_[0]->{expected_scope}->{boot_id} = 'other' }],
    ['VG mismatch', sub { $_[0]->{storecfg}->{ids}->{store}->{'slt-expected-vg-uuid'} = 'other' }],
    ['missing name binding', sub { delete $_[0]->{bindings}->{objects}->{'opaque:0'} }],
    ['binding UUID mismatch', sub { $_[0]->{bindings}->{objects}->{'opaque:0'}->{uuid} = 'wrong' }],
    ['extra binding', sub { $_[0]->{bindings}->{objects}->{extra} = { name => 'extra', uuid => 'extra' } }],
    ['duplicate physical name', sub { $_[0]->{bindings}->{objects}->{'opaque:1'}->{name} = 'real-lv-0' }],
    ['source closure tamper', sub { pop @{$_[0]->{storage_plan}->{volumes}->[0]->{persistent_objects}} }],
) {
    subtest $case->[0] => sub {
        my ($args) = fixture('thin');
        $case->[1]->($args);
        my $ok = eval { $adapter->adapt_plan(%$args); 1 };
        ok(!$ok, 'refused');
        like($@, qr/UNKNOWN/, 'explicit unknown, never mutation authority');
    };
}

sub shared_fixture {
    my ($args) = fixture('thin');
    my $doc = $args->{storage_plan}->{before};
    push @{$doc->{roots}->[0]->{volume_ids}}, 'store:vm-101-disk-1';
    push @{$doc->{roots}->[0]->{object_ids}}, 'second';
    push @{$doc->{objects}}, { id => 'second', uuid => 'second-uuid', root_id => 'opaque:0', kind => 'thin_lv',
        owner_vmid => '101', namespace => 'ns', volume_id => 'store:vm-101-disk-1', origin_uuid => '' };
    push @{$doc->{roots}->[0]->{object_ids}}, 'second-snapshot';
    push @{$doc->{objects}}, { id => 'second-snapshot', uuid => 'second-snapshot-uuid', root_id => 'opaque:0', kind => 'snapshot',
        owner_vmid => '101', namespace => 'ns', volume_id => 'store:vm-101-disk-1', origin_uuid => 'second-uuid' };
    $args->{bindings}->{objects}->{second} = { name => 'vm-101-disk-1', uuid => 'second-uuid' };
    $args->{bindings}->{objects}->{'second-snapshot'} = { name => 'snap_vm-101-disk-1_snapA', uuid => 'second-snapshot-uuid' };
    $args->{raw_config} .= "scsi1: store:vm-101-disk-1\n";
    $locked_config->{scsi1} = 'store:vm-101-disk-1';
    $locked_config->{digest} = sha1_hex($args->{raw_config});
    $args->{locked_config} = copy($locked_config);
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $doc });
    $args->{storage_plan} = $planner->plan(vmid => 101, volumes => [qw(store:vm-101-disk-0 store:vm-101-disk-1)]);
    return ($args, $planner, \$doc);
}

subtest 'v1 still refuses shared Thin root' => sub {
    my ($args) = shared_fixture();
    my $ok = eval { $adapter->adapt_plan(%$args); 1 };
    ok(!$ok, 'shared root is not falsely assigned to a single volume');
    like($@, qr/shared multi-volume root unsupported/, 'explicit bounded limitation');
};

subtest 'v2 normalizes two Thin leaves plus snapshot and pool structure' => sub {
    my ($args, $planner, $current) = shared_fixture();
    $args->{plan_version} = 2;
    my $adapted = $adapter->adapt_plan(%$args);
    is($adapted->{schema}, 'sharedlvmthin-vm-destroy-executor-plan/v2', 'explicit v2');
    is($adapted->{authority}, 'NONE', 'no authority promotion');
    is(scalar(@{$adapted->{roots}}), 1, 'one shared root');
    is(scalar(@{$adapted->{roots}->[0]->{objects}}), 6, 'each pool/disk/snapshot object stored once');
    my @structure = grep { $_->{volid} eq '' } @{$adapted->{roots}->[0]->{objects}};
    is(scalar(@structure), 3, 'pool/data/meta retain root-only ownership');
    is(scalar(@{$adapted->{volumes}->[0]->{object_ids}}), 1, 'first disk has one leaf');
    is(scalar(@{$adapted->{volumes}->[1]->{object_ids}}), 2, 'second disk owns its exact snapshot leaf');
    ok(!exists($adapted->{roots}->[0]->{runtime_ids}->[0]->{volid}), 'root runtime has no invented per-volume owner');
    my $ids = PVE::SharedLvmThinVMDestroy::_validate_plan(101, $locked_config,
        $args->{storecfg}, [qw(store:vm-101-disk-0 store:vm-101-disk-1)], $adapted);
    is(scalar(@$ids), 7, 'six persistent and one runtime IDs checked once');
    my $ok = eval { $adapter->observe($planner, $adapted); 1 };
    ok(!$ok, 'still-present shared pool cannot complete');
    ${$current}->{$_} = [] for qw(roots objects runtimes);
    my $proof = $adapter->observe($planner, $adapted);
    is_deeply($proof->{object_ids}, $ids, 'whole-root absence composes with executor');
    is($proof->{authority}, 'NONE', 'observation remains diagnostic');
};

for my $case (
    ['omitted structural member', sub { pop @{$_[0]->{roots}->[0]->{objects}} }],
    ['duplicate shared object', sub { push @{$_[0]->{roots}->[0]->{objects}}, $_[0]->{roots}->[0]->{objects}->[0] }],
    ['invented structural owner', sub { $_[0]->{roots}->[0]->{objects}->[0]->{volid} = 'store:vm-101-disk-0' }],
    ['swapped leaves', sub { my $v = $_[0]->{volumes}; ($v->[0]->{object_ids}, $v->[1]->{object_ids}) = ($v->[1]->{object_ids}, $v->[0]->{object_ids}); }],
    ['omitted leaf volume', sub { pop @{$_[0]->{volumes}} }],
    ['duplicate root', sub { push @{$_[0]->{roots}}, $_[0]->{roots}->[0] }],
    ['runtime assigned to leaf', sub { $_[0]->{roots}->[0]->{runtime_ids}->[0]->{volid} = 'store:vm-101-disk-0' }],
    ['authority promotion', sub { $_[0]->{authority} = 'GRANT' }],
    ['unknown schema', sub { $_[0]->{schema} = 'sharedlvmthin-vm-destroy-executor-plan/v99' }],
) {
    subtest "v2 $case->[0]" => sub {
        my ($args) = shared_fixture();
        $args->{plan_version} = 2;
        my $adapted = $adapter->adapt_plan(%$args);
        $case->[1]->($adapted);
        my $ok = eval { PVE::SharedLvmThinVMDestroy::_validate_plan(101, $locked_config,
            $args->{storecfg}, [qw(store:vm-101-disk-0 store:vm-101-disk-1)], $adapted); 1 };
        ok(!$ok, 'executor refuses, no effect path invoked');
    };
}

subtest 'v2 physical root must match current locked storage configuration' => sub {
    my ($args) = shared_fixture();
    $args->{plan_version} = 2;
    my $adapted = $adapter->adapt_plan(%$args);
    my $changed = copy($args->{storecfg});
    $changed->{ids}->{store}->{'slt-expected-pv-uuid'} = 'other-pv';
    my $ok = eval { PVE::SharedLvmThinVMDestroy::_validate_plan(101, $locked_config,
        $changed, $adapted->{volids}, $adapted); 1 };
    ok(!$ok, 'current PV drift refused despite intact historical plan digest');
};

done_testing();
