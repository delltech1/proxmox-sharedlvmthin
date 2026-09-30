use strict;
use warnings;
use Test::More;
use lib 'tests/unit/lib', 'usr/share/perl5';
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::SharedLvmThinThick;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $sid = 'thick-test';
my $vol = 'vm-900001-disk-0';
my $cfg = { shared => 1, 'slt-vgname' => 'testvg',
    'slt-allocation-mode' => 'thick-generations',
    'slt-expected-vg-uuid' => 'vg-uuid', 'slt-expected-pv-uuid' => 'pv-uuid',
    'slt-expected-wwid' => '3600abcd' };
my $ns = 'vg-uuid';
my $anchor = PVE::SharedLvmThinThick::anchor_name($ns, $vol);
my @gen = map { PVE::SharedLvmThinThick::generation_name($ns, $vol, $_) } 0..2;
my $front = PVE::SharedLvmThinThick::mapper_name($ns, $vol);
my $front_uuid = 'SLT-TG2-' . PVE::SharedLvmThinThick::object_key($ns, $vol);

sub dm_lv {
    my ($lv) = @_;
    $lv =~ s/-/--/g;
    return "testvg-$lv";
}

sub fixture {
    my $state = { sid => $sid, vol => $vol, phase => 'MATERIALIZED', tx => ('1' x 32),
        op => 'ALLOC', snapshot => 'none', source => $gen[2], old => $gen[2],
        new => $gen[2], head => $gen[2], generation => 2, region => 8 };
    my $objects = { $anchor => { lv_uuid => 'anchor-uuid', lv_size => 4096,
        tags => join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$state)}) } };
    for my $i (0..2) {
        my %tags = (sid => $sid, vol => $vol, role => $i == 2 ? 'head' : 'snapshot', generation => $i);
        $tags{snapshot} = 'snap' . $i if $i < 2;
        $objects->{$gen[$i]} = { lv_uuid => "gen-$i-uuid", lv_size => 4096,
            tags => join(',', @{PVE::SharedLvmThinThick::generation_tags(%tags)}) };
    }
    return { testvg => $objects };
}

sub run_tree {
    my (%options) = @_;
    my $inventory = fixture();
    $options{mutate}->($inventory) if $options{mutate};
    my (@effects, @checks);
    my $lock_depth = 0;
    my $locks = 0;
    my $read = 0;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        die "recursive VG lock\n" if $lock_depth;
        $lock_depth = 1;
        $locks++;
        my $result = $_[3]->();
        $lock_depth = 0;
        return $result;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub {
        die "OPEN intent\n" if $options{intent};
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_assert_no_active_storage_worker = sub {
        die "live writer\n" if $options{writer};
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub {
        $read++;
        if ($options{drift} && $read == 3) { $inventory->{testvg}->{$gen[1]}->{lv_uuid} = 'replaced-uuid'; }
        return $inventory;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_pve_reference_files = sub {
        return $options{refs} ? ['/etc/pve/nodes/n/qemu-server/900001.conf'] : [];
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_call_frames = sub {
        return $options{frames} // [];
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thin_local_node = sub { 'n' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_reference_digest = sub {
        my (undef, $store, $volume, $admission, $refs) = @_;
        die "foreign reference\n" if $options{foreign};
        return 'changed' if $options{config_drift} && $read >= 2;
        return $class->_thick_validate_destroy_config($store, $volume,
            $options{config} // "scsi0: $sid:$vol\n[snap0]\nscsi0: $sid:$vol\n[snap1]\nscsi0: $sid:$vol\n",
            $admission->{snapshots});
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_autoactivation_disabled = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_snapshot_readonly = sub {
        push @checks, $_[3];
        die "last snapshot not readonly\n" if $options{last_bad} && $_[3] eq $gen[1];
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_tree_kernel_rows = sub {
        return $options{rows} // [];
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_tree_peer_absence = sub {
        die "peer UNKNOWN\n" if $options{peer_unknown};
        push @checks, 'peers';
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_volume_snapshot_delete_locked = sub {
        die "snapshot outside lock\n" if !$lock_depth;
        die "destruction before whole-tree preflight\n"
            if !grep($_ eq $gen[1], @checks) || !grep($_ eq 'peers', @checks);
        my $snap = $_[4];
        push @effects, "snapshot:$snap";
        die "injected snapshot failure; OPEN intent\n" if $options{snapshot_failure};
        my $i = $snap eq 'snap0' ? 0 : 1;
        delete $inventory->{testvg}->{$gen[$i]};
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_free_image_single_locked = sub {
        die "HEAD outside lock\n" if !$lock_depth;
        die "snapshot remains before HEAD\n" if exists($inventory->{testvg}->{$gen[0]})
            || exists($inventory->{testvg}->{$gen[1]});
        die "final orphan fence missing\n" if !$_[6];
        push @effects, 'head';
        return;
    };
    my $ok = eval { $class->_thick_free_image($sid, $cfg, $vol, 0); 1 };
    return { ok => $ok, error => $@, effects => \@effects, checks => \@checks, locks => $locks };
}

subtest 'whole tree under one lock, preflight before first destructive step' => sub {
    my $r = run_tree();
    ok($r->{ok}, 'exact unreferenced tree accepted') or diag($r->{error});
    is_deeply($r->{effects}, ['snapshot:snap0', 'snapshot:snap1', 'head'], 'ordered bounded deletion');
    is($r->{locks}, 1, 'one canonical lock, no recursion');
    ok(grep($_ eq $gen[1], @{$r->{checks}}), 'last snapshot preflighted');
    ok(grep($_ eq 'peers', @{$r->{checks}}), 'peer absence is required');
};

subtest 'entire preflight fails before any snapshot deletion' => sub {
    my @cases = (
        ['reference', { refs => 1 }], ['writer', { writer => 1 }],
        ['intent', { intent => 1 }], ['peer unknown', { peer_unknown => 1 }],
        ['last snapshot unsafe', { last_bad => 1 }], ['identity drift', { drift => 1 }],
        ['open HEAD', { rows => [[$front, $front_uuid, 1]] }],
        ['open last snapshot', { rows => [[dm_lv($gen[1]), 'LVM-vguuidgen1uuid', 1]] }],
        ['transition metadata', { mutate => sub { $_[0]->{testvg}->{'sltg-m-' .
            PVE::SharedLvmThinThick::object_key($ns, $vol) . '-00000000'} = {} } }],
        ['unsigned snapshot', { mutate => sub { $_[0]->{testvg}->{$gen[1]}->{tags} = '' } }],
        ['foreign snapshot', { mutate => sub {
            $_[0]->{testvg}->{$gen[1]}->{tags} = join(',', @{PVE::SharedLvmThinThick::generation_tags(
                sid => 'foreign', vol => $vol, role => 'snapshot', generation => 1, snapshot => 'snap1')});
        } }],
        ['transition anchor', { mutate => sub {
            my $state = PVE::SharedLvmThinThick::decode_anchor_tags($_[0]->{testvg}->{$anchor}->{tags});
            $state->{phase} = 'HYDRATING';
            @$state{qw(op snapshot source old)} = ('SNAPSHOT', 'snap1', $gen[1], $gen[1]);
            $_[0]->{testvg}->{$anchor}->{tags} = join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$state)});
        } }],
        ['duplicate UUID', { mutate => sub { $_[0]->{testvg}->{$gen[1]}->{lv_uuid} = 'gen-0-uuid' } }],
        ['duplicate snapshot name', { mutate => sub {
            $_[0]->{testvg}->{$gen[1]}->{tags} = join(',', @{PVE::SharedLvmThinThick::generation_tags(
                sid => $sid, vol => $vol, role => 'snapshot', generation => 1, snapshot => 'snap0')});
        } }],
    );
    for my $case (@cases) {
        my ($name, $options) = @$case;
        my $r = run_tree(%$options);
        ok(!$r->{ok}, "$name refused");
        is_deeply($r->{effects}, [], "$name: no partial tree deletion");
    }
};

subtest 'settled SNAPSHOT operation is not confused with an active transition' => sub {
    my $r = run_tree(mutate => sub {
        my $state = PVE::SharedLvmThinThick::decode_anchor_tags($_[0]->{testvg}->{$anchor}->{tags});
        @$state{qw(op snapshot source old)} = ('SNAPSHOT', 'snap1', $gen[1], $gen[1]);
        $_[0]->{testvg}->{$anchor}->{tags} = join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$state)});
    });
    ok($r->{ok}, 'MATERIALIZED/SNAPSHOT tree admitted') or diag($r->{error});
    is_deeply($r->{effects}, ['snapshot:snap0', 'snapshot:snap1', 'head'], 'same exact cleanup path');
};

subtest 'failed snapshot transaction stops before next snapshot or HEAD' => sub {
    my $r = run_tree(snapshot_failure => 1);
    ok(!$r->{ok}, 'failure propagated');
    like($r->{error}, qr/OPEN intent/, 'recovery authority preserved');
    is_deeply($r->{effects}, ['snapshot:snap0'], 'no automatic retry or continuation');
};

sub destroy_frames {
    my $storecfg = { ids => { $sid => $cfg } };
    my $callback = sub { };
    return [
        { sub => $class . '::free_image', args => [$class, $sid, $cfg, $vol, 0, 'raw'] },
        { sub => 'PVE::Storage::vdisk_free', args => [$storecfg, "$sid:$vol"] },
        { sub => 'PVE::QemuServer::destroy_vm', args => [$storecfg, 900001, undef, { lock => 'destroyed' }, 1] },
        { sub => 'PVE::AbstractConfig::lock_config_full', args => ['PVE::QemuConfig', 900001, 10, $callback] },
        { sub => 'PVE::AbstractConfig::lock_config', args => ['PVE::QemuConfig', 900001, $callback] },
    ];
}

sub capture_probe {
    return $class->_thick_destroy_call_frames();
}

subtest 'qualified in-process destroy contract, never process-name authority' => sub {
    is($class->_thick_validate_destroy_frames($sid, $vol, destroy_frames()), 900001,
        'exact locked qmdestroy stack admitted');
    my $observed = capture_probe('exact-argument');
    my ($probe) = grep { $_->{sub} eq 'main::capture_probe' } @$observed;
    is_deeply($probe->{args}, ['exact-argument'], 'real caller captures exact arguments, not argv');
    my @cases = (
        ['direct pvesm free', sub { splice(@{$_[0]}, 2) }],
        ['missing public hook', sub { shift @{$_[0]} }],
        ['missing enclosing lock', sub { pop @{$_[0]} }],
        ['skiplock', sub { $_[0]->[2]->{args}->[2] = 1 }],
        ['restore replacement', sub { $_[0]->[2]->{args}->[3] = { lock => 'restore' } }],
        ['extra replacement key', sub { $_[0]->[2]->{args}->[3]->{extra} = 1 }],
        ['wrong volume', sub { $_[0]->[1]->{args}->[1] = "$sid:vm-2-disk-0" }],
        ['wrong VM lock', sub { $_[0]->[4]->{args}->[1] = 2 }],
        ['API signature drift', sub { push @{$_[0]->[2]->{args}}, 'new-argument' }],
        ['public hook signature drift', sub { push @{$_[0]->[0]->{args}}, 'new-argument' }],
        ['caller drift', sub { $_[0]->[2]->{sub} = 'PVE::QemuServer::destroy_vm_v2' }],
        ['duplicate caller', sub { push @{$_[0]}, $_[0]->[2] }],
        ['lock order inversion', sub { @{$_[0]}[3,4] = @{$_[0]}[4,3] }],
        ['full lock timeout drift', sub { $_[0]->[3]->{args}->[2] = 20 }],
    );
    for my $case (@cases) {
        my $frames = destroy_frames();
        $case->[1]->($frames);
        my $r = run_tree(refs => 1, frames => $frames);
        ok(!$r->{ok}, "$case->[0] refused");
        is_deeply($r->{effects}, [], "$case->[0]: zero deletion");
    }
};

subtest 'only stable owner current and signed snapshot references are admitted' => sub {
    my $r = run_tree(refs => 1, frames => destroy_frames());
    ok($r->{ok}, 'owning config may remain until PVE finishes destroy') or diag($r->{error});
    is_deeply($r->{effects}, ['snapshot:snap0', 'snapshot:snap1', 'head'], 'same guarded whole-tree deletion');
    for my $case (
        ['foreign VM', { foreign => 1 }],
        ['config drift', { config_drift => 1 }],
        ['pending', { config => "scsi0: $sid:$vol\n[PENDING]\ncores: 2\n" }],
        ['special', { config => "scsi0: $sid:$vol\n[special:foo]\nfoo: bar\n" }],
        ['migration lock', { config => "scsi0: $sid:$vol\nlock: migrate\n" }],
        ['backup lock', { config => "scsi0: $sid:$vol\nlock: backup\n" }],
        ['unfinished snapshot', { config => "scsi0: $sid:$vol\nsnapstate: prepare\n" }],
        ['unknown reference', { config => "description: $sid:$vol\n" }],
        ['unsigned snapshot ref', { config => "[not-owned]\nscsi0: $sid:$vol\n" }],
        ['duplicate slot', { config => "scsi0: $sid:$vol\nscsi0: $sid:$vol\n" }],
        ['malformed config', { config => "scsi0: $sid:$vol\nbroken\n" }],
        ['peer UNKNOWN', { peer_unknown => 1 }],
    ) {
        my $bad = run_tree(refs => 1, frames => destroy_frames(), %{$case->[1]});
        ok(!$bad->{ok}, "$case->[0] refused");
        is_deeply($bad->{effects}, [], "$case->[0]: no partial deletion");
    }
    no warnings 'redefine';
    my $admission = { path => '/etc/pve/nodes/n/qemu-server/900001.conf', vmid => 900001 };
    for my $refs (['/etc/pve/nodes/n/qemu-server/2.conf'],
                  ['/etc/pve/nodes/foreign/qemu-server/900001.conf'], []) {
        eval { $class->_thick_destroy_reference_digest($sid, $vol, $admission, $refs) };
        like($@, qr/foreign or ambiguous/, 'real reader rejects non-owner/remote/absent references before open');
    }
};

subtest 'kernel proof catches renamed UUID, busy backing and remote presence' => sub {
    my $plan = $class->_thick_tree_plan($sid, $cfg, $vol, fixture());
    for my $case (
        [ [['renamed', 'LVM-vguuidgen1uuid', 0]], 0 ],
        [ [[dm_lv($gen[1]), 'LVM-vguuidgen1uuid', 0]], 1 ],
        [ [[dm_lv($gen[1]), 'WRONG', 0]], 0 ],
    ) {
        my $ok = eval { $class->_thick_tree_check_kernel_rows($cfg, $vol, $plan, @$case); 1 };
        ok(!$ok, 'ambiguous or remote mapping refuses deletion');
    }
    ok($class->_thick_tree_check_kernel_rows($cfg, $vol, $plan, [], 1), 'empty complete peer inventory accepted');
};

subtest 'kernel inventory parser fails closed on command and protocol errors' => sub {
    no warnings 'redefine';
    for my $lines (['x:uuid:0', 'x:uuid:0'], ['broken'], ['x:uuid:unknown']) {
        local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub {
            my ($argv, %opts) = @_;
            $opts{outfunc}->($_) for @$lines;
        };
        my $ok = eval { $class->_thick_tree_kernel_rows(); 1 };
        ok(!$ok, 'malformed/duplicate evidence refused');
    }
    local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub { die "timeout\n" };
    eval { $class->_thick_tree_kernel_rows(); };
    like($@, qr/timeout/, 'timeout never proves absence');
};

subtest 'all configured peers must positively report absence' => sub {
    my $plan = $class->_thick_tree_plan($sid, $cfg, $vol, fixture());
    my $members = { local => { online => 1, ip => '192.0.2.1' },
        peer => { online => 1, ip => '192.0.2.2' } };
    my $nodes = ['local', 'peer'];
    my @probes;
    no warnings 'redefine';
    local *PVE::Cluster::cfs_update = sub { 1 };
    local *PVE::Cluster::get_members = sub { $members };
    local *PVE::Cluster::get_nodelist = sub { $nodes };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thin_local_node = sub { 'local' };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_tree_kernel_rows = sub {
        push @probes, $_[1]; return [];
    };
    ok($class->_thick_tree_peer_absence($cfg, $vol, $plan), 'exact configured peer checked');
    is(scalar(@probes), 1, 'one bounded probe for one peer');
    like(join(' ', @{$probes[0]}), qr/BatchMode=yes.*ConnectTimeout=3/, 'noninteractive bounded SSH');
    $members->{peer}->{online} = 0;
    eval { $class->_thick_tree_peer_absence($cfg, $vol, $plan) };
    like($@, qr/offline or unknown/, 'offline is UNKNOWN, never fenced');
    is(scalar(@probes), 1, 'offline peer not treated as absent');
    $members->{peer}->{online} = 1;
    $nodes = ['local', 'missing'];
    eval { $class->_thick_tree_peer_absence($cfg, $vol, $plan) };
    like($@, qr/offline or unknown/, 'configured missing node refused');
    $nodes = ['local', 'local'];
    eval { $class->_thick_tree_peer_absence($cfg, $vol, $plan) };
    like($@, qr/ambiguous/, 'duplicate node list refused');
};

subtest 'materialized Lazy keeps executor then VG dispatch without recursive executor' => sub {
    my $lazy = { %$cfg, 'slt-allocation-mode' => 'thick-generations-lazy' };
    my @calls;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_lazy_volume_executor_lock = sub {
        push @calls, 'executor'; return $_[3]->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        push @calls, 'classify'; return $_[3]->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ({ phase => 'MATERIALIZED' }, {}, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_free_image = sub {
        push @calls, 'thick-tree'; return;
    };
    $class->_lazy_free_image($sid, $lazy, $vol, 0);
    is_deeply(\@calls, ['executor', 'classify', 'thick-tree'], 'Lazy converged delete uses new public Thick path');
};

done_testing();
