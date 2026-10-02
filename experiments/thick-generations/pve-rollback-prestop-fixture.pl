#!/usr/bin/perl

use strict;
use warnings;

use Test::More;
use PVE::AbstractConfig;
use PVE::QemuConfig;
use PVE::Replication;
use PVE::ReplicationConfig;
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $plugin = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $cfg = {
    'slt-allocation-mode' => 'thick-generations-lazy',
    # Exercise the intended shared-SAN Thick rollback path.  Without this
    # exact property the plugin correctly refuses earlier at its storage
    # topology boundary and the fixture never proves the HYDRATING pre-stop
    # invariant it claims to cover.
    shared => 1,
    'slt-vgname' => 'fixture-vg',
    'slt-expected-vg-uuid' => 'fixture-vg-uuid',
    'slt-expected-pv-uuid' => 'fixture-pv-uuid',
    'slt-expected-wwid' => 'fixture-wwid',
    'slt-vg-reserve-percent' => 1,
    'slt-mutation-admission-timeout' => 10,
};
my @events;
my $conf;

{
    package TG53::RollbackFixture;
    use parent 'PVE::AbstractConfig';

    sub load_config { return $conf; }
    sub lock_config { my (undef, undef, $code) = @_; return $code->(); }
    sub is_template { return 0; }
    sub check_lock { push @events, 'check-lock'; return; }
    sub write_config { push @events, 'write-config'; return; }
    sub __snapshot_activate_storages { push @events, 'activate-storages'; return; }
    sub __snapshot_check_running { push @events, 'running-check'; return 0; }
    sub __snapshot_rollback_vm_stop { push @events, 'vm-stop'; return; }
    sub __snapshot_rollback_hook { push @events, 'rollback-hook'; return; }
    sub __snapshot_rollback_get_unused { return []; }
    sub __snapshot_rollback_vm_start { push @events, 'vm-start'; return; }
    sub add_unused_volume { push @events, 'add-unused'; return; }
    sub volid_key { return 'file'; }
    sub get_replicatable_volumes { return {}; }

    sub foreach_volume {
        my (undef, $section, $code) = @_;
        for my $volume (@{$section->{fixture_volumes} // []}) {
            $code->('scsi0', $volume);
        }
        return;
    }

    sub __snapshot_rollback_vol_possible {
        my (undef, $volume, $snapname, $blockers) = @_;
        push @events, "possible:$volume->{file}";
        return $plugin->volume_rollback_is_possible(
            $cfg, 'lazy-a', $volume->{file}, $snapname, $blockers,
        );
    }

    sub __snapshot_rollback_vol_rollback {
        my (undef, $volume) = @_;
        push @events, "mutate:$volume->{file}";
        return;
    }
}

my $abstract_path = $INC{'PVE/AbstractConfig.pm'} // '';
ok($abstract_path ne '', 'installed PVE::AbstractConfig was loaded');
diag("ABSTRACT_CONFIG=$abstract_path");

{
    no warnings 'redefine';
    local *PVE::ReplicationConfig::check_for_existing_jobs = sub { return 0; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_assert_package_operations_released
        = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum
        = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity
        = sub { return 1; };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub {
        return {
            'fixture-vg' => {
                'anchor-vm-900001-disk-0' => { lv_uuid => 'anchor-uuid-0' },
                'anchor-vm-900001-disk-1' => { lv_uuid => 'anchor-uuid-1' },
            },
        };
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_read_anchor = sub {
        my (undef, undef, undef, $volname) = @_;
        return ({ phase => 'MATERIALIZED' }, {}, "anchor-$volname")
            if $volname !~ /disk-1$/;
        return ({
            v => 5,
            phase => 'HYDRATING',
            op => 'SNAPSHOT',
            tx => ('a' x 32),
            old => 'generation-old',
            new => 'generation-new',
            head => 'generation-new',
            generation => 2,
            snapshot => 'snap1',
        }, {}, "anchor-$volname");
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_find_snapshot = sub {
        return ('signed-snapshot', 1, { lv_uuid => 'target-uuid' });
    };
    my @clock = (0, 0, 10);
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_progress_clock = sub {
        return shift(@clock) // 10;
    };

    $conf = {
        snapshots => {
            snap1 => {
                fixture_volumes => [
                    { file => 'vm-900001-disk-0' },
                    { file => 'vm-900001-disk-1' },
                ],
            },
        },
    };
    @events = ();
    eval { TG53::RollbackFixture->snapshot_rollback(900001, 'snap1'); };
    like($@, qr/rollback admission timed out.*guest was not stopped/s,
        'second transitional disk refuses the real upstream rollback before stop');
    is_deeply(
        \@events,
        [
            'activate-storages',
            'possible:vm-900001-disk-0',
            'possible:vm-900001-disk-1',
        ],
        'upstream orchestration issued no VM stop, config write, or storage mutation',
    );
    ok(!exists($conf->{lock}), 'rollback lock was not published');
}

{
    no warnings 'redefine';
    my $storecfg = {
        ids => {
            shared => { shared => 1 },
            local => { shared => 0 },
        },
    };
    local *PVE::Storage::parse_volume_id = sub {
        my ($volid) = @_;
        return split(/:/, $volid, 2);
    };
    local *PVE::Storage::storage_config = sub {
        my ($cfg, $sid) = @_;
        return $cfg->{ids}->{$sid};
    };
    local *PVE::Storage::path = sub {
        my (undef, $volid) = @_;
        return ("/dev/mock/$volid", 900001, 'images');
    };
    local *PVE::Storage::volume_has_feature = sub { return 1; };
    my $volumes = PVE::QemuConfig->get_replicatable_volumes(
        $storecfg, 900001,
        {
            scsi0 => 'shared:vm-900001-disk-0,replicate=1',
            scsi1 => 'local:vm-900001-disk-1,replicate=1',
        },
        0, 0,
    );
    is_deeply(
        $volumes,
        { 'local:vm-900001-disk-1' => 1 },
        'actual QemuConfig replication inventory excludes the shared SAN disk',
    );
}

{
    package TG53::ReplicationOrderingFixture;
    use parent 'PVE::AbstractConfig';

    sub load_config { return $conf; }
    sub lock_config { my (undef, undef, $code) = @_; return $code->(); }
    sub is_template { return 0; }
    sub check_lock { push @events, 'check-lock'; return; }
    sub write_config { push @events, 'write-config'; return; }
    sub __snapshot_activate_storages { push @events, 'activate-storages'; return; }
    sub __snapshot_check_running { return 0; }
    sub __snapshot_rollback_vm_stop { push @events, 'vm-stop'; return; }
    sub __snapshot_rollback_hook { push @events, 'rollback-hook'; return; }
    sub __snapshot_rollback_get_unused { return []; }
    sub volid_key { return 'file'; }
    sub get_replicatable_volumes { return { 'local:vm-900001-disk-1' => 1 }; }
    sub foreach_volume {
        my (undef, $section, $code) = @_;
        for my $volume (@{$section->{fixture_volumes} // []}) {
            $code->('scsi0', $volume);
        }
    }
    sub __snapshot_rollback_vol_possible {
        my (undef, $volume, undef, $blockers) = @_;
        push @events, "possible:$volume->{file}:" . (defined($blockers) ? 'replication' : 'main');
        die "local-legacy-refusal\n" if $volume->{file} =~ /^local:/;
        die "shared-prestop-refusal\n";
    }
    sub __snapshot_rollback_vol_rollback { push @events, 'mutate'; }
}

{
    no warnings 'redefine';
    local *PVE::Storage::config = sub { return {}; };
    local *PVE::ReplicationConfig::new = sub { return bless {}, 'PVE::ReplicationConfig'; };
    local *PVE::ReplicationConfig::check_for_existing_jobs = sub { return 1; };
    local *PVE::Replication::prepare = sub {
        my (undef, $volids) = @_;
        push @events, 'replication-prepare:' . join(',', @$volids);
    };
    $conf = {
        lock => 'backup',
        snapshots => {
            snap1 => {
                fixture_volumes => [
                    { file => 'shared:vm-900001-disk-0' },
                    { file => 'local:vm-900001-disk-1' },
                ],
            },
        },
    };
    @events = ();
    eval { TG53::ReplicationOrderingFixture->snapshot_rollback(900001, 'snap1'); };
    like($@, qr/shared-prestop-refusal/, 'main shared pre-stop refusal is preserved');
    is_deeply(
        \@events,
        [
            'activate-storages',
            'possible:local:vm-900001-disk-1:replication',
            'replication-prepare:local:vm-900001-disk-1',
            'possible:shared:vm-900001-disk-0:main',
        ],
        'legacy empty-blocker replication cleanup precedes lock and main storage refusal',
    );
    ok(scalar(grep { $_ eq 'check-lock' || $_ eq 'vm-stop' || $_ eq 'mutate' } @events) == 0,
        'ordering fixture reaches no VM stop or volume rollback');
}

done_testing();
