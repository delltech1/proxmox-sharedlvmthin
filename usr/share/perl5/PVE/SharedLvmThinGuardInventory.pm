# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinGuardInventory;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(build_guard_inventory dm_mapper_name dm_pool_uuid);

sub dm_mapper_name {
    my ($vg, $pool) = @_;
    die "invalid VG name\n" if !defined($vg) || $vg !~ /^[A-Za-z0-9_.+\-]+$/;
    die "invalid pool name\n" if !defined($pool) || $pool !~ /^[A-Za-z0-9_.+\-]+$/;
    for ($vg, $pool) { s/-/--/g }
    return "$vg-$pool-tpool";
}

sub dm_pool_uuid {
    my ($vg_uuid, $lv_uuid) = @_;
    for my $item ($vg_uuid, $lv_uuid) {
        die "invalid LVM UUID\n" if !defined($item) || $item !~ /^[A-Za-z0-9-]+$/;
        $item =~ s/-//g;
    }
    return "LVM-$vg_uuid$lv_uuid-tpool";
}

sub _tags {
    my ($raw) = @_;
    my %tags;
    for my $tag (grep { length($_) } split(/,/, $raw // '')) {
        die "duplicate LVM tag '$tag'\n" if $tags{$tag}++;
    }
    return \%tags;
}

sub _single_prefixed_tag {
    my ($tags, $prefix, $pattern) = @_;
    my @values = map { substr($_, length($prefix)) }
        grep { index($_, $prefix) == 0 } keys %$tags;
    die "missing $prefix tag\n" if @values == 0;
    die "ambiguous $prefix tags\n" if @values != 1;
    die "malformed $prefix tag\n" if $values[0] !~ $pattern;
    return $values[0];
}

# Convert already-collected, read-only evidence into the canonical guardian
# inventory.  Collection is deliberately separate so unit tests can exercise
# every ambiguity without requiring LVM, QEMU or a cluster.
sub build_guard_inventory {
    my (%e) = @_;
    die "LVM rows must be an array\n" if ref($e{lvs}) ne 'ARRAY';
    die "DM inventory must be a hash\n" if ref($e{dm}) ne 'HASH';
    die "QEMU references must be a hash\n" if ref($e{qemu_refs}) ne 'HASH';
    die "storage inventory must be a hash\n" if ref($e{storages}) ne 'HASH';
    die "local node is malformed\n"
        if !defined($e{local_node}) || $e{local_node} !~ /^[A-Za-z0-9_.-]+$/;

    my (%seen_pool, @pools);
    for my $row (@{$e{lvs}}) {
        die "malformed LVM row\n" if ref($row) ne 'HASH';
        next if ($row->{lv_name} // '') !~ /^sltp-(\d+)$/;
        my $vmid = 0 + $1;
        die "duplicate managed pool $row->{vg_name}/$row->{lv_name}\n"
            if $seen_pool{"$row->{vg_name}\0$row->{lv_name}"}++;
        die "managed pool has invalid LVM attributes\n"
            if ($row->{lv_attr} // '') !~ /^t/;

        my $tags = _tags($row->{lv_tags});
        die "managed pool lacks schema tag\n" if !$tags->{'pve-slt-owner-v1'};
        my $sid = _single_prefixed_tag($tags, 'pve-slt-sid-', qr/^[A-Za-z0-9_.-]+$/);
        my $owner = _single_prefixed_tag($tags, 'pve-slt-owner-node-', qr/^[A-Za-z0-9_.-]+$/);
        my $epoch = _single_prefixed_tag($tags, 'pve-slt-owner-epoch-', qr/^[a-f0-9]{32}$/);
        my $storage = $e{storages}->{$sid}
            or die "pool references unknown storage '$sid'\n";
        die "storage '$sid' is not Thin mode\n"
            if ($storage->{allocation_mode} // 'thin') ne 'thin';
        die "pool VG differs from storage '$sid' VG\n"
            if ($storage->{vg_name} // '') ne ($row->{vg_name} // '');
        die "pool VG UUID differs from pinned storage identity\n"
            if ($storage->{vg_uuid} // '') ne ($row->{vg_uuid} // '');

        my $mapper = dm_mapper_name($row->{vg_name}, $row->{lv_name});
        my $expected_dm_uuid = dm_pool_uuid($row->{vg_uuid}, $row->{lv_uuid});
        my $observed_dm_uuid = $e{dm}->{$mapper};
        my $locally_active = defined($observed_dm_uuid) ? 1 : 0;
        die "local mapper UUID mismatch for $mapper\n"
            if $locally_active && $observed_dm_uuid ne $expected_dm_uuid;

        my $refs = $e{qemu_refs}->{$vmid} // [];
        die "QEMU reference set for VM $vmid is malformed\n" if ref($refs) ne 'ARRAY';
        my @exact = grep { $_ eq "$sid:vm-$vmid-disk-0" || /^\Q$sid:vm-$vmid-disk-\E\d+$/ } @$refs;
        my $qemu_reference_exact = @exact ? 1 : 0;
        die "active pool owner is not the local node\n"
            if $locally_active && $owner ne $e{local_node};
        die "active pool has no exact local QEMU volume reference\n"
            if $locally_active && !$qemu_reference_exact;

        push @pools, {
            vmid => $vmid,
            storage_id => $sid,
            vg_name => $row->{vg_name},
            pool_name => $row->{lv_name},
            pool_uuid => $row->{lv_uuid},
            vg_uuid => $row->{vg_uuid},
            owner_node => $owner,
            owner_epoch => $epoch,
            mapper_name => $mapper,
            mapper_uuid => $expected_dm_uuid,
            locally_active => $locally_active,
            qemu_reference_exact => $qemu_reference_exact,
        };
    }

    return {
        schema => 'BASTRIX_THIN_GUARD_INVENTORY_V1',
        local_node => $e{local_node},
        quorum => $e{quorum} ? 1 : 0,
        pools => [sort { $a->{pool_uuid} cmp $b->{pool_uuid} } @pools],
    };
}

1;
