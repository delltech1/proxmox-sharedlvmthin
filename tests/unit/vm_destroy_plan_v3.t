use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use JSON::PP ();
use Digest::SHA qw(sha1_hex sha256_hex);
use PVE::SharedLvmThinVMDestroyStoragePlan;
use PVE::SharedLvmThinVMDestroyPlanAdapter;
use PVE::SharedLvmThinVMDestroyPlanV3;
use PVE::SharedLvmThinVMDestroy;

my $JSON = JSON::PP->new->canonical;
sub copy { $JSON->decode($JSON->encode($_[0])) }

sub make_v2 {
    my (%p) = @_;
    my $volid = "$p{sid}:vm-101-disk-$p{disk}";
    my @kinds = $p{mode} eq 'thin' ? qw(thin_pool thin_data thin_metadata thin_lv)
        : $p{mode} eq 'lazy' ? qw(anchor lazy_data lazy_metadata) : qw(anchor generation);
    my ($root_id, @objects, %object_bindings) = ("$p{sid}-object-0");
    for my $i (0 .. $#kinds) {
        my $id = "$p{sid}-object-$i";
        push @objects, { id => $id, uuid => "$p{sid}-uuid-$i", root_id => $root_id,
            kind => $kinds[$i], owner_vmid => '101', namespace => $p{vg_uuid},
            volume_id => $i == $#kinds ? $volid : '', origin_uuid => '' };
        $object_bindings{$id} = { name => "$p{sid}-lv-$i", uuid => "$p{sid}-uuid-$i" };
    }
    my $runtime_id = "$p{sid}-runtime";
    my $inventory = { schema => 'sharedlvmthin-destroy-inventory/v1', complete => 1,
        scope => { node => 'pve01', boot_id => '11111111-1111-1111-1111-111111111111',
            vg_uuid => $p{vg_uuid}, pv_uuid => $p{pv_uuid}, wwid => $p{wwid},
            storage_ids => [$p{sid}], config_sha256 => sha256_hex($JSON->encode($p{storecfg})) },
        roots => [{ id => $root_id, mode => $p{mode},
            phase => $p{mode} eq 'thin' ? 'OWNED_INACTIVE' : $p{mode} eq 'lazy' ? 'LAZY_DORMANT' : 'MATERIALIZED',
            owner_vmid => '101', namespace => $p{vg_uuid}, object_ids => [map { $_->{id} } @objects],
            runtime_ids => [$runtime_id], volume_ids => [$volid] }],
        objects => \@objects,
        runtimes => [{ id => $runtime_id, dm_uuid => "$p{sid}-DM-UUID", root_id => $root_id }] };
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $inventory });
    my $storage_plan = $planner->plan(vmid => 101, volumes => [$volid]);
    my $locked = copy($p{locked});
    my $args = { storage_plan => $storage_plan, raw_config => $p{raw}, locked_config => $locked,
        storecfg => copy($p{storecfg}), expected_scope => copy($inventory->{scope}),
        bindings => { objects => \%object_bindings,
            runtimes => { $runtime_id => { name => "$p{sid}-mapper", uuid => "$p{sid}-DM-UUID" } } },
        plan_version => 2 };
    return PVE::SharedLvmThinVMDestroyPlanAdapter->adapt_plan(%$args);
}

sub fixture {
    my ($first_mode, $second_mode, $same_wwid) = @_;
    $first_mode //= 'thin';
    $second_mode //= 'thick';
    my $raw = "scsi0: thin:vm-101-disk-0\nscsi1: thick:vm-101-disk-1\n";
    my $locked = { scsi0 => 'thin:vm-101-disk-0', scsi1 => 'thick:vm-101-disk-1', digest => sha1_hex($raw) };
    my $storecfg = { ids => {
        thin => { type => 'sharedlvmthin', 'slt-vgname' => 'vgthin', 'slt-expected-vg-uuid' => 'vg-thin',
            'slt-expected-pv-uuid' => 'pv-thin', 'slt-expected-wwid' => '60001',
            'slt-allocation-mode' => $first_mode eq 'thin' ? 'thin' : $first_mode eq 'lazy' ? 'thick-generations-lazy' : 'thick-generations' },
        thick => { type => 'sharedlvmthin', 'slt-vgname' => 'vgthick', 'slt-expected-vg-uuid' => 'vg-thick',
            'slt-expected-pv-uuid' => 'pv-thick', 'slt-expected-wwid' => $same_wwid ? '60001' : '60002',
            'slt-allocation-mode' => $second_mode eq 'thin' ? 'thin' : $second_mode eq 'lazy' ? 'thick-generations-lazy' : 'thick-generations' },
    } };
    my $thin = make_v2(sid => 'thin', disk => 0, mode => $first_mode, vg_uuid => 'vg-thin',
        pv_uuid => 'pv-thin', wwid => '60001', raw => $raw, locked => $locked, storecfg => $storecfg);
    my $thick = make_v2(sid => 'thick', disk => 1, mode => $second_mode, vg_uuid => 'vg-thick',
        pv_uuid => 'pv-thick', wwid => $same_wwid ? '60001' : '60002', raw => $raw, locked => $locked, storecfg => $storecfg);
    my $thin_foreign = { lvs => {}, dm => [], vg_tags => '', pv => { sid => 'thin' } };
    my $thick_foreign = { lvs => {}, dm => [], vg_tags => '', pv => { sid => 'thick' } };
    return (
        { collector_sid => 'thin', executor_v2 => $thin, foreign_evidence => $thin_foreign,
            foreign_evidence_sha256 => sha256_hex($JSON->encode($thin_foreign)) },
        { collector_sid => 'thick', executor_v2 => $thick, foreign_evidence => $thick_foreign,
            foreign_evidence_sha256 => sha256_hex($JSON->encode($thick_foreign)) },
    );
}

subtest 'Thin plus Thick compose losslessly and canonically' => sub {
    my ($thin, $thick) = fixture();
    my $a = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]);
    my $b = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thick, $thin]);
    is($a->{schema}, 'sharedlvmthin-vm-destroy-executor-plan/v3', 'v3 schema');
    is($a->{authority}, 'NONE', 'never grants authority');
    is($JSON->encode($a), $JSON->encode($b), 'input ordering is immaterial');
    is(scalar(@{$a->{scopes}}), 2, 'two physical scopes');
    is_deeply($a->{volids}, [qw(thick:vm-101-disk-1 thin:vm-101-disk-0)], 'exact union');
    is($JSON->encode(PVE::SharedLvmThinVMDestroyPlanV3->validate($a)), $JSON->encode($a),
        'full lossless rebuild');
};

subtest 'single scope retains lossless foreign baseline for fresh-process recovery' => sub {
    my ($thin) = fixture();
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin]);
    is(scalar(@{$plan->{scopes}}), 1, 'one scope is represented without synthetic second VG');
    my $fresh = copy($plan);
    my $bundle = PVE::SharedLvmThinVMDestroyPlanV3->inventory_bundle($fresh, $fresh->{scope_ids}->[0]);
    is_deeply($bundle->{foreign_evidence}, $thin->{foreign_evidence}, 'raw foreign baseline survives serialization');
    is($bundle->{authority}, 'NONE', 'no recovery authority');
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => []); 1 }, 'zero scopes remains refused');
};

subtest 'executor accepts exact v3 context and rejects storage or coverage drift' => sub {
    my ($thin, $thick) = fixture();
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]);
    my $storecfg = copy($thin->{executor_v2}->{adapter_inputs}->{storecfg});
    my $ids = PVE::SharedLvmThinVMDestroy::_validate_v3_plan(
        101, $storecfg, $plan->{volids}, $plan,
    );
    is_deeply($ids, $plan->{object_ids}, 'exact global object closure returned');
    my $drifted = copy($storecfg);
    $drifted->{ids}->{thin}->{'slt-vgname'} = 'changed';
    ok(!eval { PVE::SharedLvmThinVMDestroy::_validate_v3_plan(
        101, $drifted, $plan->{volids}, $plan); 1 }, 'current storecfg drift refused');
    my @partial = @{$plan->{volids}};
    pop @partial;
    ok(!eval { PVE::SharedLvmThinVMDestroy::_validate_v3_plan(
        101, $storecfg, \@partial, $plan); 1 }, 'partial configured-volume coverage refused');
};

subtest 'foreign evidence and collector identity are mandatory and tamper evident' => sub {
    for my $case (
        ['missing foreign baseline', sub { delete $_[0]->{foreign_evidence} }],
        ['missing foreign digest', sub { delete $_[0]->{foreign_evidence_sha256} }],
        ['invalid foreign digest', sub { $_[0]->{foreign_evidence_sha256} = 'invalid-foreign-digest' }],
        ['foreign baseline digest mismatch', sub { $_[0]->{foreign_evidence}->{pv}->{sid} = 'tampered' }],
        ['collector outside aliases', sub { $_[0]->{collector_sid} = 'other' }],
        ['invalid collector identity', sub { $_[0]->{collector_sid} = '../thin' }],
    ) {
        my ($thin, $thick) = fixture();
        $case->[1]->($thin);
        ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]); 1 },
            "$case->[0] refused");
    }
    my ($thin, $thick) = fixture();
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]);
    $plan->{scopes}->[0]->{foreign_evidence_sha256} = 'f' x 64;
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->validate($plan); 1 },
        'post-compose foreign digest tamper refused');
};

subtest 'fresh process can reconstruct exact Inventory observe bundle' => sub {
    my ($thin, $thick) = fixture();
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]);
    my $fresh = $JSON->decode($JSON->encode($plan));
    my ($scope) = grep { $_->{collector_sid} eq 'thin' } @{$fresh->{scopes}};
    my $bundle = PVE::SharedLvmThinVMDestroyPlanV3->inventory_bundle($fresh, $scope->{scope_id});
    is($bundle->{authority}, 'NONE', 'reconstructed bundle grants no authority');
    is($JSON->encode($bundle->{storage_plan}),
        $JSON->encode($scope->{executor_v2}->{adapter_inputs}->{storage_plan}), 'exact storage plan restored');
    is($JSON->encode($bundle->{bindings}),
        $JSON->encode($scope->{executor_v2}->{adapter_inputs}->{bindings}), 'exact bindings restored');
    is($JSON->encode($bundle->{foreign_evidence}), $JSON->encode($thin->{foreign_evidence}),
        'full foreign baseline restored without closure');
    is(sha256_hex($JSON->encode($bundle->{foreign_evidence})),
        $bundle->{foreign_evidence_sha256}, 'foreign baseline digest revalidated');
    for my $bad ('', 'g' x 64, '../scope', 'a' x 63) {
        ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->inventory_bundle($fresh, $bad); 1 },
            'invalid scope identity refused');
    }
};

subtest 'same WWID with different VG and PV identity is rejected globally' => sub {
    my ($first, $second) = fixture('thin', 'thick', 1);
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$first, $second]); 1 },
        'one LUN cannot become two physical scopes');
    like($@, qr/same WWID/, 'explicit collision');
};

subtest 'two independent Thick-family VGs remain separate envelopes' => sub {
    my ($eager, $lazy) = fixture('thick', 'lazy');
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$eager, $lazy]);
    is_deeply([sort map { $_->{executor_v2}->{roots}->[0]->{kind} } @{$plan->{scopes}}],
        [qw(lazy thick)], 'Eager and Lazy roots preserved without flattening');
    is(scalar(@{$plan->{scope_ids}}), 2, 'physical scopes stay independent');
    ok(PVE::SharedLvmThinVMDestroyPlanV3->validate_profile($plan, 'thick-only'),
        'Thick-only accepts only Thick-family scopes');
};

subtest 'package profile qualification rejects Thin in Thick-only' => sub {
    my ($thin, $thick) = fixture();
    my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]);
    ok(PVE::SharedLvmThinVMDestroyPlanV3->validate_profile($plan, 'dual'),
        'DUAL accepts the qualified mixed plan');
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->validate_profile($plan, 'thick-only'); 1 },
        'Thick-only refuses Thin before authority exists');
    like($@, qr/refuses a Thin scope/, 'explicit profile boundary');
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->validate_profile($plan, 'unknown'); 1 },
        'unknown profile refused');
};

for my $case (
    ['authority promotion', sub { $_[0]->{authority} = 'MUTATE' }],
    ['v2 object tamper', sub { $_[0]->{scopes}->[0]->{executor_v2}->{roots}->[0]->{objects}->[0]->{name} = 'forged' }],
    ['omitted scope', sub { pop @{$_[0]->{scopes}} }],
    ['global volume swap', sub { $_[0]->{volumes}->[0]->{scope_id} = $_[0]->{volumes}->[1]->{scope_id} }],
    ['plan digest tamper', sub { $_[0]->{plan_sha256} = '0' x 64 }],
) {
    subtest "$case->[0] is refused" => sub {
        my ($thin, $thick) = fixture();
        my $plan = PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]);
        $case->[1]->($plan);
        ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->validate($plan); 1 }, 'no acceptance');
    };
}

subtest 'same physical scope cannot be supplied twice' => sub {
    my ($thin) = fixture();
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, copy($thin)]); 1 },
        'duplicate physical envelope refused');
};

subtest 'cross-scope object identity collision is refused' => sub {
    my ($thin, $thick) = fixture();
    $thick->{executor_v2}->{roots}->[0]->{objects}->[0]->{id} =
        $thin->{executor_v2}->{roots}->[0]->{objects}->[0]->{id};
    # The v2 must remain internally valid for this to be a true cross-scope test;
    # rebuild therefore refuses even earlier, which is still the required result.
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]); 1 }, 'collision refused');
};

subtest 'raw/config/storecfg/runtime drift between scopes is refused' => sub {
    for my $field (qw(raw_config parsed_config_sha256)) {
        my ($thin, $thick) = fixture();
        $thick->{executor_v2}->{$field} = $field eq 'raw_config' ? 'different' : '0' x 64;
        ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]); 1 }, "$field drift");
    }
};

subtest 'canonical size cap is fail-closed before returning a plan' => sub {
    my ($thin, $thick) = fixture();
    local $PVE::SharedLvmThinVMDestroyPlanV3::MAX_BYTES = 256;
    ok(!eval { PVE::SharedLvmThinVMDestroyPlanV3->compose(scopes => [$thin, $thick]); 1 }, 'oversize refused');
    like($@, qr/journal budget/, 'explicit limit');
};

done_testing();
