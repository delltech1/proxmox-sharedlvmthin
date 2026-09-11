# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::Storage::Custom::SharedLvmThinPlugin;

use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use JSON::PP qw(decode_json);
use Scalar::Util qw(tainted);
use PVE::Storage::Plugin;
use PVE::Storage::LVMPlugin;
use PVE::Cluster;
use PVE::Tools ();
use PVE::SharedLvmThinSafety;
use PVE::SharedLvmThinThick qw(
    anchor_name clone_geometry decode_anchor_tags decode_generation_tags
    generation_name mapper_name object_key
    materialized_rebase_state validate_anchor_transition validate_generation_tags
    vg_intent_tags decode_vg_intent_tags
    transition_tags validate_transition_tags
);

use base qw(PVE::Storage::Plugin);

sub api {
    # Advertise the exact host API only inside the explicitly qualified range.
    # This avoids a false "older storage API" warning on API 15 without ever
    # claiming compatibility with an unaudited future API.
    my $runtime = __PACKAGE__->_runtime_storage_api();
    die "PVE Storage API $runtime is outside the tested SharedLvmThin range 14..15\n"
        if $runtime < 14 || $runtime > 15;
    return $runtime;
}

use constant MIN_TESTED_PVE_STORAGE_API => 14;
use constant MAX_TESTED_PVE_STORAGE_API => 15;

# PVE workers run with Perl taint checks.  Every external command crosses this
# single argv-only boundary.  Values derived from the API, LVM, sysfs or DM are
# accepted only as scalar arguments without control characters.  A tainted
# value may never become an option; operation switches must be code constants.
# PVE::Tools::run_command receives an argv array, never a shell command.
sub _validated_exec_argv {
    my ($command) = @_;
    die "external command must be an argv array\n" if ref($command) ne 'ARRAY' || !@$command;
    my @safe;
    for my $index (0 .. $#$command) {
        my $arg = $command->[$index];
        die "external command argument $index is not a scalar\n"
            if !defined($arg) || ref($arg);
        die "tainted external command argument $index may not be an option\n"
            if $index > 0 && tainted($arg) && $arg =~ /^-/;
        die "external command argument $index contains a control character\n"
            if $arg !~ /^([^\x00-\x1f\x7f]*)$/;
        push @safe, $1;
    }
    die "external command executable must be an absolute path\n"
        if $safe[0] !~ m{^/[A-Za-z0-9_./+-]+$};
    return \@safe;
}

sub run_command {
    my ($command, @options) = @_;
    return PVE::Tools::run_command(_validated_exec_argv($command), @options);
}

sub _runtime_storage_api {
    require PVE::Storage;
    return PVE::Storage::APIVER();
}

sub type {
    return 'sharedlvmthin';
}

sub plugindata {
    return {
        content => [
            { images => 1, rootdir => 1 },
            { images => 1 },
        ],
        format => [
            { raw => 1 },
            'raw',
        ],
        'sensitive-properties' => {},
    };
}

#
# IMPORTANT:
# Prefix custom properties so we never collide with
# native Proxmox storage properties.
#
sub properties {
    return {
        'slt-vgname' => {
            description => 'Backing shared LVM volume group.',
            type => 'string',
        },
        'slt-allocation-mode' => {
            description => 'Volume backend: per-VM thin pools or experimental fully allocated Thick Generations.',
            type => 'string',
            enum => ['thin', 'thick-generations'],
            default => 'thin',
        },
        'slt-tg-hydration-timeout' => {
            description => 'Bounded Thick Generations hydration observation timeout in seconds.',
            type => 'integer',
            minimum => 60,
            maximum => 86400,
            default => 3600,
        },
        'slt-tg-hydration-threshold' => {
            description => 'Maximum number of Thick Generations regions copied concurrently during background hydration.',
            type => 'integer',
            minimum => 1,
            maximum => 256,
            default => 32,
        },
        'slt-tg-hydration-batch-size' => {
            description => 'Maximum contiguous Thick Generations regions combined into one background copy request.',
            type => 'integer',
            minimum => 1,
            maximum => 256,
            default => 32,
        },
        'slt-tg-online-materialization' => {
            description => 'Materialize an online Thick Generations snapshot after returning control to PVE, or synchronously while the VM remains paused.',
            type => 'string',
            enum => ['asynchronous', 'synchronous'],
            default => 'asynchronous',
        },
        'slt-initial-pool-size' => {
            description => 'Initial physical size of each per-VM thin pool in GiB.',
            type => 'integer',
            minimum => 1,
            maximum => 1024,
            default => 16,
        },
        'slt-initial-pool-mode' => {
            description => 'Allocation headroom policy: fixed, proportional, elastic, or full.',
            type => 'string',
            enum => ['fixed', 'proportional', 'elastic', 'full'],
            default => 'fixed',
        },
        'slt-initial-pool-percent' => {
            description => 'Requested disk percentage reserved as physical headroom in proportional mode.',
            type => 'integer',
            minimum => 1,
            maximum => 100,
            default => 50,
        },
        'slt-initial-pool-max' => {
            description => 'Optional proportional/full allocation target ceiling in GiB.',
            type => 'integer',
            minimum => 1,
            maximum => 1048576,
        },
        'slt-burst-headroom-gib' => {
            description => 'Absolute physical write-burst headroom maintained by elastic allocation and autogrow.',
            type => 'integer', minimum => 1, maximum => 1024, default => 64,
        },
        'slt-expected-vg-uuid' => {
            description => 'Expected backing VG UUID. A mismatch blocks activation and mutations.',
            type => 'string',
            pattern => '[A-Za-z0-9-]+',
        },
        'slt-expected-pv-uuid' => {
            description => 'Expected UUID of the single backing PV.',
            type => 'string',
            pattern => '[A-Za-z0-9-]+',
        },
        'slt-expected-wwid' => {
            description => 'Expected multipath WWID from /dev/mapper/<WWID>.',
            type => 'string',
            pattern => '[0-9A-Fa-f]+',
        },
        'slt-expected-min-paths' => {
            description => 'Diagnostic minimum number of healthy multipath paths. This does not change or gate SAN policy.',
            type => 'integer',
            minimum => 1,
            maximum => 64,
        },
        'slt-vg-reserve-percent' => {
            description => 'Physical VG reserve percentage protected from automatic thin-pool growth.',
            type => 'integer',
            minimum => 0,
            maximum => 50,
        },
        'slt-vg-reserve-gib' => {
            description => 'Fixed physical VG reserve in GiB protected from automatic thin-pool growth.',
            type => 'integer',
            minimum => 0,
            maximum => 1048576,
        },
    };
}

sub options {
    return {
        'slt-vgname' => { fixed => 1 },
        'slt-allocation-mode' => { fixed => 1, optional => 1 },
        'slt-tg-hydration-timeout' => { optional => 1 },
        'slt-tg-hydration-threshold' => { optional => 1 },
        'slt-tg-hydration-batch-size' => { optional => 1 },
        'slt-tg-online-materialization' => { optional => 1 },
        'slt-initial-pool-size' => { optional => 1 },
        'slt-initial-pool-mode' => { optional => 1 },
        'slt-initial-pool-percent' => { optional => 1 },
        'slt-initial-pool-max' => { optional => 1 },
        'slt-burst-headroom-gib' => { optional => 1 },
        'slt-expected-vg-uuid' => { optional => 1 },
        'slt-expected-pv-uuid' => { optional => 1 },
        'slt-expected-wwid' => { optional => 1 },
        'slt-expected-min-paths' => { optional => 1 },
        'slt-vg-reserve-percent' => { optional => 1 },
        'slt-vg-reserve-gib' => { optional => 1 },

        # These are common properties inherited from PVE::Storage::Plugin.
        nodes => { optional => 1 },
        disable => { optional => 1 },
        content => { optional => 1 },
        shared => { optional => 1 },
    };
}

sub _allocation_mode {
    my ($class, $scfg) = @_;
    my $mode = $scfg->{'slt-allocation-mode'} // 'thin';
    die "unknown SharedLvmThin allocation mode '$mode'\n"
        if $mode ne 'thin' && $mode ne 'thick-generations';
    return $mode;
}

sub _thick_hydration_tuning {
    my ($class, $scfg) = @_;
    my $threshold = $scfg->{'slt-tg-hydration-threshold'} // 32;
    my $batch = $scfg->{'slt-tg-hydration-batch-size'} // 32;
    die "invalid thick-generations hydration threshold\n"
        if $threshold !~ /^\d+$/ || $threshold < 1 || $threshold > 256;
    die "invalid thick-generations hydration batch size\n"
        if $batch !~ /^\d+$/ || $batch < 1 || $batch > 256;
    die "thick-generations hydration batch size cannot exceed its threshold\n"
        if $batch > $threshold;
    return (int($threshold), int($batch));
}

sub _thick_online_materialization_mode {
    my ($class, $scfg) = @_;
    my $mode = $scfg->{'slt-tg-online-materialization'} // 'asynchronous';
    die "invalid thick-generations online materialization mode\n"
        if $mode ne 'asynchronous' && $mode ne 'synchronous';
    return $mode;
}

sub _require_thick_identity_config {
    my ($class, $storeid, $scfg) = @_;
    die "thick-generations storage '$storeid' must be configured as shared\n"
        if !$scfg->{shared};
    for my $field (qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid)) {
        die "thick-generations storage '$storeid' requires '$field'\n"
            if !defined($scfg->{$field}) || $scfg->{$field} eq '';
    }
    die "thick-generations storage '$storeid' requires a protected VG reserve\n"
        if !defined($scfg->{'slt-vg-reserve-percent'})
        && !defined($scfg->{'slt-vg-reserve-gib'});
    return 1;
}

sub _thick_namespace {
    my ($class, $scfg) = @_;
    my $uuid = $scfg->{'slt-expected-vg-uuid'};
    die "thick-generations requires a pinned VG UUID before path resolution\n"
        if !defined($uuid) || $uuid eq '';
    return lc($uuid);
}

sub _thick_read_anchor {
    my ($class, $storeid, $scfg, $volname, $lvs) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    if (!defined($lvs)) {
        my $wwid = $scfg->{'slt-expected-wwid'}
            // die "thick-generations anchor inventory requires a pinned WWID\n";
        $lvs = $class->_thick_list_volumes_scoped($vg, "/dev/mapper/$wwid");
    }
    die "thick-generations storage '$storeid' is unavailable: VG '$vg' is not visible\n"
        if !$lvs->{$vg};
    my $info = $lvs->{$vg}->{$anchor};
    die "thick-generations anchor '$vg/$anchor' is missing\n" if !$info;
    my $state = decode_anchor_tags($info->{tags} // '');
    die "thick-generations anchor '$vg/$anchor' belongs to another storage or volume\n"
        if $state->{sid} ne $storeid || $state->{vol} ne $volname;
    my $head = $lvs->{$vg}->{$state->{head}};
    die "thick-generations head '$vg/$state->{head}' is missing\n" if !$head;
    validate_generation_tags(
        $head->{tags} // '', sid => $storeid, vol => $volname,
        role => 'head', generation => $state->{generation},
    );
    return ($state, $head, $anchor);
}

sub _thick_anchor {
    my ($class, @args) = @_;
    my ($state, $head, $anchor) = $class->_thick_read_anchor(@args);
    my (undef, $scfg) = @args;
    my $vg = $scfg->{'slt-vgname'};
    die "thick-generations anchor '$vg/$anchor' is not materialized; recovery required\n"
        if $state->{phase} ne 'MATERIALIZED';
    return ($state, $head, $anchor);
}

sub _thick_find_snapshot {
    my ($class, $storeid, $scfg, $volname, $snapname, $lvs) = @_;
    $snapname = _thick_snapshot_name($snapname);
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $key = object_key($namespace, $volname);
    if (!defined($lvs)) {
        my $wwid = $scfg->{'slt-expected-wwid'}
            // die "thick-generations snapshot inventory requires a pinned WWID\n";
        $lvs = $class->_thick_list_volumes_scoped($vg, "/dev/mapper/$wwid");
    }
    die "thick-generations storage '$storeid' is unavailable: VG '$vg' is not visible\n"
        if !$lvs->{$vg};
    my @matches;
    for my $name (sort grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg}}) {
        my ($generation) = $name =~ /-(\d{8})$/;
        my $valid = eval {
            my $decoded = decode_generation_tags($lvs->{$vg}->{$name}->{tags} // '');
            die "snapshot belongs to another storage\n"
                if defined($storeid) && $decoded->{sid} ne $storeid;
            die "snapshot identity mismatch\n"
                if $decoded->{vol} ne $volname || $decoded->{role} ne 'snapshot'
                || int($decoded->{generation}) != int($generation)
                || $decoded->{snapshot} ne $snapname;
            1;
        };
        push @matches, [$name, int($generation), $lvs->{$vg}->{$name}] if $valid;
    }
    die "snapshot '$snapname' for '$volname' is missing or ambiguous\n" if @matches != 1;
    return @{$matches[0]};
}

sub _thick_verify_snapshot_readonly {
    my ($class, $vg, $lv, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '-o', 'lv_attr', "$vg/$lv");
    my $lines = _command_lines(
        \@command,
        "reading snapshot permissions of '$vg/$lv' failed",
    );
    die "snapshot permissions of '$vg/$lv' are ambiguous\n" if @$lines != 1;
    die "snapshot '$vg/$lv' is not read-only\n" if $lines->[0] !~ /^.r/;
    return 1;
}

sub _thick_ensure_snapshot_readonly {
    my ($class, $vg, $lv, $info, $device) = @_;
    die "snapshot permission inventory for '$vg/$lv' is missing\n"
        if ref($info) ne 'HASH';
    my $attr = $info->{lv_attr} // '';
    die "snapshot permission inventory for '$vg/$lv' is ambiguous\n"
        if $attr !~ /^.[rw]/;
    if (substr($attr, 1, 1) eq 'w') {
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-pr', "$vg/$lv"],
            errmsg => "making snapshot generation '$vg/$lv' read-only failed",
        );
    }
    return $class->_thick_verify_snapshot_readonly($vg, $lv, $device);
}

sub _thick_filesystem_path {
    my ($class, $scfg, $volname, $snapname) = @_;
    if (defined($snapname)) {
        my ($snapshot) = $class->_thick_find_snapshot(
            undef, $scfg, $volname, $snapname,
        );
        my (undef, undef, $vmid) = $class->parse_volname($volname);
        my $path = "/dev/$scfg->{'slt-vgname'}/$snapshot";
        return wantarray ? ($path, $vmid, 'images') : $path;
    }
    my $mapper = mapper_name($class->_thick_namespace($scfg), $volname);
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    return wantarray ? ("/dev/mapper/$mapper", $vmid, 'images') : "/dev/mapper/$mapper";
}

sub _thick_list_images {
    my ($class, $storeid, $scfg, $vmid, $vollist, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
    my $res = [];
    return $res if !$lvs->{$vg};
    for my $anchor (sort grep { /^sltg-a-[0-9a-f]{24}$/ } keys %{$lvs->{$vg}}) {
        my $state = decode_anchor_tags($lvs->{$vg}->{$anchor}->{tags} // '');
        next if $state->{sid} ne $storeid;
        my (undef, $name, $owner) = $class->parse_volname($state->{vol});
        next if defined($vmid) && $owner != $vmid;
        my $expected_anchor = anchor_name($class->_thick_namespace($scfg), $name);
        die "thick-generations anchor name mismatch for '$vg/$anchor'\n"
            if $anchor ne $expected_anchor;
        die "thick-generations object '$vg/$anchor' requires recovery\n"
            if $state->{phase} ne 'MATERIALIZED'
            && $state->{phase} ne 'HYDRATING'
            && $state->{phase} ne 'HYDRATION_COMPLETE'
            && $state->{phase} ne 'LINEAR_PIVOTED';
        my $head = $lvs->{$vg}->{$state->{head}};
        die "thick-generations head '$vg/$state->{head}' is missing\n" if !$head;
        validate_generation_tags(
            $head->{tags} // '', sid => $storeid, vol => $name,
            role => 'head', generation => $state->{generation},
        );
        my $volid = "$storeid:$name";
        next if $vollist && !grep { $_ eq $volid } @$vollist;
        push @$res, {
            volid => $volid, format => 'raw', size => $head->{lv_size},
            vmid => $owner, ctime => $head->{ctime},
        };
    }
    return $res;
}

sub _thick_verify_frontend {
    my ($class, $scfg, $volname, $head_name, $expected_sectors) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    my $info = _command_lines(
        ['/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly', $mapper],
        "reading thick-generations frontend '$mapper' failed",
    );
    die "thick-generations frontend '$mapper' identity is ambiguous\n" if @$info != 1;
    my ($actual_uuid, $readonly) = split(/\|/, $info->[0], -1);
    for ($actual_uuid, $readonly) { s/^\s+|\s+$//g; }
    die "thick-generations frontend '$mapper' UUID mismatch\n" if $actual_uuid ne $uuid;
    die "thick-generations frontend '$mapper' is unexpectedly read-only\n"
        if lc($readonly) ne 'writeable';
    my $table = _command_lines(
        ['/sbin/dmsetup', 'table', $mapper],
        "reading thick-generations frontend table '$mapper' failed",
    );
    die "thick-generations frontend '$mapper' table is not one linear segment\n"
        if @$table != 1 || $table->[0] !~ /^0\s+(\d+)\s+linear\s+/;
    my ($actual_sectors) = $table->[0] =~ /^0\s+(\d+)\s+linear\s+/;
    die "thick-generations frontend '$mapper' size mismatch\n"
        if defined($expected_sectors) && $actual_sectors != $expected_sectors;
    my $deps = _command_lines(
        ['/sbin/dmsetup', 'deps', '-o', 'devname', $mapper],
        "reading thick-generations frontend dependencies '$mapper' failed",
    );
    my $head_dm = $vg;
    $head_dm =~ s/-/--/g;
    my $escaped_head = $head_name;
    $escaped_head =~ s/-/--/g;
    my $expected = "$head_dm-$escaped_head";
    die "thick-generations frontend '$mapper' does not depend only on '$vg/$head_name'\n"
        if @$deps != 1 || $deps->[0] !~ /^1\s+dependencies\s*:\s*\(\Q$expected\E\)$/;
    return 1;
}

sub _thick_source_mapper_name {
    my ($class, $scfg, $volname, $generation) = @_;
    return mapper_name($class->_thick_namespace($scfg), $volname)
        . sprintf('-src-%08d', $generation);
}

sub _thick_verify_source_mapper {
    my ($class, $scfg, $mapper, $source, $sectors, $tx) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $info = _command_lines(
        ['/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly', $mapper],
        "reading thick-generations source mapper '$mapper' failed",
    );
    die "thick-generations source mapper '$mapper' identity is ambiguous\n"
        if @$info != 1;
    my ($uuid, $readonly) = split(/\|/, $info->[0], -1);
    for ($uuid, $readonly) { s/^\s+|\s+$//g; }
    die "thick-generations source mapper '$mapper' UUID mismatch\n"
        if $uuid ne "SLT-TG3-SOURCE-$tx";
    die "thick-generations source mapper '$mapper' is not read-only\n"
        if lc($readonly) ne 'read-only';
    my $table = _command_lines(
        ['/sbin/dmsetup', 'table', $mapper],
        "reading thick-generations source mapper '$mapper' table failed",
    );
    die "thick-generations source mapper '$mapper' table mismatch\n"
        if @$table != 1 || $table->[0] !~ /^0\s+\Q$sectors\E\s+linear\s+/;
    my $deps = _command_lines(
        ['/sbin/dmsetup', 'deps', '-o', 'devname', $mapper],
        "reading thick-generations source mapper '$mapper' dependencies failed",
    );
    my $vg_dm = $vg;
    $vg_dm =~ s/-/--/g;
    my $source_dm = $source;
    $source_dm =~ s/-/--/g;
    my $expected = "$vg_dm-$source_dm";
    die "thick-generations source mapper '$mapper' dependency mismatch\n"
        if @$deps != 1 || $deps->[0] !~ /^1\s+dependencies\s*:\s*\(\Q$expected\E\)$/;
    return 1;
}

sub _thick_mapper_is_suspended {
    my ($class, $mapper) = @_;
    my $state = _command_lines(
        ['/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'suspended', $mapper],
        "reading device-mapper suspend state for '$mapper' failed",
    );
    die "device-mapper suspend state for '$mapper' is ambiguous\n" if @$state != 1;
    my $value = $state->[0];
    $value =~ s/^\s+|\s+$//g;
    return 0 if $value eq 'Active';
    return 1 if $value eq 'Suspended';
    die "device-mapper suspend state for '$mapper' is unknown: '$value'\n";
}

# Intentionally inert production hook. Qualification drivers may locally
# override this method to terminate only their own disposable worker at an
# exact persisted crash boundary. No configuration or environment variable can
# enable fault injection in the packaged plugin.
sub _thick_fault_point {
    return;
}

sub _thick_transition_anchor {
    my ($class, $vg, $anchor, $state, %change) = @_;
    my $device = delete $change{_device};
    my $old = PVE::SharedLvmThinThick::anchor_tags(%$state);
    my %next = (%$state, %change);
    validate_anchor_transition($state, \%next);
    my $new = PVE::SharedLvmThinThick::anchor_tags(%next);
    $class->_change_exact_tags(
        $vg, $anchor, $old, $new,
        "advancing thick-generations anchor '$vg/$anchor' failed",
        $device,
    );
    return \%next;
}

sub _thick_verify_clone_status {
    my ($class, $mapper, $must_be_complete) = @_;
    my $lines = _command_lines(
        ['/sbin/dmsetup', 'status', '--noflush', $mapper],
        "reading dm-clone status for '$mapper' failed",
    );
    die "dm-clone status for '$mapper' is ambiguous\n" if @$lines != 1;
    my ($hydrated, $total, $hydrating) =
        $lines->[0] =~ /^0\s+\d+\s+clone\s+\S+\s+\S+\s+\S+\s+(\d+)\/(\d+)\s+(\d+)(?:\s|$)/;
    die "dm-clone status for '$mapper' is malformed\n"
        if !defined($hydrated) || !$total || !defined($hydrating);
    die "dm-clone hydration for '$mapper' is incomplete ($hydrated/$total, $hydrating active)\n"
        if $must_be_complete && ($hydrated != $total || $hydrating != 0);
    return (int($hydrated), int($total), int($hydrating));
}

sub _thick_verify_transition_metadata {
    my ($class, $storeid, $scfg, $volname, $info, %expected) = @_;
    my $device = delete $expected{device};
    die "transition metadata inventory is missing\n" if ref($info) ne 'HASH';
    validate_transition_tags(
        $info->{tags} // '', sid => $storeid, vol => $volname,
        tx => $expected{tx}, kind => 'metadata',
        generation => $expected{generation}, region => $expected{region},
    );
    die "transition metadata size is unknown or smaller than planned\n"
        if !defined($info->{lv_size}) || $info->{lv_size} !~ /^\d+$/
        || $info->{lv_size} < $expected{metadata_bytes};
    $class->_verify_autoactivation_disabled(
        $scfg->{'slt-vgname'}, $expected{name}, $device,
    );
    return 1;
}

sub _thick_verify_clone_frontend {
    my ($class, $scfg, $volname, %expected) = @_;
    my ($threshold, $batch) = $class->_thick_hydration_tuning($scfg);
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    my $info = _command_lines(
        ['/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
            '-o', 'uuid,readonly', $mapper],
        "reading dm-clone frontend '$mapper' failed",
    );
    die "dm-clone frontend '$mapper' identity is ambiguous\n" if @$info != 1;
    my ($actual_uuid, $readonly) = split(/\|/, $info->[0], -1);
    for ($actual_uuid, $readonly) { s/^\s+|\s+$//g; }
    die "dm-clone frontend '$mapper' UUID mismatch\n" if $actual_uuid ne $uuid;
    die "dm-clone frontend '$mapper' is unexpectedly read-only\n"
        if lc($readonly) ne 'writeable';

    my $table = _command_lines(
        ['/sbin/dmsetup', 'table', $mapper],
        "reading dm-clone frontend table '$mapper' failed",
    );
    die "dm-clone frontend '$mapper' table mismatch\n"
        if @$table != 1
        || $table->[0] !~ /^0\s+\Q$expected{sectors}\E\s+clone\s+\S+\s+\S+\s+\S+\s+\Q$expected{region}\E\s+2\s+no_hydration\s+no_discard_passdown\s+4\s+hydration_threshold\s+\Q$threshold\E\s+hydration_batch_size\s+\Q$batch\E$/;

    my $deps = _command_lines(
        ['/sbin/dmsetup', 'deps', '-o', 'devname', $mapper],
        "reading dm-clone frontend dependencies '$mapper' failed",
    );
    die "dm-clone frontend '$mapper' dependency report is ambiguous\n" if @$deps != 1;
    my @actual = sort($deps->[0] =~ /\(([^()]+)\)/g);
    my $vg_dm = $scfg->{'slt-vgname'};
    $vg_dm =~ s/-/--/g;
    my @wanted;
    for my $name ($expected{meta}, $expected{new}) {
        my $escaped = $name;
        $escaped =~ s/-/--/g;
        push @wanted, "$vg_dm-$escaped";
    }
    push @wanted, $expected{source_map};
    @wanted = sort @wanted;
    die "dm-clone frontend '$mapper' dependency graph mismatch\n"
        if @actual != @wanted || grep { $actual[$_] ne $wanted[$_] } 0 .. $#wanted;
    return 1;
}

sub _thick_wait_for_hydration {
    my ($class, $mapper, $timeout) = @_;
    die "invalid thick-generations hydration timeout\n"
        if !defined($timeout) || $timeout !~ /^\d+$/ || $timeout < 60 || $timeout > 86400;
    my ($hydrated, $total, $hydrating) = $class->_thick_verify_clone_status($mapper, 0);
    return 1 if $hydrated == $total && $hydrating == 0;

    my $events = _command_lines(
        ['/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'events', $mapper],
        "reading dm-clone event counter for '$mapper' failed",
    );
    die "dm-clone event counter for '$mapper' is ambiguous\n"
        if @$events != 1 || $events->[0] !~ /^\d+$/;
    my $event = int($events->[0]);

    # Close the completion-before-wait race after capturing the event number.
    ($hydrated, $total, $hydrating) = $class->_thick_verify_clone_status($mapper, 0);
    return 1 if $hydrated == $total && $hydrating == 0;

    my $wait_error = '';
    eval {
        run_command(
            ['/usr/bin/timeout', '--kill-after=5s', "${timeout}s",
                '/sbin/dmsetup', 'wait', $mapper, "$event"],
            errmsg => "waiting for dm-clone hydration event failed",
        );
    };
    $wait_error = $@ if $@;
    my $complete = eval { $class->_thick_verify_clone_status($mapper, 1); 1 };
    die "dm-clone hydration is not positively complete; one bounded wait was used and "
        . "no additional probe was spawned"
        . ($wait_error ? ": $wait_error" : "\n") if !$complete;
    return 1;
}

sub _thick_frontend_open_count {
    my ($class, $mapper) = @_;
    my $lines = _command_lines(
        ['/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'open', $mapper],
        "reading thick-generations frontend open count '$mapper' failed",
    );
    die "thick-generations frontend '$mapper' open count is ambiguous\n"
        if @$lines != 1 || $lines->[0] !~ /^\s*\d+\s*$/;
    return int($lines->[0]);
}

sub _thick_schedule_materialization {
    my ($class, $storeid, $volname, $snap, $operation, $tx, $timeout) = @_;
    die "invalid thick-generations worker transaction UUID\n"
        if !defined($tx) || $tx !~ /^[0-9a-f]{32}$/;
    die "invalid thick-generations worker operation\n"
        if !defined($operation) || $operation ne 'SNAPSHOT';
    $snap = _thick_snapshot_name($snap);
    die "invalid thick-generations hydration timeout\n"
        if !defined($timeout) || $timeout !~ /^\d+$/ || $timeout < 60 || $timeout > 86400;

    my $unit = "pve-sharedlvmthin-tg-$tx";
    my $worker = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize';
    run_command(
        ['/usr/bin/systemd-run', '--quiet', '--collect', "--unit=$unit",
            '--on-active=3s', '--timer-property=AccuracySec=100ms',
            '--property=Type=exec', '--property=Nice=10',
            '--property=IOSchedulingClass=best-effort', '--property=IOSchedulingPriority=7',
            "--property=TimeoutStartSec=" . ($timeout + 300),
            $worker, $storeid, $volname, $snap, $operation, $tx],
        errmsg => "scheduling asynchronous Thick Generations materialization failed",
    );
    return $unit;
}

sub _thick_snapshot_name {
    my ($snap) = @_;
    die "snapshot name is missing\n" if !defined($snap) || $snap eq '';
    die "snapshot name contains characters unsafe for persistent LVM metadata\n"
        if $snap !~ /^[A-Za-z0-9_.+-]+$/;
    return $snap;
}

sub _thick_activate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_verify_mutation_quorum($storeid, $scfg);
    $class->_verify_storage_identity($storeid, $scfg, $device);
    my $lvs = $class->_thick_list_volumes_scoped($scfg->{'slt-vgname'}, $device);
    if (defined($snapname)) {
        my ($snapshot) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snapname, $lvs,
        );
        my $vg = $scfg->{'slt-vgname'};
        $class->_thick_verify_snapshot_readonly($vg, $snapshot, $device);
        $class->_verify_autoactivation_disabled($vg, $snapshot, $device);
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-ay', '-K', "$vg/$snapshot"],
            errmsg => "activating thick-generations snapshot '$vg/$snapshot' failed",
        );
        $class->_thick_verify_snapshot_readonly($vg, $snapshot, $device);
        return 1;
    }
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    if (_block_device_exists("/dev/mapper/$mapper")) {
        if ($state->{phase} eq 'MATERIALIZED') {
            $class->_thick_verify_frontend($scfg, $volname, $state->{head});
        } else {
            $class->_thick_verify_published_transition_frontend(
                $storeid, $scfg, $volname, $state,
            );
        }
        return 1;
    }
    die "thick-generations volume '$storeid:$volname' is materializing and its exact frontend is missing; recovery required\n"
        if $state->{phase} ne 'MATERIALIZED';
    run_command(
        ['/sbin/lvchange', '--devices', $device, '-ay', '-K',
            "$vg/$state->{head}", "$vg/$anchor"],
        errmsg => "activating thick-generations state for '$vg/$volname' failed",
    );
    $class->_verify_autoactivation_disabled($vg, $state->{head}, $device);
    my $sectors = _command_lines(
        ['/sbin/blockdev', '--getsz', "/dev/$vg/$state->{head}"],
        "reading thick-generations head size '$vg/$state->{head}' failed",
    );
    die "thick-generations head size is ambiguous\n"
        if @$sectors != 1 || $sectors->[0] !~ /^\d+$/ || $sectors->[0] == 0;
    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    run_command(
        ['/sbin/dmsetup', '--verifyudev', 'create', $mapper, '--uuid', $uuid,
            '--table', "0 $sectors->[0] linear /dev/$vg/$state->{head} 0"],
        errmsg => "creating stable thick-generations frontend '$mapper' failed",
    );
    $class->_thick_verify_frontend($scfg, $volname, $state->{head}, $sectors->[0]);
    return 1;
}

sub _thick_deactivate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_verify_storage_identity($storeid, $scfg, $device);
    my $lvs = $class->_thick_list_volumes_scoped($scfg->{'slt-vgname'}, $device);
    if (defined($snapname)) {
        my ($snapshot) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snapname, $lvs,
        );
        my $vg = $scfg->{'slt-vgname'};
        $class->_thick_verify_snapshot_readonly($vg, $snapshot, $device);
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$snapshot"],
            errmsg => "deactivating thick-generations snapshot '$vg/$snapshot' failed",
        );
        return 1;
    }
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
    my $vg = $scfg->{'slt-vgname'};
    my $mapper = mapper_name($class->_thick_namespace($scfg), $volname);
    if (_block_device_exists("/dev/mapper/$mapper")) {
        if ($state->{phase} eq 'MATERIALIZED') {
            $class->_thick_verify_frontend($scfg, $volname, $state->{head});
        } else {
            $class->_thick_verify_published_transition_frontend(
                $storeid, $scfg, $volname, $state,
            );
        }
        my $opens;
        for my $attempt (0 .. 20) {
            $opens = $class->_thick_frontend_open_count($mapper);
            last if $opens == 0;
            last if $attempt == 20;
            select(undef, undef, undef, 0.1);
        }
        die "refusing to deactivate open thick-generations frontend '$mapper' after bounded close wait\n"
            if $opens != 0;
        # A close can race qmeventd cleanup. Revalidate the exact table after
        # the bounded wait so a name reuse or table change cannot be mistaken
        # for the frontend verified before waiting.
        if ($state->{phase} eq 'MATERIALIZED') {
            $class->_thick_verify_frontend($scfg, $volname, $state->{head});
        } else {
            $class->_thick_verify_published_transition_frontend(
                $storeid, $scfg, $volname, $state,
            );
        }
        # A materialization worker owns the published transition mapping and
        # its dependencies.  A guest stop may close the frontend while that
        # worker is still hydrating or finalizing.  Leaving the exact verified
        # zero-open mapping in place is safe; removing any part of it here
        # would turn a normal stop into a partial transaction.
        return 1 if $state->{phase} ne 'MATERIALIZED';
        run_command(
            ['/sbin/dmsetup', 'remove', '--retry', $mapper],
            errmsg => "removing stable thick-generations frontend '$mapper' failed",
        );
    }
    run_command(
        ['/sbin/lvchange', '--devices', $device, '-an',
            "$vg/$anchor", "$vg/$state->{head}"],
        errmsg => "deactivating thick-generations state for '$vg/$volname' failed",
    );
    return 1;
}

sub _command_lines {
    my ($command, $errmsg) = @_;
    my @lines;

    run_command(
        $command,
        outfunc => sub {
            my ($line) = @_;
            $line =~ s/^\s+|\s+$//g;
            push @lines, $line if length($line);
        },
        errmsg => $errmsg,
    );

    return \@lines;
}

sub _thick_list_volumes_scoped {
    my ($class, $vg, $device) = @_;
    die "scoped Thick Generations inventory requires an exact mapper device\n"
        if !defined($device) || $device !~ m{^/dev/mapper/[0-9A-Fa-f]+$};
    my @json;
    run_command(
        ['/sbin/lvs', '--readonly', '--reportformat', 'json', '--units', 'b',
            '--nosuffix', '--devices', $device,
            '-o', 'vg_name,lv_name,lv_size,lv_attr,lv_tags', $vg],
        outfunc => sub { push @json, $_[0]; },
        errmsg => "reading scoped Thick Generations inventory of VG '$vg' failed",
    );
    my $report = eval { decode_json(join("\n", @json)) };
    die "scoped Thick Generations inventory of VG '$vg' is malformed: $@\n"
        if $@ || ref($report) ne 'HASH'
        || ref($report->{report}) ne 'ARRAY' || @{$report->{report}} != 1
        || ref($report->{report}->[0]->{lv}) ne 'ARRAY';
    my $result = {};
    for my $row (@{$report->{report}->[0]->{lv}}) {
        die "scoped Thick Generations inventory contains a malformed row\n"
            if ref($row) ne 'HASH' || ($row->{vg_name} // '') ne $vg
            || ($row->{lv_name} // '') eq '' || ($row->{lv_size} // '') !~ /^\d+$/
            || ($row->{lv_attr} // '') eq '';
        my $name = $row->{lv_name};
        die "scoped Thick Generations inventory contains duplicate LV '$name'\n"
            if exists($result->{$vg}->{$name});
        $result->{$vg}->{$name} = {
            lv_size => int($row->{lv_size}),
            lv_attr => $row->{lv_attr},
            lv_state => substr($row->{lv_attr}, 4, 1),
            lv_type => substr($row->{lv_attr}, 0, 1),
            tags => $row->{lv_tags} // '',
        };
    }
    return $result;
}

sub _block_device_exists {
    my ($path) = @_;
    return -b $path;
}

sub _canonical_vg_lock_id {
    my ($class, $scfg) = @_;
    my $uuid = $scfg->{'slt-expected-vg-uuid'};
    die "shared VG mutation requires a pinned VG UUID\n"
        if !defined($uuid) || $uuid eq '';
    return 'slt-vg-' . substr(sha256_hex(lc($uuid)), 0, 32);
}

sub _canonical_node_scope {
    my ($nodes) = @_;
    return '' if !defined($nodes);

    my @nodes;
    if (!ref($nodes)) {
        @nodes = split(/,/, $nodes);
    } elsif (ref($nodes) eq 'HASH') {
        @nodes = keys %$nodes;
    } elsif (ref($nodes) eq 'ARRAY') {
        @nodes = @$nodes;
    } else {
        die "same-VG alias has an unsupported PVE node-scope representation\n";
    }

    for my $node (@nodes) {
        die "same-VG alias has an ambiguous PVE node scope\n"
            if !defined($node) || ref($node);
        $node =~ s/^\s+|\s+$//g;
        die "same-VG alias has an ambiguous PVE node scope\n" if $node eq '';
    }
    @nodes = sort @nodes;
    die "same-VG alias has an ambiguous PVE node scope\n"
        if do { my %seen; grep { $seen{$_}++ } @nodes };
    return join(',', @nodes);
}

sub _verify_same_vg_alias_configuration {
    my ($class, $storeid, $scfg) = @_;
    my $vg = $scfg->{'slt-vgname'} // die "storage '$storeid' has no VG name\n";
    my $cfg = PVE::Storage::config();
    my $ids = $cfg->{ids};
    die "PVE storage configuration inventory is unavailable\n"
        if ref($ids) ne 'HASH';

    my @foreign_vg_references = sort grep {
        my $candidate = $ids->{$_};
        ref($candidate) eq 'HASH'
            && ($candidate->{type} // '') ne 'sharedlvmthin'
            && defined($candidate->{vgname})
            && $candidate->{vgname} eq $vg;
    } keys %$ids;
    die "shared VG '$vg' is also referenced by non-SharedLvmThin storage '"
        . join("', '", @foreign_vg_references)
        . "'; canonical mutation locking cannot be guaranteed\n"
        if @foreign_vg_references;

    my @aliases = sort grep {
        my $candidate = $ids->{$_};
        ref($candidate) eq 'HASH'
            && ($candidate->{type} // '') eq 'sharedlvmthin'
            && ($candidate->{'slt-vgname'} // '') eq $vg;
    } keys %$ids;
    return 1 if @aliases <= 1;
    die "shared VG '$vg' has more than two SharedLvmThin aliases; supported same-VG "
        . "coexistence is exactly one thin and one thick-generations alias\n"
        if @aliases != 2;

    my %mode;
    my %identity;
    my %reserve;
    my %minimum_paths;
    my %node_scope;
    for my $alias (@aliases) {
        my $candidate = $ids->{$alias};
        die "same-VG alias '$alias' is not configured as shared storage\n"
            if !$candidate->{shared};
        my $allocation = $class->_allocation_mode($candidate);
        die "shared VG '$vg' has duplicate '$allocation' allocation aliases\n"
            if $mode{$allocation}++;
        for my $field (qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid)) {
            my $value = $candidate->{$field};
            die "same-VG alias '$alias' must pin '$field'\n"
                if !defined($value) || $value eq '';
            $identity{$field}->{lc($value)} = 1;
        }
        my $reserve_key = join('|',
            $candidate->{'slt-vg-reserve-percent'} // '',
            $candidate->{'slt-vg-reserve-gib'} // '',
        );
        $reserve{$reserve_key} = 1;
        $minimum_paths{$candidate->{'slt-expected-min-paths'} // ''} = 1;
        $node_scope{_canonical_node_scope($candidate->{nodes})} = 1;
    }
    die "shared VG '$vg' requires exactly one thin and one thick-generations alias\n"
        if !$mode{thin} || !$mode{'thick-generations'};
    for my $field (sort keys %identity) {
        die "same-VG aliases disagree on '$field'\n"
            if keys(%{$identity{$field}}) != 1;
    }
    die "same-VG aliases must use identical protected VG reserve settings\n"
        if keys(%reserve) != 1;
    die "same-VG aliases must use the same expected minimum path count\n"
        if keys(%minimum_paths) != 1;
    die "same-VG aliases must use the same PVE node scope\n"
        if keys(%node_scope) != 1;
    return 1;
}

sub _with_vg_lock {
    my ($class, $storeid, $scfg, $code, $device) = @_;
    my $lockid = $class->_canonical_vg_lock_id($scfg);
    # Fail immediately when quorum is already absent. The same gate is repeated
    # under the lock because quorum may disappear while the caller waits.
    $class->_verify_mutation_quorum($storeid, $scfg);
    return $class->cluster_lock_storage(
        $lockid, $scfg->{shared}, undef,
        sub {
            $class->_verify_mutation_quorum($storeid, $scfg);
            $class->_verify_storage_identity($storeid, $scfg, $device);
            $class->_verify_same_vg_alias_configuration($storeid, $scfg);
            return $code->();
        },
    );
}

sub _with_mutation_lock {
    my ($class, $storeid, $scfg, $code) = @_;
    my $uuid = $scfg->{'slt-expected-vg-uuid'};

    # Pinned storage aliases that share one VG must serialize on one canonical
    # lock, regardless of whether the selected allocation mode is thin or
    # Thick Generations. Legacy unpinned configurations retain their historic
    # per-storage lock and are not qualified for same-VG mixed-mode use.
    if (defined($uuid) && $uuid ne '') {
        my $device = defined($scfg->{'slt-expected-wwid'})
            ? "/dev/mapper/$scfg->{'slt-expected-wwid'}"
            : undef;
        return $class->_with_vg_lock($storeid, $scfg, sub {
            $class->_require_no_vg_intent($scfg->{'slt-vgname'}, $device);
            return $code->();
        }, $device);
    }

    $class->_verify_mutation_quorum($storeid, $scfg);
    return $class->cluster_lock_storage(
        $storeid, $scfg->{shared}, undef,
        sub {
            $class->_verify_mutation_quorum($storeid, $scfg);
            $class->_verify_storage_identity($storeid, $scfg);
            return $code->();
        },
    );
}

sub _vg_state_digest {
    my ($class, $vg, $device) = @_;
    my @command = ('/sbin/vgs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--units', 'b', '--nosuffix', '--separator', '|',
        '-o', 'vg_uuid,vg_seqno,vg_free_count,vg_extent_size', $vg);
    my $lines = _command_lines(
        \@command,
        "reading before-state of VG '$vg' failed",
    );
    die "VG '$vg' before-state is ambiguous\n" if @$lines != 1;
    my $state = $lines->[0];
    $state =~ s/\s+//g;
    die "VG '$vg' before-state is malformed\n"
        if $state !~ /^([A-Za-z0-9-]+\|\d+\|\d+\|\d+(?:\.\d+)?)$/;
    $state = $1;
    return substr(sha256_hex($state), 0, 32);
}

sub _vg_tags {
    my ($class, $vg, $device) = @_;
    my @command = ('/sbin/vgs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--noheadings', '--separator', '|',
        '-o', 'vg_name,vg_tags', $vg);
    my $lines = _command_lines(
        \@command,
        "reading mutation intent of VG '$vg' failed",
    );
    die "VG '$vg' tag state is ambiguous\n" if @$lines != 1;
    my ($observed_vg, $tags) = split(/\|/, $lines->[0], 2);
    for ($observed_vg, $tags) { $_ //= ''; s/^\s+|\s+$//g; }
    die "VG '$vg' tag state belongs to '$observed_vg'\n" if $observed_vg ne $vg;
    return $tags;
}

sub _read_vg_intent {
    my ($class, $vg, $device) = @_;
    return decode_vg_intent_tags($class->_vg_tags($vg, $device));
}

sub _require_no_vg_intent {
    my ($class, $vg, $device) = @_;
    my $intent = $class->_read_vg_intent($vg, $device);
    die "VG '$vg' has unresolved transaction '$intent->{tx}' ($intent->{op} $intent->{object}); mutation refused\n"
        if $intent;
    return 1;
}

sub _require_exact_vg_intent {
    my ($class, $vg, %expected) = @_;
    my $device = delete($expected{_device});
    my $intent = $class->_read_vg_intent($vg, $device);
    die "VG '$vg' has no recoverable mutation intent\n" if !$intent;
    die "VG '$vg' mutation intent does not match the requested transaction\n"
        if grep { "$intent->{$_}" ne "$expected{$_}" }
            qw(tx state op object before);
    return 1;
}

sub _set_vg_intent {
    my ($class, $vg, %intent) = @_;
    my $device = delete($intent{_device});
    $class->_require_no_vg_intent($vg, $device);
    my $observed = $class->_vg_state_digest($vg, $device);
    die "VG '$vg' changed before mutation intent could be committed\n"
        if $observed ne $intent{before};
    my $tags = vg_intent_tags(%intent);
    my @command = ('/sbin/vgchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, map { ('--addtag', $_) } @$tags;
    push @command, $vg;
    run_command(\@command, errmsg => "setting mutation intent on VG '$vg' failed");
    my $after = $class->_read_vg_intent($vg, $device);
    die "VG '$vg' mutation-intent postcondition failed\n"
        if !$after || grep { "$after->{$_}" ne "$intent{$_}" }
            qw(tx state op object before);
    return 1;
}

sub _clear_vg_intent {
    my ($class, $vg, %expected) = @_;
    my $device = delete($expected{_device});
    my $current = $class->_read_vg_intent($vg, $device);
    die "VG '$vg' mutation-intent clear precondition failed\n"
        if !$current || grep { "$current->{$_}" ne "$expected{$_}" }
            qw(tx state op object before);
    my $tags = vg_intent_tags(%$current);
    my @command = ('/sbin/vgchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, map { ('--deltag', $_) } @$tags;
    push @command, $vg;
    run_command(\@command, errmsg => "clearing mutation intent on VG '$vg' failed");
    die "VG '$vg' mutation-intent clear postcondition failed\n"
        if defined($class->_read_vg_intent($vg, $device));
    return 1;
}

sub _thick_require_transition_intent {
    my ($class, $storeid, $scfg, $volname, $intent) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    if (!$intent->{_anchor_scoped}) {
        my %exact = %$intent;
        delete $exact{_anchor_scoped};
        return $class->_require_exact_vg_intent(
            $vg, %exact, _device => $device,
        );
    }

    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    die "anchor-scoped transition intent does not match persistent state\n"
        if $anchor ne ($intent->{object} // '')
        || ($state->{tx} // '') ne ($intent->{tx} // '')
        || ($state->{op} // '') ne 'SNAPSHOT'
        || ($intent->{op} // '') ne 'DM_CUTOVER'
        || ($state->{phase} // '') !~ /^(?:HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
    return 1;
}

sub _thick_scope_transition_intent_to_anchor {
    my ($class, $storeid, $scfg, $volname, $intent) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $class->_require_exact_vg_intent($vg, %$intent, _device => $device);
    my ($state, undef, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname);
    die "published transition is not safe for anchor-scoped materialization\n"
        if $anchor ne ($intent->{object} // '')
        || ($state->{tx} // '') ne ($intent->{tx} // '')
        || ($state->{op} // '') ne 'SNAPSHOT'
        || ($state->{phase} // '') ne 'HYDRATING';
    $class->_clear_vg_intent($vg, %$intent, _device => $device);
    die "VG intent remained after anchor-scoped handoff\n"
        if defined($class->_read_vg_intent($vg, $device));
    $intent->{_anchor_scoped} = 1;
    return 1;
}

sub _new_transaction_id {
    open(my $fh, '<', '/proc/sys/kernel/random/uuid')
        or die "cannot obtain kernel transaction UUID: $!\n";
    my $tx = <$fh> // '';
    close($fh);
    $tx =~ s/[^0-9A-Fa-f]//g;
    $tx = lc($tx);
    die "kernel returned an invalid transaction UUID\n" if $tx !~ /^([0-9a-f]{32})$/;
    return $1;
}

sub _thick_capacity_gate {
    my ($class, $storeid, $scfg, $size_kib, $overhead_bytes) = @_;
    die "thick-generations allocation requires slt-vg-reserve-percent or slt-vg-reserve-gib\n"
        if !defined($scfg->{'slt-vg-reserve-percent'})
        && !defined($scfg->{'slt-vg-reserve-gib'});
    my $vg = $scfg->{'slt-vgname'};
    my ($vg_size, $vg_free, $extent_size) = _allocation_numeric_fields(
        [
            '/sbin/vgs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
            '--separator', '|', '-o', 'vg_size,vg_free,vg_extent_size', $vg,
        ],
        "reading thick-generations capacity of VG '$vg' failed", 3,
    );
    my $bytes = int($size_kib) * 1024;
    $overhead_bytes = 8 * 1024 * 1024 if !defined($overhead_bytes);
    my $decision = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => int($vg_size), vg_free => int($vg_free),
        growth_bytes => $bytes, overhead_bytes => $overhead_bytes,
        extent_bytes => int($extent_size),
        reserve_percent => $scfg->{'slt-vg-reserve-percent'} // 0,
        reserve_gib => $scfg->{'slt-vg-reserve-gib'} // 0,
    );
    die "thick-generations allocation rejected for '$vg': requires "
        . "$decision->{required_physical_bytes} bytes; projected free "
        . "$decision->{free_after_bytes} bytes would cross protected reserve "
        . "$decision->{reserve_bytes} bytes; no LV was created\n"
        if !$decision->{allowed};
    return $decision;
}

sub _change_exact_tags {
    my ($class, $vg, $lv, $remove, $add, $errmsg, $device) = @_;
    my $read_tags = sub {
        my @command = ('/sbin/lvs', '--readonly');
        push @command, ('--devices', $device) if defined($device);
        push @command, ('--noheadings', '-o', 'lv_tags', "$vg/$lv");
        my $lines = _command_lines(
            \@command,
            "reading exact tag state of '$vg/$lv' failed",
        );
        die "tag state of '$vg/$lv' is ambiguous\n" if @$lines > 1;
        return [] if !@$lines || $lines->[0] eq '';
        my @tags = split(/,/, $lines->[0]);
        for (@tags) { s/^\s+|\s+$//g; }
        die "tag state of '$vg/$lv' contains an empty or duplicate tag\n"
            if grep { $_ eq '' } @tags
            || do { my %seen; grep { $seen{$_}++ } @tags };
        return \@tags;
    };
    my $same = sub {
        my ($left, $right) = @_;
        return 0 if @$left != @$right;
        my %left = map { $_ => 1 } @$left;
        return !grep { !$left{$_} } @$right;
    };

    my $before = $read_tags->();
    die "tag mutation precondition failed for '$vg/$lv'\n"
        if !$same->($before, $remove);
    my %remove = map { $_ => 1 } @$remove;
    my %add = map { $_ => 1 } @$add;
    my @remove_delta = grep { !$add{$_} } @$remove;
    my @add_delta = grep { !$remove{$_} } @$add;
    my @command = ('/sbin/lvchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, map { ('--deltag', $_) } @remove_delta;
    push @command, map { ('--addtag', $_) } @add_delta;
    push @command, "$vg/$lv";
    run_command(\@command, errmsg => $errmsg);
    my $after = $read_tags->();
    die "tag mutation postcondition failed for '$vg/$lv'; state preserved for recovery\n"
        if !$same->($after, $add);
    return 1;
}

sub _thick_verify_allocation_state {
    my ($class, $storeid, $scfg, $volname, $tx, $phase, $generation, $device) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $head = generation_name($namespace, $volname, $generation);
    die "thick-generations allocation verification requires a pinned mapper device\n"
        if !defined($device);
    my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
    die "thick-generations allocation state is unavailable\n" if !$lvs->{$vg};
    die "thick-generations allocation object is incomplete\n"
        if !$lvs->{$vg}->{$anchor} || !$lvs->{$vg}->{$head};
    my $state = decode_anchor_tags($lvs->{$vg}->{$anchor}->{tags} // '');
    die "thick-generations allocation anchor mismatch\n"
        if $state->{sid} ne $storeid || $state->{vol} ne $volname
        || $state->{tx} ne $tx || $state->{phase} ne $phase
        || $state->{old} ne $head || $state->{new} ne $head
        || $state->{head} ne $head || $state->{generation} != $generation;
    validate_generation_tags(
        $lvs->{$vg}->{$head}->{tags} // '', sid => $storeid, vol => $volname,
        role => 'head', generation => $generation,
    );
    $class->_verify_autoactivation_disabled($vg, $head, $device);
    $class->_verify_autoactivation_disabled($vg, $anchor, $device);
    return ($state, $anchor, $head);
}

sub _thick_require_fresh_object_names {
    my ($class, $vg, $objects, $volname, $anchor, $head) = @_;
    die "thick-generations name-collision check requires an exact VG inventory\n"
        if ref($objects) ne 'HASH';

    for my $candidate (
        [$volname, 'PVE volume'],
        [$anchor, 'anchor'],
        [$head, 'generation'],
    ) {
        my ($name, $kind) = @$candidate;
        next if !exists($objects->{$name});
        die "refusing thick-generations allocation: $kind name collision at "
            . "'$vg/$name'; an existing LV is never adopted or overwritten\n";
    }
    return 1;
}

sub _thick_select_fresh_guest_name {
    my ($class, $namespace, $vmid, $requested, $objects) = @_;
    die "thick-generations name selection requires an exact VG inventory\n"
        if ref($objects) ne 'HASH';
    die "thick-generations name selection requires a canonical guest disk name\n"
        if !defined($requested) || $requested !~ /^vm-\Q$vmid\E-disk-\d+$/;

    for my $index (0 .. 9999) {
        my $candidate = "vm-$vmid-disk-$index";
        my $anchor = anchor_name($namespace, $candidate);
        my $head = generation_name($namespace, $candidate, 0);
        next if exists($objects->{$candidate});
        next if exists($objects->{$anchor}) || exists($objects->{$head});
        return $candidate;
    }
    die "no collision-free Thick Generations guest disk name is available for VM $vmid\n";
}

sub _thick_recover_empty_allocation {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "empty-allocation recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $key = object_key($namespace, $volname);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($vg, $device);
        die "VG '$vg' has no recoverable mutation intent\n" if !$intent;
        die "VG '$vg' intent is not the exact empty ALLOC transaction for '$volname'\n"
            if $intent->{state} ne 'OPEN' || $intent->{op} ne 'ALLOC'
            || $intent->{object} ne $anchor;

        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        die "empty-allocation recovery inventory is unavailable\n" if !$lvs->{$vg};
        my $objects = $lvs->{$vg};
        my @related = sort grep {
            $_ eq $volname || $_ eq $anchor
            || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$objects;
        die "empty-allocation recovery refused: transaction-related LV state exists ("
            . join(', ', @related) . ")\n" if @related;
        die "empty-allocation recovery refused: transaction frontend '$mapper' exists\n"
            if _block_device_exists("/dev/mapper/$mapper");

        $class->_clear_vg_intent($vg, %$intent, _device => $device);
        return 'EMPTY_ALLOCATION_INTENT_RECOVERED';
    }, $device);
}

sub _thick_pve_reference_files {
    my ($class, $storeid, $volname) = @_;
    my $volid = "$storeid:$volname";
    my %files;
    for my $pattern (
        '/etc/pve/nodes/*/qemu-server/*.conf', '/etc/pve/nodes/*/lxc/*.conf',
        '/etc/pve/qemu-server/*.conf', '/etc/pve/lxc/*.conf',
    ) {
        $files{$_} = 1 for glob($pattern);
    }
    my @references;
    for my $file (sort keys %files) {
        next if !-f $file;
        open(my $fh, '<', $file) or die "reading PVE reference file '$file' failed: $!\n";
        my $text = do { local $/; <$fh> };
        close($fh) or die "closing PVE reference file '$file' failed: $!\n";
        push @references, $file if ($text // '') =~ /\Q$volid\E(?:,|\s|$)/m;
    }
    return \@references;
}

sub _thick_recover_partial_allocation {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my (undef, $name) = $class->parse_volname($volname);
    die "partial-allocation recovery requires the canonical volume name\n"
        if $name ne $volname;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $anchor = anchor_name($namespace, $volname);
    my $head = generation_name($namespace, $volname, 0);
    my $key = object_key($namespace, $volname);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($vg, $device);
        die "VG '$vg' has no recoverable mutation intent\n" if !$intent;
        die "VG '$vg' intent is not the exact partial ALLOC transaction for '$volname'\n"
            if $intent->{state} ne 'OPEN' || $intent->{op} ne 'ALLOC'
            || $intent->{object} ne $anchor;

        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        die "partial-allocation recovery inventory is unavailable\n" if !$lvs->{$vg};
        my $objects = $lvs->{$vg};
        my @related = sort grep {
            $_ eq $volname || $_ eq $anchor || /^sltg-(?:g|m)-\Q$key\E-/
        } keys %$objects;
        die "partial-allocation recovery requires exactly the signed anchor and generation-0 HEAD\n"
            if @related != 2 || $related[0] ne $anchor || $related[1] ne $head;
        die "partial-allocation recovery refused: transaction frontend '$mapper' exists\n"
            if _block_device_exists("/dev/mapper/$mapper");

        my $state = decode_anchor_tags($objects->{$anchor}->{tags} // '');
        die "partial-allocation anchor does not match the exact OPEN ALLOC transaction\n"
            if $state->{sid} ne $storeid || $state->{vol} ne $volname
            || $state->{phase} ne 'PREPARED' || $state->{op} ne 'ALLOC'
            || $state->{tx} ne $intent->{tx} || $state->{snapshot} ne 'none'
            || $state->{generation} != 0 || $state->{head} ne $head
            || $state->{source} ne $head || $state->{old} ne $head
            || $state->{new} ne $head;
        validate_generation_tags(
            $objects->{$head}->{tags} // '', sid => $storeid, vol => $volname,
            role => 'head', generation => 0,
        );
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "partial-allocation recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;
        for my $object ($anchor, $head) {
            $class->_verify_autoactivation_disabled($vg, $object, $device);
            my $active = _command_lines(
                ['/sbin/lvs', '--noheadings', '--devices', $device,
                    '-o', 'lv_attr', "$vg/$object"],
                "reading partial-allocation LV state '$vg/$object' failed",
            );
            die "partial-allocation recovery refused: '$vg/$object' active state is ambiguous\n"
                if @$active != 1 || $active->[0] !~ /^....([a-]).*$/;
            if ($1 eq 'a') {
                run_command(
                    ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$object"],
                    errmsg => "deactivating partial-allocation object '$vg/$object' failed",
                );
                my $after = _command_lines(
                    ['/sbin/lvs', '--noheadings', '--devices', $device,
                        '-o', 'lv_attr', "$vg/$object"],
                    "verifying partial-allocation LV state '$vg/$object' failed",
                );
                die "partial-allocation recovery refused: '$vg/$object' did not become inactive\n"
                    if @$after != 1 || $after->[0] !~ /^....-.*$/;
            }
        }
        my $command_error = '';
        eval {
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$head"],
                errmsg => "removing partial thick generation '$vg/$head' failed",
            );
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$anchor"],
                errmsg => "removing partial thick anchor '$vg/$anchor' failed",
            );
        };
        $command_error = $@ if $@;
        my $after = $class->_thick_list_volumes_scoped($vg, $device);
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL ALLOCATION CLEANUP: storage identity could not be revalidated; "
            . "OPEN ALLOC intent preserved: $@" if $@;
        my $after_objects = $after->{$vg} // {};
        die "PARTIAL ALLOCATION CLEANUP: exact objects remain; OPEN ALLOC intent preserved"
            . ($command_error ? ": $command_error" : "\n")
            if exists($after_objects->{$head}) || exists($after_objects->{$anchor});
        die "PARTIAL ALLOCATION CLEANUP: removal reported an error after exact objects disappeared; "
            . "OPEN ALLOC intent preserved and no command was retried: $command_error"
            if $command_error;
        $class->_clear_vg_intent($vg, %$intent, _device => $device);
        return 'PARTIAL_ALLOCATION_RECOVERED';
    }, $device);
}

sub _thick_alloc_image {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;
    die "unsupported format '$fmt'\n" if defined($fmt) && $fmt ne 'raw';
    $class->_require_thick_identity_config($storeid, $scfg);
    $name = $class->find_free_diskname($storeid, $scfg, $vmid) if !$name;
    my $is_guest = $name =~ /^vm-\Q$vmid\E-disk-\d+$/;
    my $is_aux = $name =~ /^vm-\Q$vmid\E-(?:state-[A-Za-z0-9][A-Za-z0-9_.-]*|fleece-\d+|cloudinit)$/;
    die "illegal volume name '$name'\n" if !$is_guest && !$is_aux;
    die "invalid thick-generations allocation size\n"
        if !defined($size) || $size !~ /^\d+$/ || $size < 1;

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $generation = 0;
    my $geometry = clone_geometry(int($size) * 1024);
    my ($anchor, $head);
    my $tx = $class->_new_transaction_id();
    my %intent;

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($vg, $device);
        $class->_thick_capacity_gate($storeid, $scfg, $size);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my $objects = $lvs->{$vg} // {};
        if ($is_guest && exists($objects->{$name})) {
            $name = $class->_thick_select_fresh_guest_name(
                $namespace, $vmid, $name, $objects,
            );
        }
        $anchor = anchor_name($namespace, $name);
        $head = generation_name($namespace, $name, $generation);
        $class->_thick_require_fresh_object_names(
            $vg, $objects, $name, $anchor, $head,
        );
        my $before = $class->_vg_state_digest($vg, $device);
        %intent = (
            tx => $tx, state => 'OPEN', op => 'ALLOC', object => $anchor,
            before => $before,
        );
        $class->_set_vg_intent($vg, %intent, _device => $device);
        run_command(
            ['/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
                '--devices', $device, '-L', "${size}K", '-n', $head,
                '--setactivationskip', 'y', $vg],
            errmsg => "creating thick generation '$vg/$head' failed",
        );
        run_command(
            ['/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
                '--devices', $device, '-L', '8M', '-n', $anchor,
                '--setactivationskip', 'y', $vg],
            errmsg => "creating thick generation anchor '$vg/$anchor' failed",
        );
        my $head_tags = PVE::SharedLvmThinThick::generation_tags(
            sid => $storeid, vol => $name, role => 'head', generation => $generation,
        );
        my $anchor_tags = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $name, phase => 'PREPARED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head,
            old => $head, new => $head, head => $head, generation => $generation,
            region => $geometry->{region_sectors},
        );
        $class->_change_exact_tags($vg, $head, [], $head_tags,
            "tagging thick generation '$vg/$head' failed", $device);
        $class->_change_exact_tags($vg, $anchor, [], $anchor_tags,
            "tagging thick generation anchor '$vg/$anchor' failed", $device);
        $class->_disable_and_verify_autoactivation($vg, $head, $device);
        $class->_disable_and_verify_autoactivation($vg, $anchor, $device);
        $class->_thick_verify_allocation_state(
            $storeid, $scfg, $name, $tx, 'PREPARED', $generation, $device,
        );
        return;
    }, $device);

    eval {
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-ay', '-K', "$vg/$head"],
            errmsg => "activating new thick generation '$vg/$head' for zeroing failed",
        );
        my $zero_bytes = int($size) * 1024;
        run_command(
            ['/usr/bin/dd', 'if=/dev/zero', "of=/dev/$vg/$head", 'bs=4M',
                "count=$zero_bytes", 'iflag=count_bytes', 'oflag=direct',
                'conv=fsync,nocreat', 'status=none'],
            errmsg => "zero-initializing new thick generation '$vg/$head' failed",
        );
        run_command(
            ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$head"],
            errmsg => "flushing new thick generation '$vg/$head' failed",
        );
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$head"],
            errmsg => "deactivating zeroed thick generation '$vg/$head' failed",
        );
    };
    if (my $error = $@) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $anchor, $error,
            "PREPARED thick generation '$vg/$head' preserved with OPEN VG intent",
        );
    }

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_exact_vg_intent($vg, %intent, _device => $device);
        $class->_thick_verify_allocation_state(
            $storeid, $scfg, $name, $tx, 'PREPARED', $generation, $device,
        );
        my $old_tags = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $name, phase => 'PREPARED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head,
            old => $head, new => $head, head => $head, generation => $generation,
            region => $geometry->{region_sectors},
        );
        my $new_tags = PVE::SharedLvmThinThick::anchor_tags(
            sid => $storeid, vol => $name, phase => 'MATERIALIZED', tx => $tx,
            op => 'ALLOC', snapshot => 'none', source => $head,
            old => $head, new => $head, head => $head, generation => $generation,
            region => $geometry->{region_sectors},
        );
        $class->_change_exact_tags($vg, $anchor, $old_tags, $new_tags,
            "committing materialized thick generation '$vg/$head' failed", $device);
        $class->_thick_verify_allocation_state(
            $storeid, $scfg, $name, $tx, 'MATERIALIZED', $generation, $device,
        );
        $class->_clear_vg_intent($vg, %intent, _device => $device);
        return;
    }, $device);
    return $name;
}

sub _verify_storage_identity {
    my ($class, $storeid, $scfg, $device) = @_;

    my $expected_vg = $scfg->{'slt-expected-vg-uuid'};
    my $expected_pv = $scfg->{'slt-expected-pv-uuid'};
    my $expected_wwid = $scfg->{'slt-expected-wwid'};

    return 1 if !defined($expected_vg)
        && !defined($expected_pv)
        && !defined($expected_wwid);

    my $vg = $scfg->{'slt-vgname'};
    my @vg_command = ('/sbin/vgs', '--readonly');
    push @vg_command, ('--devices', $device) if defined($device);
    push @vg_command, ('--noheadings', '-o', 'vg_uuid', $vg);
    my $vg_lines = _command_lines(
        \@vg_command,
        "reading identity of VG '$vg' failed",
    );

    die "storage '$storeid' identity is ambiguous: expected exactly one VG '$vg'\n"
        if @$vg_lines != 1;

    die "storage '$storeid' VG UUID mismatch: expected '$expected_vg', found '$vg_lines->[0]'\n"
        if defined($expected_vg) && lc($vg_lines->[0]) ne lc($expected_vg);

    my @pv_command = ('/sbin/pvs', '--readonly');
    push @pv_command, ('--devices', $device) if defined($device);
    push @pv_command, ('--noheadings', '--separator', '|', '-o', 'pv_uuid,pv_name',
        '--select', "vg_name=$vg");
    my $pv_lines = _command_lines(
        \@pv_command,
        "reading backing PV identity of VG '$vg' failed",
    );

    die "storage '$storeid' identity is ambiguous: expected exactly one backing PV for '$vg'\n"
        if @$pv_lines != 1;

    my ($pv_uuid, $pv_name) = split(/\|/, $pv_lines->[0], 2);
    for ($pv_uuid, $pv_name) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }

    die "storage '$storeid' PV UUID mismatch: expected '$expected_pv', found '$pv_uuid'\n"
        if defined($expected_pv) && lc($pv_uuid) ne lc($expected_pv);

    if (defined($expected_wwid)) {
        my ($actual_wwid) = $pv_name =~ m{^/dev/mapper/([0-9A-Fa-f]+)$};
        die "storage '$storeid' cannot verify WWID: PV '$pv_name' is not a stable /dev/mapper/<WWID> path\n"
            if !defined($actual_wwid);
        die "storage '$storeid' WWID mismatch: expected '$expected_wwid', found '$actual_wwid'\n"
            if lc($actual_wwid) ne lc($expected_wwid);
    }

    return 1;
}

sub _verify_owned_volume {
    my ($class, $storeid, $scfg, $volname) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $expected_tag = "pve-slt-sid-$storeid";
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $vg_lvs = $lvs->{$vg} // {};
    my $pool_info = $vg_lvs->{$pool};
    my $volume_info = $vg_lvs->{$volname};

    die "refusing to mutate '$vg/$volname': ownership pool '$pool' is missing\n" if !$pool_info;
    die "refusing to mutate '$vg/$volname': '$pool' is not a thin pool\n"
        if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';
    $class->_verify_pool_health($vg, $pool);
    my $tags = $pool_info->{tags} // '';
    die "refusing to mutate '$vg/$volname': pool ownership is not positively proven for storage '$storeid'\n"
        if $tags !~ /(?:^|,)\Q$expected_tag\E(?:,|$)/;
    die "refusing to mutate '$vg/$volname': volume is missing\n" if !$volume_info;
    die "refusing to mutate '$vg/$volname': volume is not in expected pool '$pool'\n"
        if !defined($volume_info->{pool_lv}) || $volume_info->{pool_lv} ne $pool;
    return 1;
}

sub _verify_pool_health {
    my ($class, $vg, $pool) = @_;
    my $lines = _command_lines(
        [
            '/sbin/lvs', '--noheadings', '--separator', '|',
            '-o', 'lv_attr,lv_health_status,lv_check_needed', "$vg/$pool",
        ],
        "reading thin-pool health of '$vg/$pool' failed",
    );
    die "CRITICAL: thin-pool health of '$vg/$pool' is ambiguous; mutations disabled; manual recovery required\n"
        if @$lines != 1;
    my ($attr, $health, $check_needed) = split(/\|/, $lines->[0], 3);
    for ($attr, $health, $check_needed) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }
    my $decision = PVE::SharedLvmThinSafety::evaluate_pool_health(
        lv_attr => $attr,
        health_status => $health,
        check_needed => $check_needed,
    );
    die "CRITICAL: '$vg/$pool': $decision->{reason}; mutations disabled; data preserved; manual recovery required\n"
        if $decision->{blocks_operation};
    return 1;
}

sub _forced_single_node_quorum_from_evidence {
    my ($configured_nodes, $online_nodes, $expected_votes, $qdevice_configured) = @_;
    return 0 if !defined($configured_nodes) || !defined($online_nodes) || !defined($expected_votes);
    return $configured_nodes > 1
        && $online_nodes == 1
        && $expected_votes == 1
        && !$qdevice_configured;
}

sub _verify_no_forced_single_node_quorum {
    return 1 if !-e '/etc/pve/corosync.conf';

    open(my $fh, '<', '/etc/pve/corosync.conf')
        or return 1; # Native PVE quorum remains authoritative if diagnostics are unavailable.
    local $/;
    my $config = <$fh> // '';
    close($fh);

    my $configured_nodes = () = $config =~ /^\s*node\s*\{/mg;
    return 1 if $configured_nodes < 2;
    my $qdevice_configured = $config =~ /^\s*quorum\s*\{.*?^\s*device\s*\{/ms ? 1 : 0;

    my $lines = _command_lines(
        ['/usr/bin/pvecm', 'status'],
        'reading cluster vote state failed',
    );
    my $status = join("\n", @$lines);
    my ($online_nodes) = $status =~ /^Nodes:\s+(\d+)/m;
    my ($expected_votes) = $status =~ /^Expected votes:\s+(\d+)/m;

    die "CRITICAL: forced single-node quorum (expected_votes=1) detected in a multi-node cluster; SharedLvmThin mutations refused\n"
        if _forced_single_node_quorum_from_evidence(
            $configured_nodes, $online_nodes, $expected_votes, $qdevice_configured,
        );
    return 1;
}

sub _verify_mutation_quorum {
    my ($class, $storeid, $scfg) = @_;
    my ($runtime_api, $runtime_error);
    {
        local $@;
        $runtime_api = eval {
            $class->_runtime_storage_api();
        };
        $runtime_error = $@;
    }
    die "unable to determine running PVE Storage API; mutation refused\n"
        if !defined($runtime_api) || $runtime_error;
    die "running PVE Storage API $runtime_api is outside the tested SharedLvmThin range "
        . MIN_TESTED_PVE_STORAGE_API . '..' . MAX_TESTED_PVE_STORAGE_API
        . "; mutation refused\n"
        if $runtime_api < MIN_TESTED_PVE_STORAGE_API
        || $runtime_api > MAX_TESTED_PVE_STORAGE_API;
    return 1 if !$scfg->{shared};
    PVE::Cluster::check_cfs_quorum();
    _verify_no_forced_single_node_quorum();
    return 1;
}

sub _partial_allocation_error {
    my ($storeid, $vmid, $name, $pool, $reason, $object_state) = @_;
    chomp($reason //= 'unknown allocation failure');
    return join(
        "\n",
        'PARTIAL ALLOCATION',
        "Storage: $storeid",
        "VMID: $vmid",
        "Object: $name",
        "Pool: $pool",
        "Preserved state: $object_state",
        'Automatic cleanup was intentionally NOT performed.',
        "Reason: $reason",
        'Existing objects were preserved to protect data.',
        'Required action: verify VG UUID, PV UUID, WWID, ownership tags, PVE references, quorum, and the storage lock before any cleanup.',
        '',
    );
}

sub _verify_resize_postcondition {
    my ($class, $scfg, $volname, $expected_size) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    die "resize postcondition unavailable: VG '$vg' is not visible\n" if !$lvs->{$vg};
    my $info = $lvs->{$vg}->{$volname};
    die "resize postcondition failed: '$vg/$volname' is missing\n" if !$info;
    die "resize postcondition failed: '$vg/$volname' size is unknown\n"
        if !defined($info->{lv_size});
    die "resize postcondition failed: '$vg/$volname' is smaller than requested\n"
        if $info->{lv_size} < $expected_size;
    return 1;
}

sub _disable_and_verify_autoactivation {
    my ($class, $vg, $lv, $device) = @_;

    my @command = ('/sbin/lvchange');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--setautoactivation', 'n', "$vg/$lv");
    run_command(
        \@command,
        errmsg => "disabling autoactivation for '$vg/$lv' failed",
    );

    return $class->_verify_autoactivation_disabled($vg, $lv, $device);
}

sub _verify_autoactivation_disabled {
    my ($class, $vg, $lv, $device) = @_;
    my @command = ('/sbin/lvs', '--readonly');
    push @command, ('--devices', $device) if defined($device);
    push @command, ('--binary', '--noheadings', '-o', 'lv_autoactivation', "$vg/$lv");
    my $lines = _command_lines(
        \@command,
        "reading autoactivation state of '$vg/$lv' failed",
    );
    die "autoactivation postcondition failed: state of '$vg/$lv' is ambiguous\n"
        if @$lines != 1;
    my $state = lc($lines->[0] // '');
    $state =~ s/^\s+|\s+$//g;
    die "autoactivation postcondition failed: '$vg/$lv' has lv_autoactivation='$state', expected '0'\n"
        if $state ne '0';
    return 1;
}

sub _verify_snapshot_postcondition {
    my ($class, $scfg, $volname, $snapvol, $must_exist) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    die "snapshot postcondition unavailable: VG '$vg' is not visible\n" if !$lvs->{$vg};
    my $info = $lvs->{$vg}->{$snapvol};
    if ($must_exist) {
        die "snapshot postcondition failed: '$vg/$snapvol' is missing\n" if !$info;
        die "snapshot postcondition failed: '$vg/$snapvol' is not in pool '$pool'\n"
            if !defined($info->{pool_lv}) || $info->{pool_lv} ne $pool;
        my $lines = _command_lines(
            ['/sbin/lvs', '--noheadings', '-o', 'lv_attr', "$vg/$snapvol"],
            "reading snapshot flags of '$vg/$snapvol' failed",
        );
        die "snapshot postcondition failed: flags of '$vg/$snapvol' are ambiguous\n"
            if @$lines != 1;
        my $attr = $lines->[0] // '';
        $attr =~ s/^\s+|\s+$//g;
        die "snapshot postcondition failed: '$vg/$snapvol' must be a read-only thin LV with activation-skip (found '$attr')\n"
            if length($attr) < 10
            || substr($attr, 0, 1) ne 'V'
            || substr($attr, 1, 1) ne 'r'
            || substr($attr, 6, 1) ne 't'
            || substr($attr, 9, 1) ne 'k';
    } else {
        die "snapshot delete postcondition failed: '$vg/$snapvol' still exists\n" if $info;
    }
    return 1;
}

sub parse_volname {
    my ($class, $volname) = @_;

    PVE::Storage::Plugin::parse_lvm_name($volname);

    if ($volname =~ m/^((vm|base)-(\d+)-\S+)$/) {
        return ('images', $1, $3, undef, undef, $2 eq 'base', 'raw');
    }

    die "unable to parse shared LVM thin volume name '$volname'\n";
}

sub filesystem_path {
    my ($class, $scfg, $volname, $snapname) = @_;
    return $class->_thick_filesystem_path($scfg, $volname, $snapname)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    my ($vtype, $name, $vmid) = $class->parse_volname($volname);

    my $vg = $scfg->{'slt-vgname'};

    my $lv =
        defined($snapname)
        ? "snap_${name}_${snapname}"
        : $name;

    my $path = "/dev/$vg/$lv";

    return wantarray ? ($path, $vmid, $vtype) : $path;
}

sub volume_size_info {
    my ($class, $scfg, $storeid, $volname, $timeout) = @_;

    # Every guest volume exposed by this plugin is a raw block device.  Do not
    # use the generic file_size_info() implementation here: qemu-img can omit
    # its format result while QEMU holds an active raw device, which makes the
    # PVE content endpoint return "no format" and forces backup products to
    # retry/fall back to a full inventory scan.  blockdev is read-only and
    # reports the kernel-visible size without opening the device for writing.
    my ($format) = ($class->parse_volname($volname))[6];
    die "unsupported volume format '$format' for '$storeid:$volname'\n"
        if $format ne 'raw';

    my $path = $class->filesystem_path($scfg, $volname);
    my $size;
    my %options = (
        errmsg => "can't get size of '$path'",
        outfunc => sub {
            my $line = shift;
            die "ambiguous block-device size for '$path'\n" if defined($size);
            die "invalid block-device size for '$path'\n"
                if !defined($line) || $line !~ /^\s*([0-9]+)\s*$/;
            $size = int($1);
        },
    );
    $options{timeout} = $timeout if defined($timeout);
    run_command(['/usr/sbin/blockdev', '--getsize64', $path], %options);
    die "missing block-device size for '$path'\n" if !defined($size) || $size <= 0;

    return wantarray ? ($size, 'raw', 0, undef) : $size;
}

sub activate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;
    $class->_require_thick_identity_config($storeid, $scfg)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    my $vg = $scfg->{'slt-vgname'};

    my $vgs = PVE::Storage::LVMPlugin::lvm_vgs();

    die "shared LVM VG '$vg' not found\n"
        if !defined($vgs->{$vg});

    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_same_vg_alias_configuration($storeid, $scfg);

    return 1;
}

sub deactivate_storage {
    return 1;
}

sub status {
    my ($class, $storeid, $scfg, $cache) = @_;
    $class->_allocation_mode($scfg);

    my $vg = $scfg->{'slt-vgname'};

    my $vgs = PVE::Storage::LVMPlugin::lvm_vgs();

    return if !defined($vgs->{$vg});

    my $info = $vgs->{$vg};

    #
    # lvm_vgs reports bytes in these fields in current PVE.
    #
    my $total = $info->{size};
    my $free  = $info->{free};

    return if !defined($total) || !defined($free);

    my $used = $total - $free;

    return ($total, $free, $used, 1);
}

sub list_images {
    my ($class, $storeid, $scfg, $vmid, $vollist, $cache) = @_;
    return $class->_thick_list_images($storeid, $scfg, $vmid, $vollist, $cache)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    my $vg = $scfg->{'slt-vgname'};

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $res = [];

    return $res if !$lvs->{$vg};

    foreach my $name (sort keys %{$lvs->{$vg}}) {
        next if $name !~ /^vm-(\d+)-disk-\d+$/;

        my $owner = $1;
        next if defined($vmid) && $owner != $vmid;

        my $info = $lvs->{$vg}->{$name};

        # Only expose thin guest LVs living in our per-VM pool.
        my $expected_pool = "sltp-$owner";
        next if !defined($info->{pool_lv});
        next if $info->{pool_lv} ne $expected_pool;

        my $pool_info = $lvs->{$vg}->{$expected_pool};
        next if !$pool_info;
        next if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        # Tagged pools belonging to another storage must never be exposed.
        # Untagged pools remain visible for RC3 legacy compatibility, but
        # are never silently adopted or automatically removed.
        my $expected_tag = "pve-slt-sid-$storeid";
        my $tags = $pool_info->{tags} // '';
        next if $tags =~ /(?:^|,)pve-slt-sid-[A-Za-z0-9_-]+(?:,|$)/
            && $tags !~ /(?:^|,)\Q$expected_tag\E(?:,|$)/;

        my $volid = "$storeid:$name";

        if ($vollist) {
            my $found = grep { $_ eq $volid } @$vollist;
            next if !$found;
        }

        push @$res, {
            volid  => $volid,
            format => 'raw',
            size   => $info->{lv_size},
            vmid   => $owner,
            ctime  => $info->{ctime},
        };
    }

    return $res;
}

#
# Per-VM thin-pool allocation.
#
sub _allocation_numeric_fields {
    my ($command, $errmsg, $expected, $allow_empty_last) = @_;
    my $lines = _command_lines($command, $errmsg);
    die "$errmsg: expected exactly one result\n" if @$lines != 1;
    my @fields = split(/\|/, $lines->[0], -1);
    die "$errmsg: expected $expected fields\n" if @fields != $expected;
    for my $index (0 .. $#fields) {
        local $_ = $fields[$index];
        s/^\s+|\s+$//g;
        if ($allow_empty_last && $index == $#fields && $_ eq '') {
            $fields[$index] = undef;
            next;
        }
        die "$errmsg: non-numeric result\n" if !/^\d+(?:\.\d+)?$/;
        $fields[$index] = $_;
    }
    return @fields;
}

sub _allocation_metadata_overhead_bytes {
    my ($target_bytes) = @_;
    my $gib = 1024 * 1024 * 1024;
    my $overhead = int(($target_bytes + 99) / 100); # conservative 1%
    $overhead = $gib if $overhead < $gib;
    $overhead = 32 * $gib if $overhead > 32 * $gib;
    return $overhead;
}

sub _allocation_headroom_plan {
    my ($class, $storeid, $scfg, $pool, $size_kib, $pool_exists) = @_;
    my $mode = $scfg->{'slt-initial-pool-mode'} // 'fixed';
    my $fixed = $scfg->{'slt-initial-pool-size'} // 16;
    my $percent = $scfg->{'slt-initial-pool-percent'} // 50;
    my $maximum = $scfg->{'slt-initial-pool-max'};
    my $headroom = $scfg->{'slt-burst-headroom-gib'} // 64;

    # Exact backwards compatibility: fixed mode retains the RC4/early-RC5
    # allocation behavior and does not introduce a new reserve dependency.
    if ($mode eq 'fixed') {
        return PVE::SharedLvmThinSafety::evaluate_allocation_target(
            mode => $mode,
            fixed_gib => $fixed,
            requested_kib => $size_kib,
            used_bytes => 0,
            current_pool_bytes => $pool_exists ? $fixed * 1024 * 1024 * 1024 : 0,
        );
    }

    die "allocation headroom policy requires slt-vg-reserve-percent or slt-vg-reserve-gib\n"
        if !defined($scfg->{'slt-vg-reserve-percent'})
        && !defined($scfg->{'slt-vg-reserve-gib'});

    my $vg = $scfg->{'slt-vgname'};
    my ($vg_size, $vg_free, $extent_size) = _allocation_numeric_fields(
        [
            '/sbin/vgs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
            '--separator', '|', '-o', 'vg_size,vg_free,vg_extent_size', $vg,
        ],
        "reading allocation capacity of VG '$vg' failed", 3,
    );

    my ($current_pool, $used, $usage_known) = (0, 0, 1);
    if ($pool_exists) {
        my ($pool_size, $data_percent) = _allocation_numeric_fields(
            [
                '/sbin/lvs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
                '--separator', '|', '-o', 'lv_size,data_percent', "$vg/$pool",
            ],
            "reading allocation state of '$vg/$pool' failed", 2, 1,
        );
        $current_pool = int($pool_size);
        if (defined($data_percent)) {
            $used = int(($current_pool * $data_percent + 99) / 100);
        } else {
            # Inactive thin pools can legitimately omit Data%.  Never activate
            # shared storage merely to improve an admission estimate.  An
            # unknown value must not be converted into used=current-size:
            # doing so adds headroom again after every cancelled allocation.
            # The policy engine keeps an existing pool unchanged, while full
            # admission fails closed because its guarantee cannot be proven.
            $usage_known = 0;
            warn "SharedLvmThin: Data% unavailable for inactive '$vg/$pool'; "
                . "automatic pre-growth disabled until usage is known\n";
        }
    }

    my %target_args = (
        mode => $mode,
        fixed_gib => $fixed,
        requested_kib => $size_kib,
        used_bytes => $used,
        current_pool_bytes => $current_pool,
        usage_known => $usage_known,
    );
    $target_args{percent} = $percent if $mode eq 'proportional';
    $target_args{headroom_gib} = $headroom if $mode eq 'elastic';
    $target_args{max_gib} = $maximum if defined($maximum);
    my $plan = PVE::SharedLvmThinSafety::evaluate_allocation_target(%target_args);

    my $overhead = $pool_exists ? 0
        : _allocation_metadata_overhead_bytes($plan->{target_bytes});
    my $reserve = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => int($vg_size),
        vg_free => int($vg_free),
        growth_bytes => $plan->{growth_bytes},
        overhead_bytes => $overhead,
        extent_bytes => int($extent_size),
        reserve_percent => $scfg->{'slt-vg-reserve-percent'} // 0,
        reserve_gib => $scfg->{'slt-vg-reserve-gib'} // 0,
    );
    die "allocation headroom rejected for '$vg/$pool': requires "
        . "$reserve->{required_physical_bytes} physical bytes including extent/metadata allowance; "
        . "projected VG free $reserve->{free_after_bytes} bytes would cross protected reserve "
        . "$reserve->{reserve_bytes} bytes; no lvcreate/lvextend was run\n"
        if !$reserve->{allowed};

    $plan->{reserve} = $reserve;
    return $plan;
}

sub _grow_pool_for_allocation {
    my ($class, $vg, $pool, $plan) = @_;
    return if !$plan->{growth_bytes};

    my $target_kib = int(($plan->{target_bytes} + 1023) / 1024);
    my $grow_error;
    eval {
        run_command(
            ['/sbin/lvextend', '-L', "${target_kib}K", "$vg/$pool"],
            errmsg => "allocation pre-grow of '$vg/$pool' failed",
        );
    };
    $grow_error = $@;

    my $actual;
    my $post_error;
    eval {
        ($actual) = _allocation_numeric_fields(
            [
                '/sbin/lvs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
                '-o', 'lv_size', "$vg/$pool",
            ],
            "reading allocation pre-grow postcondition of '$vg/$pool' failed", 1,
        );
    };
    $post_error = $@;

    die "$grow_error"
        . "UNKNOWN: allocation pre-grow outcome cannot be verified; no retry or shrink will be attempted\n"
        if $post_error || !defined($actual);
    die "$grow_error"
        . "PARTIAL: pool remains at $actual bytes below required target $plan->{target_bytes}; "
        . "no retry or shrink will be attempted\n"
        if $actual < $plan->{target_bytes};
    warn "SharedLvmThin PARTIAL/COMPLETED: lvextend reported an error but allocation target was reached; no retry\n"
        if $grow_error;
    return;
}

sub _verify_allocation_reserve_postcondition {
    my ($class, $storeid, $scfg, $pool) = @_;
    return if ($scfg->{'slt-initial-pool-mode'} // 'fixed') eq 'fixed';

    my $vg = $scfg->{'slt-vgname'};
    my ($vg_size, $vg_free) = _allocation_numeric_fields(
        [
            '/sbin/vgs', '--readonly', '--noheadings', '--units', 'b', '--nosuffix',
            '--separator', '|', '-o', 'vg_size,vg_free', $vg,
        ],
        "reading allocation reserve postcondition of VG '$vg' failed", 2,
    );
    my $decision = PVE::SharedLvmThinSafety::evaluate_allocation_reserve(
        vg_size => int($vg_size),
        vg_free => int($vg_free),
        growth_bytes => 0,
        overhead_bytes => 0,
        extent_bytes => 1,
        reserve_percent => $scfg->{'slt-vg-reserve-percent'} // 0,
        reserve_gib => $scfg->{'slt-vg-reserve-gib'} // 0,
    );
    die "PARTIAL ALLOCATION: '$vg/$pool' reached its allocation target, but actual VG free "
        . "$vg_free bytes is below protected reserve $decision->{reserve_bytes} bytes; "
        . "pool preserved; guest LV creation refused; no shrink, cleanup, or retry attempted\n"
        if !$decision->{allowed};
    return 1;
}

sub alloc_image {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;
    return $class->_thick_alloc_image($storeid, $scfg, $vmid, $fmt, $name, $size)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_alloc_image_locked(
            $storeid, $scfg, $vmid, $fmt, $name, $size,
        );
    });
}

sub _alloc_image_locked {
    my ($class, $storeid, $scfg, $vmid, $fmt, $name, $size) = @_;

    die "unsupported format '$fmt'\n"
        if defined($fmt) && $fmt ne 'raw';

    $class->_verify_mutation_quorum($storeid, $scfg);
    $class->_verify_storage_identity($storeid, $scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $pool = "sltp-$vmid";

    $name = $class->find_free_diskname($storeid, $scfg, $vmid)
        if !$name;

    my $is_guest_disk = $name =~ /^vm-\Q$vmid\E-disk-\d+$/;
    my $is_vmstate = $name =~ /^vm-\Q$vmid\E-state-[A-Za-z0-9][A-Za-z0-9_.-]*$/;
    my $is_fleecing = $name =~ /^vm-\Q$vmid\E-fleece-\d+$/;
    my $is_cloudinit = $name eq "vm-$vmid-cloudinit";
    my $is_auxiliary = $is_vmstate || $is_fleecing || $is_cloudinit;

    die "illegal volume name '$name'\n"
        if !$is_guest_disk && !$is_auxiliary;

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    my $pool_exists =
        $lvs->{$vg}
        && $lvs->{$vg}->{$pool};

    die "refusing VMID reuse for $vmid: object '$vg/$name' already exists; naming is not ownership proof\n"
        if $lvs->{$vg} && $lvs->{$vg}->{$name};

    my $created_pool = 0;
    my $sid_tag = "pve-slt-sid-$storeid";

    die "refusing auxiliary allocation '$vg/$name': owned VM pool '$vg/$pool' does not exist\n"
        if !$pool_exists && $is_auxiliary;

    my $headroom_plan = $class->_allocation_headroom_plan(
        $storeid, $scfg, $pool, $size, $pool_exists,
    );

    if (!$pool_exists) {
        my $pool_size_k = int(($headroom_plan->{target_bytes} + 1023) / 1024);
        my $pool_create_error;

        eval {
            run_command(
                [
                    '/sbin/lvcreate',
                    '--yes', '--wipesignatures', 'y',
                    '-L', "${pool_size_k}K",
                    '-n', $pool,
                    $vg,
                ],
                errmsg => "creating backing LV '$vg/$pool' failed",
            );

            #
            # From this point on this invocation owns the newly-created
            # backing LV and must clean it up if a later step fails.
            #
            $created_pool = 1;

            run_command(
                [
                    '/sbin/lvconvert',
                    '-y',
                    '--type', 'thin-pool',
                    "$vg/$pool",
                ],
                errmsg => "converting '$vg/$pool' to thin pool failed",
            );

            run_command(
                ['/sbin/lvchange', '--addtag', $sid_tag, "$vg/$pool"],
                errmsg => "tagging thin pool '$vg/$pool' failed",
            );

            $class->_disable_and_verify_autoactivation($vg, $pool);
            $class->_verify_allocation_reserve_postcondition(
                $storeid, $scfg, $pool,
            );
        };

        $pool_create_error = $@;

        if ($pool_create_error) {
            die _partial_allocation_error(
                $storeid, $vmid, $name, $pool, $pool_create_error,
                $created_pool ? "backing object '$vg/$pool' may exist" : 'no created object was confirmed',
            );
        }
    } else {
        #
        # Existing pools must already belong to this storage.
        # Never silently adopt a foreign per-VM pool.
        #
        my $pool_info = $lvs->{$vg}->{$pool};

        die "existing pool '$vg/$pool' is not a thin pool\n"
            if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        die "existing pool '$vg/$pool' does not belong to storage '$storeid'\n"
            if !defined($pool_info->{tags})
            || $pool_info->{tags} !~ /(?:^|,)\Q$sid_tag\E(?:,|$)/;

        my @owned_disks = grep {
            /^vm-\Q$vmid\E-disk-\d+$/
            && defined($lvs->{$vg}->{$_}->{pool_lv})
            && $lvs->{$vg}->{$_}->{pool_lv} eq $pool
        } keys %{$lvs->{$vg}};
        my @stale_snapshots = grep {
            /^snap_vm-\Q$vmid\E-disk-\d+_/
        } keys %{$lvs->{$vg}};

        die "VMID reuse requires recovery review: owned pool '$vg/$pool' exists without an active owned disk"
            . (@stale_snapshots ? " and contains stale snapshots" : '')
            . "; automatic adoption is disabled\n"
            if !@owned_disks;
    }

    $class->_verify_pool_health($vg, $pool);

    if ($pool_exists && ($scfg->{'slt-initial-pool-mode'} // 'fixed') ne 'fixed') {
        $class->_grow_pool_for_allocation($vg, $pool, $headroom_plan);
        $class->_verify_pool_health($vg, $pool);
        $class->_verify_allocation_reserve_postcondition(
            $storeid, $scfg, $pool,
        );
    }

    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    die "volume '$vg/$name' appeared during allocation; preserving all objects\n"
        if $lvs->{$vg} && $lvs->{$vg}->{$name};

    my $alloc_error;

    eval {
        run_command(
            [
                '/sbin/lvcreate',
                '--yes', '--wipesignatures', 'y',
                '-V', "${size}K",
                '-n', $name,
                '--thinpool', "$vg/$pool",
            ],
            errmsg => "creating thin LV '$vg/$name' failed",
        );
    };

    $alloc_error = $@;

    if ($alloc_error) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $pool, $alloc_error,
            $created_pool ? "owned pool '$vg/$pool' exists; guest LV completion is unknown" : "pre-existing owned pool '$vg/$pool' preserved",
        );
    }

    my $autoactivation_error;
    eval { $class->_disable_and_verify_autoactivation($vg, $name); };
    $autoactivation_error = $@;
    if ($autoactivation_error) {
        die _partial_allocation_error(
            $storeid, $vmid, $name, $pool, $autoactivation_error,
            "guest LV '$vg/$name' exists but safe shared-storage autoactivation state is unconfirmed",
        );
    }

    return $name;
}

sub activate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    return $class->_thick_activate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    my $vg = $scfg->{'slt-vgname'};
    my $lv = $snapname ? "snap_${volname}_${snapname}" : $volname;

    run_command(
        ['/sbin/lvchange', '-ay', '-K', "$vg/$lv"],
        errmsg => "activating shared thin LV '$vg/$lv' failed",
    );

    return 1;
}

sub deactivate_volume {
    my ($class, $storeid, $scfg, $volname, $snapname, $cache) = @_;
    return $class->_thick_deactivate_volume($storeid, $scfg, $volname, $snapname, $cache)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    my $vg = $scfg->{'slt-vgname'};
    my $lv = $snapname ? "snap_${volname}_${snapname}" : $volname;

    run_command(
        ['/sbin/lvchange', '-an', "$vg/$lv"],
        errmsg => "deactivating shared thin LV '$vg/$lv' failed",
    );

    return 1;
}

sub _thick_resume_transition {
    my ($class, $scfg, $storeid, $volname, $snap, $operation, $intent, $lvs) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    $lvs //= $class->_thick_list_volumes_scoped($vg, $device);
    my ($state, $head_info, $anchor) =
        $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
    my $expected_intent_op = $operation eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER';

    die "existing VG intent is not the exact resumable transition\n"
        if !defined($intent) || $intent->{state} ne 'OPEN'
        || $intent->{op} ne $expected_intent_op || $intent->{object} ne $anchor
        || $intent->{tx} ne $state->{tx};
    die "recoverable transition request identity mismatch\n"
        if $state->{phase} !~ /^(?:PREPARED|SOURCE_READY|COMMITTED|HYDRATING|HYDRATION_COMPLETE)$/
        || $state->{op} ne $operation || $state->{snapshot} ne $snap
        || ($state->{phase} =~ /^(?:PREPARED|SOURCE_READY)$/ && $state->{head} ne $state->{old})
        || ($state->{phase} =~ /^(?:COMMITTED|HYDRATING|HYDRATION_COMPLETE)$/
            && $state->{head} ne $state->{new});

    my ($old, $new, $source) = @{$state}{qw(old new source)};
    my ($old_gen) = $old =~ /-(\d{8})$/;
    my ($new_gen) = $new =~ /-(\d{8})$/;
    die "recoverable transition generation names are malformed\n"
        if !defined($old_gen) || !defined($new_gen);
    ($old_gen, $new_gen) = (int($old_gen), int($new_gen));
    die "recoverable transition generations are not consecutive\n"
        if $new_gen != $old_gen + 1;
    my $expected_anchor_gen = $state->{phase} =~ /^(?:PREPARED|SOURCE_READY)$/
        ? $old_gen : $new_gen;
    die "recoverable transition anchor generation mismatch\n"
        if int($state->{generation}) != $expected_anchor_gen;
    my $key = object_key($namespace, $volname);
    my $expected_new = generation_name($namespace, $volname, $new_gen);
    my $meta = sprintf('sltg-m-%s-%08d', $key, $new_gen);
    die "prepared transition destination name mismatch\n" if $new ne $expected_new;
    for my $name ($old, $source, $new, $meta) {
        die "prepared transition object '$vg/$name' is missing\n"
            if !$lvs->{$vg} || !$lvs->{$vg}->{$name};
    }

    my ($source_gen, $source_info);
    if ($operation eq 'ROLLBACK') {
        my $found;
        ($found, $source_gen, $source_info) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snap, $lvs,
        );
        die "prepared rollback source identity mismatch\n" if $found ne $source;
    } else {
        $source_gen = $old_gen;
        $source_info = $lvs->{$vg}->{$source};
        die "prepared snapshot source identity mismatch\n" if $source ne $old;
    }
    my $size = $source_info->{lv_size};
    my $old_size = $lvs->{$vg}->{$old}->{lv_size};
    die "prepared transition size is invalid\n"
        if !defined($size) || $size !~ /^\d+$/ || !$size || $size % 512
        || !defined($old_size) || $old_size !~ /^\d+$/ || !$old_size || $old_size % 512;
    my $geometry = clone_geometry(int($size));
    die "prepared transition geometry mismatch\n"
        if int($state->{region}) != $geometry->{region_sectors};
    validate_generation_tags(
        $lvs->{$vg}->{$new}->{tags} // '', sid => $storeid,
        vol => $volname, role => 'head', generation => $new_gen,
    );
    $class->_thick_verify_transition_metadata(
        $storeid, $scfg, $volname, $lvs->{$vg}->{$meta},
        tx => $intent->{tx}, generation => $new_gen,
        region => $geometry->{region_sectors},
        metadata_bytes => $geometry->{metadata_bytes}, name => $meta, device => $device,
    );
    $class->_verify_autoactivation_disabled($vg, $new, $device);
    $class->_verify_autoactivation_disabled($vg, $meta, $device);
    my $source_map = $class->_thick_source_mapper_name($scfg, $volname, $source_gen);
    if (_block_device_exists("/dev/mapper/$source_map")) {
        $class->_thick_verify_source_mapper(
            $scfg, $source_map, $source, int($size / 512), $intent->{tx},
        );
        if ($state->{phase} eq 'PREPARED') {
            $state = $class->_thick_transition_anchor(
                $vg, $anchor, $state, phase => 'SOURCE_READY', _device => $device,
            );
        }
    } elsif ($state->{phase} ne 'PREPARED') {
        my $front = mapper_name($namespace, $volname);
        die "$state->{phase} transition runtime is partial; source mapper '$source_map' is missing\n"
            if _block_device_exists("/dev/mapper/$front");
    }
    $class->_thick_verify_frontend($scfg, $volname, $old, int($old_size / 512))
        if $state->{phase} =~ /^(?:PREPARED|SOURCE_READY)$/;

    return {
        state => $state, anchor => $anchor, old => $old, new => $new,
        source => $source, source_gen => $source_gen,
        old_gen => $old_gen, new_gen => $new_gen, meta => $meta,
        source_map => $source_map, size => int($size), old_size => int($old_size),
        geometry => $geometry, operation => $operation, snapshot => $snap,
    };
}

sub _thick_reconstruct_missing_transition_runtime {
    my ($class, $scfg, $volname, $tr, $intent) = @_;
    my $phase = $tr->{state}->{phase} // '';
    return 1 if $phase eq 'PREPARED';

    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $front = mapper_name($namespace, $volname);
    my $source_map = $tr->{source_map};
    my $front_exists = _block_device_exists("/dev/mapper/$front");
    my $source_exists = _block_device_exists("/dev/mapper/$source_map");
    return 1 if $front_exists && $source_exists;
    die "$phase transition runtime is partial; refusing reconstruction\n"
        if $front_exists || $source_exists;

    # A host reboot removes all transient device-mapper tables while the LVM
    # anchor, immutable generations, clone metadata, and OPEN VG intent remain
    # persistent. Reconstruct only the exact dependency graph described by
    # those already-verified objects. No global scan or cleanup is performed.
    run_command(
        ['/sbin/lvchange', '--devices', $device, '-ay', '-K',
            "$vg/$tr->{source}", "$vg/$tr->{new}", "$vg/$tr->{meta}"],
        errmsg => "activating exact persisted transition objects failed",
    );
    run_command(
        ['/sbin/dmsetup', '--verifyudev', 'create', $source_map,
            '--readonly', '--uuid', "SLT-TG3-SOURCE-$intent->{tx}", '--table',
            "0 " . int($tr->{size} / 512) . " linear /dev/$vg/$tr->{source} 0"],
        errmsg => "reconstructing immutable transition source failed",
    );
    $class->_thick_verify_source_mapper(
        $scfg, $source_map, $tr->{source}, int($tr->{size} / 512), $intent->{tx},
    );

    my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
    if ($phase eq 'SOURCE_READY') {
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-ay', '-K',
                "$vg/$tr->{old}", "$vg/$tr->{anchor}"],
            errmsg => "activating exact pre-cutover state failed",
        );
        run_command(
            ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
                '--table', "0 " . int($tr->{old_size} / 512)
                    . " linear /dev/$vg/$tr->{old} 0"],
            errmsg => "reconstructing pre-cutover frontend failed",
        );
        $class->_thick_verify_frontend(
            $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512),
        );
        return 1;
    }

    die "$phase transition is not safe for runtime reconstruction\n"
        if $phase ne 'COMMITTED'
        && $phase ne 'HYDRATING'
        && $phase ne 'HYDRATION_COMPLETE';
    my ($threshold, $batch) = $class->_thick_hydration_tuning($scfg);
    my $sectors = int($tr->{size} / 512);
    run_command(
        ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
            '--table', "0 $sectors clone /dev/$vg/$tr->{meta} /dev/$vg/$tr->{new} "
                . "/dev/mapper/$source_map " . $tr->{geometry}->{region_sectors}
                . " 2 no_hydration no_discard_passdown "
                . "4 hydration_threshold $threshold hydration_batch_size $batch"],
        errmsg => "reconstructing persisted dm-clone frontend failed",
    );
    $class->_thick_verify_clone_frontend(
        $scfg, $volname, sectors => $sectors,
        region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
        new => $tr->{new}, source_map => $source_map,
    );
    $class->_thick_verify_clone_status(
        $front, $phase eq 'HYDRATION_COMPLETE' ? 1 : 0,
    );
    return 1;
}

sub _thick_verify_published_transition_frontend {
    my ($class, $storeid, $scfg, $volname, $state) = @_;
    my $phase = $state->{phase} // '';
    if ($phase eq 'LINEAR_PIVOTED') {
        return $class->_thick_verify_frontend($scfg, $volname, $state->{head});
    }
    die "thick-generations frontend is not in a published transition state; recovery required\n"
        if $phase ne 'HYDRATING' && $phase ne 'HYDRATION_COMPLETE';

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $intent = $class->_read_vg_intent($vg, $device);
    die "published thick-generations transition has no exact VG intent\n"
        if !defined($intent);
    my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
    my $tr = $class->_thick_resume_transition(
        $scfg, $storeid, $volname, $state->{snapshot}, $state->{op}, $intent, $lvs,
    );
    $class->_thick_verify_clone_frontend(
        $scfg, $volname, sectors => int($tr->{size} / 512),
        region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
        new => $tr->{new}, source_map => $tr->{source_map},
    );
    $class->_thick_verify_clone_status(
        mapper_name($class->_thick_namespace($scfg), $volname),
        $phase eq 'HYDRATION_COMPLETE' ? 1 : 0,
    );
    return 1;
}

sub _thick_volume_snapshot {
    my ($class, $scfg, $storeid, $volname, $snap, $operation, $materialize_now) = @_;
    $operation //= 'SNAPSHOT';
    die "invalid thick-generations materialization operation\n"
        if $operation ne 'SNAPSHOT' && $operation ne 'ROLLBACK';
    my $rollback = $operation eq 'ROLLBACK';
    $snap = _thick_snapshot_name($snap);
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $namespace = $class->_thick_namespace($scfg);
    my $front = mapper_name($namespace, $volname);
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my ($tr, %intent);
    my $async_snapshot = 0;

    if (!$rollback && !$materialize_now
        && $class->_thick_online_materialization_mode($scfg) eq 'asynchronous'
        && _block_device_exists("/dev/mapper/$front")) {
        # PVE opens and pauses a running QEMU disk before invoking a storage
        # snapshot callback.  Waiting for full hydration in that callback
        # would therefore turn background materialization into VM downtime.
        # A zero-open frontend denotes an offline snapshot and remains
        # synchronous so no idle transition is left behind unnecessarily.
        $async_snapshot = $class->_thick_frontend_open_count($front) > 0 ? 1 : 0;
    }

    if ($rollback && _block_device_exists("/dev/mapper/$front")) {
        my $open = $class->_thick_frontend_open_count($front);
        die "refusing thick-generations rollback while the frontend is open\n"
            if $open != 0;
    }

    $class->_with_vg_lock($storeid, $scfg, sub {
        my $existing_intent = $class->_read_vg_intent($vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my $this_anchor = anchor_name($namespace, $volname);
        if (defined($existing_intent)
            && (!$materialize_now || ($existing_intent->{object} // '') eq $this_anchor)) {
            $tr = $class->_thick_resume_transition(
                $scfg, $storeid, $volname, $snap, $operation, $existing_intent, $lvs,
            );
            %intent = %$existing_intent;
            return;
        }
        if ($materialize_now) {
            my ($persisted, undef, $persisted_anchor) =
                $class->_thick_read_anchor($storeid, $scfg, $volname, $lvs);
            if (($persisted->{phase} // '') =~ /^(?:HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/) {
                %intent = (
                    tx => $persisted->{tx}, state => 'OPEN', op => 'DM_CUTOVER',
                    object => $persisted_anchor, before => ('0' x 32),
                    _anchor_scoped => 1,
                );
                $tr = $class->_thick_resume_transition(
                    $scfg, $storeid, $volname, $snap, $operation, \%intent, $lvs,
                );
                return;
            }
            die "materialization worker found no exact resumable transition\n";
        }
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my ($old, $old_gen) = ($state->{head}, int($state->{generation}));
        my ($source, $source_gen, $source_info) = ($old, $old_gen, $head_info);
        if ($rollback) {
            ($source, $source_gen, $source_info) = $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $snap, $lvs,
            );
            $class->_thick_verify_snapshot_readonly($vg, $source);
            $class->_verify_autoactivation_disabled($vg, $source, $device);
        }
        my $new_gen = $old_gen + 1;
        die "thick-generations generation limit reached\n" if $new_gen > 99_999_999;
        my $key = object_key($namespace, $volname);
        my $new = generation_name($namespace, $volname, $new_gen);
        my $meta = sprintf('sltg-m-%s-%08d', $key, $new_gen);
        my $source_map = $class->_thick_source_mapper_name($scfg, $volname, $source_gen);
        my $size = $source_info->{lv_size};
        my $old_size = $head_info->{lv_size};
        die "thick-generations source size is unknown or not sector aligned\n"
            if !defined($size) || $size !~ /^\d+$/ || !$size || $size % 512;
        die "thick-generations previous HEAD size is unknown or not sector aligned\n"
            if !defined($old_size) || $old_size !~ /^\d+$/ || !$old_size || $old_size % 512;
        my $geometry = clone_geometry(int($size));

        for my $name (grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg}}) {
            next if $rollback;
            my ($generation) = $name =~ /-(\d{8})$/;
            my $exists = eval {
                validate_generation_tags(
                    $lvs->{$vg}->{$name}->{tags} // '', sid => $storeid,
                    vol => $volname, role => 'snapshot',
                    generation => int($generation), snapshot => $snap,
                );
                1;
            };
            die "snapshot '$snap' already exists for '$volname'\n" if $exists;
        }
        die "thick-generations transition object already exists\n"
            if $lvs->{$vg}->{$new} || $lvs->{$vg}->{$meta}
            || _block_device_exists("/dev/mapper/$source_map");
        $class->_thick_capacity_gate(
            $storeid, $scfg, int(($size + 1023) / 1024),
            $geometry->{metadata_bytes},
        );
        $class->_thick_fault_point('C0', $operation, $storeid, $volname);
        %intent = (
            tx => $class->_new_transaction_id(), state => 'OPEN',
            op => ($rollback ? 'DM_PIVOT' : 'DM_CUTOVER'), object => $anchor,
            before => $class->_vg_state_digest($vg, $device),
        );
        $class->_set_vg_intent($vg, %intent, _device => $device);
        $class->_thick_fault_point('C1', $operation, $storeid, $volname);
        run_command(
            ['/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
                '--devices', $device, '-L', "${size}B", '-n', $new,
                '--setactivationskip', 'y', $vg],
            errmsg => "creating thick snapshot destination '$vg/$new' failed",
        );
        run_command(
            ['/sbin/lvcreate', '--yes', '--wipesignatures', 'y', '--ignoreactivationskip',
                '--devices', $device,
                '-L', $geometry->{metadata_bytes} . 'B', '-n', $meta,
                '--setactivationskip', 'y', $vg],
            errmsg => "creating dm-clone metadata '$vg/$meta' failed",
        );
        $class->_change_exact_tags(
            $vg, $new, [], PVE::SharedLvmThinThick::generation_tags(
                sid => $storeid, vol => $volname, role => 'head',
                generation => $new_gen,
            ), "tagging thick snapshot destination '$vg/$new' failed", $device,
        );
        $class->_change_exact_tags(
            $vg, $meta, [], transition_tags(
                sid => $storeid, vol => $volname, tx => $intent{tx},
                kind => 'metadata', generation => $new_gen,
                region => $geometry->{region_sectors},
            ), "tagging dm-clone metadata '$vg/$meta' failed", $device,
        );
        $class->_disable_and_verify_autoactivation($vg, $new, $device);
        $class->_disable_and_verify_autoactivation($vg, $meta, $device);
        $class->_thick_fault_point('C2', $operation, $storeid, $volname);
        my $prepared = $class->_thick_transition_anchor(
            $vg, $anchor, $state,
            phase => 'PREPARED', tx => $intent{tx}, old => $old, new => $new,
            op => $operation, snapshot => $snap, source => $source,
            head => $old, generation => $old_gen,
            region => $geometry->{region_sectors},
            _device => $device,
        );
        $tr = {
            state => $prepared, anchor => $anchor, old => $old, new => $new,
            source => $source, source_gen => $source_gen,
            old_gen => $old_gen, new_gen => $new_gen, meta => $meta,
            source_map => $source_map, size => int($size), old_size => int($old_size),
            geometry => $geometry, operation => $operation, snapshot => $snap,
        };
        $class->_thick_fault_point('C3', $operation, $storeid, $volname);
        return;
    }, $device);

    $class->_thick_reconstruct_missing_transition_runtime(
        $scfg, $volname, $tr, \%intent,
    );

    if ($tr->{state}->{phase} eq 'PREPARED') {
        eval {
            run_command(
                ['/sbin/lvchange', '--devices', $device, '-ay', '-K', "$vg/$tr->{meta}"],
                errmsg => "activating dm-clone metadata failed",
            );
            run_command(
                ['/usr/bin/dd', 'if=/dev/zero', "of=/dev/$vg/$tr->{meta}",
                    'bs=4096', 'count=1', 'conv=fsync,nocreat', 'status=none'],
                errmsg => "initialising dm-clone metadata failed",
            );
            run_command(
                ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$tr->{meta}"],
                errmsg => "flushing dm-clone metadata failed",
            );
        };
        die "PARTIAL SNAPSHOT for '$storeid:$volname': metadata initialisation is uncertain; "
            . "OPEN intent and all objects preserved; no retry or cleanup: $@" if $@;

        $class->_with_vg_lock($storeid, $scfg, sub {
            $class->_thick_require_transition_intent(
                $storeid, $scfg, $volname, \%intent,
            );
            my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
            my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
            die "snapshot transition anchor disappeared\n" if !$info;
            my $state = decode_anchor_tags($info->{tags} // '');
            die "snapshot transition PREPARED state mismatch\n"
                if $state->{phase} ne 'PREPARED' || $state->{tx} ne $intent{tx}
                || $state->{head} ne $tr->{old} || $state->{old} ne $tr->{old}
                || $state->{new} ne $tr->{new}
                || $state->{region} != $tr->{geometry}->{region_sectors};
            for my $lv ($tr->{old}, $tr->{source}, $tr->{new}, $tr->{meta}) {
                die "snapshot transition object '$vg/$lv' is missing\n"
                    if !$lvs->{$vg}->{$lv};
            }
            validate_generation_tags(
                $lvs->{$vg}->{$tr->{new}}->{tags} // '', sid => $storeid,
                vol => $volname, role => 'head', generation => $tr->{new_gen},
            );
            $class->_thick_verify_transition_metadata(
                $storeid, $scfg, $volname, $lvs->{$vg}->{$tr->{meta}},
                tx => $intent{tx}, generation => $tr->{new_gen},
                region => $tr->{geometry}->{region_sectors},
                metadata_bytes => $tr->{geometry}->{metadata_bytes}, name => $tr->{meta},
                device => $device,
            );
            if (!_block_device_exists("/dev/mapper/$front")) {
                run_command(
                    ['/sbin/lvchange', '--devices', $device, '-ay', '-K',
                        "$vg/$tr->{old}", "$vg/$tr->{anchor}"],
                    errmsg => "activating prepared thick-generations source failed",
                );
                my $uuid = 'SLT-TG2-' . object_key($namespace, $volname);
                my $sectors = int($tr->{old_size} / 512);
                run_command(
                    ['/sbin/dmsetup', '--verifyudev', 'create', $front, '--uuid', $uuid,
                        '--table', "0 $sectors linear /dev/$vg/$tr->{old} 0"],
                    errmsg => "creating prepared thick-generations frontend failed",
                );
            }
            $class->_thick_verify_frontend(
                $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512),
            );
            run_command(
                ['/sbin/lvchange', '--devices', $device, '-ay', '-K',
                    "$vg/$tr->{source}", "$vg/$tr->{new}", "$vg/$tr->{meta}"],
                errmsg => "activating snapshot transition LVs failed",
            );
        # Construct the read-only source view while the old linear frontend is
        # still active.  No LVM command may run while that frontend is
        # suspended: udev may inspect the dependency chain and deadlock behind
        # the suspended device.  The source mapper is not published to the
        # frontend until the atomic cutover below.
            if (!_block_device_exists("/dev/mapper/$tr->{source_map}")) {
                run_command(
                    ['/sbin/dmsetup', '--verifyudev', 'create', $tr->{source_map},
                        '--readonly', '--uuid', "SLT-TG3-SOURCE-$intent{tx}", '--table',
                        "0 " . int($tr->{size} / 512) . " linear /dev/$vg/$tr->{source} 0"],
                    errmsg => "creating immutable snapshot source mapper failed",
                );
            }
            $class->_thick_verify_source_mapper(
                $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
                $intent{tx},
            );
            $state = $class->_thick_transition_anchor(
                $vg, $tr->{anchor}, $state, phase => 'SOURCE_READY', _device => $device,
            );
            $tr->{state} = $state;
            $class->_thick_fault_point('C4', $operation, $storeid, $volname);
            return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'SOURCE_READY') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
        die "snapshot transition anchor disappeared before cutover\n" if !$info;
        my $state = decode_anchor_tags($info->{tags} // '');
        die "snapshot transition SOURCE_READY state mismatch\n"
            if $state->{phase} ne 'SOURCE_READY' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{old} || $state->{old} ne $tr->{old}
            || $state->{new} ne $tr->{new};
        $class->_thick_verify_source_mapper(
            $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
            $intent{tx},
        );
        if (!$class->_thick_mapper_is_suspended($front)) {
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'suspend', '--noflush', $front],
                errmsg => "suspending thick-generations frontend for snapshot failed",
            );
        }
        die "thick-generations frontend did not enter suspended state\n"
            if !$class->_thick_mapper_is_suspended($front);
        $class->_thick_fault_point('C5', $operation, $storeid, $volname);
        $state = $class->_thick_transition_anchor(
            $vg, $tr->{anchor}, $state, phase => 'COMMITTED',
            head => $tr->{new}, generation => $tr->{new_gen}, _device => $device,
        );
        if (!$rollback) {
            $class->_change_exact_tags(
                $vg, $tr->{old},
                PVE::SharedLvmThinThick::generation_tags(
                    sid => $storeid, vol => $volname, role => 'head',
                    generation => $tr->{old_gen},
                ),
                PVE::SharedLvmThinThick::generation_tags(
                    sid => $storeid, vol => $volname, role => 'snapshot',
                    generation => $tr->{old_gen}, snapshot => $snap,
                ),
                "committing immutable snapshot generation failed",
                $device,
            );
        }
        $class->_thick_fault_point('C6', $operation, $storeid, $volname);
        $tr->{state} = $state;
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'COMMITTED') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my $info = $lvs->{$vg} && $lvs->{$vg}->{$tr->{anchor}};
        die "snapshot transition anchor disappeared before clone publication\n" if !$info;
        my $state = decode_anchor_tags($info->{tags} // '');
        die "snapshot transition COMMITTED state mismatch\n"
            if $state->{phase} ne 'COMMITTED' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new} || $state->{old} ne $tr->{old}
            || $state->{new} ne $tr->{new};
        $class->_thick_verify_source_mapper(
            $scfg, $tr->{source_map}, $tr->{source}, int($tr->{size} / 512),
            $intent{tx},
        );
        my $sectors = int($tr->{size} / 512);
        if ($class->_thick_mapper_is_suspended($front)) {
            my ($threshold, $batch) = $class->_thick_hydration_tuning($scfg);
            $class->_thick_verify_frontend(
                $scfg, $volname, $tr->{old}, int($tr->{old_size} / 512),
            );
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'load', $front, '--table',
                    "0 $sectors clone /dev/$vg/$tr->{meta} /dev/$vg/$tr->{new} "
                        . "/dev/mapper/$tr->{source_map} "
                        . $tr->{geometry}->{region_sectors} . " "
                        . "2 no_hydration no_discard_passdown "
                        . "4 hydration_threshold $threshold hydration_batch_size $batch"],
                errmsg => "loading dm-clone snapshot transition failed",
            );
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'resume', $front],
                errmsg => "publishing dm-clone snapshot transition failed",
            );
        }
        $class->_thick_verify_clone_frontend(
            $scfg, $volname, sectors => $sectors,
            region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
            new => $tr->{new}, source_map => $tr->{source_map},
        );
        $class->_thick_verify_clone_status($front, 0);
        if (!$rollback) {
            $class->_thick_ensure_snapshot_readonly(
                $vg, $tr->{old}, $lvs->{$vg}->{$tr->{old}}, $device,
            );
        } else {
            $class->_thick_verify_snapshot_readonly($vg, $tr->{source}, $device);
        }
        $class->_thick_fault_point('C7', $operation, $storeid, $volname);
        $state = $class->_thick_transition_anchor(
            $vg, $tr->{anchor}, $state, phase => 'HYDRATING', _device => $device,
        );
        $tr->{state} = $state;
        $class->_thick_fault_point('C8', $operation, $storeid, $volname);
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'HYDRATING') {
        run_command(
            ['/sbin/dmsetup', 'message', $front, '0', 'enable_hydration'],
            errmsg => "enabling dm-clone hydration failed",
        );
        if ($async_snapshot) {
            my $timeout = $scfg->{'slt-tg-hydration-timeout'} // 3600;
            $class->_with_vg_lock($storeid, $scfg, sub {
                $class->_thick_scope_transition_intent_to_anchor(
                    $storeid, $scfg, $volname, \%intent,
                );
                return;
            }, $device);
            my $scheduled = eval {
                $class->_thick_schedule_materialization(
                    $storeid, $volname, $snap, $operation, $intent{tx}, $timeout,
                );
                1;
            };
            if ($scheduled) {
                # COMMITTED data and an exact persistent clone mapping are
                # already authoritative.  Returning now lets PVE resume QEMU;
                # the transaction-scoped worker performs bounded hydration,
                # the linear pivot, and exact cleanup. The signed non-
                # MATERIALIZED anchor blocks another mutation of this volume,
                # while independent volumes in the same VG can participate in
                # the same PVE multi-disk snapshot operation.
                return;
            }
            warn "asynchronous Thick Generations materialization could not be scheduled; "
                . "completing synchronously: $@";
        }
        $class->_thick_wait_for_hydration(
            $front, $scfg->{'slt-tg-hydration-timeout'} // 3600,
        );

        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my $state = decode_anchor_tags($lvs->{$vg}->{$tr->{anchor}}->{tags} // '');
        die "snapshot transition changed before hydration completion\n"
            if $state->{phase} ne 'HYDRATING' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new};
        $class->_thick_verify_clone_frontend(
            $scfg, $volname, sectors => int($tr->{size} / 512),
            region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
            new => $tr->{new}, source_map => $tr->{source_map},
        );
        $class->_thick_verify_clone_status($front, 1);
        $state = $class->_thick_transition_anchor(
            $vg, $tr->{anchor}, $state, phase => 'HYDRATION_COMPLETE', _device => $device,
        );
        $tr->{state} = $state;
        $class->_thick_fault_point('C9', $operation, $storeid, $volname);
        return;
        }, $device);
    }

    if ($tr->{state}->{phase} eq 'HYDRATION_COMPLETE') {
        $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_thick_require_transition_intent(
            $storeid, $scfg, $volname, \%intent,
        );
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my $state = decode_anchor_tags($lvs->{$vg}->{$tr->{anchor}}->{tags} // '');
        die "snapshot transition changed before linear pivot\n"
            if $state->{phase} ne 'HYDRATION_COMPLETE' || $state->{tx} ne $intent{tx}
            || $state->{head} ne $tr->{new};
        $class->_thick_verify_clone_frontend(
            $scfg, $volname, sectors => int($tr->{size} / 512),
            region => $tr->{geometry}->{region_sectors}, meta => $tr->{meta},
            new => $tr->{new}, source_map => $tr->{source_map},
        );
        $class->_thick_verify_clone_status($front, 1);
        my $sectors = int($tr->{size} / 512);
        run_command(
            ['/sbin/dmsetup', '--verifyudev', 'reload', $front, '--table',
                "0 $sectors linear /dev/$vg/$tr->{new} 0"],
            errmsg => "loading canonical linear frontend failed",
        );
        my $inactive = _command_lines(
            ['/sbin/dmsetup', 'table', '--inactive', $front],
            "reading inactive linear pivot table failed",
        );
        die "inactive linear pivot table postcondition failed\n"
            if @$inactive != 1 || $inactive->[0] !~ /^0\s+\Q$sectors\E\s+linear\s+/;
        run_command(
            ['/sbin/dmsetup', '--verifyudev', 'suspend', '--noflush', $front],
            errmsg => "suspending hydrated frontend for linear pivot failed",
        );
        run_command(
            ['/sbin/dmsetup', '--verifyudev', 'resume', $front],
            errmsg => "publishing canonical linear frontend failed",
        );
        $class->_thick_verify_frontend($scfg, $volname, $tr->{new}, $sectors);
        $state = $class->_thick_transition_anchor(
            $vg, $tr->{anchor}, $state, phase => 'LINEAR_PIVOTED', _device => $device,
        );

        $class->_thick_verify_transition_metadata(
            $storeid, $scfg, $volname, $lvs->{$vg}->{$tr->{meta}},
            tx => $intent{tx}, generation => $tr->{new_gen},
            region => $tr->{geometry}->{region_sectors},
            metadata_bytes => $tr->{geometry}->{metadata_bytes}, name => $tr->{meta},
            device => $device,
        );
        $class->_thick_verify_snapshot_readonly($vg, $tr->{source}, $device);
        run_command(
            ['/sbin/dmsetup', '--verifyudev', 'remove', $tr->{source_map}],
            errmsg => "removing detached snapshot source mapper failed",
        );
        run_command(
            ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$tr->{meta}"],
            errmsg => "removing detached dm-clone metadata failed",
        );
        die "detached snapshot source mapper still exists after removal\n"
            if _block_device_exists("/dev/mapper/$tr->{source_map}");
        my $after = $class->_thick_list_volumes_scoped($vg, $device);
        die "detached dm-clone metadata still exists after removal\n"
            if $after->{$vg} && $after->{$vg}->{$tr->{meta}};
        if ($rollback) {
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$tr->{old}"],
                errmsg => "removing superseded rollback HEAD failed",
            );
            $after = $class->_thick_list_volumes_scoped($vg, $device);
            die "superseded rollback HEAD still exists after removal\n"
                if $after->{$vg} && $after->{$vg}->{$tr->{old}};
            my ($kept_snapshot) = $class->_thick_find_snapshot(
                $storeid, $scfg, $volname, $tr->{snapshot}, $after,
            );
            die "rollback source snapshot identity changed\n"
                if $kept_snapshot ne $tr->{source};
        }
        $state = $class->_thick_transition_anchor(
            $vg, $tr->{anchor}, $state, phase => 'MATERIALIZED', _device => $device,
        );
        $class->_clear_vg_intent($vg, %intent, _device => $device)
            if !$intent{_anchor_scoped};
        return;
    }, $device);
    }
    return;
}

sub _thick_free_image {
    my ($class, $storeid, $scfg, $volname, $isBase, $require_orphan_alloc,
        $require_orphan_tree) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);

    return $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        die "thick-generations storage '$storeid' is unavailable: VG '$vg' is not visible\n"
            if !$lvs->{$vg};

        my ($state, undef, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        if ($require_orphan_alloc || $require_orphan_tree) {
            die "orphan recovery requires a canonical materialized ALLOC state\n"
                if $state->{phase} ne 'MATERIALIZED' || $state->{op} ne 'ALLOC'
                || $state->{snapshot} ne 'none';
            die "orphan-allocation recovery requires generation zero\n"
                if $require_orphan_alloc && $state->{generation} != 0;
            my $references = $class->_thick_pve_reference_files($storeid, $volname);
            die "orphan recovery refused: PVE still references '$storeid:$volname' in "
                . join(', ', @$references) . "\n" if @$references;
        }
        my $head = $state->{head};
        my $key = object_key($namespace, $volname);
        my @generations = sort grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %{$lvs->{$vg}};
        die "refusing to delete thick-generations volume '$volname': "
            . "owned snapshots or ambiguous generations remain\n"
            if @generations != 1 || $generations[0] ne $head;
        validate_generation_tags(
            $lvs->{$vg}->{$head}->{tags} // '', sid => $storeid, vol => $volname,
            role => 'head', generation => $state->{generation},
        );
        $class->_verify_autoactivation_disabled($vg, $head, $device);
        $class->_verify_autoactivation_disabled($vg, $anchor, $device);

        # PVE can call free_image() directly after cancelling a storage mirror
        # without first calling deactivate_volume().  The stable frontend is
        # therefore not ownership proof that the target is still in use.  It
        # is safe to dismantle only after exact identity/dependency checks and
        # a positively verified zero open count.  An open or ambiguous mapper
        # remains a hard refusal.
        if (_block_device_exists("/dev/mapper/$mapper")) {
            $class->_thick_verify_frontend($scfg, $volname, $head);
            my $opens = _command_lines(
                ['/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'open', $mapper],
                "reading thick-generations frontend open count '$mapper' before delete failed",
            );
            die "thick-generations frontend '$mapper' open count is ambiguous\n"
                if @$opens != 1 || $opens->[0] !~ /^\s*\d+\s*$/;
            die "refusing to delete open thick-generations volume '$volname'\n"
                if int($opens->[0]) != 0;
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'remove', '--retry', $mapper],
                errmsg => "removing idle thick-generations frontend '$mapper' before delete failed",
            );
            die "refusing thick-generations delete: frontend '$mapper' removal is unconfirmed\n"
                if _block_device_exists("/dev/mapper/$mapper");
            run_command(
                ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$anchor", "$vg/$head"],
                errmsg => "deactivating thick-generations state for '$vg/$volname' before delete failed",
            );
        }

        my $tx = $class->_new_transaction_id();
        my %intent = (
            tx => $tx, state => 'OPEN', op => 'REMOVE', object => $anchor,
            before => $class->_vg_state_digest($vg, $device),
        );
        $class->_set_vg_intent($vg, %intent, _device => $device);

        my $command_error = '';
        eval {
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$head"],
                errmsg => "removing thick generation '$vg/$head' failed",
            );
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$anchor"],
                errmsg => "removing thick generation anchor '$vg/$anchor' failed",
            );
        };
        $command_error = $@ if $@;

        my $after = $class->_thick_list_volumes_scoped($vg, $device);
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL DELETE for '$storeid:$volname': storage identity/availability "
            . "could not be revalidated; OPEN REMOVE intent preserved and no retry attempted: $@"
            if $@;
        # lvm_list_volumes() omits an otherwise healthy VG when it becomes empty.
        # Positive identity revalidation above distinguishes that from disappearance.
        my $after_objects = $after->{$vg} // {};
        my $head_remains = exists($after_objects->{$head});
        my $anchor_remains = exists($after_objects->{$anchor});
        die "PARTIAL DELETE for '$storeid:$volname': head=$head_remains "
            . "anchor=$anchor_remains; OPEN REMOVE intent preserved and no retry attempted\n"
            if $head_remains || $anchor_remains;

        warn "thick-generations delete command reported an error, but exact postcondition "
            . "proves both owned objects absent; treating operation as completed without retry: "
            . $command_error
            if $command_error;
        $class->_clear_vg_intent($vg, %intent, _device => $device);
        return undef;
    }, $device);
}

sub free_image {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;

    return $class->_thick_free_image($storeid, $scfg, $volname, $isBase)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_free_image_locked(
            $storeid, $scfg, $volname, $isBase,
        );
    });
}

sub _thin_recover_orphan {
    my ($class, $scfg, $storeid, $volname) = @_;
    die "thin orphan recovery is unavailable for Thick Generations storage\n"
        if $class->_allocation_mode($scfg) eq 'thick-generations';
    my ($vtype, undef, undef) = $class->parse_volname($volname);
    die "thin orphan recovery requires a canonical guest disk volume\n"
        if $vtype ne 'images' || $volname !~ /^vm-\d+-disk-\d+$/;

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "thin orphan recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;
        return $class->_free_image_locked($storeid, $scfg, $volname, 0);
    });
}

sub _free_image_locked {
    my ($class, $storeid, $scfg, $volname, $isBase) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};

    $class->_verify_storage_identity($storeid, $scfg);

    my ($vtype, $name, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    my $pool_info = $lvs->{$vg} && $lvs->{$vg}->{$pool};
    my $expected_tag = "pve-slt-sid-$storeid";
    my $pool_owned = 0;
    my $pool_legacy = 0;

    if ($pool_info) {
        die "refusing to modify foreign pool '$vg/$pool': not a thin pool\n"
            if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        $class->_verify_pool_health($vg, $pool);

        my $tags = $pool_info->{tags} // '';

        if ($tags =~ /(?:^|,)\Q$expected_tag\E(?:,|$)/) {
            $pool_owned = 1;
        } elsif ($tags =~ /(?:^|,)pve-slt-sid-[A-Za-z0-9_-]+(?:,|$)/) {
            die "refusing to modify foreign pool '$vg/$pool': owned by another storage\n";
        } else {
            $pool_legacy = 1;
        }
    }

    die "refusing to modify legacy pool '$vg/$pool': ownership is not positively proven\n"
        if $pool_legacy;

    if ($lvs->{$vg} && $lvs->{$vg}->{$volname}) {
        my $volume_info = $lvs->{$vg}->{$volname};

        die "refusing to remove '$vg/$volname': expected pool '$pool' is missing\n"
            if !$pool_info;

        die "refusing to remove '$vg/$volname': volume is not in expected pool '$pool'\n"
            if !defined($volume_info->{pool_lv})
            || $volume_info->{pool_lv} ne $pool;
    }

    # Remove snapshots belonging to this particular disk first.
    if ($lvs->{$vg}) {
        foreach my $lv (keys %{$lvs->{$vg}}) {
            next if $lv !~ /^snap_\Q$volname\E_/;

            my $snapshot_info = $lvs->{$vg}->{$lv};

            die "refusing to remove '$vg/$lv': expected pool '$pool' is missing\n"
                if !$pool_info;

            die "refusing to remove '$vg/$lv': snapshot is not in expected pool '$pool'\n"
                if !defined($snapshot_info->{pool_lv})
                || $snapshot_info->{pool_lv} ne $pool;

            $class->_verify_snapshot_postcondition($scfg, $volname, $lv, 1);

            run_command(
                ['/sbin/lvremove', '-f', "$vg/$lv"],
                errmsg => "removing snapshot '$vg/$lv' failed",
            );
        }
    }

    # Remove the guest LV itself.
    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    if ($lvs->{$vg} && $lvs->{$vg}->{$volname}) {
        run_command(
            ['/sbin/lvremove', '-f', "$vg/$volname"],
            errmsg => "removing volume '$vg/$volname' failed",
        );
    }

    #
    # Refresh metadata and remove the per-VM thin pool only when
    # absolutely no VM disk or snapshot belonging to this VM remains.
    #
    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);

    die "storage '$storeid' became UNAVAILABLE after deleting '$vg/$volname'; pool cleanup was intentionally NOT attempted\n"
        if !$lvs->{$vg};

    my $in_use = 0;

    if ($lvs->{$vg}) {
        foreach my $lv (keys %{$lvs->{$vg}}) {
            next if $lv eq $pool;
            my $info = $lvs->{$vg}->{$lv};
            if ((defined($info->{pool_lv}) && $info->{pool_lv} eq $pool)
                || $lv =~ /^vm-\Q$vmid\E-disk-\d+$/
                || $lv =~ /^snap_vm-\Q$vmid\E-disk-\d+_/) {
                $in_use = 1;
                last;
            }
        }
    }

    if (!$in_use && $lvs->{$vg} && $lvs->{$vg}->{$pool}) {
        my $pool_info = $lvs->{$vg}->{$pool};

        die "refusing to remove foreign pool '$vg/$pool': not a thin pool\n"
            if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

        if (!$pool_owned) {
            warn "preserving legacy untagged pool '$vg/$pool'; add storage ownership only after administrator review\n"
                if $pool_legacy;
            return undef;
        }

        $class->_verify_pool_health($vg, $pool);

        run_command(
            ['/sbin/lvremove', '-f', "$vg/$pool"],
            errmsg => "removing empty per-VM thin pool '$vg/$pool' failed",
        );
    }

    return undef;
}

sub _thick_volume_resize {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;
    die "resizing thick-generations snapshots is not supported\n" if defined($snapname);
    die "invalid resize size\n" if !defined($size) || $size !~ /^(\d+)$/ || $size < 1;
    $size = 0 + $1;
    $class->_require_thick_identity_config($storeid, $scfg);

    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $namespace = $class->_thick_namespace($scfg);
    my $mapper = mapper_name($namespace, $volname);
    my %resize;

    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my ($state, $head_info, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $head = $state->{head};
        my $old_size = $head_info->{lv_size};
        die "thick-generations head '$vg/$head' size is unknown\n"
            if !defined($old_size) || $old_size !~ /^\d+$/ || $old_size < 1;
        die "shrinking thick-generations volumes is not supported\n" if $size < $old_size;
        return if $size == $old_size;
        die "thick-generations size is not sector aligned\n"
            if $old_size % 512 || $size % 512;

        my $delta = $size - $old_size;
        $class->_thick_capacity_gate(
            $storeid, $scfg, int(($delta + 1023) / 1024), 0,
        );
        my $frontend = _block_device_exists("/dev/mapper/$mapper") ? 1 : 0;
        $class->_thick_verify_frontend(
            $scfg, $volname, $head, int($old_size / 512),
        ) if $frontend;

        my $tx = $class->_new_transaction_id();
        my %intent = (
            tx => $tx, state => 'OPEN', op => 'EXTEND', object => $anchor,
            before => $class->_vg_state_digest($vg, $device),
        );
        $class->_set_vg_intent($vg, %intent, _device => $device);

        my $extend_error = '';
        eval {
            run_command(
                ['/sbin/lvextend', '--devices', $device, '-L', "${size}B", "$vg/$head"],
                errmsg => "extending thick generation '$vg/$head' failed",
            );
        };
        $extend_error = $@ if $@;
        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL RESIZE for '$storeid:$volname': storage identity/availability "
            . "could not be revalidated; OPEN EXTEND intent preserved: $@"
            if $@;
        my $after = $class->_thick_list_volumes_scoped($vg, $device);
        my ($after_state, $after_head) =
            $class->_thick_anchor($storeid, $scfg, $volname, $after);
        die "PARTIAL RESIZE for '$storeid:$volname': authoritative head changed; "
            . "OPEN EXTEND intent preserved\n"
            if $after_state->{head} ne $head;
        my $new_size = $after_head->{lv_size};
        die "PARTIAL RESIZE for '$storeid:$volname': resulting size is unknown or "
            . "smaller than requested; OPEN EXTEND intent preserved; no retry attempted"
            . ($extend_error ? ": $extend_error" : "\n")
            if !defined($new_size) || $new_size !~ /^\d+$/ || $new_size < $size;
        die "PARTIAL RESIZE for '$storeid:$volname': resulting size is not sector aligned; "
            . "OPEN EXTEND intent preserved\n"
            if $new_size % 512;
        warn "thick-generations lvextend reported an error, but its exact postcondition "
            . "proves the requested size was reached; continuing without retry: $extend_error"
            if $extend_error;
        %resize = (
            intent => \%intent, anchor => $anchor, head => $head,
            old_size => int($old_size), new_size => int($new_size),
            frontend => $frontend,
        );
        return;
    }, $device);
    return if !%resize;

    my $zero_error = '';
    eval {
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-ay', '-K', "$vg/$resize{head}"],
            errmsg => "activating extended thick generation '$vg/$resize{head}' failed",
        ) if !$resize{frontend};
        my $length = $resize{new_size} - $resize{old_size};
        run_command(
            ['/usr/bin/dd', 'if=/dev/zero', "of=/dev/$vg/$resize{head}", 'bs=4M',
                "seek=$resize{old_size}", "count=$length", 'iflag=count_bytes',
                'oflag=seek_bytes,direct', 'conv=fsync,nocreat', 'status=none'],
            errmsg => "zero-initializing extended range of '$vg/$resize{head}' failed",
        );
        run_command(
            ['/sbin/blockdev', '--flushbufs', "/dev/$vg/$resize{head}"],
            errmsg => "flushing extended thick generation '$vg/$resize{head}' failed",
        );
        run_command(
            ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$resize{head}"],
            errmsg => "deactivating extended thick generation '$vg/$resize{head}' failed",
        ) if !$resize{frontend};
    };
    $zero_error = $@ if $@;
    die "PARTIAL RESIZE for '$storeid:$volname': the backing LV may be extended, but "
        . "the new range was not safely published; OPEN EXTEND intent preserved: $zero_error"
        if $zero_error;

    $class->_with_vg_lock($storeid, $scfg, sub {
        my %intent = %{$resize{intent}};
        $class->_require_exact_vg_intent($vg, %intent, _device => $device);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my ($state, $head_info) = $class->_thick_anchor(
            $storeid, $scfg, $volname, $lvs,
        );
        die "PARTIAL RESIZE for '$storeid:$volname': head or size changed before "
            . "publication; OPEN EXTEND intent preserved\n"
            if $state->{head} ne $resize{head}
            || !defined($head_info->{lv_size})
            || $head_info->{lv_size} != $resize{new_size};
        $class->_verify_autoactivation_disabled($vg, $resize{head}, $device);

        my $frontend_now = _block_device_exists("/dev/mapper/$mapper") ? 1 : 0;
        die "PARTIAL RESIZE for '$storeid:$volname': frontend presence changed; "
            . "OPEN EXTEND intent preserved\n"
            if $frontend_now != $resize{frontend};
        if ($frontend_now) {
            my $old_sectors = int($resize{old_size} / 512);
            my $new_sectors = int($resize{new_size} / 512);
            $class->_thick_verify_frontend(
                $scfg, $volname, $resize{head}, $old_sectors,
            );
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'reload', $mapper, '--table',
                    "0 $new_sectors linear /dev/$vg/$resize{head} 0"],
                errmsg => "loading extended thick-generations frontend '$mapper' failed",
            );
            my $inactive = _command_lines(
                ['/sbin/dmsetup', 'table', '--inactive', $mapper],
                "reading inactive table of '$mapper' failed",
            );
            die "PARTIAL RESIZE for '$storeid:$volname': inactive frontend table "
                . "postcondition failed; OPEN EXTEND intent preserved\n"
                if @$inactive != 1
                || $inactive->[0] !~ /^0\s+\Q$new_sectors\E\s+linear\s+/;
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'suspend', '--noflush', $mapper],
                errmsg => "suspending thick-generations frontend '$mapper' for resize failed",
            );
            run_command(
                ['/sbin/dmsetup', '--verifyudev', 'resume', $mapper],
                errmsg => "publishing extended thick-generations frontend '$mapper' failed",
            );
            $class->_thick_verify_frontend(
                $scfg, $volname, $resize{head}, $new_sectors,
            );
        }
        $class->_clear_vg_intent($vg, %intent, _device => $device);
        return;
    }, $device);
    return;
}

sub volume_resize {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;

    return $class->_thick_volume_resize(
        $scfg, $storeid, $volname, $size, $running, $snapname,
    ) if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_resize_locked(
            $scfg, $storeid, $volname, $size, $running, $snapname,
        );
    });
}

sub _volume_resize_locked {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;

    die "resizing snapshots is not supported\n" if $snapname;

    $class->_verify_mutation_quorum($storeid, $scfg);
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_autoactivation_disabled($vg, $volname);

    die "invalid resize size\n"
        if !defined($size) || $size <= 0;

    #
    # PVE passes the requested size in bytes here.
    # LVM accepts bytes explicitly with the B suffix.
    #
    run_command(
        [
            '/sbin/lvextend',
            '-L', "${size}B",
            "$vg/$volname",
        ],
        errmsg => "resizing shared thin LV '$vg/$volname' failed",
    );

    $class->_verify_resize_postcondition($scfg, $volname, $size);
    $class->_verify_autoactivation_disabled($vg, $volname);

    return;
}

sub volume_snapshot {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    return $class->_thick_volume_snapshot($scfg, $storeid, $volname, $snap)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_snapshot_locked(
            $scfg, $storeid, $volname, $snap,
        );
    });
}

sub _volume_snapshot_locked {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $snapvol = "snap_${volname}_${snap}";

    run_command(
        ['/sbin/lvcreate', '-n', $snapvol, '-pr', '-s', "$vg/$volname"],
        errmsg => "creating snapshot '$vg/$snapvol' failed",
    );

    $class->_disable_and_verify_autoactivation($vg, $snapvol);
    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1);

    return;
}

sub volume_snapshot_delete {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    return $class->_thick_volume_snapshot_delete($scfg, $storeid, $volname, $snap)
        if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_snapshot_delete_locked(
            $scfg, $storeid, $volname, $snap,
        );
    });
}

sub _thick_volume_snapshot_delete {
    my ($class, $scfg, $storeid, $volname, $snap, $require_orphan) = @_;
    $snap = _thick_snapshot_name($snap);
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";

    return $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my ($state, undef, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        if ($require_orphan) {
            my $references = $class->_thick_pve_reference_files($storeid, $volname);
            die "orphan-tree recovery refused: PVE still references '$storeid:$volname' in "
                . join(', ', @$references) . "\n" if @$references;
        }
        my ($snapshot, $generation) = $class->_thick_find_snapshot(
            $storeid, $scfg, $volname, $snap, $lvs,
        );
        die "refusing to delete authoritative HEAD as a snapshot\n"
            if $snapshot eq $state->{head};
        $class->_thick_verify_snapshot_readonly($vg, $snapshot, $device);
        $class->_verify_autoactivation_disabled($vg, $snapshot, $device);

        my $path = "/dev/$vg/$snapshot";
        if (_block_device_exists($path)) {
            my $open = _command_lines(
                ['/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'open', $path],
                "reading open count of snapshot '$vg/$snapshot' failed",
            );
            die "snapshot '$vg/$snapshot' open count is ambiguous\n"
                if @$open != 1 || $open->[0] !~ /^\d+$/;
            die "refusing to delete open snapshot '$vg/$snapshot'\n" if int($open->[0]) != 0;
        }

        my %intent = (
            tx => $class->_new_transaction_id(), state => 'OPEN',
            op => 'REMOVE_SNAPSHOT', object => $snapshot,
            before => $class->_vg_state_digest($vg, $device),
        );
        my $rebased = materialized_rebase_state($state, $intent{tx});
        $class->_thick_fault_point('D0', 'REMOVE_SNAPSHOT', $storeid, $volname);
        $class->_set_vg_intent($vg, %intent, _device => $device);
        $class->_thick_fault_point('D1', 'REMOVE_SNAPSHOT', $storeid, $volname);
        eval {
            $class->_change_exact_tags(
                $vg, $anchor,
                PVE::SharedLvmThinThick::anchor_tags(%$state),
                PVE::SharedLvmThinThick::anchor_tags(%$rebased),
                "rebasing materialized anchor '$vg/$anchor' before snapshot delete failed",
                $device,
            );
            $class->_thick_fault_point('D2', 'REMOVE_SNAPSHOT', $storeid, $volname);
            if (_block_device_exists($path)) {
                run_command(
                    ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$snapshot"],
                    errmsg => "deactivating snapshot '$vg/$snapshot' before delete failed",
                );
            }
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$snapshot"],
                errmsg => "removing snapshot '$vg/$snapshot' failed",
            );
            $class->_thick_fault_point('D3', 'REMOVE_SNAPSHOT', $storeid, $volname);
        };
        my $error = $@;

        eval { $class->_verify_storage_identity($storeid, $scfg, $device); };
        die "PARTIAL SNAPSHOT DELETE for '$storeid:$volname\@$snap': identity is uncertain; "
            . "OPEN intent preserved and no retry attempted: $@" if $@;
        my $after = $class->_thick_list_volumes_scoped($vg, $device);
        my $objects = $after->{$vg} // {};
        die "PARTIAL SNAPSHOT DELETE for '$storeid:$volname\@$snap': exact object remains; "
            . "OPEN intent preserved and no retry attempted"
            . ($error ? ": $error" : "\n") if exists($objects->{$snapshot});
        die "PARTIAL SNAPSHOT DELETE for '$storeid:$volname\@$snap': command failed after "
            . "the exact object disappeared; OPEN intent preserved for manual classification: $error"
            if $error;
        my ($after_state) = $class->_thick_anchor(
            $storeid, $scfg, $volname, $after,
        );
        die "snapshot delete changed authoritative HEAD or generation\n"
            if $after_state->{head} ne $state->{head}
            || int($after_state->{generation}) != int($state->{generation});
        die "snapshot delete did not leave a canonical materialized anchor\n"
            if $after_state->{tx} ne $intent{tx}
            || $after_state->{op} ne 'ALLOC'
            || $after_state->{snapshot} ne 'none'
            || $after_state->{source} ne $after_state->{head}
            || $after_state->{old} ne $after_state->{head}
            || $after_state->{new} ne $after_state->{head};
        $class->_clear_vg_intent($vg, %intent, _device => $device);
        $class->_thick_fault_point('D4', 'REMOVE_SNAPSHOT', $storeid, $volname);
        return;
    }, $device);
}

sub _thick_recover_orphan_tree {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my @snapshots;

    # Inventory under the canonical VG lock, but run each existing exact
    # snapshot-delete transaction separately. This keeps every destructive
    # step independently recoverable after host loss.
    $class->_with_vg_lock($storeid, $scfg, sub {
        $class->_require_no_vg_intent($vg, $device);
        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        my ($state) = $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $references = $class->_thick_pve_reference_files($storeid, $volname);
        die "orphan-tree recovery refused: PVE still references '$storeid:$volname' in "
            . join(', ', @$references) . "\n" if @$references;

        my $namespace = $class->_thick_namespace($scfg);
        my $key = object_key($namespace, $volname);
        my $objects = $lvs->{$vg} // {};
        die "orphan-tree recovery found transition metadata\n"
            if grep { /^sltg-m-\Q$key\E-/ } keys %$objects;

        for my $name (sort grep { /^sltg-g-\Q$key\E-\d{8}$/ } keys %$objects) {
            next if $name eq $state->{head};
            my $owned = decode_generation_tags($objects->{$name}->{tags} // '');
            die "orphan-tree recovery found a generation with ambiguous ownership\n"
                if $owned->{sid} ne $storeid || $owned->{vol} ne $volname
                || $owned->{role} ne 'snapshot'
                || generation_name($namespace, $volname, $owned->{generation}) ne $name;
            _thick_snapshot_name($owned->{snapshot});
            push @snapshots, $owned->{snapshot};
        }
        return;
    }, $device);

    for my $snapshot (@snapshots) {
        $class->_thick_volume_snapshot_delete(
            $scfg, $storeid, $volname, $snapshot, 1,
        );
    }
    return $class->_thick_free_image($storeid, $scfg, $volname, 0, 0, 1);
}

sub _thick_recover_snapshot_delete {
    my ($class, $scfg, $storeid, $volname) = @_;
    $class->_require_thick_identity_config($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";

    return $class->_with_vg_lock($storeid, $scfg, sub {
        my $intent = $class->_read_vg_intent($vg, $device);
        die "VG '$vg' has no snapshot-delete transaction to recover\n"
            if !defined($intent);
        die "VG '$vg' intent is not an OPEN snapshot-delete transaction\n"
            if ($intent->{state} // '') ne 'OPEN'
            || ($intent->{op} // '') ne 'REMOVE_SNAPSHOT';

        my %expected = %$intent;
        $class->_require_exact_vg_intent(
            $vg, %expected, _device => $device,
        );

        my $lvs = $class->_thick_list_volumes_scoped($vg, $device);
        die "snapshot-delete recovery cannot see VG '$vg'\n" if !$lvs->{$vg};
        my ($state, undef, $anchor) =
            $class->_thick_anchor($storeid, $scfg, $volname, $lvs);
        my $snapshot = $intent->{object};
        die "snapshot-delete intent targets the authoritative HEAD\n"
            if $snapshot eq $state->{head};

        my $verify_snapshot = sub {
            my ($objects) = @_;
            my $info = $objects->{$snapshot};
            return undef if !defined($info);
            my $owned = decode_generation_tags($info->{tags} // '');
            die "snapshot-delete intent object is not an owned snapshot generation\n"
                if $owned->{sid} ne $storeid
                || $owned->{vol} ne $volname
                || $owned->{role} ne 'snapshot';
            my $namespace = $class->_thick_namespace($scfg);
            die "snapshot-delete intent object name does not match its signed generation\n"
                if generation_name($namespace, $volname, $owned->{generation}) ne $snapshot;
            return $owned;
        };

        my $owned = $verify_snapshot->($lvs->{$vg});
        my $verify_snapshot_inactive = sub {
            $class->_thick_verify_snapshot_readonly($vg, $snapshot, $device);
            $class->_verify_autoactivation_disabled($vg, $snapshot, $device);
            my $path = "/dev/$vg/$snapshot";
            return if !_block_device_exists($path);
            my $open = _command_lines(
                ['/sbin/dmsetup', 'info', '-c', '--noheadings', '-o', 'open', $path],
                "reading open count of snapshot '$vg/$snapshot' failed",
            );
            die "snapshot '$vg/$snapshot' open count is ambiguous\n"
                if @$open != 1 || $open->[0] !~ /^\d+$/;
            die "refusing to recover deletion of open snapshot '$vg/$snapshot'\n"
                if int($open->[0]) != 0;
            return 1;
        };
        $verify_snapshot_inactive->() if defined($owned);

        if ($state->{tx} ne $intent->{tx}) {
            die "snapshot-delete recovery cannot rebase after the exact object disappeared\n"
                if !defined($owned);
            my $rebased = materialized_rebase_state($state, $intent->{tx});
            $class->_change_exact_tags(
                $vg, $anchor,
                PVE::SharedLvmThinThick::anchor_tags(%$state),
                PVE::SharedLvmThinThick::anchor_tags(%$rebased),
                "recovering materialized anchor '$vg/$anchor' before snapshot delete failed",
                $device,
            );
            $state = $rebased;
        } else {
            die "snapshot-delete recovery found a non-canonical rebased anchor\n"
                if $state->{op} ne 'ALLOC'
                || $state->{snapshot} ne 'none'
                || $state->{source} ne $state->{head}
                || $state->{old} ne $state->{head}
                || $state->{new} ne $state->{head};
        }

        if (defined($owned)) {
            $verify_snapshot_inactive->();
            my $path = "/dev/$vg/$snapshot";
            if (_block_device_exists($path)) {
                run_command(
                    ['/sbin/lvchange', '--devices', $device, '-an', "$vg/$snapshot"],
                    errmsg => "deactivating snapshot '$vg/$snapshot' during recovery failed",
                );
            }
            run_command(
                ['/sbin/lvremove', '--devices', $device, '-f', "$vg/$snapshot"],
                errmsg => "removing snapshot '$vg/$snapshot' during recovery failed",
            );
        }

        $class->_verify_storage_identity($storeid, $scfg, $device);
        my $after = $class->_thick_list_volumes_scoped($vg, $device);
        die "snapshot-delete recovery cannot confirm VG '$vg'\n" if !$after->{$vg};
        die "snapshot-delete recovery did not remove the exact snapshot object\n"
            if exists($after->{$vg}->{$snapshot});
        my ($final) = $class->_thick_anchor($storeid, $scfg, $volname, $after);
        die "snapshot-delete recovery did not preserve the canonical HEAD\n"
            if $final->{tx} ne $intent->{tx}
            || $final->{head} ne $state->{head}
            || int($final->{generation}) != int($state->{generation})
            || $final->{op} ne 'ALLOC'
            || $final->{snapshot} ne 'none'
            || $final->{source} ne $final->{head}
            || $final->{old} ne $final->{head}
            || $final->{new} ne $final->{head};
        $class->_clear_vg_intent(
            $vg, %expected, _device => $device,
        );
        return 'SNAPSHOT_DELETE_RECOVERED';
    }, $device);
}

sub _volume_snapshot_delete_locked {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $snapvol = "snap_${volname}_${snap}";

    # The origin's ownership is not sufficient proof that an LV with the
    # expected snapshot name is ours.  Revalidate the snapshot itself before
    # the destructive command so a stale/foreign name collision fails closed.
    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1);

    run_command(
        ['/sbin/lvremove', '-f', "$vg/$snapvol"],
        errmsg => "removing snapshot '$vg/$snapvol' failed",
    );

    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 0);

    return;
}

sub volume_snapshot_rollback {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    return $class->_thick_volume_snapshot(
        $scfg, $storeid, $volname, $snap, 'ROLLBACK',
    ) if $class->_allocation_mode($scfg) eq 'thick-generations';

    return $class->_with_mutation_lock($storeid, $scfg, sub {
        return $class->_volume_snapshot_rollback_locked(
            $scfg, $storeid, $volname, $snap,
        );
    });
}

sub _volume_snapshot_rollback_locked {
    my ($class, $scfg, $storeid, $volname, $snap) = @_;

    $class->_verify_mutation_quorum($storeid, $scfg);
    my $vg = $scfg->{'slt-vgname'};
    $class->_verify_storage_identity($storeid, $scfg);
    $class->_verify_owned_volume($storeid, $scfg, $volname);
    my $snapvol = "snap_${volname}_${snap}";
    my (undef, undef, $vmid) = $class->parse_volname($volname);
    my $pool = "sltp-$vmid";
    my $expected_tag = "pve-slt-sid-$storeid";
    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $vg_lvs = $lvs->{$vg} // {};
    my $pool_info = $vg_lvs->{$pool};

    die "rollback pool '$vg/$pool' is missing\n" if !$pool_info;
    die "rollback pool '$vg/$pool' is not a thin pool\n"
        if !defined($pool_info->{lv_type}) || $pool_info->{lv_type} ne 't';

    my $tags = $pool_info->{tags} // '';
    die "rollback pool '$vg/$pool' belongs to another storage\n"
        if $tags =~ /(?:^|,)pve-slt-sid-[A-Za-z0-9_-]+(?:,|$)/
        && $tags !~ /(?:^|,)\Q$expected_tag\E(?:,|$)/;

    for my $lv ($volname, $snapvol) {
        my $info = $vg_lvs->{$lv};
        die "rollback volume '$vg/$lv' is missing\n" if !$info;
        die "rollback volume '$vg/$lv' is not in expected pool '$pool'\n"
            if !defined($info->{pool_lv}) || $info->{pool_lv} ne $pool;
    }

    $class->_verify_snapshot_postcondition($scfg, $volname, $snapvol, 1);

    # Materialize the rollback result before removing the current origin.
    # The old RC3 ordering removed the origin first and could leave the VM
    # disk missing if snapshot creation then failed.
    my $temporary = "slt-rb-$volname-$$";
    PVE::Storage::Plugin::parse_lvm_name($temporary);
    die "temporary rollback LV '$vg/$temporary' already exists\n"
        if $vg_lvs->{$temporary};

    my $temporary_created = 0;
    my $origin_removed = 0;
    my $rollback_error;

    eval {
        run_command(
            ['/sbin/lvcreate', '-kn', '-n', $temporary, '-s', "$vg/$snapvol"],
            errmsg => "preparing rollback from '$vg/$snapvol' failed",
        );
        $temporary_created = 1;

        $class->_disable_and_verify_autoactivation($vg, $temporary);

        run_command(
            ['/sbin/lvremove', '-f', "$vg/$volname"],
            errmsg => "removing '$vg/$volname' for rollback failed",
        );
        $origin_removed = 1;

        run_command(
            ['/sbin/lvrename', $vg, $temporary, $volname],
            errmsg => "renaming rollback LV '$vg/$temporary' failed",
        );
        $temporary_created = 0;
    };

    $rollback_error = $@;

    if ($rollback_error) {
        if ($origin_removed) {
            die "$rollback_error"
                . "rollback data is preserved as '$vg/$temporary'; manual recovery is required\n";
        }

        if ($temporary_created) {
            die "$rollback_error"
                . "original '$vg/$volname' remains intact; prepared replacement '$vg/$temporary' was intentionally preserved; automatic cleanup was NOT performed\n";
        }

        die $rollback_error;
    }

    $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($vg);
    my $restored = $lvs->{$vg} && $lvs->{$vg}->{$volname};

    die "rollback postcondition failed: '$vg/$volname' is missing\n"
        if !$restored;
    die "rollback postcondition failed: '$vg/$volname' is not in pool '$pool'\n"
        if !defined($restored->{pool_lv}) || $restored->{pool_lv} ne $pool;
    $class->_verify_autoactivation_disabled($vg, $volname);

    return;
}

sub volume_rollback_is_possible {
    return 1;
}

sub clone_image {
    die "linked clones are not supported by sharedlvmthin\n";
}

sub volume_has_feature {
    my ($class, $scfg, $feature, $storeid, $volname, $snapname, $running) = @_;

    my $features = {
        snapshot => {
            current => 1,
        },
        resize => {
            current => 1,
        },
        copy => {
            current => 1,
            snap => 1,
        },
        # A newly-created LVM thin LV is logically zero-initialized.  Tell
        # qemu-img callers so they can avoid materialising zero extents during
        # clone/import operations.  This reduces write amplification; it is
        # not a substitute for physical thin-pool headroom.
        sparseinit => {
            current => 1,
        },
    };

    my ($vtype, $name, $vmid, $basename, $basevmid, $isBase)
        = $class->parse_volname($volname);

    my $key;

    if ($snapname) {
        $key = 'snap';
    } else {
        $key = $isBase ? 'base' : 'current';
    }

    return 1
        if $features->{$feature}
        && $features->{$feature}->{$key};

    return undef;
}

1;
