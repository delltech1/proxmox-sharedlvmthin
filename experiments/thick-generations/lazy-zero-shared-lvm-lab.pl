#!/usr/bin/perl
use strict;
use warnings;

use Config;
use Fcntl qw(O_CREAT O_EXCL O_WRONLY);
use IO::Handle;
use JSON::PP qw(encode_json);
use PVE::Storage;
use PVE::Tools qw(run_command);
use PVE::SharedLvmThinThick qw(vg_intent_tags);
use PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';

sub fail { die "lazy shared-LVM lab: $_[0]\n"; }
sub command_lines {
    my ($argv, $label) = @_;
    my @lines;
    run_command($argv, outfunc => sub { push @lines, $_[0] }, errmsg => $label);
    return \@lines;
}
sub lv_record {
    my ($vg, $name, $device) = @_;
    my $lines = command_lines([
        '/sbin/lvs', '--readonly', '--devices', $device, '--noheadings',
        '--units', 'b', '--nosuffix', '--separator', '|',
        '-o', 'vg_name,lv_name,lv_uuid,lv_size,lv_attr,lv_tags,lv_skip_activation,lv_autoactivation',
        "$vg/$name",
    ], "reading exact lab LV failed");
    fail("LV '$vg/$name' is ambiguous") if @$lines != 1;
    my @fields = split(/\|/, $lines->[0], 8);
    fail("LV '$vg/$name' record is malformed") if @fields != 8;
    s/^\s+|\s+$//g for @fields;
    return { vg => $fields[0], name => $fields[1], uuid => $fields[2],
        size => 0 + $fields[3], attr => $fields[4], tags => $fields[5],
        skip_activation => $fields[6], autoactivation => $fields[7] };
}
sub lv_absent {
    my ($vg, $name, $device) = @_;
    my @lines;
    my $error = '';
    eval {
        run_command(['/sbin/lvs', '--readonly', '--devices', $device,
            '--noheadings', '-o', 'lv_name', $vg],
            outfunc => sub { push @lines, $_[0] },
            errmsg => 'enumerating lab VG failed');
    };
    $error = $@ if $@;
    die $error if $error ne '';
    for my $line (@lines) {
        $line =~ s/^\s+|\s+$//g;
        return 0 if $line eq $name;
    }
    return 1;
}
sub lv_mapper_name {
    my ($vg, $lv) = @_;
    $vg =~ s/-/--/g;
    $lv =~ s/-/--/g;
    return "$vg-$lv";
}
sub dm_kernel_inventory {
    my $lines = command_lines([
        '/sbin/dmsetup', 'info', '-c', '--noheadings', '--separator', '|',
        '-o', 'name,uuid,suspended',
    ], 'reading complete device-mapper kernel inventory failed');
    my %inventory;
    for my $line (@$lines) {
        my ($name, $uuid, $suspended) = split(/\|/, $line, -1);
        for ($name, $uuid, $suspended) {
            $_ //= '';
            s/^\s+|\s+$//g;
        }
        fail('device-mapper kernel inventory record is malformed')
            if $name eq '' || $uuid eq '' || $suspended eq '' || exists($inventory{$name});
        $inventory{$name} = { uuid => $uuid, suspended => $suspended };
    }
    return \%inventory;
}
sub require_kernel_absence {
    my ($vg, @lvs) = @_;
    my $inventory = dm_kernel_inventory();
    for my $lv (@lvs) {
        my $mapper = lv_mapper_name($vg, $lv);
        fail("exact lab LV '$vg/$lv' remains active in the kernel")
            if exists($inventory->{$mapper});
    }
}
sub require_active_identity {
    my ($vg, $lv, $device, $expected_lv_uuid) = @_;
    my $lines = command_lines([
        '/sbin/lvs', '--readonly', '--devices', $device, '--noheadings',
        '--separator', '|', '-o', 'vg_uuid,lv_uuid,lv_name', "$vg/$lv",
    ], "reading active lab LV identity of '$vg/$lv' failed");
    fail("active lab LV identity of '$vg/$lv' is ambiguous") if @$lines != 1;
    my ($vg_uuid, $lv_uuid, $actual_name) = split(/\|/, $lines->[0], -1);
    for ($vg_uuid, $lv_uuid, $actual_name) {
        $_ //= '';
        s/^\s+|\s+$//g;
    }
    fail("active lab LV identity returned unexpected object '$actual_name'")
        if $actual_name ne $lv || $lv_uuid ne $expected_lv_uuid;
    for ($vg_uuid, $lv_uuid) {
        fail('active lab LV identity contains an invalid UUID')
            if !/^[A-Za-z0-9-]+$/;
        s/-//g;
    }
    my $mapper = lv_mapper_name($vg, $lv);
    my $kernel = dm_kernel_inventory();
    fail("active lab LV '$vg/$lv' has no exact kernel mapper")
        if !exists($kernel->{$mapper});
    fail("active lab LV '$vg/$lv' kernel UUID mismatch")
        if $kernel->{$mapper}->{uuid} ne "LVM-$vg_uuid$lv_uuid";
    fail("active lab LV '$vg/$lv' is suspended")
        if lc($kernel->{$mapper}->{suspended}) ne 'active';
}
sub hold_path {
    my ($tx) = @_;
    return "/var/lib/pve-sharedlvmthin/r1-holds/$tx";
}
sub require_no_local_hold {
    my ($tx) = @_;
    my $path = hold_path($tx);
    fail("persistent peer hold blocks activation for transaction '$tx'") if -e $path || -l $path;
}
sub sync_directory {
    my ($path, $label) = @_;
    open(my $dh, '<', $path) or fail("cannot open $label directory: $!");
    $dh->sync() or fail("cannot sync $label directory: $!");
    close($dh) or fail("cannot close $label directory: $!");
}
sub ensure_local_hold {
    my ($tx, $nonce, $coordinator, $coordinator_boot) = @_;
    my $parent = '/var/lib/pve-sharedlvmthin';
    my $base = "$parent/r1-holds";
    fail('peer hold parent is unsafe') if -l $parent || !-d $parent;
    fail('peer hold base is a symlink') if -l $base;
    if (!-e $base) {
        mkdir($base, 0700) or fail("cannot create peer hold base: $!");
        sync_directory($parent, 'peer hold parent');
    }
    fail('peer hold base is not a directory') if !-d $base;
    chmod(0700, $base) or fail("cannot protect peer hold base: $!");
    my $path = hold_path($tx);
    my $expected = "SCHEMA=1\nSTATE=HELD\nTX=$tx\nNONCE=$nonce\n"
        . "COORDINATOR=$coordinator\nCOORDINATOR_BOOT_ID=$coordinator_boot\n";
    if (-e $path || -l $path) {
        fail('existing peer hold path is unsafe') if -l $path || !-d $path;
        my $file = "$path/hold";
        fail('existing peer hold record is unsafe') if -l $file || !-f $file;
        open(my $existing, '<', $file) or fail("cannot read existing peer hold: $!");
        local $/;
        my $content = <$existing>;
        fail('existing peer hold identity mismatch') if $content ne $expected;
        $existing->sync() or fail("cannot sync retained peer hold record: $!");
        close($existing) or fail("cannot close existing peer hold: $!");
        sync_directory($path, 'retained peer hold');
        sync_directory($base, 'retained peer hold base');
        sync_directory($parent, 'retained peer hold parent');
        return ($path, 0);
    }
    mkdir($path, 0700) or fail("peer hold exists or cannot be created: $!");
    my $file = "$path/hold";
    sysopen(my $fh, $file, O_WRONLY | O_CREAT | O_EXCL, 0600)
        or fail("cannot create peer hold record: $!");
    print {$fh} $expected
        or fail("cannot write peer hold record: $!");
    $fh->sync() or fail("cannot sync peer hold record: $!");
    close($fh) or fail("cannot close peer hold record: $!");
    sync_directory($path, 'peer hold');
    sync_directory($base, 'peer hold base');
    return ($path, 1);
}
sub validate_record {
    my ($record, $vg, $name, $bytes, $data_bytes, $tx) = @_;
    fail("LV identity mismatch") if $record->{vg} ne $vg || $record->{name} ne $name;
    fail("LV size mismatch") if $record->{size} != $bytes;
    fail("LV UUID malformed") if $record->{uuid} !~ /^[A-Za-z0-9-]+$/;
    fail("LV is not a plain linear LV") if $record->{attr} !~ /^-wi-/;
    fail("LV activation-skip flag is absent")
        if $record->{skip_activation} ne 'skip activation' || $record->{attr} !~ /k$/;
    fail("LV autoactivation is enabled") if $record->{autoactivation} ne '';
    my @tag_list = split(/,/, $record->{tags});
    my %tags = map { $_ => 1 } @tag_list;
    my @size_tags = grep { /^slt_lazy_bytes_/ } @tag_list;
    fail("LV ownership tags missing")
        if !$tags{'slt_lazy_lab_v1'} || !$tags{"slt_lazy_tx_$tx"}
        || @size_tags != 1 || $size_tags[0] ne "slt_lazy_bytes_$data_bytes";
}
sub set_intent_terminal {
    my ($scfg, $vg, $device, $intent) = @_;
    $class->_require_no_vg_intent($scfg, $vg, $device);
    my $observed = $class->_vg_state_digest($scfg, $vg, $device);
    fail('VG changed before lab intent dispatch') if $observed ne $intent->{before};
    my $tags = vg_intent_tags(%$intent);
    # Deliberately no userspace timeout and no ambiguous-success recovery: the
    # sole vgchange must terminate with exit 0 before any lvcreate is allowed.
    run_command(['/sbin/vgchange', '--devices', $device,
        (map { ('--addtag', $_) } @$tags), $vg],
        errmsg => 'persisting terminal lab intent failed');
    $class->_require_exact_vg_intent($scfg, $vg, %$intent, _device => $device);
}
sub clear_intent_terminal {
    my ($scfg, $vg, $device, $intent) = @_;
    $class->_require_exact_vg_intent($scfg, $vg, %$intent, _device => $device);
    my $tags = vg_intent_tags(%$intent);
    run_command(['/sbin/vgchange', '--devices', $device,
        (map { ('--deltag', $_) } @$tags), $vg],
        errmsg => 'clearing terminal lab intent failed');
    fail('lab intent remains after terminal clear')
        if defined($class->_read_vg_intent($scfg, $vg, $device));
}
sub parse_common {
    my ($args) = @_;
    my %values;
    while (@$args) {
        my $key = shift @$args;
        fail("unknown or incomplete argument '$key'") if $key !~ /^--/ || !@$args;
        $values{substr($key, 2)} = shift @$args;
    }
    for my $key (qw(storeid tx nonce data-gib)) {
        fail("missing --$key") if !defined($values{$key});
    }
    fail('invalid transaction') if $values{tx} !~ /^[0-9a-f]{32}$/;
    fail('invalid nonce') if $values{nonce} !~ /^[0-9a-f]{32}$/;
    fail('invalid storage ID') if $values{storeid} !~ /^[A-Za-z0-9_.-]+$/;
    fail('data size must be an integer from 1 through 512 GiB')
        if $values{'data-gib'} !~ /^(?:[1-9]|[1-9][0-9]|[1-4][0-9]{2}|5(?:0[0-9]|1[0-2]))$/;
    return \%values;
}

my $action = shift(@ARGV) // '';
fail('action must be setup, hold, activate, deactivate or cleanup')
    if $action ne 'setup' && $action ne 'activate'
    && $action ne 'deactivate' && $action ne 'hold' && $action ne 'cleanup';
my $values = parse_common(\@ARGV);
my $cfg = PVE::Storage::config();
my $scfg = PVE::Storage::storage_config($cfg, $values->{storeid});
fail('storage is not Thick Generations')
    if ($scfg->{type} // '') ne $class->type()
    || ($scfg->{'slt-allocation-mode'} // '') ne 'thick-generations';
my $vg = $scfg->{'slt-vgname'};
my $device = '/dev/mapper/' . ($scfg->{'slt-expected-wwid'} // '');
fail('storage lacks pinned VG/PV/WWID identity')
    if !$scfg->{'slt-expected-vg-uuid'} || !$scfg->{'slt-expected-pv-uuid'}
    || $device !~ m{^/dev/mapper/[0-9a-f]+$};
my $data = "sltlz-d-$values->{nonce}";
my $meta = "sltlz-m-$values->{nonce}";
my $object = "lazy-$values->{nonce}";
fail('64-bit integer arithmetic is required') if $Config{ivsize} < 8;
my $data_gib = 0 + $values->{'data-gib'};
my $data_bytes = $data_gib * 1024 * 1024 * 1024;
my $meta_bytes = 256 * 1024 * 1024;
my $region_bytes = 1024 * 1024;
my $data_sectors = int($data_bytes / 512);
my $total_regions = int($data_bytes / $region_bytes);
fail('derived lazy geometry is not exact')
    if $data_sectors * 512 != $data_bytes
    || $total_regions * $region_bytes != $data_bytes;

if ($action eq 'setup') {
    my $result = $class->_with_mutation_lock($values->{storeid}, $scfg, sub {
        fail('data LV already exists') if !lv_absent($vg, $data, $device);
        fail('metadata LV already exists') if !lv_absent($vg, $meta, $device);
        # Reserve policy is evaluated while holding the canonical mutation lock
        # and before publishing intent or dispatching either lvcreate.
        $class->_thick_capacity_gate(
            $values->{storeid}, $scfg,
            int(($data_bytes + $meta_bytes + 1023) / 1024));
        my $before = $class->_vg_state_digest($scfg, $vg, $device);
        my %intent = (tx => $values->{tx}, state => 'OPEN', op => 'DM_PIVOT',
            object => $object, before => $before);
        set_intent_terminal($scfg, $vg, $device, \%intent);
        for my $spec ([$data, "${data_gib}G"], [$meta, '256M']) {
            run_command(['/sbin/lvcreate', '--devices', $device, '--yes', '--zero', 'n',
                '--wipesignatures', 'n', '--addtag', 'slt_lazy_lab_v1',
                '--addtag', "slt_lazy_tx_$values->{tx}",
                '--addtag', "slt_lazy_bytes_$data_bytes", '-L', $spec->[1],
                '--ignoreactivationskip', '--setactivationskip', 'y',
                '--setautoactivation', 'n', '-n', $spec->[0], $vg],
                errmsg => "creating lab LV '$spec->[0]' failed");
        }
        my $data_record = lv_record($vg, $data, $device);
        my $meta_record = lv_record($vg, $meta, $device);
        validate_record($data_record, $vg, $data, $data_bytes, $data_bytes, $values->{tx});
        validate_record($meta_record, $vg, $meta, $meta_bytes, $data_bytes, $values->{tx});
        return { classification => 'LAB_LVS_CREATED_INTENT_HELD', storeid => $values->{storeid},
            vg => $vg, device => $device, before => $before, tx => $values->{tx},
            object => $object, data_gib => $data_gib, data_bytes => $data_bytes,
            data_sectors => $data_sectors, region_bytes => $region_bytes,
            total_regions => $total_regions, metadata_bytes => $meta_bytes,
            data => $data_record, metadata => $meta_record };
    });
    print encode_json($result), "\n";
    exit 0;
}

for my $key (qw(before data-uuid meta-uuid)) {
    fail("missing --$key") if !defined($values->{$key});
}
fail('invalid before digest') if $values->{before} !~ /^[0-9a-f]{32}$/;
for my $key (qw(data-uuid meta-uuid)) {
    fail("invalid $key") if $values->{$key} !~ /^[A-Za-z0-9-]+$/;
}
if ($action eq 'activate' || $action eq 'deactivate' || $action eq 'hold') {
    my $result = $class->_with_vg_lock($values->{storeid}, $scfg, sub {
        my %intent = (tx => $values->{tx}, state => 'OPEN', op => 'DM_PIVOT',
            object => $object, before => $values->{before});
        $class->_require_exact_vg_intent($scfg, $vg, %intent, _device => $device);
        my $data_record = lv_record($vg, $data, $device);
        my $meta_record = lv_record($vg, $meta, $device);
        fail('data UUID changed') if $data_record->{uuid} ne $values->{'data-uuid'};
        fail('metadata UUID changed') if $meta_record->{uuid} ne $values->{'meta-uuid'};
        validate_record($data_record, $vg, $data, $data_bytes, $data_bytes, $values->{tx});
        validate_record($meta_record, $vg, $meta, $meta_bytes, $data_bytes, $values->{tx});
        if ($action eq 'hold') {
            fail('invalid --coordinator')
                if !defined($values->{coordinator})
                || $values->{coordinator} !~ /^[A-Za-z0-9_.-]+$/;
            fail('invalid --coordinator-boot')
                if !defined($values->{'coordinator-boot'})
                || $values->{'coordinator-boot'} !~ /^[0-9a-f-]{36}$/;
            my $inventory = dm_kernel_inventory();
            for my $mapper (lv_mapper_name($vg, $data), lv_mapper_name($vg, $meta),
                "slt-lz-zero-$values->{nonce}", "slt-lz-delay-$values->{nonce}",
                "slt-lz-clone-$values->{nonce}") {
                fail("peer hold cannot cover active mapper '$mapper'")
                    if exists($inventory->{$mapper});
            }
            my ($path, $created) = ensure_local_hold($values->{tx}, $values->{nonce},
                $values->{coordinator}, $values->{'coordinator-boot'});
            return { classification => ($created
                    ? 'LAB_PEER_HOLD_INSTALLED_ABSENCE_PROVEN'
                    : 'LAB_PEER_HOLD_RETAINED_EXACT_ABSENCE_PROVEN'),
                tx => $values->{tx}, hold => $path,
                data_uuid => $values->{'data-uuid'}, meta_uuid => $values->{'meta-uuid'} };
        }
        require_no_local_hold($values->{tx});
        if ($action eq 'activate') {
            # Reboot admission cannot reconcile an ambiguous mutation from a later
            # postcondition.  Dispatch this command exactly once, wait for its real
            # terminal exit, and fail UNKNOWN on every command error.
            run_command(['/sbin/lvchange', '--devices', $device,
                '--activationmode', 'complete', '-ay', '-K',
                "$vg/$data", "$vg/$meta"],
                errmsg => 'activating exact lazy lab LVs failed');
            require_active_identity($vg, $data, $device, $values->{'data-uuid'});
            require_active_identity($vg, $meta, $device, $values->{'meta-uuid'});
        } else {
            my $before = dm_kernel_inventory();
            for my $lv ($data, $meta) {
                my $mapper = lv_mapper_name($vg, $lv);
                require_active_identity($vg, $lv, $device,
                    $lv eq $data ? $values->{'data-uuid'} : $values->{'meta-uuid'})
                    if exists($before->{$mapper});
            }
            run_command(['/sbin/lvchange', '--devices', $device, '-an',
                "$vg/$data", "$vg/$meta"],
                errmsg => 'deactivating exact lazy lab LVs failed');
            require_kernel_absence($vg, $data, $meta);
        }
        $data_record = lv_record($vg, $data, $device);
        $meta_record = lv_record($vg, $meta, $device);
        my $expect_active = $action eq 'activate';
        # Do not infer runtime activity from lvs --readonly.  It deliberately
        # avoids the kernel/device-mapper consultation needed for that claim.
        validate_record($data_record, $vg, $data, $data_bytes, $data_bytes, $values->{tx});
        validate_record($meta_record, $vg, $meta, $meta_bytes, $data_bytes, $values->{tx});
        return { classification => $expect_active
                ? 'LAB_LVS_EXACTLY_ACTIVATED' : 'LAB_LVS_EXACTLY_DEACTIVATED',
            tx => $values->{tx}, data_uuid => $values->{'data-uuid'},
            meta_uuid => $values->{'meta-uuid'} };
    }, $device);
    print encode_json($result), "\n";
    exit 0;
}
my $cleanup = $class->_with_vg_lock($values->{storeid}, $scfg, sub {
    my %intent = (tx => $values->{tx}, state => 'OPEN', op => 'DM_PIVOT',
        object => $object, before => $values->{before});
    $class->_require_exact_vg_intent($scfg, $vg, %intent, _device => $device);
    require_no_local_hold($values->{tx});
    my $data_record = lv_record($vg, $data, $device);
    my $meta_record = lv_record($vg, $meta, $device);
    fail('data UUID changed') if $data_record->{uuid} ne $values->{'data-uuid'};
    fail('metadata UUID changed') if $meta_record->{uuid} ne $values->{'meta-uuid'};
    validate_record($data_record, $vg, $data, $data_bytes, $data_bytes, $values->{tx});
    validate_record($meta_record, $vg, $meta, $meta_bytes, $data_bytes, $values->{tx});
    my $kernel = dm_kernel_inventory();
    my @active = grep { exists($kernel->{lv_mapper_name($vg, $_)}) } ($meta, $data);
    if (@active) {
        for my $lv (@active) {
            require_active_identity($vg, $lv, $device,
                $lv eq $data ? $values->{'data-uuid'} : $values->{'meta-uuid'});
        }
        run_command(['/sbin/lvchange', '--devices', $device, '-an',
            map { "$vg/$_" } @active], errmsg => 'deactivating exact lab LVs failed');
    }
    require_kernel_absence($vg, $meta, $data);
    $data_record = lv_record($vg, $data, $device);
    $meta_record = lv_record($vg, $meta, $device);
    for my $record ($meta_record, $data_record) {
        my $error = '';
        eval { run_command(['/sbin/lvremove', '--devices', $device, '--yes',
            "$vg/$record->{name}"], errmsg => "removing lab LV '$record->{name}' failed"); };
        $error = $@ if $@;
        fail("removal outcome unknown for '$record->{name}': $error")
            if !lv_absent($vg, $record->{name}, $device);
        warn "remove reported an error but exact absence is proven: $error" if $error ne '';
    }
    clear_intent_terminal($scfg, $vg, $device, \%intent);
    return { classification => 'LAB_LVS_REMOVED_INTENT_CLEARED', tx => $values->{tx},
        data_uuid => $values->{'data-uuid'}, meta_uuid => $values->{'meta-uuid'} };
}, $device);
print encode_json($cleanup), "\n";
