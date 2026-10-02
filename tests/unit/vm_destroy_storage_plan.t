#!/usr/bin/perl
use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use JSON::PP;
use PVE::SharedLvmThinVMDestroyStoragePlan;

my $json = JSON::PP->new->canonical;
sub clone { $json->decode($json->encode($_[0])) }
sub fixture {
    my ($mode) = @_;
    my $doc = { schema => 'sharedlvmthin-destroy-inventory/v1', complete => 1,
        scope => { node => 'node-a', boot_id => 'boot-a', vg_uuid => 'vg-1',
            pv_uuid => 'pv-1', wwid => '60001', storage_ids => ['store'], config_sha256 => ('a' x 64) },
        roots => [], objects => [], runtimes => [] };
    for my $vmid (101, 202) {
        my $root = "root-$vmid";
        my $vol = "store:vm-$vmid-disk-0";
        my @kinds = $mode eq 'thin' ? qw(thin_pool thin_data thin_metadata thin_lv snapshot)
            : $mode eq 'lazy' ? qw(anchor lazy_data lazy_metadata) : qw(anchor generation snapshot);
        my @ids;
        for my $index (0 .. $#kinds) {
            my $kind = $kinds[$index];
            my $id = $index == 0 ? $root : "object-$vmid-$kind";
            push @ids, $id;
            push @{$doc->{objects}}, { id => $id, uuid => "uuid-$id", root_id => $root,
                kind => $kind, owner_vmid => "$vmid", namespace => "ns-$vmid",
                volume_id => $index == 0 || $kind =~ /^thin_(?:data|metadata)$/ ? '' : $vol,
                origin_uuid => $mode eq 'thin' && $kind eq 'snapshot' ? "uuid-object-$vmid-thin_lv" : '' };
        }
        my $runtime = "dm-$vmid";
        push @{$doc->{runtimes}}, { id => $runtime, dm_uuid => "DM-UUID-$vmid", root_id => $root };
        push @{$doc->{roots}}, { id => $root, mode => $mode,
            phase => $mode eq 'thin' ? 'OWNED_INACTIVE' : $mode eq 'lazy' ? 'LAZY_DORMANT' : 'MATERIALIZED',
            owner_vmid => "$vmid", namespace => "ns-$vmid", object_ids => \@ids,
            runtime_ids => [$runtime], volume_ids => [$vol] };
    }
    return $doc;
}
sub removed {
    my ($doc) = @_;
    my $after = clone($doc);
    $after->{roots} = [grep { $_->{owner_vmid} ne '101' } @{$after->{roots}}];
    $after->{objects} = [grep { $_->{root_id} ne 'root-101' } @{$after->{objects}}];
    $after->{runtimes} = [grep { $_->{root_id} ne 'root-101' } @{$after->{runtimes}}];
    return $after;
}
sub fail_like {
    my ($callback, $pattern, $label) = @_;
    my $ok = eval { $callback->(); 1 };
    ok(!$ok, "$label refused");
    like($@, $pattern, "$label reason");
}

for my $mode (qw(thin thick lazy)) {
    subtest "$mode complete plan/absence and foreign preservation" => sub {
        my $current = fixture($mode);
        my $original = clone($current);
        my $calls = 0;
        my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $calls++; $current });
        my $plan = $planner->plan(vmid => 101, volumes => ['store:vm-101-disk-0']);
        is($plan->{authority}, 'NONE', 'no mutation authorization');
        is($plan->{volumes}->[0]->{mode}, $mode, 'exact per-volume mode');
        is_deeply($plan->{root_ids}, ['root-101'], 'root identity pinned');
        is_deeply($current, $original, 'collector evidence never modified');
        fail_like(sub { $planner->observe($plan) }, qr/planned object remains/, 'still present');
        $current = removed($current);
        my $result = $planner->observe($plan);
        is($result->{result}, 'EXACT_PLANNED_STORAGE_ABSENT', 'exact disappearance accepted');
        is($result->{plan_sha256}, $plan->{plan_sha256}, 'result bound to plan');
        is($calls, 3, 'fresh collector called for every observation');
        $current->{objects} = [reverse @{$current->{objects}}];
        is($planner->observe($plan)->{result}, 'EXACT_PLANNED_STORAGE_ABSENT', 'ordering immaterial');
    };
}

subtest 'shared Thin pool closure includes every disk and snapshot' => sub {
    my $current = fixture('thin');
    my $root = $current->{roots}->[0];
    push @{$root->{volume_ids}}, 'store:vm-101-disk-1';
    push @{$root->{object_ids}}, 'second-thin-disk';
    push @{$current->{objects}}, { id => 'second-thin-disk', uuid => 'uuid-second', root_id => 'root-101',
        kind => 'thin_lv', owner_vmid => '101', namespace => 'ns-101',
        volume_id => 'store:vm-101-disk-1', origin_uuid => '' };
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $current });
    fail_like(sub { $planner->plan(vmid => 101, volumes => ['store:vm-101-disk-0']) }, qr/omits/, 'partial pool');
    my $plan = $planner->plan(vmid => 101, volumes => [qw(store:vm-101-disk-0 store:vm-101-disk-1)]);
    is(scalar(@{$plan->{volumes}}), 2, 'both disks represented');
    is(scalar(@{$plan->{volumes}->[0]->{persistent_objects}}), 6, 'pool, data, metadata, both disks, snapshot');
    $current->{objects}->[-1]->{owner_vmid} = '303';
    fail_like(sub { $planner->plan(vmid => 101, volumes => [qw(store:vm-101-disk-0 store:vm-101-disk-1)]) },
        qr/foreign root member/, 'foreign pool member');
};

for my $case (
    ['omitted object', sub { pop @{$_[0]->{objects}} }, qr/closure/],
    ['duplicate UUID', sub { $_[0]->{objects}->[1]->{uuid} = $_[0]->{objects}->[0]->{uuid} }, qr/duplicate.*UUID/],
    ['unknown collection', sub { $_[0]->{complete} = 0 }, qr/completeness/],
    ['unsupported phase', sub { $_[0]->{roots}->[0]->{phase} = 'LAZY_ACTIVE' }, qr/mode\/phase/],
    ['foreign snapshot origin', sub { $_[0]->{objects}->[4]->{origin_uuid} = 'uuid-foreign' }, qr/origin outside/],
) {
    subtest $case->[0] => sub {
        my $current = fixture('thin');
        $case->[1]->($current);
        my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $current });
        fail_like(sub { $planner->plan(vmid => 101, volumes => ['store:vm-101-disk-0']) },
            $case->[2], $case->[0]);
    };
}

for my $case (
    ['renamed UUID alias', sub { $_[0]->{objects}->[1]->{uuid} = 'uuid-object-101-generation' }, qr/UUID alias/],
    ['foreign drift', sub { $_[0]->{objects}->[1]->{uuid} = 'new-foreign-uuid' }, qr/foreign baseline drift/],
    ['DM UUID alias', sub { $_[0]->{runtimes}->[0]->{dm_uuid} = 'DM-UUID-101' }, qr/DM UUID alias/],
    ['new boot', sub { $_[0]->{scope}->{boot_id} = 'boot-b' }, qr/scope\/boot\/config drift/],
    ['name reuse', sub {
        $_[0]->{objects}->[1]->{id} = 'object-101-generation';
        $_[0]->{roots}->[0]->{object_ids}->[1] = 'object-101-generation';
    }, qr/name reused/],
    ['extra object', sub {
        my $extra = clone($_[0]->{objects}->[1]);
        $extra->{id} = 'extra'; $extra->{uuid} = 'extra-uuid';
        push @{$_[0]->{objects}}, $extra;
        push @{$_[0]->{roots}->[0]->{object_ids}}, 'extra';
    }, qr/foreign baseline drift or extra object/],
) {
    subtest $case->[0] => sub {
        my $current = fixture('thick');
        my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $current });
        my $plan = $planner->plan(vmid => 101, volumes => ['store:vm-101-disk-0']);
        $current = removed($current);
        $case->[1]->($current);
        fail_like(sub { $planner->observe($plan) }, $case->[2], $case->[0]);
    };
}

subtest 'plan tamper and collector errors never prove absence' => sub {
    my $current = fixture('lazy');
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $current });
    my $plan = $planner->plan(vmid => 101, volumes => ['store:vm-101-disk-0']);
    $plan->{volumes}->[0]->{persistent_objects} = [];
    fail_like(sub { $planner->observe($plan) }, qr/plan digest\/schema\/derived closure/, 'plan omission');
    my $broken = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { die 'timeout' });
    fail_like(sub { $broken->plan(vmid => 101, volumes => ['store:vm-101-disk-0']) }, qr/collector failed/, 'timeout');
};

subtest 'opaque foreign LV and pmspare baseline is lossless' => sub {
    my $current = fixture('thin');
    $current->{opaque_foreign_objects} = [
        {type=>'lvm-lv',name=>'lvol0_pmspare',uuid=>'spare-uuid',scope_uuid=>'vg-1',evidence_sha256=>'a' x 64},
        {type=>'lvm-lv',name=>'admin-data',uuid=>'admin-uuid',scope_uuid=>'vg-1',evidence_sha256=>'b' x 64},
    ];
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $current });
    my $plan = $planner->plan(vmid=>101,volumes=>['store:vm-101-disk-0']);
    is(scalar(@{$plan->{foreign_baseline}->{opaque_foreign_objects}}), 2, 'opaque entries included');
    ok(!exists($plan->{foreign_baseline}->{opaque_foreign_objects}->[0]->{owner_vmid}), 'no invented owner');
    $current = removed($current);
    is($planner->observe($plan)->{result}, 'EXACT_PLANNED_STORAGE_ABSENT', 'unchanged opaque namespace accepted');
    for my $field (qw(name uuid evidence_sha256)) {
        my $old = $current->{opaque_foreign_objects}->[0]->{$field};
        $current->{opaque_foreign_objects}->[0]->{$field} = $field eq 'evidence_sha256' ? 'c' x 64 : 'changed';
        fail_like(sub { $planner->observe($plan) }, qr/foreign baseline drift/, "opaque $field drift");
        $current->{opaque_foreign_objects}->[0]->{$field} = $old;
    }
};

subtest 'opaque foreign UUID alias or duplicate is refused' => sub {
    my $current = fixture('thick');
    $current->{opaque_foreign_objects} = [{type=>'lvm-lv',name=>'admin-data',
        uuid=>$current->{objects}->[0]->{uuid},scope_uuid=>'vg-1',evidence_sha256=>'b' x 64}];
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector=>sub{$current});
    fail_like(sub{$planner->plan(vmid=>101,volumes=>['store:vm-101-disk-0'])},qr/UUID alias/,'opaque-to-owned alias');
    $current->{opaque_foreign_objects}->[0]->{uuid}='opaque-uuid';
    push @{$current->{opaque_foreign_objects}}, clone($current->{opaque_foreign_objects}->[0]);
    fail_like(sub{$planner->plan(vmid=>101,volumes=>['store:vm-101-disk-0'])},qr/duplicate name/,'duplicate opaque');
};

done_testing();
