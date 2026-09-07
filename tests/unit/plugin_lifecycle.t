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
    is($class->api(), 14, 'plugin declares oldest qualified API');

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

done_testing();
