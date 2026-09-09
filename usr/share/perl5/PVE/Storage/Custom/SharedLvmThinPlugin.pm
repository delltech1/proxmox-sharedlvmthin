# Copyright (C) 2026 Stanislav Baran
# SPDX-License-Identifier: GPL-3.0-only

package PVE::Storage::Custom::SharedLvmThinPlugin;

use strict;
use warnings;

use PVE::Storage::Plugin;
use PVE::Storage::LVMPlugin;
use PVE::Cluster;
use PVE::Tools qw(run_command);
use PVE::SharedLvmThinSafety;

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

sub _verify_storage_identity {
    my ($class, $storeid, $scfg) = @_;

    my $expected_vg = $scfg->{'slt-expected-vg-uuid'};
    my $expected_pv = $scfg->{'slt-expected-pv-uuid'};
    my $expected_wwid = $scfg->{'slt-expected-wwid'};

    return 1 if !defined($expected_vg)
        && !defined($expected_pv)
        && !defined($expected_wwid);

    my $vg = $scfg->{'slt-vgname'};
    my $vg_lines = _command_lines(
        ['/sbin/vgs', '--readonly', '--noheadings', '-o', 'vg_uuid', $vg],
        "reading identity of VG '$vg' failed",
    );

    die "storage '$storeid' identity is ambiguous: expected exactly one VG '$vg'\n"
        if @$vg_lines != 1;

    die "storage '$storeid' VG UUID mismatch: expected '$expected_vg', found '$vg_lines->[0]'\n"
        if defined($expected_vg) && lc($vg_lines->[0]) ne lc($expected_vg);

    my $pv_lines = _command_lines(
        [
            '/sbin/pvs', '--readonly', '--noheadings',
            '--separator', '|', '-o', 'pv_uuid,pv_name',
            '--select', "vg_name=$vg",
        ],
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
    my ($class, $vg, $lv) = @_;

    run_command(
        ['/sbin/lvchange', '--setautoactivation', 'n', "$vg/$lv"],
        errmsg => "disabling autoactivation for '$vg/$lv' failed",
    );

    return $class->_verify_autoactivation_disabled($vg, $lv);
}

sub _verify_autoactivation_disabled {
    my ($class, $vg, $lv) = @_;
    my $lines = _command_lines(
        ['/sbin/lvs', '--readonly', '--binary', '--noheadings', '-o', 'lv_autoactivation', "$vg/$lv"],
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

    my ($vtype, $name, $vmid) = $class->parse_volname($volname);

    my $vg = $scfg->{'slt-vgname'};

    my $lv =
        defined($snapname)
        ? "snap_${name}_${snapname}"
        : $name;

    my $path = "/dev/$vg/$lv";

    return wantarray ? ($path, $vmid, $vtype) : $path;
}

sub activate_storage {
    my ($class, $storeid, $scfg, $cache) = @_;

    my $vg = $scfg->{'slt-vgname'};

    my $vgs = PVE::Storage::LVMPlugin::lvm_vgs();

    die "shared LVM VG '$vg' not found\n"
        if !defined($vgs->{$vg});

    $class->_verify_storage_identity($storeid, $scfg);

    return 1;
}

sub deactivate_storage {
    return 1;
}

sub status {
    my ($class, $storeid, $scfg, $cache) = @_;

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

    my ($current_pool, $used) = (0, 0);
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
            # shared storage merely to improve an admission estimate.  Treat
            # the entire current pool as used, which can over-reserve but
            # cannot under-provision the next bulk allocation.
            $used = $current_pool;
            warn "SharedLvmThin: Data% unavailable for inactive '$vg/$pool'; "
                . "using conservative used=current-size allocation estimate\n";
        }
    }

    my %target_args = (
        mode => $mode,
        fixed_gib => $fixed,
        requested_kib => $size_kib,
        used_bytes => $used,
        current_pool_bytes => $current_pool,
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

    my $vg = $scfg->{'slt-vgname'};
    my $lv = $snapname ? "snap_${volname}_${snapname}" : $volname;

    run_command(
        ['/sbin/lvchange', '-an', "$vg/$lv"],
        errmsg => "deactivating shared thin LV '$vg/$lv' failed",
    );

    return 1;
}

sub free_image {
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

sub volume_resize {
    my ($class, $scfg, $storeid, $volname, $size, $running, $snapname) = @_;

    return $class->cluster_lock_storage(
        $storeid,
        $scfg->{shared},
        undef,
        sub {
            return $class->_volume_resize_locked(
                $scfg, $storeid, $volname, $size, $running, $snapname,
            );
        },
    );
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

    return $class->cluster_lock_storage(
        $storeid,
        $scfg->{shared},
        undef,
        sub {
            return $class->_volume_snapshot_locked(
                $scfg, $storeid, $volname, $snap,
            );
        },
    );
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

    return $class->cluster_lock_storage(
        $storeid,
        $scfg->{shared},
        undef,
        sub {
            return $class->_volume_snapshot_delete_locked(
                $scfg, $storeid, $volname, $snap,
            );
        },
    );
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

    return $class->cluster_lock_storage(
        $storeid,
        $scfg->{shared},
        undef,
        sub {
            return $class->_volume_snapshot_rollback_locked(
                $scfg, $storeid, $volname, $snap,
            );
        },
    );
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
