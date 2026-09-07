# Copyright (C) 2026 Stanislav Baran
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinSafety;

use strict;
use warnings;

sub evaluate_allocation_target {
    my (%args) = @_;
    for my $required (qw(mode fixed_gib requested_kib used_bytes current_pool_bytes)) {
        die "missing $required\n" if !defined($args{$required});
    }

    my $mode = $args{mode};
    die "invalid allocation mode '$mode'\n"
        if $mode !~ /^(?:fixed|proportional|full)$/;
    for my $numeric (qw(fixed_gib requested_kib used_bytes current_pool_bytes)) {
        die "invalid $numeric\n"
            if $args{$numeric} !~ /^\d+$/;
    }
    die "invalid fixed_gib\n" if $args{fixed_gib} < 1;

    my $gib = 1024 * 1024 * 1024;
    my $requested = $args{requested_kib} * 1024;
    my $fixed = $args{fixed_gib} * $gib;
    my $target;

    if ($mode eq 'fixed') {
        $target = $fixed;
    } elsif ($mode eq 'proportional') {
        die "missing percent\n" if !defined($args{percent});
        die "invalid percent\n"
            if $args{percent} !~ /^\d+$/ || $args{percent} < 1 || $args{percent} > 100;
        my $proportional = int(($requested * $args{percent} + 99) / 100);
        my $headroom = $proportional > $fixed ? $proportional : $fixed;
        $target = $args{used_bytes} + $headroom;
    } else {
        my $headroom = $requested;
        $headroom = $fixed
            if $args{current_pool_bytes} == 0 && $headroom < $fixed;
        $target = $args{used_bytes} + $headroom;
    }

    if (defined($args{max_gib})) {
        die "invalid max_gib\n"
            if $args{max_gib} !~ /^\d+$/ || $args{max_gib} < 1;
        my $maximum = $args{max_gib} * $gib;
        die "configured maximum $maximum bytes is below fixed minimum $fixed bytes\n"
            if $mode eq 'proportional' && $maximum < $fixed;
        die "full allocation target $target bytes exceeds configured maximum $maximum bytes\n"
            if $mode eq 'full' && $target > $maximum;
        $target = $maximum if $mode eq 'proportional' && $target > $maximum;
    }

    $target = $args{current_pool_bytes} if $target < $args{current_pool_bytes};
    die "allocation target exceeds safe signed 64-bit arithmetic\n"
        if $target > 9_000_000_000_000_000_000;
    my $growth = $target - $args{current_pool_bytes};
    return {
        target_bytes => $target,
        growth_bytes => $growth,
        requested_bytes => $requested,
        burst_guarantee => $mode eq 'full' ? 'FULL_AT_ADMISSION'
            : $mode eq 'proportional' ? 'BOUNDED' : 'NONE',
    };
}

sub evaluate_allocation_reserve {
    my (%args) = @_;
    for my $required (qw(vg_size vg_free growth_bytes overhead_bytes extent_bytes reserve_percent reserve_gib)) {
        die "missing $required\n" if !defined($args{$required});
        die "invalid $required\n" if $args{$required} !~ /^\d+(?:\.\d+)?$/;
    }
    die "invalid extent_bytes\n" if $args{extent_bytes} <= 0;

    my $gib = 1024 * 1024 * 1024;
    my $percent_bytes = int(($args{vg_size} * $args{reserve_percent} + 99) / 100);
    my $fixed_bytes = int($args{reserve_gib} * $gib);
    my $reserve = $percent_bytes > $fixed_bytes ? $percent_bytes : $fixed_bytes;
    my $raw_required = $args{growth_bytes} + $args{overhead_bytes};
    my $required_extents = int(($raw_required + $args{extent_bytes} - 1) / $args{extent_bytes});
    my $required = $required_extents * $args{extent_bytes};
    my $after = $args{vg_free} - $required;

    return {
        reserve_bytes => $reserve,
        required_physical_bytes => $required,
        required_extents => $required_extents,
        free_after_bytes => $after,
        allowed => $after >= $reserve ? 1 : 0,
    };
}

sub evaluate_autogrow_reserve {
    my (%args) = @_;
    for my $required (qw(vg_size vg_free pool_size growth_percent reserve_percent reserve_gib)) {
        die "missing $required\n" if !defined($args{$required});
        die "invalid $required\n" if $args{$required} !~ /^\d+(?:\.\d+)?$/;
    }

    my $gib = 1024 * 1024 * 1024;
    my $percent_bytes = int(($args{vg_size} * $args{reserve_percent} + 99) / 100);
    my $fixed_bytes = int($args{reserve_gib} * $gib);
    my $reserve = $percent_bytes > $fixed_bytes ? $percent_bytes : $fixed_bytes;
    my $growth = int(($args{pool_size} * $args{growth_percent} + 99) / 100);
    my $after = $args{vg_free} - $growth;
    my $usable = $args{vg_free} > $reserve ? $args{vg_free} - $reserve : 0;

    return {
        reserve_bytes => $reserve,
        growth_bytes => $growth,
        free_after_bytes => $after,
        usable_free_bytes => $usable,
        allowed => $after >= $reserve ? 1 : 0,
    };
}

sub evaluate_metadata_health {
    my (%args) = @_;
    return { status => 'N_A', reason => 'pool inactive', blocks_operation => 0 }
        if !$args{active};
    return { status => 'UNKNOWN', reason => 'metadata usage unavailable', blocks_operation => 0 }
        if !defined($args{metadata_percent}) || $args{metadata_percent} !~ /^\d+(?:\.\d+)?$/;

    my $percent = 0 + $args{metadata_percent};
    return { status => 'CRITICAL', reason => 'metadata usage is at or above 90%', blocks_operation => 0 }
        if $percent >= 90;
    return { status => 'WARN', reason => 'metadata usage is at or above 75%', blocks_operation => 0 }
        if $percent >= 75;
    return { status => 'HEALTHY', reason => 'metadata usage is below 75%', blocks_operation => 0 };
}

sub evaluate_chunk_geometry {
    my (%args) = @_;
    for my $required (qw(pool_size chunk_size)) {
        return { status => 'UNKNOWN', reason => "$required unavailable", blocks_operation => 0 }
            if !defined($args{$required}) || $args{$required} !~ /^\d+(?:\.\d+)?$/ || $args{$required} <= 0;
    }

    my $chunks = $args{pool_size} / $args{chunk_size};
    my $ceiling = 15_800_000;
    return {
        status => $chunks > $ceiling ? 'WARN' : 'HEALTHY',
        reason => $chunks > $ceiling
            ? 'chunk count exceeds the 15.8M validation ceiling'
            : 'chunk count is within the 15.8M validation ceiling',
        chunk_count => $chunks,
        blocks_operation => 0,
    };
}

sub evaluate_pool_health {
    my (%args) = @_;
    my $attr = $args{lv_attr};
    my $health = $args{health_status};
    my $check_needed = $args{check_needed};

    return {
        status => 'UNKNOWN', blocks_operation => 1,
        reason => 'thin-pool attributes are unavailable',
    } if !defined($attr) || $attr eq '';

    return {
        status => 'CRITICAL', blocks_operation => 1,
        reason => 'thin pool needs metadata check',
    } if substr($attr, 4, 1) eq 'c'
        || (defined($check_needed) && $check_needed =~ /^(?:1|yes|check_needed)$/i);

    return {
        status => 'CRITICAL', blocks_operation => 1,
        reason => 'thin-pool metadata is read-only',
    } if substr($attr, 8, 1) eq 'M';

    return {
        status => 'CRITICAL', blocks_operation => 1,
        reason => "unexpected thin-pool attributes '$attr'",
    } if $attr !~ /^twi-[-a][o-]t[z-]--$/;

    $health //= '';
    $health =~ s/^\s+|\s+$//g;
    return {
        status => 'CRITICAL', blocks_operation => 1,
        reason => "thin-pool health status is '$health'",
    } if $health ne '' && lc($health) ne 'ok';

    return {
        status => 'HEALTHY', blocks_operation => 0,
        reason => 'thin-pool attributes and metadata state are writable',
    };
}

1;
