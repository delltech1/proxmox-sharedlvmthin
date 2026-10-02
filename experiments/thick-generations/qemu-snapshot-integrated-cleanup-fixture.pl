#!/usr/bin/perl

use strict;
use warnings;

use lib '/usr/share/perl5';
use PVE::QemuConfig ();
use Test::More;

my $vmid = 999_996;

{
    package FixtureIntegratedSnapshotClass;
    our @ISA = ('PVE::QemuConfig');
    our ($conf, @events, $commit_count, $snapshot_behavior);

    sub reset {
        my ($class, $behavior) = @_;
        my $snap = {
            snapstate => 'prepare',
            vmstate => 'state:vm-999996-state-snap',
            scsi0 => 'state:vm-999996-disk-0,size=1G',
            scsi1 => 'state:vm-999996-disk-1,size=1G',
        };
        $conf = {
            lock => 'snapshot',
            snapshots => { snap => $snap },
            scsi0 => $snap->{scsi0},
            scsi1 => $snap->{scsi1},
        };
        @events = ();
        $commit_count = 0;
        $snapshot_behavior = $behavior // 'second-before-effect';
    }

    sub __snapshot_prepare { push @events, 'prepare'; return $conf->{snapshots}->{snap}; }
    sub load_config { return $conf; }
    sub write_config { push @events, 'write-config'; }
    sub lock_config {
        my ($class, $observed_vmid, $callback, @args) = @_;
        die "wrong VM\n" if $observed_vmid != $vmid;
        push @events, 'lock-config';
        return $callback->(@args);
    }
    sub __snapshot_check_freeze_needed { return (1, 0); }
    sub __snapshot_activate_storages { push @events, 'activate-storages'; }
    sub __snapshot_create_vol_snapshot {
        my ($class, $observed_vmid, $disk) = @_;
        push @events, "snapshot:$disk";
        if ($disk eq 'scsi1' && $snapshot_behavior eq 'second-before-effect') {
            die "second-disk-injected\n";
        }
        if ($disk eq 'scsi1' && $snapshot_behavior eq 'second-after-effect') {
            push @events, 'snapshot-effect:scsi1';
            die "second-disk-after-effect-injected\n";
        }
    }
    sub __snapshot_commit { $commit_count++; push @events, 'commit'; }
}

sub run_case {
    my ($name, $mode, $snapshot_behavior) = @_;
    $snapshot_behavior //= 'second-before-effect';
    FixtureIntegratedSnapshotClass->reset($snapshot_behavior);
    my $vmstate_open = 1;
    my @external;
    my @warnings;

    no warnings 'redefine';
    local *PVE::Storage::config = sub { return {}; };
    local *PVE::Storage::path = sub { return '/dev/mock-vmstate'; };
    local *PVE::Storage::activate_volumes = sub { push @external, 'activate-vmstate'; };
    local *PVE::Storage::deactivate_volumes = sub {
        my (undef, $volids) = @_;
        my $volid = ref($volids) eq 'ARRAY' ? join(',', @$volids) : '<invalid>';
        push @external, "deactivate-vmstate:$volid";
        $vmstate_open = 0;
    };
    local *PVE::Storage::parse_volume_id = sub { return 'state'; };
    local *PVE::Storage::vdisk_free = sub {
        my (undef, $volid) = @_;
        push @external, "free-vmstate-attempt:$volid";
        die "vmstate-open-refusal\n" if $vmstate_open;
        push @external, "free-vmstate-success:$volid";
    };
    local *PVE::QemuMigrate::Helpers::set_migration_caps = sub {
        push @external, 'migration-caps';
    };
    local *PVE::QemuServer::qemu_volume_snapshot_delete = sub {
        my ($observed_vmid, $storecfg, $drive, $snapname) = @_;
        push @external, "delete:$drive->{file}:$snapname";
    };
    local *PVE::QemuConfig::mon_cmd = sub {
        my ($observed_vmid, $command) = @_;
        push @external, "qmp:$command";
        die "savevm-end-injected\n" if $command eq 'savevm-end' && $mode eq 'end-fail';
        die "query-savevm-finalizer-injected\n"
            if $command eq 'query-savevm' && $mode eq 'query-fail'
                && grep { $_ eq 'qmp:savevm-end' } @external;
        return { status => 'completed', bytes => 1024, 'total-time' => 1 }
            if $command eq 'query-savevm' && !grep { $_ eq 'qmp:savevm-end' } @external;
        return { bytes => 0 } if $command eq 'query-savevm';
        return {};
    };
    local $SIG{__WARN__} = sub { push @warnings, @_ };

    my $ok = eval {
        FixtureIntegratedSnapshotClass->snapshot_create(
            $vmid, 'snap', 1, 'integrated fixture',
        );
        1;
    };
    if ($snapshot_behavior eq 'both-success' && $mode eq 'end-fail') {
        ok($ok, "$name exposes upstream warning-only finalizer result");
        is($FixtureIntegratedSnapshotClass::commit_count, 1,
            "$name upstream commits despite savevm-end warning");
        ok(exists($FixtureIntegratedSnapshotClass::conf->{snapshots}->{snap}),
            "$name committed snapshot section remains present");
        ok($vmstate_open, "$name vmstate remains open after warning-only finalizer");
        like(join('', @warnings), qr/savevm-end-injected/,
            "$name preserves the savevm-end warning");
        is(scalar(grep { /^delete:/ } @external), 0,
            "$name performs no forced disk cleanup after reported success");
        is(scalar(grep { /sentinel/ } @external), 0,
            "$name never touches the unrelated vmstate sentinel");
        diag("$name EVENTS=" . join(',', @external));
        return;
    }

    ok(!$ok, "$name preserves failure");
    if ($mode eq 'query-fail') {
        like($@, qr/query-savevm-finalizer-injected/,
            "$name finalizer exception replaces original disk failure");
        is($FixtureIntegratedSnapshotClass::commit_count, 0, "$name never commits");
        ok(exists($FixtureIntegratedSnapshotClass::conf->{snapshots}->{snap}),
            "$name leaves prepared snapshot section for explicit recovery");
        is($FixtureIntegratedSnapshotClass::conf->{lock}, 'snapshot',
            "$name leaves snapshot lock for explicit recovery");
        is(scalar(grep { /^delete:/ } @external), 0,
            "$name performs no forced disk cleanup");
        is(scalar(grep { /^free-vmstate/ } @external), 0,
            "$name performs no vmstate cleanup");
        diag("$name EVENTS=" . join(',', @external));
        return;
    }
    my $expected_failure = $snapshot_behavior eq 'second-after-effect'
        ? qr/second-disk-after-effect-injected/
        : qr/second-disk-injected/;
    like($@, $expected_failure, "$name returns original second-disk failure");
    is($FixtureIntegratedSnapshotClass::commit_count, 0, "$name never commits");
    ok(!exists($FixtureIntegratedSnapshotClass::conf->{snapshots}->{snap}),
        "$name real forced cleanup removes snapshot config section");
    ok(!exists($FixtureIntegratedSnapshotClass::conf->{lock}),
        "$name real forced cleanup removes snapshot lock");
    is(scalar(grep { /^delete:state:vm-999996-disk-0:snap$/ } @external), 1,
        "$name deletes only completed disk snapshot");
    is(scalar(grep { /^delete:state:vm-999996-disk-1:/ } @external), 0,
        "$name never deletes failed disk snapshot");
    if ($snapshot_behavior eq 'second-after-effect') {
        is(scalar(grep { $_ eq 'snapshot-effect:scsi1' }
            @FixtureIntegratedSnapshotClass::events), 1,
            "$name proves disk 2 may have taken an effect before throwing");
        is(scalar(grep { /^delete:state:vm-999996-disk-1:/ } @external), 0,
            "$name preserves the ambiguous disk 2 effect for explicit recovery");
    }

    if ($mode eq 'end-fail') {
        ok($vmstate_open, "$name vmstate remains open after finalizer warning");
        is(scalar(grep { /^free-vmstate-success:/ } @external), 0,
            "$name never frees open vmstate");
        like(join('', @warnings), qr/savevm-end-injected.*vmstate-open-refusal/s,
            "$name preserves finalizer and forced-cleanup warnings");
    } else {
        ok(!$vmstate_open, "$name deactivates vmstate before forced cleanup");
        is(scalar(grep { $_ eq 'free-vmstate-success:state:vm-999996-state-snap' }
            @external), 1,
            "$name frees closed vmstate exactly once");
    }
    is(scalar(grep { /sentinel/ } @external), 0,
        "$name never touches the unrelated vmstate sentinel");
    diag("$name EVENTS=" . join(',', @external));
}

run_case('finalizer-success', 'ok');
run_case('finalizer-warning', 'end-fail');
run_case('finalizer-query-failure', 'query-fail');
run_case('finalizer-warning-after-all-disks', 'end-fail', 'both-success');
run_case('second-disk-ambiguous-effect', 'ok', 'second-after-effect');

done_testing();
