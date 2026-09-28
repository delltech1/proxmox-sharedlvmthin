# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmAdmission;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(
    evaluate_latch_acquire evaluate_latch_close evaluate_prepare_ack
    validate_latch_identity validate_executor_record
    evaluate_executor_reserve evaluate_executor_bind evaluate_executor_dispatch
    evaluate_executor_finish evaluate_executor_close
);

sub _blocked {
    my ($reason) = @_;
    return { allowed => 0, action => 'BLOCKED', reason => $reason };
}

sub _exact_bool {
    my ($value) = @_;
    return defined($value) && !ref($value) && "$value" =~ /\A[01]\z/;
}

sub validate_executor_record {
    my ($record) = @_;
    die "executor reservation must be an object\n" if ref($record) ne 'HASH';
    my %allowed = map { $_ => 1 } qw(
        schema kind enrollment_epoch vg_uuid volume object_key transaction
        attempt node boot_id unit code_digest policy_digest state invocation_id
        executor_result
    );
    for my $field (keys %$record) {
        die "unknown executor reservation field '$field'\n" if !$allowed{$field};
    }
    die "unsupported executor reservation schema\n"
        if !defined($record->{schema}) || ref($record->{schema})
        || "$record->{schema}" ne '1';
    die "invalid executor reservation kind\n"
        if ($record->{kind} // '') !~ /\A(?:THICK_EXECUTOR|THICK_EXECUTOR_LAB)\z/;
    for my $field (qw(enrollment_epoch code_digest policy_digest)) {
        die "invalid executor reservation $field\n"
            if ($record->{$field} // '') !~ /\A[a-f0-9]{64}\z/;
    }
    die "invalid executor reservation VG UUID\n"
        if ($record->{vg_uuid} // '')
            !~ /\A[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}\z/;
    die "invalid executor reservation volume\n"
        if ($record->{volume} // '') !~ /\A(?:vm|base)-[1-9][0-9]*-disk-[0-9]+\z/;
    die "invalid executor reservation object key\n"
        if ($record->{object_key} // '') !~ /\A[a-f0-9]{24}\z/;
    for my $field (qw(transaction attempt)) {
        die "invalid executor reservation $field\n"
            if ($record->{$field} // '') !~ /\A[a-f0-9]{32}\z/;
    }
    die "invalid executor reservation node\n"
        if ($record->{node} // '') !~ /\A[A-Za-z0-9][A-Za-z0-9_.-]*\z/;
    die "invalid executor reservation boot ID\n"
        if ($record->{boot_id} // '')
            !~ /\A[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\z/;
    my $unit_prefix = $record->{kind} eq 'THICK_EXECUTOR_LAB'
        ? 'slt-thick-lab-exec-' : 'slt-thick-exec-';
    die "invalid executor reservation unit\n"
        if ($record->{unit} // '') ne "$unit_prefix$record->{attempt}.service";
    die "invalid executor reservation state\n"
        if ($record->{state} // '') !~ /\A(?:RESERVED|BOUND|TERMINAL|UNKNOWN)\z/;
    if ($record->{state} eq 'RESERVED') {
        die "reserved executor already has runtime identity\n"
            if exists($record->{invocation_id}) || exists($record->{executor_result});
    } else {
        die "bound executor has invalid invocation ID\n"
            if ($record->{invocation_id} // '') !~ /\A[a-f0-9]{32}\z/;
    }
    if ($record->{state} eq 'TERMINAL') {
        die "terminal executor has invalid result\n"
            if ($record->{executor_result} // '') !~ /\A(?:SUCCESS|FAILED)\z/;
    } elsif (exists($record->{executor_result})) {
        die "non-terminal executor carries a result\n";
    }
    return 1;
}

sub _executor_identity_equal {
    my ($left, $right) = @_;
    for my $field (qw(
        schema kind enrollment_epoch vg_uuid volume object_key transaction
        attempt node boot_id unit code_digest policy_digest
    )) {
        return 0 if ($left->{$field} // '') ne ($right->{$field} // '');
    }
    return 1;
}

sub evaluate_executor_reserve {
    my (%args) = @_;
    my $requested = $args{requested};
    eval { validate_executor_record($requested); 1 }
        or return _blocked("requested executor reservation is malformed: $@");
    return _blocked('new executor reservation must start in RESERVED state')
        if $requested->{state} ne 'RESERVED';
    return _blocked('executor admission continuity is not positively proven')
        if !_exact_bool($args{continuity_proven}) || $args{continuity_proven} ne '1';
    return _blocked('execution attempt freshness is not positively proven')
        if !_exact_bool($args{attempt_fresh_proven}) || $args{attempt_fresh_proven} ne '1';
    return _blocked('a per-volume executor reservation already exists; same transaction and timeout takeover are forbidden')
        if defined($args{existing});
    return { allowed => 1, action => 'RESERVE_ATOMIC', dispatch_allowed => 0 };
}

sub evaluate_executor_bind {
    my (%args) = @_;
    my $record = $args{record};
    eval { validate_executor_record($record); 1 }
        or return _blocked("executor reservation is malformed: $@");
    return _blocked('only a RESERVED executor may bind an invocation')
        if $record->{state} ne 'RESERVED';
    my $claim = $args{claim};
    return _blocked('executor bind claim is unavailable') if ref($claim) ne 'HASH';
    return _blocked('executor bind identity changed')
        if ref($claim->{identity}) ne 'HASH'
        || !eval { validate_executor_record($claim->{identity}); 1 }
        || !_executor_identity_equal($record, $claim->{identity});
    return _blocked('executor bind claim must describe the exact RESERVED record')
        if ($claim->{identity}->{state} // '') ne 'RESERVED';
    return _blocked('executor invocation ID is invalid')
        if ($claim->{invocation_id} // '') !~ /\A[a-f0-9]{32}\z/;
    return _blocked('executor startup proof is not an exact boolean')
        if !_exact_bool($claim->{startup_proven}) || $claim->{startup_proven} ne '1';
    return {
        allowed => 1, action => 'BIND_ATOMIC', dispatch_allowed => 0,
        record => {%$record, state => 'BOUND', invocation_id => $claim->{invocation_id}},
    };
}

sub evaluate_executor_dispatch {
    my (%args) = @_;
    my ($persisted, $claim) = @args{qw(persisted claim)};
    eval { validate_executor_record($persisted); 1 }
        or return _blocked("persisted executor reservation is malformed: $@");
    return _blocked('dispatch requires an authoritatively persisted BOUND reservation')
        if $persisted->{state} ne 'BOUND';
    return _blocked('executor dispatch claim is unavailable') if ref($claim) ne 'HASH';
    return _blocked('executor dispatch identity changed')
        if ref($claim->{identity}) ne 'HASH'
        || !eval { validate_executor_record($claim->{identity}); 1 }
        || !_executor_identity_equal($persisted, $claim->{identity})
        || ($claim->{identity}->{state} // '') ne 'BOUND'
        || ($claim->{identity}->{invocation_id} // '') ne $persisted->{invocation_id};
    return _blocked('persisted executor binding is not positively confirmed')
        if !_exact_bool($args{binding_persisted}) || $args{binding_persisted} ne '1';
    return { allowed => 1, action => 'DISPATCH_EXACT_INVOCATION', dispatch_allowed => 1 };
}

sub evaluate_executor_finish {
    my (%args) = @_;
    my $record = $args{record};
    eval { validate_executor_record($record); 1 }
        or return _blocked("executor reservation is malformed: $@");
    return _blocked('only a BOUND executor may publish terminal evidence')
        if $record->{state} ne 'BOUND';
    my $evidence = $args{evidence};
    return _blocked('executor terminal evidence is unavailable') if ref($evidence) ne 'HASH';
    return _blocked('executor terminal identity changed')
        if ref($evidence->{identity}) ne 'HASH'
        || !eval { validate_executor_record($evidence->{identity}); 1 }
        || !_executor_identity_equal($record, $evidence->{identity})
        || ($evidence->{identity}->{state} // '') ne 'BOUND'
        || ($evidence->{identity}->{invocation_id} // '') ne $record->{invocation_id};
    for my $field (qw(cgroup_terminal pending_jobs_absent io_terminal storage_postcondition_proven)) {
        return _blocked("executor $field evidence is not an exact boolean")
            if !_exact_bool($evidence->{$field});
    }
    my $complete = !grep { $evidence->{$_} ne '1' }
        qw(cgroup_terminal pending_jobs_absent io_terminal storage_postcondition_proven);
    my $result = $evidence->{executor_result} // '';
    if (!$complete || $result !~ /\A(?:SUCCESS|FAILED)\z/) {
        return {
            allowed => 1, action => 'MARK_UNKNOWN', dispatch_allowed => 0,
            record => {%$record, state => 'UNKNOWN'},
        };
    }
    return {
        allowed => 1, action => 'MARK_TERMINAL', dispatch_allowed => 0,
        record => {%$record, state => 'TERMINAL', executor_result => $result},
    };
}

sub evaluate_executor_close {
    my (%args) = @_;
    my ($current, $expected) = @args{qw(current expected)};
    eval { validate_executor_record($current); 1 }
        or return _blocked("current executor reservation is malformed: $@");
    eval { validate_executor_record($expected); 1 }
        or return _blocked("expected executor reservation is malformed: $@");
    return _blocked('stale executor closer cannot close another attempt or invocation')
        if !_executor_identity_equal($current, $expected)
        || ($current->{invocation_id} // '') ne ($expected->{invocation_id} // '');
    return _blocked('only exact terminal executor evidence permits normal close')
        if $current->{state} ne 'TERMINAL' || $expected->{state} ne 'TERMINAL'
        || ($current->{executor_result} // '') ne ($expected->{executor_result} // '');
    return {
        allowed => 1, action => 'CLOSE_ATOMIC_EXACT', dispatch_allowed => 0,
        expected_record => {%$current},
    };
}

sub validate_latch_identity {
    my ($record) = @_;
    die "admission record must be an object\n" if ref($record) ne 'HASH';
    my %allowed = map { $_ => 1 } qw(
        schema kind transaction cluster_id vg_uuid storage_set node boot_id
        operation executor volume object_key
    );
    for my $field (keys %$record) {
        die "unknown admission identity field '$field'\n" if !$allowed{$field};
    }
    die "unsupported admission schema\n"
        if !defined($record->{schema}) || ref($record->{schema})
        || "$record->{schema}" ne '1';
    die "invalid admission kind\n"
        if !defined($record->{kind})
        || $record->{kind} !~ /\A(?:VG_MUTATION|THICK_ACTIVATE|THICK_PREPARE)\z/;
    die "invalid admission transaction\n"
        if !defined($record->{transaction})
        || $record->{transaction} !~ /\A[a-f0-9]{32}\z/;
    die "invalid admission cluster identity\n"
        if !defined($record->{cluster_id})
        || $record->{cluster_id} !~ /\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\z/;
    die "invalid admission VG UUID\n"
        if !defined($record->{vg_uuid})
        || $record->{vg_uuid} !~ /\A[A-Za-z0-9]{6}(?:-[A-Za-z0-9]{4}){5}-[A-Za-z0-9]{6}\z/;
    die "invalid admission storage-set digest\n"
        if !defined($record->{storage_set})
        || $record->{storage_set} !~ /\A[a-f0-9]{64}\z/;
    die "invalid admission node\n"
        if !defined($record->{node})
        || $record->{node} !~ /\A[A-Za-z0-9][A-Za-z0-9_.-]*\z/;
    die "invalid admission boot ID\n"
        if !defined($record->{boot_id})
        || $record->{boot_id}
            !~ /\A[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\z/;
    die "invalid admission operation\n"
        if !defined($record->{operation})
        || $record->{operation} !~ /\A(?:ALLOC|FREE|SNAPSHOT|SNAPSHOT_DELETE|SNAPSHOT_ROLLBACK|RESIZE|ACTIVATE|DEACTIVATE|MATERIALIZE|RECOVER|AUTOGROW)\z/;
    die "invalid admission executor identity\n"
        if !defined($record->{executor})
        || $record->{executor} ne "slt-admission-$record->{transaction}.service";
    if ($record->{kind} =~ /^THICK_/) {
        die "invalid Thick admission volume identity\n"
            if !defined($record->{volume})
            || $record->{volume} !~ /\Avm-[1-9][0-9]*-disk-[0-9]+\z/;
        die "invalid Thick admission object key\n"
            if !defined($record->{object_key})
            || $record->{object_key} !~ /\A[a-f0-9]{24}\z/;
    } elsif (exists($record->{volume}) || exists($record->{object_key})) {
        die "VG admission contains a volume identity\n";
    }
    return 1;
}

sub _same_identity {
    my ($left, $right) = @_;
    for my $field (qw(
        schema kind transaction cluster_id vg_uuid storage_set node boot_id
        operation executor
    )) {
        return 0 if ($left->{$field} // '') ne ($right->{$field} // '');
    }
    for my $field (qw(volume object_key)) {
        return 0 if defined($left->{$field}) != defined($right->{$field});
        return 0 if defined($left->{$field}) && $left->{$field} ne $right->{$field};
    }
    return 1;
}

sub evaluate_latch_acquire {
    my (%args) = @_;
    my $requested = $args{requested};
    eval { validate_latch_identity($requested); 1 }
        or return _blocked("requested latch identity is malformed: $@");

    if (!defined($args{existing})) {
        return _blocked('control-plane continuity is not positively proven')
            if !_exact_bool($args{control_plane_continuity_proven})
            || $args{control_plane_continuity_proven} ne '1';
        return { allowed => 1, action => 'ACQUIRE_ATOMIC', dispatch_allowed => 0 };
    }

    my $existing = $args{existing};
    eval { validate_latch_identity($existing); 1 }
        or return _blocked("existing latch is empty or malformed: $@");

    return _blocked('an admission latch already exists; overwrite and timeout takeover are forbidden')
        if !_same_identity($existing, $requested);
    return _blocked('exact latch already exists; ordinary invocation cannot redispatch')
        if !$args{explicit_resume};
    return {
        allowed => 1,
        action => 'INSPECT_EXACT_EXISTING',
        dispatch_allowed => 0,
    };
}

sub evaluate_latch_close {
    my (%args) = @_;
    my $record = $args{record};
    eval { validate_latch_identity($record); 1 }
        or return _blocked("latch identity is malformed: $@");
    my $evidence = $args{evidence};
    return _blocked('close evidence is unavailable') if ref($evidence) ne 'HASH';
    return _blocked('complete latch identity is not proven during close')
        if ref($evidence->{identity}) ne 'HASH'
        || !eval { validate_latch_identity($evidence->{identity}); 1 }
        || !_same_identity($record, $evidence->{identity});
    return _blocked('postcondition proof is not an exact boolean')
        if !_exact_bool($evidence->{postcondition_proven});
    return _blocked('storage postcondition is not positively proven')
        if $evidence->{postcondition_proven} ne '1';
    return _blocked('executor termination proof is not an exact boolean')
        if !_exact_bool($evidence->{executor_terminal});
    return _blocked('executor or descendant termination is not positively proven')
        if $evidence->{executor_terminal} ne '1';
    return _blocked('executor result remains ambiguous')
        if ($evidence->{executor_result} // '') !~ /^(?:SUCCESS|FAILED)$/;

    if ($args{recovery}) {
        return _blocked('fencing proof is not an exact boolean')
            if !_exact_bool($evidence->{original_node_fenced});
        return _blocked('recovery close requires confirmed fencing of the original node')
            if $evidence->{original_node_fenced} ne '1';
        return _blocked('storage reconciliation proof is not an exact boolean')
            if !_exact_bool($evidence->{storage_reconciled});
        return _blocked('recovery close requires complete storage reconciliation')
            if $evidence->{storage_reconciled} ne '1';
        return { allowed => 1, action => 'CLOSE_AFTER_FENCED_RECOVERY' };
    }

    return _blocked('normal close node identity changed')
        if ($evidence->{node} // '') ne $record->{node};
    return _blocked('normal close boot identity changed')
        if ($evidence->{boot_id} // '') ne $record->{boot_id};
    return { allowed => 1, action => 'CLOSE_EXACT_NORMAL' };
}

sub evaluate_prepare_ack {
    my (%args) = @_;
    my $reservation = $args{reservation};
    eval { validate_latch_identity($reservation); 1 }
        or return _blocked("PREPARE reservation is malformed: $@");
    return _blocked('peer ACK requires a Thick PREPARE reservation')
        if $reservation->{kind} ne 'THICK_PREPARE';
    my $peer = $args{peer};
    return _blocked('peer evidence is unavailable') if ref($peer) ne 'HASH';
    my $expected_peer = $args{expected_peer};
    return _blocked('expected peer identity is unavailable')
        if ref($expected_peer) ne 'HASH';
    return _blocked('peer identity is malformed')
        if ($peer->{node} // '') !~ /\A[A-Za-z0-9][A-Za-z0-9_.-]*\z/
        || ($peer->{boot_id} // '')
            !~ /\A[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}\z/;
    return _blocked('peer identity changed since participant selection')
        if ($expected_peer->{node} // '') ne $peer->{node}
        || ($expected_peer->{boot_id} // '') ne $peer->{boot_id};
    return _blocked('peer guard is not established for the exact reservation')
        if ($peer->{guard_established} // '') ne 'YES'
        || ($peer->{reservation_transaction} // '') ne $reservation->{transaction}
        || ($peer->{volume} // '') ne $reservation->{volume}
        || ($peer->{object_key} // '') ne $reservation->{object_key}
        || ($peer->{storage_set} // '') ne $reservation->{storage_set};
    for my $field (qw(inventory_complete mapper_present head_path_present clone_dependency_present)) {
        return _blocked("peer $field evidence is not an exact boolean")
            if !_exact_bool($peer->{$field});
    }
    return _blocked('peer inventory is incomplete or ambiguous')
        if $peer->{inventory_complete} ne '1';
    return _blocked('peer activation executor is unresolved')
        if ($peer->{activation_executor} // '') ne 'ABSENT';
    return _blocked('peer has a mapper or alias for the protected volume')
        if $peer->{mapper_present} ne '0';
    return _blocked('peer has a direct active path to the signed HEAD')
        if $peer->{head_path_present} ne '0';
    return _blocked('peer has unresolved dm-clone dependencies')
        if $peer->{clone_dependency_present} ne '0';
    return {
        allowed => 1,
        action => 'ACK_GUARDED_ABSENCE',
        node => $peer->{node},
        boot_id => $peer->{boot_id},
        transaction => $reservation->{transaction},
    };
}

1;
