#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::Storage::Custom::SharedLvmThinPlugin;

my @lvm_results;
my @commands;
my @locks;
my $command_failure;
my $verify_owned_volume = \&PVE::Storage::Custom::SharedLvmThinPlugin::_verify_owned_volume;
my $verify_mutation_quorum = \&PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum;
my $forced_single_node_quorum = \&PVE::Storage::Custom::SharedLvmThinPlugin::_forced_single_node_quorum_from_evidence;
my $verify_resize_postcondition = \&PVE::Storage::Custom::SharedLvmThinPlugin::_verify_resize_postcondition;
my $verify_snapshot_postcondition = \&PVE::Storage::Custom::SharedLvmThinPlugin::_verify_snapshot_postcondition;
my $verify_pool_health = \&PVE::Storage::Custom::SharedLvmThinPlugin::_verify_pool_health;
my $disable_and_verify_autoactivation = \&PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation;
my $verify_autoactivation_disabled = \&PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled;
my $record_disable_autoactivation = sub {
    my ($class, $vg, $lv) = @_;
    PVE::Storage::Custom::SharedLvmThinPlugin::run_command([
        '/sbin/lvchange', '--setautoactivation', 'n', "$vg/$lv",
    ]);
    return 1;
};

no warnings 'redefine';
local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub {
    die "unexpected lvm_list_volumes call\n" if !@lvm_results;
    return shift @lvm_results;
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub {
    my ($command, %options) = @_;
    push @commands, [@$command];
    die "$command_failure\n"
        if defined($command_failure)
        && join(' ', @$command) =~ $command_failure;
    return;
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::cluster_lock_storage = sub {
    my ($class, $storeid, $shared, $timeout, $code) = @_;
    push @locks, [$storeid, $shared, $timeout];
    return $code->();
};
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_owned_volume = sub { return 1; };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { return 1; };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_resize_postcondition = sub { return 1; };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_snapshot_postcondition = sub { return 1; };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_pool_health = sub { return 1; };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation = sub { return 1; };
local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub { return 1; };

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $scfg = {
    'slt-vgname' => 'testvg',
    'slt-initial-pool-size' => 4,
};

sub reset_mocks {
    @lvm_results = ();
    @commands = ();
    @locks = ();
    $command_failure = undef;
}

sub command_lines {
    return map { join(' ', @$_) } @commands;
}

subtest 'thin-pool health gate blocks mutation before repair or mutation commands' => sub {
    for my $case (
        ['twi-aotz--||', 1, 'healthy'],
        ['twi-cotz--|needs_check|check_needed', 0, 'needs_check'],
        ['twi-aotzM-||', 0, 'metadata read-only'],
        ['twi-aotz--||yes', 0, 'explicit check-needed field'],
        ['unexpected||', 0, 'unexpected attributes'],
    ) {
        my ($line, $allowed, $name) = @$case;
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub { return [$line]; };
        my $ok = eval { $verify_pool_health->($class, 'testvg', 'sltp-900001'); 1 };
        is($ok ? 1 : 0, $allowed, "$name decision");
        like($@, qr/mutations disabled|^$/, "$name reports fail-closed state");
    }
    is(scalar(@commands), 0, 'health failures executed zero LVM mutation or repair commands');
};

subtest 'explicit PVE Storage API 14..15 compatibility policy' => sub {
    for my $api (14, 15) {
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_runtime_storage_api = sub { return $api; };
        is($class->api(), $api, "plugin declares exact qualified host API $api");
    }

    for my $api (13, 16) {
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_runtime_storage_api = sub { return $api; };
        my $ok = eval { $class->api(); 1 };
        ok(!$ok, "plugin registration fails closed on API $api");
        like($@, qr/outside the tested SharedLvmThin range 14\.\.15/, 'registration reports explicit tested range');
    }

    for my $api (14, 15) {
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_runtime_storage_api = sub { return $api; };
        my $ok = eval { $verify_mutation_quorum->($class, 'local-test', { shared => 0 }); 1 };
        ok($ok, "runtime API $api allows mutation preflight");
    }

    for my $api (13, 16) {
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_runtime_storage_api = sub { return $api; };
        my $ok = eval { $verify_mutation_quorum->($class, 'local-test', { shared => 0 }); 1 };
        ok(!$ok, "runtime API $api fails closed");
        like($@, qr/outside the tested SharedLvmThin range 14\.\.15/, 'explicit tested range reported');
    }
};

subtest 'volume_resize accepts API14 and API15 call signatures' => sub {
    my @received;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_volume_resize_locked = sub {
        @received = @_;
        return;
    };

    $class->volume_resize($scfg, 'sharedthin-test', 'vm-100-disk-0', 1024, 0);
    is(scalar(@received), 7, 'API14 call is normalized with an undefined snapshot argument');
    ok(!defined($received[6]), 'API14 has no snapshot target');

    $class->volume_resize($scfg, 'sharedthin-test', 'vm-100-disk-0', 1024, 0, 'snap1');
    is($received[6], 'snap1', 'API15 snapshot argument is received explicitly');
};

subtest 'delayed SAN discovery recovers without initialization or repair' => sub {
    reset_mocks();
    my $visible = 0;
    no warnings 'redefine';
    local *PVE::Storage::LVMPlugin::lvm_vgs = sub {
        return {} if !$visible;
        return { testvg => { size => 1000, free => 600 } };
    };

    my $ok = eval {
        $class->activate_storage('sharedthin-test', $scfg, undef);
        1;
    };
    ok(!$ok, 'activation fails while SAN/VG is unavailable');
    like($@, qr/VG 'testvg' not found/, 'unavailable state is explicit');
    is(scalar(@commands), 0, 'no initialize, repair, or mutation attempted');

    my @missing_status = $class->status('sharedthin-test', $scfg, undef);
    is(scalar(@missing_status), 0, 'status remains unavailable while VG is absent');

    $visible = 1;
    ok($class->activate_storage('sharedthin-test', $scfg, undef), 'later activation succeeds after discovery');
    my @recovered = $class->status('sharedthin-test', $scfg, undef);
    is_deeply(\@recovered, [1000, 600, 400, 1], 'existing VG is rediscovered without recreation');
    is(scalar(@commands), 0, 'recovery performed no initialization or repair');
};

subtest 'storage identity gate accepts an exact single-PV multipath identity' => sub {
    my @responses = (
        ['vg-uuid-expected'],
        ['pv-uuid-expected|/dev/mapper/3600deadbeef00000000000000000001'],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @responses;
    };
    my $identity = {
        %$scfg,
        'slt-expected-vg-uuid' => 'vg-uuid-expected',
        'slt-expected-pv-uuid' => 'pv-uuid-expected',
        'slt-expected-wwid' => '3600deadbeef00000000000000000001',
    };
    ok($class->_verify_storage_identity('sharedthin-test', $identity), 'identity accepted');
};

subtest 'storage identity gate fails closed before mutation' => sub {
    my @responses = (
        ['wrong-vg-uuid'],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @responses;
    };
    my $identity = {
        %$scfg,
        'slt-expected-vg-uuid' => 'vg-uuid-expected',
    };
    my $ok = eval {
        $class->_verify_storage_identity('sharedthin-test', $identity);
        1;
    };
    ok(!$ok, 'mismatch rejected');
    like($@, qr/VG UUID mismatch/, 'failure identifies the mismatched field');
    is(scalar(@commands), 0, 'no LVM mutation executed');
};

subtest 'storage identity gate rejects ambiguous or unstable PV presentation' => sub {
    my @responses = (
        ['vg-uuid-expected'],
        ['pv-one|/dev/mapper/a', 'pv-two|/dev/mapper/b'],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @responses;
    };
    my $identity = {
        %$scfg,
        'slt-expected-vg-uuid' => 'vg-uuid-expected',
        'slt-expected-pv-uuid' => 'pv-one',
    };
    my $ok = eval {
        $class->_verify_storage_identity('sharedthin-test', $identity);
        1;
    };
    ok(!$ok, 'multiple PVs rejected');
    like($@, qr/exactly one backing PV/, 'ambiguous identity is explicit');
};

subtest 'identity failure matrix performs no mutating LVM command' => sub {
    my @cases = (
        {
            name => 'wrong VG UUID',
            responses => [['wrong-vg']],
            config => {
                'slt-expected-vg-uuid' => 'expected-vg',
            },
            error => qr/VG UUID mismatch/,
        },
        {
            name => 'wrong PV UUID',
            responses => [['expected-vg'], ['wrong-pv|/dev/mapper/3600aa']],
            config => {
                'slt-expected-vg-uuid' => 'expected-vg',
                'slt-expected-pv-uuid' => 'expected-pv',
            },
            error => qr/PV UUID mismatch/,
        },
        {
            name => 'wrong WWID',
            responses => [['expected-vg'], ['expected-pv|/dev/mapper/3600bb']],
            config => {
                'slt-expected-vg-uuid' => 'expected-vg',
                'slt-expected-pv-uuid' => 'expected-pv',
                'slt-expected-wwid' => '3600aa',
            },
            error => qr/WWID mismatch/,
        },
        {
            name => 'raw sdX instead of mapper',
            responses => [['expected-vg'], ['expected-pv|/dev/sdd']],
            config => {
                'slt-expected-vg-uuid' => 'expected-vg',
                'slt-expected-pv-uuid' => 'expected-pv',
                'slt-expected-wwid' => '3600aa',
            },
            error => qr/not a stable \/dev\/mapper/,
        },
        {
            name => 'zero PV',
            responses => [['expected-vg'], []],
            config => {
                'slt-expected-vg-uuid' => 'expected-vg',
                'slt-expected-pv-uuid' => 'expected-pv',
            },
            error => qr/exactly one backing PV/,
        },
        {
            name => 'multiple PVs',
            responses => [
                ['expected-vg'],
                ['pv-one|/dev/mapper/3600aa', 'pv-two|/dev/mapper/3600bb'],
            ],
            config => {
                'slt-expected-vg-uuid' => 'expected-vg',
                'slt-expected-pv-uuid' => 'pv-one',
            },
            error => qr/exactly one backing PV/,
        },
    );

    for my $case (@cases) {
        reset_mocks();
        my @responses = @{$case->{responses}};
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
            return shift @responses;
        };
        my $identity = { %$scfg, %{$case->{config}} };
        my $ok = eval {
            $class->_verify_storage_identity('sharedthin-test', $identity);
            1;
        };
        ok(!$ok, "$case->{name}: rejected");
        like($@, $case->{error}, "$case->{name}: explicit error");
        is(scalar(@commands), 0, "$case->{name}: no mutating command");
    }
};

subtest 'unavailable VG blocks activation without mutation' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::LVMPlugin::lvm_vgs = sub { return {}; };
    my $ok = eval {
        $class->activate_storage('sharedthin-test', $scfg, undef);
        1;
    };
    ok(!$ok, 'unavailable VG rejected');
    like($@, qr/VG 'testvg' not found/, 'unavailable VG error is explicit');
    is(scalar(@commands), 0, 'unavailable VG: no mutating command');
};

subtest 'identity mismatch blocks alloc before first mutation' => sub {
    reset_mocks();
    my @responses = (['wrong-vg']);
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @responses;
    };
    my $identity = {
        %$scfg,
        'slt-expected-vg-uuid' => 'expected-vg',
    };
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $identity, 900001, 'raw',
            'vm-900001-disk-0', 1024,
        );
        1;
    };
    ok(!$ok, 'allocation rejected by identity gate');
    like($@, qr/VG UUID mismatch/, 'allocation reports identity mismatch');
    is(scalar(@commands), 0, 'allocation executed no mutating LVM command');
};

subtest 'legacy and foreign ownership are read-only' => sub {
    for my $case (
        ['legacy', ''],
        ['foreign', 'pve-slt-sid-other-storage'],
    ) {
        reset_mocks();
        @lvm_results = ({ testvg => {
            'sltp-900001' => { lv_type => 't', tags => $case->[1] },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } });
        my $ok = eval {
            $verify_owned_volume->($class, 'sharedthin-test', $scfg, 'vm-900001-disk-0');
            1;
        };
        ok(!$ok, "$case->[0] object rejected");
        like($@, qr/ownership is not positively proven/, "$case->[0] error is explicit");
        is(scalar(@commands), 0, "$case->[0] object caused no mutation");
    }
};

subtest 'quorum loss blocks shared mutation before any LVM command' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Cluster::check_cfs_quorum = sub { die "cluster not ready - no quorum\n"; };
    my $shared_cfg = { %$scfg, shared => 1 };
    my $ok = eval {
        $verify_mutation_quorum->($class, 'sharedthin-test', $shared_cfg);
        1;
    };
    ok(!$ok, 'quorum loss rejected');
    like($@, qr/no quorum/, 'quorum error propagated');
    is(scalar(@commands), 0, 'quorum failure caused no LVM mutation');
};

subtest 'standalone non-shared mutation does not require cluster quorum' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Cluster::check_cfs_quorum = sub { die "must not be called\n"; };
    ok($verify_mutation_quorum->($class, 'sharedthin-test', { %$scfg, shared => 0 }), 'non-shared allowed');
    is(scalar(@commands), 0, 'non-shared preflight is read-only');
};

subtest 'forced expected-votes override classification is topology-aware' => sub {
    ok($forced_single_node_quorum->(2, 1, 1, 0), 'two-node forced survivor detected');
    ok($forced_single_node_quorum->(10, 1, 1, 0), 'N-node forced survivor detected');
    ok(!$forced_single_node_quorum->(2, 1, 2, 1), 'qdevice survivor not misclassified');
    ok(!$forced_single_node_quorum->(2, 2, 2, 0), 'healthy two-node cluster not misclassified');
    ok(!$forced_single_node_quorum->(10, 6, 10, 0), 'healthy N-node quorum not misclassified');
};

subtest 'forced quorum gate blocks shared mutation after native quorum check' => sub {
    reset_mocks();
    no warnings 'redefine';
    my $native_checked = 0;
    local *PVE::Cluster::check_cfs_quorum = sub { $native_checked++; return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_no_forced_single_node_quorum = sub {
        die "CRITICAL: forced single-node quorum detected\n";
    };
    my $ok = eval {
        $verify_mutation_quorum->($class, 'sharedthin-test', { %$scfg, shared => 1 });
        1;
    };
    ok(!$ok, 'forced quorum rejected despite native quorate result');
    is($native_checked, 1, 'native PVE quorum remained the first authority');
    like($@, qr/forced single-node quorum/, 'critical override state is explicit');
    is(scalar(@commands), 0, 'forced quorum rejection caused no LVM mutation');
};

subtest 'lock rejection blocks direct mutating hooks before LVM' => sub {
    my @operations = (
        ['resize', sub { $class->volume_resize($scfg, 'sharedthin-test', 'vm-900001-disk-0', 2048, 0, undef) }],
        ['snapshot create', sub { $class->volume_snapshot($scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before') }],
        ['snapshot delete', sub { $class->volume_snapshot_delete($scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before') }],
        ['rollback', sub { $class->volume_snapshot_rollback($scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before') }],
    );
    for my $operation (@operations) {
        reset_mocks();
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::cluster_lock_storage = sub {
            die "storage lock rejected\n";
        };
        my $ok = eval { $operation->[1]->(); 1 };
        ok(!$ok, "$operation->[0]: lock failure propagated");
        like($@, qr/lock rejected/, "$operation->[0]: explicit lock error");
        is(scalar(@commands), 0, "$operation->[0]: zero mutating commands");
    }
};

subtest 'quorum loss at mutation boundary fails inside the held lock' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub {
        die "quorum disappeared at mutation boundary\n";
    };
    my $ok = eval {
        $class->volume_resize(
            { %$scfg, shared => 1 }, 'sharedthin-test',
            'vm-900001-disk-0', 2048, 0, undef,
        );
        1;
    };
    ok(!$ok, 'boundary quorum loss rejected');
    like($@, qr/quorum disappeared/, 'boundary failure is explicit');
    is(scalar(@locks), 1, 'storage lock was acquired before boundary revalidation');
    is(scalar(@commands), 0, 'no mutation after quorum loss');
};

subtest 'ownership ambiguity at mutation boundary fails closed' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_owned_volume = sub {
        die "ownership became ambiguous\n";
    };
    my $ok = eval {
        $class->volume_snapshot_delete(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'ambiguous ownership rejected');
    like($@, qr/ownership became ambiguous/, 'ownership failure is explicit');
    is(scalar(@commands), 0, 'no delete attempted');
};

subtest 'VG loss during snapshot creation has no destructive fallback' => sub {
    reset_mocks();
    $command_failure = qr{/sbin/lvcreate};
    my $ok = eval {
        $class->volume_snapshot(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'failed LVM mutation propagated');
    my @lines = command_lines();
    is(scalar(@lines), 1, 'only the requested mutation was attempted');
    like($lines[0], qr{^/sbin/lvcreate}, 'snapshot create was the sole command');
    unlike(join("\n", @lines), qr{/sbin/lvremove|pvcreate|vgcreate|wipefs}, 'no cleanup, repair, or initialization fallback');
};

subtest 'postcondition failures preserve state and perform no fallback cleanup' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {} });
    my $ok = eval {
        $verify_snapshot_postcondition->(
            $class, $scfg, 'vm-900001-disk-0',
            'snap_vm-900001-disk-0_before', 1,
        );
        1;
    };
    ok(!$ok, 'missing created snapshot detected');
    like($@, qr/snapshot postcondition failed/, 'snapshot create postcondition explicit');
    is(scalar(@commands), 0, 'postcondition check itself is read-only');

    reset_mocks();
    @lvm_results = ({ testvg => {
        'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
    } });
    $ok = eval {
        $verify_snapshot_postcondition->(
            $class, $scfg, 'vm-900001-disk-0',
            'snap_vm-900001-disk-0_before', 0,
        );
        1;
    };
    ok(!$ok, 'snapshot surviving delete detected');
    like($@, qr/delete postcondition failed/, 'snapshot delete postcondition explicit');
    is(scalar(@commands), 0, 'no second delete attempted');

    reset_mocks();
    @lvm_results = ({ testvg => {
        'vm-900001-disk-0' => { lv_size => 1024 },
    } });
    $ok = eval {
        $verify_resize_postcondition->(
            $class, $scfg, 'vm-900001-disk-0', 2048,
        );
        1;
    };
    ok(!$ok, 'short resize detected');
    like($@, qr/smaller than requested/, 'resize postcondition explicit');
    is(scalar(@commands), 0, 'no speculative retry attempted');
};

subtest 'snapshot postcondition requires read-only thin and activation-skip flags' => sub {
    for my $case (
        ['Vri---tz-k', 1, 'canonical snapshot flags'],
        ['Vwi---tz-k', 0, 'writable snapshot'],
        ['Vri---tz--', 0, 'missing activation-skip'],
        ['-ri---tz-k', 0, 'not a thin volume'],
        ['Vri---z--k', 0, 'not attached to a thin pool'],
        ['', 0, 'missing attributes'],
    ) {
        my ($attr, $allowed, $name) = @$case;
        reset_mocks();
        @lvm_results = ({ testvg => {
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } });
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
            return [$attr];
        };
        my $ok = eval {
            $verify_snapshot_postcondition->(
                $class, $scfg, 'vm-900001-disk-0',
                'snap_vm-900001-disk-0_before', 1,
            );
            1;
        };
        is($ok ? 1 : 0, $allowed, "$name decision");
        like($@, qr/read-only thin LV with activation-skip|^$/, "$name is explicit");
        is(scalar(@commands), 0, "$name executes no mutation");
    }
};

subtest 'snapshot acknowledgement uncertainty preserves the created snapshot' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_snapshot_postcondition = sub {
        die "snapshot postcondition unavailable: VG disappeared\n";
    };
    my $ok = eval {
        $class->volume_snapshot(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'acknowledgement uncertainty propagated');
    my @lines = command_lines();
    is(scalar(@lines), 1, 'only snapshot create attempted');
    like($lines[0], qr{/sbin/lvcreate}, 'snapshot may have been created');
    unlike(join("\n", @lines), qr{/sbin/lvremove}, 'uncertain snapshot not cleaned up');
};

subtest 'snapshot delete timeout is never blindly retried' => sub {
    reset_mocks();
    $command_failure = qr{/sbin/lvremove -f testvg/snap_};
    my $ok = eval {
        $class->volume_snapshot_delete(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'delete timeout propagated');
    is(scalar(command_lines()), 1, 'exactly one delete attempt');
};

subtest 'snapshot delete proves snapshot ownership before lvremove' => sub {
    for my $case (
        ['missing snapshot', undef, qr/is missing/],
        ['snapshot in another pool', { pool_lv => 'sltp-foreign' }, qr/is not in pool/],
    ) {
        reset_mocks();
        my ($name, $snapshot, $error) = @$case;
        my %volumes;
        $volumes{'snap_vm-900001-disk-0_before'} = $snapshot if defined($snapshot);
        @lvm_results = ({ testvg => \%volumes });
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_snapshot_postcondition
            = $verify_snapshot_postcondition;

        my $ok = eval {
            $class->volume_snapshot_delete(
                $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
            );
            1;
        };
        ok(!$ok, "$name rejected");
        like($@, $error, "$name failure is explicit");
        is(scalar(@commands), 0, "$name caused zero mutation");
    }
};

subtest 'volume-name validation rejects shell and path syntax' => sub {
    for my $name ('vm-100-disk-0;id', '../vm-100-disk-0', 'vm-100-disk-0/x') {
        my $ok = eval { $class->parse_volname($name); 1 };
        ok(!$ok, "rejected $name");
    }
    my @parsed = $class->parse_volname('vm-100-disk-0');
    is($parsed[0], 'images', 'valid volume classified as image');
    is($parsed[2], 100, 'VMID parsed');
    my @state = $class->parse_volname('vm-100-state-S1');
    is($state[0], 'images', 'vmstate classified as image content');
    is($state[2], 100, 'vmstate VMID parsed');
};

subtest 'vmstate, fleecing and cloud-init allocations require an active owned VM pool' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't',
            tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
    } }, { testvg => {
        'sltp-900001' => {
            lv_type => 't',
            tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
    } });
    my $name = $class->alloc_image(
        'sharedthin-test', $scfg, 900001, 'raw',
        'vm-900001-state-S1', 262144,
    );
    is($name, 'vm-900001-state-S1', 'exact PVE vmstate name accepted');
    my @vmstate_commands = command_lines();
    like(
        $vmstate_commands[0],
        qr{^/sbin/lvcreate -V 262144K -n vm-900001-state-S1 --thinpool testvg/sltp-900001$},
        'vmstate allocated in the existing per-VM pool',
    );

    reset_mocks();
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't',
            tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
    } }, { testvg => {
        'sltp-900001' => {
            lv_type => 't',
            tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
    } });
    is(
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw',
            'vm-900001-fleece-0', 262144,
        ),
        'vm-900001-fleece-0',
        'exact PVE backup-fleecing name accepted',
    );
    my @fleece_commands = command_lines();
    like(
        $fleece_commands[0],
        qr{^/sbin/lvcreate -V 262144K -n vm-900001-fleece-0 --thinpool testvg/sltp-900001$},
        'fleecing LV allocated in the existing per-VM pool',
    );

    reset_mocks();
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't',
            tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
    } }, { testvg => {
        'sltp-900001' => {
            lv_type => 't',
            tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
    } });
    is(
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw',
            'vm-900001-cloudinit', 4096,
        ),
        'vm-900001-cloudinit',
        'exact PVE cloud-init name accepted',
    );
    my @cloudinit_commands = command_lines();
    like(
        $cloudinit_commands[0],
        qr{^/sbin/lvcreate -V 4096K -n vm-900001-cloudinit --thinpool testvg/sltp-900001$},
        'cloud-init LV allocated in the existing per-VM pool',
    );

    for my $bad (
        'vm-900001-state-', 'vm-900001-state-../x',
        'vm-900001-memory-S1', 'vm-900001-fleece-x', 'vm-900001-fleece-0-extra',
        'vm-900001-cloudinit-extra',
    ) {
        reset_mocks();
        my $ok = eval {
            $class->alloc_image('sharedthin-test', $scfg, 900001, 'raw', $bad, 1);
            1;
        };
        ok(!$ok, "rejected non-canonical vmstate name $bad");
        is(scalar(command_lines()), 0, 'no mutation for rejected vmstate name');
    }

    reset_mocks();
    @lvm_results = ({ testvg => {} });
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw',
            'vm-900001-state-S1', 262144,
        );
        1;
    };
    ok(!$ok, 'state-only allocation without an owned pool is rejected');
    like($@, qr/owned VM pool .* does not exist/, 'missing owned pool is explicit');
    is(scalar(command_lines()), 0, 'no pool is implicitly initialized for vmstate');
};

subtest 'new VM allocation creates, converts, tags and uses one pool' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {} },
        { testvg => {
            'sltp-900001' => {
                lv_type => 't',
                tags => 'pve-slt-sid-sharedthin-test',
            },
        } },
    );
    my $name = $class->alloc_image(
        'sharedthin-test', $scfg, 900001, 'raw',
        'vm-900001-disk-0', 1024,
    );
    is($name, 'vm-900001-disk-0', 'allocated name returned');
    my @lines = command_lines();
    like($lines[0], qr{^/sbin/lvcreate -L 4194304K -n sltp-900001 testvg$}, 'backing LV command');
    like($lines[1], qr{^/sbin/lvconvert -y --type thin-pool testvg/sltp-900001$}, 'thin-pool conversion');
    like($lines[2], qr{--addtag pve-slt-sid-sharedthin-test}, 'storage ownership tag');
    like($lines[3], qr{^/sbin/lvcreate -V 1024K -n vm-900001-disk-0 --thinpool testvg/sltp-900001$}, 'thin LV allocation');
};

subtest 'proportional allocation creates physical burst headroom under reserve gate' => sub {
    reset_mocks();
    my $policy = {
        %$scfg,
        'slt-initial-pool-mode' => 'proportional',
        'slt-initial-pool-percent' => 50,
        'slt-vg-reserve-gib' => 10,
    };
    @lvm_results = (
        { testvg => {} },
        { testvg => {
            'sltp-900001' => { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' },
        } },
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_allocation_numeric_fields = sub {
        return (400 * 1024**3, 300 * 1024**3, 4 * 1024**2);
    };
    my $name = $class->alloc_image(
        'sharedthin-test', $policy, 900001, 'raw',
        'vm-900001-disk-0', 64 * 1024 * 1024,
    );
    is($name, 'vm-900001-disk-0', 'allocation completed');
    my @lines = command_lines();
    like($lines[0], qr{^/sbin/lvcreate -L 33554432K -n sltp-900001 testvg$}, '50% of 64 GiB was admitted');
    unlike(join("\n", @lines), qr{/sbin/lvextend}, 'new pool was sized directly, not grown after creation');
};

subtest 'multi-disk proportional allocation pre-grows from live usage' => sub {
    reset_mocks();
    my $policy = {
        %$scfg,
        'slt-initial-pool-mode' => 'proportional',
        'slt-initial-pool-percent' => 50,
        'slt-vg-reserve-gib' => 10,
    };
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    my $disk = { pool_lv => 'sltp-999900' };
    @lvm_results = (
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
    );
    my @numeric = (
        [400 * 1024**3, 300 * 1024**3, 4 * 1024**2],
        [32 * 1024**3, 84.375],
        [59 * 1024**3],
        [400 * 1024**3, 241 * 1024**3],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_allocation_numeric_fields = sub {
        return @{shift @numeric};
    };
    my $name = $class->alloc_image(
        'sharedthin-test', $policy, 999900, 'raw',
        'vm-999900-disk-1', 64 * 1024 * 1024,
    );
    is($name, 'vm-999900-disk-1', 'second disk allocation completed');
    my @lines = command_lines();
    like($lines[0], qr{^/sbin/lvextend -L 61865984K testvg/sltp-999900$}, 'pool target is 27 GiB used plus 32 GiB headroom');
    like($lines[1], qr{^/sbin/lvcreate -V 67108864K}, 'guest LV is created only after headroom postcondition');
    is(scalar(@numeric), 0, 'all live capacity and postcondition reads consumed');
};

subtest 'inactive pool allocation never repeats headroom growth' => sub {
    reset_mocks();
    my $policy = {
        %$scfg,
        'slt-initial-pool-mode' => 'elastic',
        'slt-burst-headroom-gib' => 16,
        'slt-vg-reserve-gib' => 10,
    };
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    my $disk = { pool_lv => 'sltp-999900' };
    @lvm_results = (
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
    );
    my @numeric = (
        [400 * 1024**3, 300 * 1024**3, 4 * 1024**2],
        [68 * 1024**3, undef],
        [400 * 1024**3, 300 * 1024**3],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_allocation_numeric_fields = sub {
        return @{shift @numeric};
    };
    my $warning = '';
    local $SIG{__WARN__} = sub { $warning .= shift };
    my $name = $class->alloc_image(
        'sharedthin-test', $policy, 999900, 'raw',
        'vm-999900-disk-1', 32 * 1024 * 1024,
    );
    is($name, 'vm-999900-disk-1', 'allocation can proceed without speculative pre-growth');
    my @lines = command_lines();
    is(scalar(grep { /lvextend/ } @lines), 0, 'inactive pool is not grown from unknown usage');
    like($warning, qr/automatic pre-growth disabled until usage is known/, 'operator sees the downgraded guarantee');
    is(scalar(@numeric), 0, 'capacity and reserve checks remain bounded');
};

subtest 'allocation reserve rejection runs zero mutations' => sub {
    reset_mocks();
    my $policy = {
        %$scfg,
        'slt-initial-pool-mode' => 'full',
        'slt-vg-reserve-gib' => 10,
    };
    @lvm_results = ({ testvg => {} });
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_allocation_numeric_fields = sub {
        return (100 * 1024**3, 20 * 1024**3, 4 * 1024**2);
    };
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $policy, 900001, 'raw',
            'vm-900001-disk-0', 64 * 1024 * 1024,
        );
        1;
    };
    ok(!$ok, 'insufficient physical capacity rejected');
    like($@, qr/no lvcreate\/lvextend was run/, 'reserve failure is explicit');
    is(scalar(command_lines()), 0, 'reserve gate executed zero mutation');
};

subtest 'uncertain allocation pre-grow is never retried or shrunk' => sub {
    for my $case (
        ['completed', 59 * 1024**3, 1],
        ['short', 40 * 1024**3, 0],
    ) {
        reset_mocks();
        my $policy = {
            %$scfg,
            'slt-initial-pool-mode' => 'proportional',
            'slt-initial-pool-percent' => 50,
            'slt-vg-reserve-gib' => 10,
        };
        my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
        my $disk = { pool_lv => 'sltp-999900' };
        @lvm_results = (
            { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
            { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        );
        my @numeric = (
            [400 * 1024**3, 300 * 1024**3, 4 * 1024**2],
            [32 * 1024**3, 84.375],
            [$case->[1]],
            [400 * 1024**3, 241 * 1024**3],
        );
        no warnings 'redefine';
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_allocation_numeric_fields = sub {
            return @{shift @numeric};
        };
        $command_failure = qr{/sbin/lvextend};
        my $ok = eval {
            $class->alloc_image(
                'sharedthin-test', $policy, 999900, 'raw',
                'vm-999900-disk-1', 64 * 1024 * 1024,
            );
            1;
        };
        is($ok ? 1 : 0, $case->[2], "$case->[0] postcondition classification");
        my @lines = command_lines();
        is(scalar(grep { /lvextend/ } @lines), 1, "$case->[0]: no grow retry");
        is(scalar(grep { /lvreduce/ } @lines), 0, "$case->[0]: no rollback shrink");
        is(scalar(grep { /lvcreate -V/ } @lines), $case->[2], "$case->[0]: guest creation follows only sufficient target");
    }
};

subtest 'unexpected post-allocation reserve loss preserves pool and refuses guest creation' => sub {
    reset_mocks();
    my $policy = {
        %$scfg,
        'slt-initial-pool-mode' => 'proportional',
        'slt-initial-pool-percent' => 50,
        'slt-vg-reserve-gib' => 10,
    };
    @lvm_results = ({ testvg => {} });
    my @numeric = (
        [100 * 1024**3, 80 * 1024**3, 4 * 1024**2],
        [100 * 1024**3, 9 * 1024**3],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_allocation_numeric_fields = sub {
        return @{shift @numeric};
    };
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $policy, 900001, 'raw',
            'vm-900001-disk-0', 16 * 1024 * 1024,
        );
        1;
    };
    ok(!$ok, 'actual reserve breach rejected after pool creation');
    like($@, qr/PARTIAL ALLOCATION.*pool preserved/s, 'preserved partial state reported');
    my @lines = command_lines();
    is(scalar(grep { /lvcreate -V/ } @lines), 0, 'guest LV was not created');
    is(scalar(grep { /lvremove|lvreduce/ } @lines), 0, 'pool was neither cleaned nor shrunk');
};

subtest 'shared LV autoactivation is a verified allocation postcondition' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $disable_and_verify_autoactivation;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled
        = $verify_autoactivation_disabled;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return ['0'];
    };
    ok(
        $class->_disable_and_verify_autoactivation('testvg', 'vm-900001-disk-0'),
        'disabled state accepted',
    );
    is(
        join(' ', @{$commands[0]}),
        '/sbin/lvchange --setautoactivation n testvg/vm-900001-disk-0',
        'autoactivation is explicitly disabled',
    );

    reset_mocks();
    my @reads;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        push @reads, [@{$_[0]}];
        return ['0'];
    };
    ok($class->_disable_and_verify_autoactivation(
        'testvg', 'vm-900001-disk-0', '/dev/mapper/3600abcd',
    ), 'device-scoped autoactivation postcondition succeeds');
    is(join(' ', @{$commands[0]}),
        '/sbin/lvchange --devices /dev/mapper/3600abcd --setautoactivation n testvg/vm-900001-disk-0',
        'autoactivation mutation is scoped to the exact device');
    is(join(' ', @{$reads[0]}),
        '/sbin/lvs --readonly --devices /dev/mapper/3600abcd --binary --noheadings -o lv_autoactivation testvg/vm-900001-disk-0',
        'autoactivation verification is scoped to the exact device');

    reset_mocks();
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $disable_and_verify_autoactivation;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return [''];
    };
    my $ok = eval {
        $class->_disable_and_verify_autoactivation('testvg', 'vm-900001-disk-0');
        1;
    };
    ok(!$ok, 'enabled state rejected');
    like($@, qr/expected '0'/, 'postcondition error is explicit');
    is(scalar(@commands), 1, 'no speculative retry or cleanup');
};

subtest 'autoactivation failure preserves allocated guest LV as partial' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {} },
        { testvg => {
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
        } },
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation = sub {
        my ($class, $vg, $lv) = @_;
        return 1 if $lv eq 'sltp-900001';
        die "autoactivation postcondition failed\n";
    };
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw', 'vm-900001-disk-0', 1024,
        );
        1;
    };
    ok(!$ok, 'allocation acknowledgement refused');
    like($@, qr/PARTIAL ALLOCATION/, 'partial state is explicit');
    like($@, qr/autoactivation state is unconfirmed/, 'preserved object state reported');
    unlike(join("\n", command_lines()), qr/lvremove/, 'allocated LV was not cleaned up');
};

subtest 'resize preserves the autoactivation invariant before mutation' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled
        = $verify_autoactivation_disabled;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return ['1'];
    };
    my $ok = eval {
        $class->volume_resize(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 2048, 0, undef,
        );
        1;
    };
    ok(!$ok, 'autoactivation-enabled volume resize rejected');
    like($@, qr/lv_autoactivation='1'/, 'resize precondition is explicit');
    is(scalar(@commands), 0, 'no lvextend or flag rewrite attempted');
};

subtest 'snapshot autoactivation uncertainty preserves created snapshot' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation = sub {
        die "snapshot autoactivation outcome unknown\n";
    };
    my $ok = eval {
        $class->volume_snapshot(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'snapshot acknowledgement refused');
    like($@, qr/autoactivation outcome unknown/, 'uncertain state propagated');
    my @lines = command_lines();
    is(scalar(@lines), 1, 'only snapshot creation was attempted');
    like($lines[0], qr{/sbin/lvcreate}, 'created snapshot remains for review');
    unlike(join("\n", @lines), qr{/sbin/lvremove}, 'no fallback cleanup');
};

subtest 'foreign existing pool is rejected before mutation' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {
        'sltp-900001' => { lv_type => 't', tags => 'foreign-tag' },
    } });
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw',
            'vm-900001-disk-0', 1024,
        );
        1;
    };
    ok(!$ok, 'allocation rejected');
    like($@, qr/does not belong/, 'ownership error is explicit');
    is(scalar(@commands), 0, 'no mutating command executed');
};

subtest 'VMID reuse rejects stale owned pool and stale snapshots without mutation' => sub {
    for my $case (
        ['empty owned pool', {}],
        ['stale snapshot', {
            'snap_vm-999900-disk-0_old' => { pool_lv => 'sltp-999900' },
        }],
    ) {
        reset_mocks();
        @lvm_results = ({ testvg => {
            'sltp-999900' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            %{$case->[1]},
        } });
        my $ok = eval {
            $class->alloc_image(
                'sharedthin-test', $scfg, 999900, 'raw',
                'vm-999900-disk-0', 1024,
            );
            1;
        };
        ok(!$ok, "$case->[0]: rejected");
        like($@, qr/VMID reuse requires recovery review/, "$case->[0]: deterministic recovery policy");
        is(scalar(@commands), 0, "$case->[0]: zero mutation");
    }
};

subtest 'VMID reuse rejects a foreign existing LV before pool creation' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {
        'vm-999900-disk-0' => { pool_lv => 'foreign-pool' },
    } });
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $scfg, 999900, 'raw',
            'vm-999900-disk-0', 1024,
        );
        1;
    };
    ok(!$ok, 'foreign LV rejected');
    like($@, qr/naming is not ownership proof/, 'VMID/name collision is not adopted');
    is(scalar(@commands), 0, 'pool was not created and foreign LV was untouched');
};

subtest 'VMID reuse allows adding a disk only to an actively owned pool' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {
            'sltp-999900' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-999900-disk-0' => { pool_lv => 'sltp-999900' },
        } },
        { testvg => {
            'sltp-999900' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-999900-disk-0' => { pool_lv => 'sltp-999900' },
        } },
    );
    my $name = $class->alloc_image(
        'sharedthin-test', $scfg, 999900, 'raw',
        'vm-999900-disk-1', 1024,
    );
    is($name, 'vm-999900-disk-1', 'second disk allocation allowed');
    is(scalar(grep { /lvcreate -V/ } command_lines()), 1, 'exactly one guest LV created');
    unlike(join("\n", command_lines()), qr{lvconvert|--addtag}, 'existing pool was not recreated or adopted');
};

subtest 'clean VMID reuse completes two full lifecycles with zero artifacts' => sub {
    reset_mocks();
    my $pool = {
        lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
    };
    my $disk = { pool_lv => 'sltp-999900' };
    my $snap = { pool_lv => 'sltp-999900' };
    @lvm_results = (
        { testvg => {} },
        { testvg => { 'sltp-999900' => $pool } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool } },
        { testvg => {} },
        { testvg => { 'sltp-999900' => $pool } },
        { testvg => {
            'sltp-999900' => $pool,
            'vm-999900-disk-0' => $disk,
            'snap_vm-999900-disk-0_before' => $snap,
        } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool, 'vm-999900-disk-0' => $disk } },
        { testvg => { 'sltp-999900' => $pool } },
    );

    is(
        $class->alloc_image('sharedthin-test', $scfg, 999900, 'raw', 'vm-999900-disk-0', 1024),
        'vm-999900-disk-0',
        'first lifecycle allocated',
    );
    $class->free_image('sharedthin-test', $scfg, 'vm-999900-disk-0', 0);
    is(
        $class->alloc_image('sharedthin-test', $scfg, 999900, 'raw', 'vm-999900-disk-0', 1024),
        'vm-999900-disk-0',
        'same VMID allocated after complete cleanup',
    );
    $class->volume_snapshot($scfg, 'sharedthin-test', 'vm-999900-disk-0', 'before');
    $class->volume_snapshot_rollback($scfg, 'sharedthin-test', 'vm-999900-disk-0', 'before');
    $class->volume_snapshot_delete($scfg, 'sharedthin-test', 'vm-999900-disk-0', 'before');
    $class->free_image('sharedthin-test', $scfg, 'vm-999900-disk-0', 0);

    my @lines = command_lines();
    is(scalar(grep { /lvconvert -y --type thin-pool testvg\/sltp-999900/ } @lines), 2, 'pool created exactly once per lifecycle');
    is(scalar(grep { m{lvremove -f testvg/sltp-999900$} } @lines), 2, 'pool removed exactly once per lifecycle');
    is(scalar(grep { /snap_vm-999900-disk-0_before/ } @lines) > 0, 1, 'snapshot lifecycle exercised');
    is(scalar(@lvm_results), 0, 'mock inventory ended with no unconsumed artifact state');
};

subtest 'failed thin LV allocation preserves the newly-created pool' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {} },
        { testvg => {
            'sltp-900001' => {
                lv_type => 't',
                tags => 'pve-slt-sid-sharedthin-test',
            },
        } },
    );
    $command_failure = qr{/sbin/lvcreate -V};
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw',
            'vm-900001-disk-0', 1024,
        );
        1;
    };
    ok(!$ok, 'allocation failure propagated');
    my @lines = command_lines();
    unlike(join("\n", @lines), qr{/sbin/lvremove}, 'no destructive cleanup attempted');
    like($@, qr/PARTIAL ALLOCATION/, 'partial allocation state reported');
    like($@, qr/Storage: sharedthin-test.*VMID: 900001.*Object: vm-900001-disk-0.*Pool: sltp-900001/s, 'recovery identity reported');
    like($@, qr/Automatic cleanup was intentionally NOT performed/, 'no-cleanup decision reported');
    like($@, qr/verify VG UUID, PV UUID, WWID, ownership tags, PVE references, quorum, and the storage lock/, 'recovery requirements reported');
};

subtest 'pool conversion failure preserves the backing LV without cleanup' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {} });
    $command_failure = qr{/sbin/lvconvert};
    my $ok = eval {
        $class->alloc_image(
            'sharedthin-test', $scfg, 900001, 'raw',
            'vm-900001-disk-0', 1024,
        );
        1;
    };
    ok(!$ok, 'conversion failure propagated');
    unlike(join("\n", command_lines()), qr{/sbin/lvremove}, 'ambiguous backing LV not deleted');
    like($@, qr/PARTIAL ALLOCATION/, 'preserved partial object is explicit');
    like($@, qr/backing object 'testvg\/sltp-900001' may exist/, 'uncertain backing object state reported');
    like($@, qr/Automatic cleanup was intentionally NOT performed/, 'automatic cleanup refusal reported');
};

subtest 'free removes disk snapshots, disk and tagged empty pool' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
        } },
        { testvg => {
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
        } },
        { testvg => {
            'sltp-900001' => {
                lv_type => 't',
                tags => 'pve-slt-sid-sharedthin-test',
            },
        } },
    );
    $class->free_image(
        'sharedthin-test', $scfg, 'vm-900001-disk-0', 0,
    );
    my @lines = command_lines();
    is_deeply(
        \@lines,
        [
            '/sbin/lvremove -f testvg/snap_vm-900001-disk-0_before',
            '/sbin/lvremove -f testvg/vm-900001-disk-0',
            '/sbin/lvremove -f testvg/sltp-900001',
        ],
        'expected lifecycle cleanup only',
    );
};

subtest 'free refuses a legacy untagged pool without deleting its disk' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'sltp-900001' => { lv_type => 't', tags => '' },
        } },
        { testvg => {
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'sltp-900001' => { lv_type => 't', tags => '' },
        } },
        { testvg => {
            'sltp-900001' => { lv_type => 't', tags => '' },
        } },
    );
    my $ok = eval {
        $class->free_image(
            'sharedthin-test', $scfg, 'vm-900001-disk-0', 0,
        );
        1;
    };
    ok(!$ok, 'legacy delete rejected');
    like($@, qr/ownership is not positively proven/, 'legacy ownership error is explicit');
    is(scalar(@commands), 0, 'legacy disk and pool untouched');
};

subtest 'free refuses a volume that is not in the expected pool' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {
        'vm-900001-disk-0' => { pool_lv => 'foreign-pool' },
        'sltp-900001' => {
            lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
        },
    } });
    my $ok = eval {
        $class->free_image(
            'sharedthin-test', $scfg, 'vm-900001-disk-0', 0,
        );
        1;
    };
    ok(!$ok, 'wrong-pool volume rejected');
    like($@, qr/not in expected pool/, 'safe refusal message');
    is(scalar(@commands), 0, 'no mutating command executed');
};

subtest 'free refuses ambiguous snapshot flags before cleanup mutation' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_snapshot_postcondition
        = $verify_snapshot_postcondition;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return ['Vwi---tz--'];
    };

    my $ok = eval {
        $class->free_image('sharedthin-test', $scfg, 'vm-900001-disk-0', 0);
        1;
    };
    ok(!$ok, 'writable/noncanonical snapshot rejected');
    like($@, qr/read-only thin LV with activation-skip/, 'ambiguous snapshot state is explicit');
    is(scalar(@commands), 0, 'snapshot, disk, and pool were all preserved');
};

subtest 'free refuses a pool tagged for another storage' => sub {
    reset_mocks();
    @lvm_results = ({ testvg => {
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        'sltp-900001' => {
            lv_type => 't', tags => 'pve-slt-sid-other-storage',
        },
    } });
    my $ok = eval {
        $class->free_image(
            'sharedthin-test', $scfg, 'vm-900001-disk-0', 0,
        );
        1;
    };
    ok(!$ok, 'foreign tagged pool rejected');
    like($@, qr/owned by another storage/, 'ownership conflict is explicit');
    is(scalar(@commands), 0, 'no mutating command executed');
};

subtest 'one-of-many disk deletion preserves the shared per-VM pool' => sub {
    reset_mocks();
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    @lvm_results = (
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'vm-900001-disk-1' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'vm-900001-disk-1' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-1' => { pool_lv => 'sltp-900001' },
        } },
    );
    $class->free_image('sharedthin-test', $scfg, 'vm-900001-disk-0', 0);
    my @lines = command_lines();
    is(scalar(grep { /lvremove/ } @lines), 1, 'only requested disk removed');
    unlike(join("\n", @lines), qr{sltp-900001$}, 'referenced pool preserved');
};

subtest 'unexpected LV reference prevents empty-pool cleanup' => sub {
    reset_mocks();
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    @lvm_results = (
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'unexpected-reference' => { pool_lv => 'sltp-900001' },
        } },
    );
    $class->free_image('sharedthin-test', $scfg, 'vm-900001-disk-0', 0);
    unlike(join("\n", command_lines()), qr{lvremove -f testvg/sltp-900001}, 'pool with unknown reference preserved');
};

subtest 'VG loss after disk delete is UNAVAILABLE and never triggers pool cleanup' => sub {
    reset_mocks();
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    @lvm_results = (
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        {},
    );
    my $ok = eval {
        $class->free_image('sharedthin-test', $scfg, 'vm-900001-disk-0', 0);
        1;
    };
    ok(!$ok, 'VG loss reported');
    like($@, qr/UNAVAILABLE/, 'transport/VG loss classification explicit');
    my @lines = command_lines();
    is(scalar(grep { /lvremove/ } @lines), 1, 'only requested disk delete was attempted');
    unlike(join("\n", @lines), qr{sltp-900001$}, 'no pool cleanup after VG loss');
};

subtest 'disk delete failure never broadens cleanup scope' => sub {
    reset_mocks();
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    @lvm_results = (
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
    );
    $command_failure = qr{/sbin/lvremove -f testvg/vm-900001-disk-0};
    my $ok = eval {
        $class->free_image('sharedthin-test', $scfg, 'vm-900001-disk-0', 0);
        1;
    };
    ok(!$ok, 'disk delete failure propagated');
    my @lines = command_lines();
    is(scalar(@lines), 1, 'only requested disk delete attempted');
    unlike(join("\n", @lines), qr{sltp-900001$}, 'pool cleanup not attempted');
};

subtest 'pool cleanup failure is reported without retry' => sub {
    reset_mocks();
    my $pool = { lv_type => 't', tags => 'pve-slt-sid-sharedthin-test' };
    @lvm_results = (
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'sltp-900001' => $pool,
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => { 'sltp-900001' => $pool } },
    );
    $command_failure = qr{/sbin/lvremove -f testvg/sltp-900001};
    my $ok = eval {
        $class->free_image('sharedthin-test', $scfg, 'vm-900001-disk-0', 0);
        1;
    };
    ok(!$ok, 'pool cleanup failure propagated');
    my @lines = command_lines();
    is(scalar(@lines), 2, 'disk delete and one pool cleanup attempt only');
    is(scalar(grep { m{lvremove -f testvg/sltp-900001$} } @lines), 1, 'pool cleanup not retried');
};

subtest 'resize uses an argv array and explicit byte size' => sub {
    reset_mocks();
    $class->volume_resize(
        $scfg, 'sharedthin-test', 'vm-900001-disk-0',
        1073741824, 0, undef,
    );
    is(
        join(' ', @{$commands[0]}),
        '/sbin/lvextend -L 1073741824B testvg/vm-900001-disk-0',
        'grow command is deterministic',
    );
    is(scalar(@locks), 1, 'resize holds one PVE storage lock');
    is($locks[0]->[0], 'sharedthin-test', 'correct storage locked');
};

subtest 'snapshot create and delete each hold the PVE storage lock' => sub {
    reset_mocks();
    $class->volume_snapshot(
        $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
    );
    $class->volume_snapshot_delete(
        $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
    );
    is(scalar(@locks), 2, 'one lock per snapshot mutation');
    like(join(' ', @{$commands[0]}), qr{^/sbin/lvcreate }, 'snapshot create executed');
    like(join(' ', @{$commands[1]}), qr{^/sbin/lvremove }, 'snapshot delete executed');
};

subtest 'rollback materializes replacement before removing current disk' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    @lvm_results = (
        { testvg => {
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
    );
    $class->volume_snapshot_rollback(
        $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
    );
    my @lines = command_lines();
    like($lines[0], qr{^/sbin/lvcreate -kn -n slt-rb-vm-900001-disk-0-\d+ -s testvg/snap_vm-900001-disk-0_before$}, 'replacement created first');
    like($lines[1], qr{^/sbin/lvchange --setautoactivation n testvg/slt-rb-}, 'replacement autoactivation disabled');
    is($lines[2], '/sbin/lvremove -f testvg/vm-900001-disk-0', 'current disk removed only after replacement exists');
    like($lines[3], qr{^/sbin/lvrename testvg slt-rb-vm-900001-disk-0-\d+ vm-900001-disk-0$}, 'replacement renamed atomically');
    is(scalar(@locks), 1, 'rollback holds one PVE storage lock');
};

subtest 'rollback preparation failure leaves current disk untouched' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
    } });
    $command_failure = qr{/sbin/lvcreate -kn};
    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'preparation failure propagated');
    unlike(join("\n", command_lines()), qr{lvremove -f testvg/vm-900001-disk-0}, 'current disk was not removed');
};

subtest 'rollback rejects noncanonical snapshot before any mutation' => sub {
    reset_mocks();
    @lvm_results = (
        { testvg => {
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_snapshot_postcondition
        = $verify_snapshot_postcondition;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return ['Vwi---tz--'];
    };

    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'writable/noncanonical rollback source rejected');
    like($@, qr/read-only thin LV with activation-skip/, 'rollback refusal is explicit');
    is(scalar(@commands), 0, 'no replacement, delete, rename, or cleanup mutation');
};

subtest 'rollback failure after replacement creation preserves both objects' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
    } });
    $command_failure = qr{/sbin/lvchange --setautoactivation};
    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'post-creation failure propagated');
    my @lines = command_lines();
    like($lines[0], qr{^/sbin/lvcreate}, 'replacement was prepared');
    unlike(join("\n", @lines), qr{/sbin/lvremove}, 'neither origin nor replacement was deleted');
    like($@, qr/original .* remains intact.*replacement .* intentionally preserved/s, 'both preserved states reported');
    like($@, qr/automatic cleanup was NOT performed/, 'cleanup refusal reported');
};

subtest 'rollback origin-delete failure preserves origin and replacement' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
    } });
    $command_failure = qr{/sbin/lvremove -f testvg/vm-900001-disk-0};
    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'origin delete failure propagated');
    my @lines = command_lines();
    is(scalar(grep { /lvrename/ } @lines), 0, 'replacement was not renamed over uncertain origin');
    is(scalar(grep { m{lvremove -f testvg/slt-rb-} } @lines), 0, 'replacement was not cleaned up');
    like($@, qr/original .* remains intact.*replacement .* intentionally preserved/s, 'recoverable state reported');
};

subtest 'rollback postcondition failure performs no destructive recovery' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    @lvm_results = (
        { testvg => {
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {} },
    );
    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'postcondition failure propagated');
    like($@, qr/rollback postcondition failed/, 'postcondition error explicit');
    my @lines = command_lines();
    is(scalar(grep { /lvrename/ } @lines), 1, 'rollback transaction reached rename once');
    is(scalar(grep { m{lvremove -f testvg/slt-rb-} } @lines), 0, 'no speculative cleanup after postcondition failure');
};

subtest 'rollback final autoactivation uncertainty preserves restored origin' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub {
        die "restored origin autoactivation unknown\n";
    };
    @lvm_results = (
        { testvg => {
            'sltp-900001' => {
                lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
            },
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
            'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
        } },
        { testvg => {
            'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        } },
    );
    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'uncertain restored-origin state rejected');
    like($@, qr/restored origin autoactivation unknown/, 'final invariant error explicit');
    my @lines = command_lines();
    is(scalar(grep { /lvrename/ } @lines), 1, 'rollback result was preserved under canonical name');
    is(scalar(grep { /lvremove/ } @lines), 1, 'no destructive recovery after final uncertainty');
};

subtest 'rollback rename failure preserves replacement for recovery' => sub {
    reset_mocks();
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation
        = $record_disable_autoactivation;
    @lvm_results = ({ testvg => {
        'sltp-900001' => {
            lv_type => 't', tags => 'pve-slt-sid-sharedthin-test',
        },
        'vm-900001-disk-0' => { pool_lv => 'sltp-900001' },
        'snap_vm-900001-disk-0_before' => { pool_lv => 'sltp-900001' },
    } });
    $command_failure = qr{/sbin/lvrename};
    my $ok = eval {
        $class->volume_snapshot_rollback(
            $scfg, 'sharedthin-test', 'vm-900001-disk-0', 'before',
        );
        1;
    };
    ok(!$ok, 'rename failure propagated');
    like($@, qr/rollback data is preserved/, 'manual recovery object reported');
    my @lines = command_lines();
    my ($temporary) = $lines[0] =~ /-n (slt-rb-\S+) -s/;
    unlike(join("\n", @lines), qr{lvremove -f testvg/\Q$temporary\E}, 'replacement was not deleted after origin removal');
};

subtest 'thin volumes advertise zero-initialized copy destinations' => sub {
    ok(
        $class->volume_has_feature(
            $scfg, 'sparseinit', 'sharedthin-test', 'vm-900001-disk-0', undef, 0,
        ),
        'current thin LV advertises sparseinit',
    );
    ok(
        !$class->volume_has_feature(
            $scfg, 'sparseinit', 'sharedthin-test', 'vm-900001-disk-0', 'before', 0,
        ),
        'snapshot source does not claim to be a new zero-initialized destination',
    );
    ok(
        $class->volume_has_feature(
            $scfg, 'copy', 'sharedthin-test', 'vm-900001-disk-0', undef, 0,
        ),
        'existing copy capability remains enabled',
    );
};

subtest 'allocation mode defaults to thin and thick mode requires explicit safety identity' => sub {
    is($class->_allocation_mode({}), 'thin', 'existing storage defaults to thin');
    is(
        $class->_allocation_mode({ 'slt-allocation-mode' => 'thick-generations' }),
        'thick-generations',
        'thick mode is explicit',
    );
    eval { $class->_allocation_mode({ 'slt-allocation-mode' => 'unknown' }) };
    like($@, qr/unknown SharedLvmThin allocation mode/, 'unknown mode fails closed');

    my $thick = {
        shared => 1,
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
        'slt-vg-reserve-gib' => 5,
    };
    ok(
        $class->_require_thick_identity_config('thick-test', $thick),
        'fully pinned shared thick storage is accepted',
    );
    for my $missing (qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid)) {
        my %incomplete = %$thick;
        delete $incomplete{$missing};
        eval { $class->_require_thick_identity_config('thick-test', \%incomplete) };
        like($@, qr/requires '\Q$missing\E'/, "$missing is mandatory");
    }
    my %not_shared = (%$thick, shared => 0);
    eval { $class->_require_thick_identity_config('thick-test', \%not_shared) };
    like($@, qr/must be configured as shared/, 'non-shared thick mode is rejected');
    my %no_reserve = %$thick;
    delete $no_reserve{'slt-vg-reserve-gib'};
    eval { $class->_require_thick_identity_config('thick-test', \%no_reserve) };
    like($@, qr/requires a protected VG reserve/, 'thick mode cannot consume the last VG extents');
};

subtest 'thick tag mutation enforces exact precondition and postcondition' => sub {
    reset_mocks();
    my @reads = (['old-a,old-b'], ['new-a,new-b']);
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @reads;
    };
    ok(
        $class->_change_exact_tags(
            'testvg', 'anchor', ['old-a', 'old-b'], ['new-a', 'new-b'],
            'test mutation failed',
        ),
        'exact mutation succeeds',
    );
    is(scalar(@commands), 1, 'exact mutation executes once');

    reset_mocks();
    @reads = (['foreign-tag']);
    eval {
        $class->_change_exact_tags(
            'testvg', 'anchor', ['old-a'], ['new-a'], 'must not run',
        );
    };
    like($@, qr/precondition failed/, 'foreign pre-state fails closed');
    is(scalar(@commands), 0, 'failed precondition executes no mutation');

    reset_mocks();
    @reads = (['old-a'], ['unexpected']);
    eval {
        $class->_change_exact_tags(
            'testvg', 'anchor', ['old-a'], ['new-a'], 'mutation failed',
        );
    };
    like($@, qr/postcondition failed/, 'unexpected post-state is recovery-required');
    is(scalar(@commands), 1, 'uncertain outcome is never retried');
};

subtest 'thick delete is exact, transaction-scoped, and never broadens cleanup' => sub {
    reset_mocks();
    my $storeid = 'thick-test';
    my $volname = 'vm-900001-disk-0';
    my $cfg = {
        shared => 1,
        'slt-vgname' => 'testvg',
        'slt-allocation-mode' => 'thick-generations',
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
        'slt-vg-reserve-gib' => 5,
    };
    my $namespace = $cfg->{'slt-expected-vg-uuid'};
    my $anchor = PVE::SharedLvmThinThick::anchor_name($namespace, $volname);
    my $head = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 0);
    my $tx = '1' x 32;
    my $anchor_tags = join(',', @{PVE::SharedLvmThinThick::anchor_tags(
        sid => $storeid, vol => $volname, phase => 'MATERIALIZED', tx => $tx,
        op => 'ALLOC', snapshot => 'none', source => $head,
        old => $head, new => $head, head => $head, generation => 0, region => 8,
    )});
    my $head_tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'head', generation => 0,
    )});
    my $inventory = { testvg => {
        $anchor => { tags => $anchor_tags, lv_size => 4096 },
        $head => { tags => $head_tags, lv_size => 4096 },
    } };
    my @intent_events;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 0; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my (undef, undef, undef, $code) = @_;
        return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_new_transaction_id = sub { return '2' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_vg_state_digest = sub { return 'a' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_set_vg_intent = sub {
        push @intent_events, 'OPEN'; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_clear_vg_intent = sub {
        push @intent_events, 'CLEAR'; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub { return 1; };
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub {
        return $inventory if !@commands;
        return {};
    };
    is($class->free_image($storeid, $cfg, $volname, 0), undef, 'exact thick delete completes');
    is_deeply([command_lines()], [
        "/sbin/lvremove -f testvg/$head",
        "/sbin/lvremove -f testvg/$anchor",
    ], 'only the exact head and anchor are removed');
    is_deeply(\@intent_events, ['OPEN', 'CLEAR'], 'intent brackets the verified delete');

    reset_mocks();
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub { return ['1']; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub { return 1; };
    eval { $class->free_image($storeid, $cfg, $volname, 0) };
    like($@, qr/refusing to delete open/, 'open frontend blocks delete');
    is(scalar(@commands), 0, 'active frontend rejection performs zero mutation');

    reset_mocks();
    my @mapper_exists = (1, 0);
    @intent_events = ();
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub {
        return shift(@mapper_exists) // 0;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub { return ['0']; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my (undef, undef, undef, $code) = @_; return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_new_transaction_id = sub { return '2' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_vg_state_digest = sub { return 'a' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_set_vg_intent = sub {
        push @intent_events, 'OPEN'; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_clear_vg_intent = sub {
        push @intent_events, 'CLEAR'; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub { return 1; };
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub {
        return $inventory if !@commands;
        return {};
    };
    is($class->free_image($storeid, $cfg, $volname, 0), undef,
        'idle verified frontend is dismantled before cancelled-target cleanup');
    is_deeply([command_lines()], [
        "/sbin/dmsetup --verifyudev remove --retry " . PVE::SharedLvmThinThick::mapper_name($namespace, $volname),
        "/sbin/lvchange -an testvg/$anchor testvg/$head",
        "/sbin/lvremove -f testvg/$head",
        "/sbin/lvremove -f testvg/$anchor",
    ], 'cancel cleanup removes only the idle frontend and exact owned objects');
    is_deeply(\@intent_events, ['OPEN', 'CLEAR'], 'cancel cleanup remains transaction-bracketed');

    reset_mocks();
    my $snapshot = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 1);
    my $with_snapshot = { testvg => { %{$inventory->{testvg}}, $snapshot => { tags => '' } } };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 0; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my (undef, undef, undef, $code) = @_; return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { return 1; };
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub { return $with_snapshot; };
    eval { $class->free_image($storeid, $cfg, $volname, 0) };
    like($@, qr/snapshots or ambiguous generations remain/, 'dependent generation blocks delete');
    is(scalar(@commands), 0, 'dependency rejection performs zero mutation');
};

subtest 'thick resize is grow-only and publishes zeroed capacity after exact proof' => sub {
    reset_mocks();
    my $storeid = 'thick-test';
    my $volname = 'vm-900001-disk-0';
    my $cfg = {
        shared => 1,
        'slt-vgname' => 'testvg',
        'slt-allocation-mode' => 'thick-generations',
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
        'slt-vg-reserve-gib' => 5,
    };
    my $namespace = $cfg->{'slt-expected-vg-uuid'};
    my $anchor = PVE::SharedLvmThinThick::anchor_name($namespace, $volname);
    my $head = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 0);
    my $tx = '3' x 32;
    my $anchor_tags = join(',', @{PVE::SharedLvmThinThick::anchor_tags(
        sid => $storeid, vol => $volname, phase => 'MATERIALIZED', tx => $tx,
        op => 'ALLOC', snapshot => 'none', source => $head,
        old => $head, new => $head, head => $head, generation => 0, region => 8,
    )});
    my $head_tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'head', generation => 0,
    )});
    my $old = 4 * 1024 * 1024;
    my $new = 8 * 1024 * 1024;
    my $old_inventory = { testvg => {
        $anchor => { tags => $anchor_tags, lv_size => $old },
        $head => { tags => $head_tags, lv_size => $old },
    } };
    my $new_inventory = { testvg => {
        $anchor => { tags => $anchor_tags, lv_size => $old },
        $head => { tags => $head_tags, lv_size => $new },
    } };
    my @inventories = ($old_inventory, $new_inventory, $new_inventory);
    my @intent_events;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my (undef, undef, undef, $code) = @_; return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_exact_vg_intent = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_new_transaction_id = sub { return '4' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_vg_state_digest = sub { return 'b' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_set_vg_intent = sub {
        push @intent_events, 'OPEN'; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_clear_vg_intent = sub {
        push @intent_events, 'CLEAR'; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_capacity_gate = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 0; };
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub { return shift @inventories; };
    is($class->volume_resize($cfg, $storeid, $volname, $new, 0, undef), undef,
        'offline grow completes');
    my @resize_commands = command_lines();
    like($resize_commands[0], qr{^/sbin/lvextend -L ${new}B testvg/\Q$head\E$},
        'backing head is extended exactly once');
    like(join("\n", @resize_commands), qr{/usr/bin/dd if=/dev/zero},
        'new range is explicitly zero initialized');
    like(join("\n", @resize_commands), qr{/sbin/blockdev --flushbufs},
        'zeroed range is flushed before publication');
    is(scalar(grep { m{/sbin/lvextend} } @resize_commands), 1,
        'lvextend is never retried');
    is_deeply(\@intent_events, ['OPEN', 'CLEAR'], 'intent clears only after publication proof');

    reset_mocks();
    @inventories = ($old_inventory);
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub { return shift @inventories; };
    eval { $class->volume_resize($cfg, $storeid, $volname, $old - 512, 0, undef) };
    like($@, qr/shrinking thick-generations volumes is not supported/, 'shrink fails closed');
    is(scalar(@commands), 0, 'shrink rejection performs zero mutation');

    {
        reset_mocks();
        @inventories = ($old_inventory, $new_inventory, $new_inventory);
        my @frontend_sectors;
        local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub { return shift @inventories; };
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 1; };
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub {
            my (undef, undef, undef, undef, $sectors) = @_;
            push @frontend_sectors, $sectors;
            return 1;
        };
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
            return ["0 " . int($new / 512) . " linear 253:7 0"];
        };
        is($class->volume_resize($cfg, $storeid, $volname, $new, 1, undef), undef,
            'online grow completes');
        my @online = command_lines();
        my ($reload) = grep { /dmsetup --verifyudev reload/ } @online;
        my ($suspend) = grep { /dmsetup --verifyudev suspend --noflush/ } @online;
        my ($resume) = grep { /dmsetup --verifyudev resume/ } @online;
        ok(defined($reload) && defined($suspend) && defined($resume),
            'online cutover contains reload, bounded noflush suspend, and resume');
        my %position;
        for my $index (0 .. $#online) {
            $position{reload} = $index if $online[$index] =~ /dmsetup --verifyudev reload/;
            $position{suspend} = $index if $online[$index] =~ /dmsetup --verifyudev suspend --noflush/;
            $position{resume} = $index if $online[$index] =~ /dmsetup --verifyudev resume/;
        }
        ok($position{reload} < $position{suspend} && $position{suspend} < $position{resume},
            'inactive table is verified before the explicit suspend/resume cutover');
        is_deeply(\@frontend_sectors, [int($old / 512), int($old / 512), int($new / 512)],
            'frontend identity is proven before load, before cutover, and after resume');
    }
};

subtest 'thick clone frontend and hydration wait require exact evidence' => sub {
    reset_mocks();
    my $cfg = {
        'slt-vgname' => 'test-vg',
        'slt-expected-vg-uuid' => 'vg-uuid',
    };
    my $volname = 'vm-900001-disk-0';
    my $mapper = PVE::SharedLvmThinThick::mapper_name('vg-uuid', $volname);
    my $uuid = 'SLT-TG2-' . PVE::SharedLvmThinThick::object_key('vg-uuid', $volname);
    my @reads = (
        ["$uuid|writeable"],
        ['0 8192 clone 253:1 253:2 253:3 8 2 no_hydration no_discard_passdown 4 hydration_threshold 32 hydration_batch_size 32'],
        ['3 dependencies : (test--vg-meta--x), (source-map), (test--vg-new--x)'],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @reads;
    };
    ok($class->_thick_verify_clone_frontend(
        $cfg, $volname, sectors => 8192, region => 8,
        meta => 'meta-x', new => 'new-x', source_map => 'source-map',
    ), 'clone frontend identity, table, and exact dependency set pass');
    is(scalar(@reads), 0, 'all exact clone evidence was consumed');

    my @status = ([8, 8, 0]);
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_clone_status = sub {
        my (undef, undef, $must_be_complete) = @_;
        my @row = @{shift @status};
        die "incomplete\n" if $must_be_complete && ($row[0] != $row[1] || $row[2] != 0);
        return @row;
    };
    ok($class->_thick_wait_for_hydration($mapper, 60),
        'already complete hydration returns without waiting');
    is(scalar(@commands), 0, 'already complete hydration spawns no wait probe');

    reset_mocks();
    @status = ([1, 8, 0], [2, 8, 1], [8, 8, 0]);
    @reads = (['17']);
    ok($class->_thick_wait_for_hydration($mapper, 60),
        'one event-numbered wait closes the completion race');
    is(scalar(@commands), 1, 'exactly one potentially blocking wait is spawned');
    like((command_lines())[0], qr{/usr/bin/timeout --kill-after=5s 60s /sbin/dmsetup wait \Q$mapper\E 17$},
        'wait is bounded and tied to the captured event number');

    reset_mocks();
    @status = ([1, 8, 0], [2, 8, 1], [2, 8, 0]);
    @reads = (['18']);
    eval { $class->_thick_wait_for_hydration($mapper, 60) };
    like($@, qr/not positively complete/, 'non-completion is recovery-required');
    is(scalar(@commands), 1, 'non-completion never spawns a second wait probe');
};

subtest 'thick hydration tuning is bounded and internally consistent' => sub {
    is_deeply([$class->_thick_hydration_tuning({})], [32, 32],
        'conservative default tuning is explicit');
    is_deeply([$class->_thick_hydration_tuning({
        'slt-tg-hydration-threshold' => 64,
        'slt-tg-hydration-batch-size' => 16,
    })], [64, 16], 'valid custom tuning is accepted');

    for my $case (
        [{ 'slt-tg-hydration-threshold' => 0 }, qr/invalid thick-generations hydration threshold/],
        [{ 'slt-tg-hydration-threshold' => 257 }, qr/invalid thick-generations hydration threshold/],
        [{ 'slt-tg-hydration-batch-size' => 0 }, qr/invalid thick-generations hydration batch size/],
        [{ 'slt-tg-hydration-batch-size' => 257 }, qr/invalid thick-generations hydration batch size/],
        [{
            'slt-tg-hydration-threshold' => 16,
            'slt-tg-hydration-batch-size' => 32,
        }, qr/hydration batch size cannot exceed its threshold/],
    ) {
        eval { $class->_thick_hydration_tuning($case->[0]) };
        like($@, $case->[1], 'unsafe hydration tuning fails closed');
    }
};

subtest 'thick snapshot lookup accepts exactly one signed immutable generation' => sub {
    my $storeid = 'thick-test';
    my $volname = 'vm-900001-disk-0';
    my $snapname = 'snap1';
    my $cfg = {
        'slt-vgname' => 'testvg',
        'slt-expected-vg-uuid' => 'vg-uuid',
    };
    my $generation = PVE::SharedLvmThinThick::generation_name('vg-uuid', $volname, 3);
    my $tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'snapshot',
        generation => 3, snapshot => $snapname,
    )});
    my $inventory = { testvg => { $generation => { tags => $tags, lv_size => 4096 } } };
    my ($found, $number) = $class->_thick_find_snapshot(
        $storeid, $cfg, $volname, $snapname, $inventory,
    );
    is($found, $generation, 'signed snapshot resolves to its deterministic generation');
    is($number, 3, 'snapshot generation number is verified against its name');

    my $duplicate = PVE::SharedLvmThinThick::generation_name('vg-uuid', $volname, 4);
    my $duplicate_tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'snapshot',
        generation => 4, snapshot => $snapname,
    )});
    my $ambiguous = { testvg => {
        %{$inventory->{testvg}}, $duplicate => { tags => $duplicate_tags, lv_size => 4096 },
    } };
    eval { $class->_thick_find_snapshot($storeid, $cfg, $volname, $snapname, $ambiguous) };
    like($@, qr/missing or ambiguous/, 'duplicate signed snapshot identity fails closed');
};

subtest 'thick snapshot delete is exact, open-count guarded, and preserves HEAD' => sub {
    reset_mocks();
    my $storeid = 'thick-test';
    my $volname = 'vm-900001-disk-0';
    my $snapshot = 'sltg-g-key-00000000';
    my $head = 'sltg-g-key-00000001';
    my $cfg = {
        shared => 1, 'slt-vgname' => 'testvg',
        'slt-allocation-mode' => 'thick-generations',
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
        'slt-vg-reserve-gib' => 5,
    };
    my $state = { head => $head, generation => 1 };
    my @inventory = (
        { testvg => { $snapshot => {}, $head => {}, anchor => {} } },
        { testvg => { $head => {}, anchor => {} } },
    );
    my @events;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my (undef, undef, undef, $code) = @_; return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_anchor = sub { return ($state, {}, 'anchor') };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub { return ($snapshot, 0, {}) };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_snapshot_readonly = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_new_transaction_id = sub { '7' x 32 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_vg_state_digest = sub { '8' x 32 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_set_vg_intent = sub { push @events, 'OPEN'; 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_clear_vg_intent = sub { push @events, 'CLEAR'; 1 };
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub { shift @inventory };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub { ['0'] };

    is($class->volume_snapshot_delete($cfg, $storeid, $volname, 'snap1'), undef,
        'exact closed snapshot is deleted');
    is_deeply([command_lines()], [
        '/sbin/lvchange -an testvg/sltg-g-key-00000000',
        '/sbin/lvremove -f testvg/sltg-g-key-00000000',
    ], 'delete deactivates and removes only the signed snapshot generation');
    is_deeply(\@events, [qw(OPEN CLEAR)], 'intent brackets the verified delete');

    reset_mocks();
    @inventory = ({ testvg => { $snapshot => {}, $head => {}, anchor => {} } });
    @events = ();
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub { ['1'] };
    eval { $class->volume_snapshot_delete($cfg, $storeid, $volname, 'snap1') };
    like($@, qr/refusing to delete open snapshot/, 'open snapshot is refused');
    is_deeply([command_lines()], [], 'open snapshot refusal performs no mutation');
    is_deeply(\@events, [], 'open snapshot refusal creates no intent');
};

subtest 'thick rollback refuses an open frontend before mutation' => sub {
    reset_mocks();
    my $cfg = {
        shared => 1, 'slt-vgname' => 'testvg',
        'slt-allocation-mode' => 'thick-generations',
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
        'slt-vg-reserve-gib' => 5,
    };
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub { ['1'] };
    eval {
        $class->volume_snapshot_rollback(
            $cfg, 'thick-test', 'vm-900001-disk-0', 'snap1',
        );
    };
    like($@, qr/refusing thick-generations rollback while the frontend is open/,
        'open frontend is rejected');
    is_deeply([command_lines()], [], 'open frontend rejection performs no mutation');
};

subtest 'thick rollback dispatches to the generation materializer' => sub {
    reset_mocks();
    my $cfg = { 'slt-allocation-mode' => 'thick-generations' };
    my @received;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_volume_snapshot = sub {
        @received = @_[1 .. 5];
        return 'rollback-result';
    };
    is($class->volume_snapshot_rollback(
        $cfg, 'thick-test', 'vm-900001-disk-0', 'snap1',
    ), 'rollback-result', 'thick rollback uses the common materialization engine');
    is_deeply(\@received,
        [$cfg, 'thick-test', 'vm-900001-disk-0', 'snap1', 'ROLLBACK'],
        'rollback passes exact storage, volume, snapshot, and operation identity');
};

subtest 'C3 resume requires exact persisted request and transaction identity' => sub {
    reset_mocks();
    my $storeid = 'thick-test';
    my $volname = 'vm-900001-disk-0';
    my $cfg = {
        shared => 1, 'slt-vgname' => 'testvg',
        'slt-allocation-mode' => 'thick-generations',
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
    };
    my $namespace = $cfg->{'slt-expected-vg-uuid'};
    my $anchor = PVE::SharedLvmThinThick::anchor_name($namespace, $volname);
    my $old = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 0);
    my $new = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 1);
    my $meta = 'sltg-m-' . PVE::SharedLvmThinThick::object_key($namespace, $volname)
        . '-00000001';
    my $tx = '7' x 32;
    my $size = 32 * 1024 * 1024;
    my $state = {
        v => 5, sid => $storeid, vol => $volname, phase => 'PREPARED',
        tx => $tx, op => 'SNAPSHOT', snapshot => 'snap1', source => $old,
        old => $old, new => $new, head => $old, generation => 0, region => 8,
    };
    my $new_tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'head', generation => 1,
    )});
    my $inventory = { testvg => {
        $anchor => { tags => join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$state)}) },
        $old => { tags => '', lv_size => $size },
        $new => { tags => $new_tags, lv_size => $size },
        $meta => { tags => '', lv_size => 20 * 1024 * 1024 },
    } };
    my $intent = {
        v => 1, tx => $tx, state => 'OPEN', op => 'DM_CUTOVER',
        object => $anchor, before => ('a' x 32),
    };
    no warnings 'redefine';
    local *PVE::Storage::LVMPlugin::lvm_list_volumes = sub { return $inventory; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        return ($state, $inventory->{testvg}->{$old}, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_transition_metadata = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_autoactivation_disabled = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 0; };

    my $tr = $class->_thick_resume_transition(
        $cfg, $storeid, $volname, 'snap1', 'SNAPSHOT', $intent,
    );
    is($tr->{snapshot}, 'snap1', 'resume reconstructs the persisted snapshot request');
    is($tr->{new}, $new, 'resume reconstructs the exact deterministic destination');
    is($tr->{meta}, $meta, 'resume reconstructs the exact deterministic metadata object');
    is($tr->{state}->{tx}, $tx, 'resume retains the existing transaction ID');

    eval { $class->_thick_resume_transition(
        $cfg, $storeid, $volname, 'other', 'SNAPSHOT', $intent,
    ) };
    like($@, qr/request identity mismatch/, 'different snapshot request fails closed');
    eval { $class->_thick_resume_transition(
        $cfg, $storeid, $volname, 'snap1', 'SNAPSHOT', { %$intent, tx => ('8' x 32) },
    ) };
    like($@, qr/not the exact resumable transition/, 'different transaction fails closed');

    {
        $state = {
            %$state, phase => 'COMMITTED', head => $new, generation => 1,
        };
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub { return 1; };
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_source_mapper = sub { return 1; };
        my $committed = $class->_thick_resume_transition(
            $cfg, $storeid, $volname, 'snap1', 'SNAPSHOT', $intent,
        );
        is($committed->{state}->{phase}, 'COMMITTED',
            'C6 resume reconstructs a committed cutover without replaying preparation');
        is($committed->{old_gen}, 0, 'C6 resume derives the old generation from its signed name');
        is($committed->{new_gen}, 1, 'C6 resume derives the new generation from its signed name');

        $state = { %$state, phase => 'HYDRATING' };
        my $hydrating = $class->_thick_resume_transition(
            $cfg, $storeid, $volname, 'snap1', 'SNAPSHOT', $intent,
        );
        is($hydrating->{state}->{phase}, 'HYDRATING',
            'C8 resume reconstructs the exact persisted hydration state');

        $state = { %$state, phase => 'HYDRATION_COMPLETE' };
        my $hydration_complete = $class->_thick_resume_transition(
            $cfg, $storeid, $volname, 'snap1', 'SNAPSHOT', $intent,
        );
        is($hydration_complete->{state}->{phase}, 'HYDRATION_COMPLETE',
            'C9 resume reconstructs the exact persisted pre-pivot state');
    }
};

subtest 'C4 source mapper verification is exact and read-only' => sub {
    my $cfg = { 'slt-vgname' => 'testvg' };
    my $mapper = 'sltg-source';
    my $source = 'sltg-g-source';
    my $tx = '9' x 32;
    my @answers = (
        ["SLT-TG3-SOURCE-$tx|Read-only"],
        ['0 65536 linear 253:7 0'],
        ['1 dependencies : (testvg-sltg--g--source)'],
    );
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return shift @answers;
    };
    ok($class->_thick_verify_source_mapper(
        $cfg, $mapper, $source, 65536, $tx,
    ), 'exact read-only source mapper is accepted');
    is(scalar(@answers), 0, 'source mapper verifier consumed all exact probes');

    @answers = (["SLT-TG3-SOURCE-$tx|Writeable"]);
    eval { $class->_thick_verify_source_mapper(
        $cfg, $mapper, $source, 65536, $tx,
    ) };
    like($@, qr/is not read-only/, 'writeable source mapper fails closed');
};

subtest 'thick snapshot follows the persisted transaction and linear-pivot order' => sub {
    reset_mocks();
    my $storeid = 'thick-test';
    my $volname = 'vm-900001-disk-0';
    my $cfg = {
        shared => 1,
        'slt-vgname' => 'testvg',
        'slt-allocation-mode' => 'thick-generations',
        'slt-expected-vg-uuid' => 'vg-uuid',
        'slt-expected-pv-uuid' => 'pv-uuid',
        'slt-expected-wwid' => '3600abcd',
        'slt-vg-reserve-gib' => 5,
        'slt-tg-hydration-timeout' => 60,
    };
    my $namespace = $cfg->{'slt-expected-vg-uuid'};
    my $anchor = PVE::SharedLvmThinThick::anchor_name($namespace, $volname);
    my $old = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 0);
    my $new = PVE::SharedLvmThinThick::generation_name($namespace, $volname, 1);
    my $meta = 'sltg-m-' . PVE::SharedLvmThinThick::object_key($namespace, $volname) . '-00000001';
    my $old_tx = '5' x 32;
    my $new_tx = '6' x 32;
    my $size = 32 * 1024 * 1024;
    my $materialized = {
        v => 5, sid => $storeid, vol => $volname, phase => 'MATERIALIZED',
        tx => $old_tx, old => $old, new => $old, head => $old,
        generation => 0, region => 8, op => 'ALLOC', snapshot => 'none', source => $old,
    };
    my $prepared = {
        %$materialized, phase => 'PREPARED', tx => $new_tx,
        old => $old, new => $new, head => $old, op => 'SNAPSHOT', snapshot => 'snap1', source => $old,
    };
    my $hydrating = {
        %$prepared, phase => 'HYDRATING', head => $new, generation => 1,
    };
    my $hydration_complete = {
        %$prepared, phase => 'HYDRATION_COMPLETE', head => $new, generation => 1,
    };
    my $source_ready = { %$prepared, phase => 'SOURCE_READY' };
    my $committed = {
        %$prepared, phase => 'COMMITTED', head => $new, generation => 1,
    };
    my $anchor_tags_prepared = join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$prepared)});
    my $anchor_tags_source_ready = join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$source_ready)});
    my $anchor_tags_committed = join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$committed)});
    my $anchor_tags_hydrating = join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$hydrating)});
    my $anchor_tags_hydration_complete = join(',', @{
        PVE::SharedLvmThinThick::anchor_tags(%$hydration_complete)
    });
    my $old_tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'head', generation => 0,
    )});
    my $new_tags = join(',', @{PVE::SharedLvmThinThick::generation_tags(
        sid => $storeid, vol => $volname, role => 'head', generation => 1,
    )});
    my $initial = { testvg => {
        $anchor => { tags => join(',', @{PVE::SharedLvmThinThick::anchor_tags(%$materialized)}) },
        $old => { tags => $old_tags, lv_size => $size, lv_attr => '-wi-XX---k' },
    } };
    my $prepared_inventory = { testvg => {
        $anchor => { tags => $anchor_tags_prepared },
        $old => { tags => $old_tags, lv_size => $size, lv_attr => '-wi-XX---k' },
        $new => { tags => $new_tags, lv_size => $size },
        $meta => { tags => '', lv_size => 24 * 1024 * 1024 },
    } };
    my $hydrating_inventory = { testvg => {
        %{$prepared_inventory->{testvg}},
        $anchor => { tags => $anchor_tags_hydrating },
    } };
    my $source_ready_inventory = { testvg => {
        %{$prepared_inventory->{testvg}},
        $anchor => { tags => $anchor_tags_source_ready },
    } };
    my $committed_inventory = { testvg => {
        %{$prepared_inventory->{testvg}},
        $anchor => { tags => $anchor_tags_committed },
    } };
    my $hydration_complete_inventory = { testvg => {
        %{$prepared_inventory->{testvg}},
        $anchor => { tags => $anchor_tags_hydration_complete },
    } };
    my $after_cleanup = { testvg => {
        $anchor => { tags => $anchor_tags_hydration_complete },
        $old => { tags => $old_tags, lv_size => $size },
        $new => { tags => $new_tags, lv_size => $size },
    } };
    my @inventories = (
        $initial, $prepared_inventory, $source_ready_inventory, $committed_inventory,
        $hydrating_inventory, $hydration_complete_inventory, $after_cleanup,
    );
    my @events;
    my @scoped_devices;
    my $phase_state = $materialized;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_with_vg_lock = sub {
        my (undef, undef, undef, $code) = @_; return $code->();
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_read_vg_intent = sub { return undef; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_exact_vg_intent = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_new_transaction_id = sub { return $new_tx; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_vg_state_digest = sub { return 'd' x 32; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_set_vg_intent = sub { push @events, 'INTENT_OPEN'; return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_clear_vg_intent = sub { push @events, 'INTENT_CLEAR'; return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_capacity_gate = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_change_exact_tags = sub {
        push @events, 'TAG_CHANGE';
        push @scoped_devices, $_[6] if defined($_[6]);
        return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_disable_and_verify_autoactivation = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_transition_metadata = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_snapshot_readonly = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_source_mapper = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_clone_frontend = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_clone_status = sub { return (8, 8, 0); };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_wait_for_hydration = sub { push @events, 'HYDRATION_WAIT'; return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_anchor = sub {
        return ($materialized, { tags => $old_tags, lv_size => $size }, $anchor);
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_transition_anchor = sub {
        my (undef, undef, undef, $state, %change) = @_;
        push @scoped_devices, delete($change{_device}) if defined($change{_device});
        my %next = (%$state, %change);
        PVE::SharedLvmThinThick::validate_anchor_transition($state, \%next);
        push @events, "PHASE_$next{phase}";
        $phase_state = \%next;
        return \%next;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_block_device_exists = sub {
        my ($path) = @_;
        return 1 if $path =~ /\/dev\/mapper\/sltg-[0-9a-f]{24}$/;
        return 0;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub {
        return shift @inventories;
    };
    my $suspend_reads = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        my ($command) = @_;
        if (grep { $_ eq 'suspended' } @$command) {
            return [++$suspend_reads == 1 ? 'Active' : 'Suspended'];
        }
        return ['0 65536 linear 253:7 0'];
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub {
        my ($command, %options) = @_;
        push @commands, [@$command];
        push @events, 'CMD_' . join('_', @$command[0 .. ($#$command < 2 ? $#$command : 2)]);
        return;
    };

    is($class->volume_snapshot($cfg, $storeid, $volname, 'snap1'), undef,
        'thick snapshot reaches materialized linear state');
    my @phases = grep { /^PHASE_/ } @events;
    is_deeply(\@phases, [qw(
        PHASE_PREPARED PHASE_SOURCE_READY PHASE_COMMITTED PHASE_HYDRATING
        PHASE_HYDRATION_COMPLETE PHASE_LINEAR_PIVOTED PHASE_MATERIALIZED
    )], 'persistent phases are monotonic and complete');
    is($phase_state->{tx}, $new_tx, 'every transition phase uses the new transaction ID');
    is($phase_state->{head}, $new, 'materialized HEAD is the independent destination');
    my @snapshot_commands = command_lines();
    is(scalar(grep { /lvcreate/ } @snapshot_commands), 2,
        'snapshot creates exactly one destination and one metadata LV');
    like(join("\n", @snapshot_commands), qr{lvcreate .* -L 20971520B -n \Q$meta\E},
        'metadata capacity comes from persisted geometry, not a fixed 16 MiB value');
    is(scalar(grep { /dmsetup .*suspend --noflush/ } @snapshot_commands), 2,
        'clone cutover and linear pivot each use one explicit noflush suspend');
    is(scalar(grep { /dmsetup .*resume/ } @snapshot_commands), 2,
        'each suspended cutover has exactly one resume');
    my ($source_index) = grep { $snapshot_commands[$_] =~ /dmsetup .*create .*src-/ } 0 .. $#snapshot_commands;
    my ($cutover_suspend) = grep { $snapshot_commands[$_] =~ /dmsetup .*suspend --noflush/ } 0 .. $#snapshot_commands;
    my ($cutover_resume) = grep {
        $_ > $cutover_suspend && $snapshot_commands[$_] =~ /dmsetup .*resume/
    } 0 .. $#snapshot_commands;
    my ($readonly_index) = grep { $snapshot_commands[$_] =~ /lvchange .* -pr/ } 0 .. $#snapshot_commands;
    ok(defined($source_index) && defined($cutover_suspend) && $source_index < $cutover_suspend,
        'read-only source mapper is prepared before the atomic cutover');
    ok(defined($cutover_resume) && defined($readonly_index) && $readonly_index > $cutover_resume,
        'persistent snapshot LV becomes read-only only after the frontend is resumed');
    is(scalar(@scoped_devices), 10,
        'every transition tag mutation carries an explicit device scope');
    ok(!scalar(grep { $_ ne '/dev/mapper/3600abcd' } @scoped_devices),
        'every scoped metadata mutation uses the pinned multipath device');
    is(scalar(grep { /lvremove .* -f testvg\/\Q$meta\E/ } @snapshot_commands), 1,
        'only the exact detached metadata LV is removed');
    is($events[-1], 'INTENT_CLEAR', 'VG intent clears only after MATERIALIZED');
    is(scalar(@inventories), 0, 'all lifecycle inventories were consumed');
};

done_testing();
