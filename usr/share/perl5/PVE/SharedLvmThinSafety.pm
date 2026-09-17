# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinSafety;

use strict;
use warnings;
use Digest::SHA qw(sha256_hex);

sub evaluate_allocation_target {
    my (%args) = @_;
    for my $required (qw(mode fixed_gib requested_kib used_bytes current_pool_bytes)) {
        die "missing $required\n" if !defined($args{$required});
    }

    my $mode = $args{mode};
    die "invalid allocation mode '$mode'\n"
        if $mode !~ /^(?:fixed|proportional|elastic|full)$/;
    for my $numeric (qw(fixed_gib requested_kib used_bytes current_pool_bytes)) {
        die "invalid $numeric\n"
            if $args{$numeric} !~ /^\d+$/;
    }
    die "invalid fixed_gib\n" if $args{fixed_gib} < 1;

    my $usage_known = $args{usage_known} // 1;
    die "invalid usage_known\n" if $usage_known !~ /^(?:0|1)$/;

    my $gib = 1024 * 1024 * 1024;
    my $requested = $args{requested_kib} * 1024;
    my $fixed = $args{fixed_gib} * $gib;
    my $target;

    if (!$usage_known && $args{current_pool_bytes} > 0) {
        die "full allocation requires known thin-pool usage\n" if $mode eq 'full';
        if ($mode eq 'proportional') {
            die "missing percent\n" if !defined($args{percent});
            die "invalid percent\n"
                if $args{percent} !~ /^\d+$/ || $args{percent} < 1 || $args{percent} > 100;
        } elsif ($mode eq 'elastic') {
            my $headroom_gib = $args{headroom_gib} // 64;
            die "invalid headroom_gib\n"
                if $headroom_gib !~ /^\d+$/ || $headroom_gib < 1;
        }
        return {
            target_bytes => $args{current_pool_bytes},
            growth_bytes => 0,
            requested_bytes => $requested,
            burst_guarantee => $mode eq 'fixed' ? 'NONE' : 'UNKNOWN_INACTIVE_NO_GROWTH',
            headroom_bytes => undef,
            usage_known => 0,
        };
    }

    if ($mode eq 'fixed') {
        $target = $fixed;
    } elsif ($mode eq 'proportional') {
        die "missing percent\n" if !defined($args{percent});
        die "invalid percent\n"
            if $args{percent} !~ /^\d+$/ || $args{percent} < 1 || $args{percent} > 100;
        my $proportional = int(($requested * $args{percent} + 99) / 100);
        my $headroom = $proportional > $fixed ? $proportional : $fixed;
        $target = $args{used_bytes} + $headroom;
    } elsif ($mode eq 'elastic') {
        my $headroom_gib = $args{headroom_gib} // 64;
        die "invalid headroom_gib\n"
            if $headroom_gib !~ /^\d+$/ || $headroom_gib < 1;
        my $headroom = $headroom_gib * $gib;
        $headroom = $requested if $requested < $headroom;
        my $bootstrap = $gib;
        $headroom = $bootstrap if $headroom < $bootstrap;
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
            : ($mode eq 'proportional' || $mode eq 'elastic') ? 'BOUNDED' : 'NONE',
        headroom_bytes => $mode eq 'elastic' ? $target - $args{used_bytes} : undef,
        usage_known => $usage_known ? 1 : 0,
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

sub evaluate_capacity_stability {
    my (%args) = @_;
    my @required = qw(
        data_free_bytes metadata_free_units
        data_rate_bytes_per_second metadata_rate_units_per_second
        extend_latency_p99_seconds lock_latency_p99_seconds reaction_margin_seconds
    );

    for my $required (@required) {
        return {
            status => 'UNKNOWN', safe => 0, grow_eligible => 0,
            blocks_operation => 1, reason => "missing $required",
        } if !defined($args{$required});
        return {
            status => 'UNKNOWN', safe => 0, grow_eligible => 0,
            blocks_operation => 1, reason => "invalid $required",
        } if $args{$required} !~ /^\d+(?:\.\d+)?$/;
    }

    my $rates_known = $args{rates_known} // 1;
    return {
        status => 'UNKNOWN', safe => 0, grow_eligible => 0,
        blocks_operation => 1, reason => 'invalid rates_known',
    } if $rates_known !~ /^(?:0|1)$/;
    return {
        status => 'UNKNOWN', safe => 0, grow_eligible => 0,
        blocks_operation => 1, reason => 'allocation rates are not established',
    } if !$rates_known;

    my $horizon = 0 + $args{extend_latency_p99_seconds}
        + $args{lock_latency_p99_seconds}
        + $args{reaction_margin_seconds};
    return {
        status => 'UNKNOWN', safe => 0, grow_eligible => 0,
        blocks_operation => 1, reason => 'safety horizon must be greater than zero',
    } if $horizon <= 0;

    my $data_rate = 0 + $args{data_rate_bytes_per_second};
    my $metadata_rate = 0 + $args{metadata_rate_units_per_second};
    my $data_runway = $data_rate > 0
        ? (0 + $args{data_free_bytes}) / $data_rate
        : undef;
    my $metadata_runway = $metadata_rate > 0
        ? (0 + $args{metadata_free_units}) / $metadata_rate
        : undef;

    # A positively observed zero rate means that dimension is not currently
    # consuming runway.  It is represented as undef/UNBOUNDED instead of an
    # invented large number so callers cannot mistake it for a measurement.
    my $data_below = defined($data_runway) && $data_runway <= $horizon;
    my $metadata_below = defined($metadata_runway) && $metadata_runway <= $horizon;
    my $grow_eligible = $data_below || $metadata_below ? 1 : 0;
    my $limiting = $data_below && $metadata_below ? 'DATA_AND_METADATA'
        : $data_below ? 'DATA'
        : $metadata_below ? 'METADATA'
        : 'NONE';

    return {
        status => $grow_eligible ? 'GROW_REQUIRED' : 'STABLE',
        safe => 1,
        grow_eligible => $grow_eligible,
        blocks_operation => 0,
        reason => $grow_eligible
            ? "predicted $limiting runway reached the safety horizon"
            : 'predicted data and metadata runway exceed the safety horizon',
        limiting_dimension => $limiting,
        safety_horizon_seconds => $horizon,
        data_runway_seconds => $data_runway,
        metadata_runway_seconds => $metadata_runway,
        data_runway_state => defined($data_runway) ? 'BOUNDED' : 'UNBOUNDED',
        metadata_runway_state => defined($metadata_runway) ? 'BOUNDED' : 'UNBOUNDED',
    };
}

sub build_thin_transaction_fingerprint {
    my (%args) = @_;
    my @required = qw(
        pool_uuid metadata_uuid data_uuid transaction_id
        owner_node owner_epoch live_table_hash dependency_hash metadata_mode
    );
    for my $required (@required) {
        die "missing fingerprint field $required\n" if !defined($args{$required});
        die "fingerprint field $required contains unsafe characters\n"
            if $args{$required} !~ /^[A-Za-z0-9_.:+-]+$/;
    }
    die "invalid dm-thin transaction ID\n"
        if $args{transaction_id} !~ /^\d+$/;
    die "invalid owner epoch\n"
        if $args{owner_epoch} !~ /^[0-9a-f]{32}$/;
    die "invalid live table hash\n"
        if $args{live_table_hash} !~ /^(?:ABSENT|[0-9a-f]{64})$/;
    die "invalid dependency hash\n"
        if $args{dependency_hash} !~ /^(?:ABSENT|[0-9a-f]{64})$/;
    die "invalid metadata mode\n"
        if $args{metadata_mode} !~ /^(?:rw|ro|unknown)$/;

    my $canonical = join("\n", map { "$_=$args{$_}" } @required) . "\n";
    return {
        version => 1,
        canonical => $canonical,
        digest => sha256_hex($canonical),
        evidence => { map { $_ => $args{$_} } @required },
    };
}

sub compare_thin_transaction_fingerprint {
    my ($expected, $observed) = @_;
    return {
        status => 'UNKNOWN', match => 0, blocks_operation => 1,
        reason => 'expected or observed fingerprint is unavailable',
    } if ref($expected) ne 'HASH' || ref($observed) ne 'HASH';
    return {
        status => 'UNKNOWN', match => 0, blocks_operation => 1,
        reason => 'unsupported fingerprint version',
    } if ($expected->{version} // 0) != 1 || ($observed->{version} // 0) != 1;
    return {
        status => 'UNKNOWN', match => 0, blocks_operation => 1,
        reason => 'fingerprint digest is malformed',
    } if ($expected->{digest} // '') !~ /^[0-9a-f]{64}$/
        || ($observed->{digest} // '') !~ /^[0-9a-f]{64}$/;
    return {
        status => 'MISMATCH', match => 0, blocks_operation => 1,
        reason => 'thin transaction fingerprint changed unexpectedly',
    } if $expected->{digest} ne $observed->{digest};
    return {
        status => 'MATCH', match => 1, blocks_operation => 0,
        reason => 'thin transaction fingerprint matches exact recorded evidence',
    };
}

sub evaluate_readonly_metadata_validation {
    my (%args) = @_;
    for my $required (qw(snapshot_reserved thin_check_completed thin_check_ok snapshot_released)) {
        return {
            status => 'UNKNOWN', blocks_operation => 1,
            reason => "missing metadata validation evidence $required",
        } if !defined($args{$required}) || $args{$required} !~ /^(?:0|1)$/;
    }
    return {
        status => 'UNKNOWN', blocks_operation => 1,
        reason => 'reserved metadata snapshot was not positively created',
    } if !$args{snapshot_reserved};
    return {
        status => 'UNKNOWN', blocks_operation => 1,
        reason => 'bounded thin_check did not complete',
    } if !$args{thin_check_completed};
    return {
        status => 'CRITICAL', blocks_operation => 1,
        reason => 'thin_check reported invalid metadata; no repair attempted',
    } if !$args{thin_check_ok};
    return {
        status => 'UNKNOWN', blocks_operation => 1,
        reason => 'reserved metadata snapshot release is unproven',
    } if !$args{snapshot_released};
    return {
        status => 'PASS', blocks_operation => 0,
        reason => 'bounded read-only metadata validation and release completed',
    };
}

sub evaluate_leaseguard_activation {
    my (%args) = @_;

    my $enabled = $args{enabled} // 0;
    die "invalid LeaseGuard enabled flag\n" if $enabled !~ /^(?:0|1)$/;
    return {
        status => 'DISABLED', blocks_operation => 0,
        reason => 'LeaseGuard is not enabled for this storage',
    } if !$enabled;

    my @required = qw(
        lease_area_identity sanlock_daemon lockspace_joined resource_held
        owner_matches quorum storage_identity fresh_pool_runtime
    );
    my @missing;
    my @negative;
    for my $field (@required) {
        if (!defined($args{$field}) || $args{$field} !~ /^(?:0|1)$/) {
            push @missing, $field;
        } elsif (!$args{$field}) {
            push @negative, $field;
        }
    }
    return {
        status => 'UNKNOWN', blocks_operation => 1,
        reason => 'LeaseGuard evidence missing or malformed: ' . join(',', @missing),
    } if @missing;
    return {
        status => 'REFUSED', blocks_operation => 1,
        reason => 'LeaseGuard positive evidence failed: ' . join(',', @negative),
    } if @negative;

    return {
        status => 'PASS', blocks_operation => 0,
        reason => 'exclusive pool lease and all activation evidence are positive',
    };
}

sub evaluate_pve_native_leaseguard {
    my (%args) = @_;

    my $enabled = $args{enabled} // 0;
    die "invalid PVE-native LeaseGuard enabled flag\n"
        if $enabled !~ /^(?:0|1)$/;
    return {
        status => 'DISABLED', blocks_operation => 0,
        reason => 'PVE-native LeaseGuard is not enabled for this storage',
    } if !$enabled;

    for my $required (qw(quorum storage_identity local_runtime_absent)) {
        return {
            status => 'UNKNOWN', blocks_operation => 1,
            reason => "missing PVE-native LeaseGuard evidence $required",
        } if !defined($args{$required}) || $args{$required} !~ /^(?:0|1)$/;
        return {
            status => 'REFUSED', blocks_operation => 1,
            reason => "PVE-native LeaseGuard evidence failed: $required",
        } if !$args{$required};
    }

    my $nodes = $args{remote_nodes};
    return {
        status => 'UNKNOWN', blocks_operation => 1,
        reason => 'remote kernel-mapper evidence is unavailable',
    } if ref($nodes) ne 'ARRAY' || !@$nodes;

    my %seen;
    for my $node (@$nodes) {
        return {
            status => 'UNKNOWN', blocks_operation => 1,
            reason => 'remote kernel-mapper evidence is malformed',
        } if ref($node) ne 'HASH'
            || ($node->{node} // '') !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/
            || ($node->{state} // '') !~ /^(?:ABSENT|PRESENT|UNKNOWN)$/
            || $seen{$node->{node}}++;
        return {
            status => 'REFUSED', blocks_operation => 1,
            reason => "remote thin-pool mapper is present on '$node->{node}'",
        } if $node->{state} eq 'PRESENT';
        return {
            status => 'UNKNOWN', blocks_operation => 1,
            reason => "remote thin-pool mapper absence is unproven on '$node->{node}'",
        } if $node->{state} eq 'UNKNOWN';
    }

    return {
        status => 'PASS', blocks_operation => 0,
        reason => 'all configured peer kernels positively report the exact thin-pool mapper absent',
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
