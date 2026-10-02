package PVE::SharedLvmThinVMDestroy;

use strict;
use warnings;
use JSON::PP ();
use Digest::SHA qw(sha1_hex sha256_hex);

# Executor only: deliberately not a CLI or a qualified production dispatcher.
# The trusted caller must supply durable create-only journal operations and
# exact, read-only runtime/plan/inventory verifiers. No permissive defaults.
# verify_runtime must prove the local VM owner, boot and exact loaded tuple.
# prepare_plan must read the raw config and enumerate ALL native effects
# (including absence of hidden fleecing/IPAM work),
# or refuse. observe_plan proves complete device-scoped persistent + kernel
# absence and foreign-object preservation. journal must reject transaction
# replay durably; these obligations are not implemented by this executor.
sub new {
    my ($class, %args) = @_;
    for my $name (qw(verify_runtime prepare_plan observe_plan observe_finalization journal)) {
        die "missing VM destroy dependency '$name'\n" if ref($args{$name}) ne 'CODE';
    }
    return bless \%args, $class;
}

sub _validate_finalization_state {
    my ($vmid, $state) = @_;
    die "VM destroy finalization state is UNKNOWN\n"
        if ref($state) ne 'HASH' || ($state->{vmid} // '') ne "$vmid";
    for my $name (qw(acl firewall config)) {
        die "VM destroy $name postcondition is UNKNOWN\n"
            if ($state->{$name} // '') !~ /^(?:ABSENT|PRESENT)$/;
    }
    if ($state->{config} eq 'PRESENT') {
        die "VM destroy present config identity is UNKNOWN\n"
            if ($state->{config_sha256} // '') !~ /^[a-f0-9]{64}$/;
    }
    return $state;
}

sub _finalization_state {
    my ($self, $vmid, $receipt) = @_;
    my $copy = JSON::PP->new->decode(_canonical($receipt));
    return _validate_finalization_state($vmid, $self->{observe_finalization}->($vmid, $copy));
}

sub _same_receipt_core {
    my ($left, $right) = @_;
    my @keys = qw(schema txid vmid config runtime plan);
    return _canonical({ map { $_ => $left->{$_} } @keys })
        eq _canonical({ map { $_ => $right->{$_} } @keys });
}

sub _canonical {
    return JSON::PP->new->canonical->encode($_[0]);
}

sub _load_pve {
    require PVE::Cluster;
    require PVE::QemuConfig;
    require PVE::QemuServer;
    require PVE::QemuServer::Network;
    require PVE::Storage;
    require PVE::HA::Config;
    require PVE::ReplicationConfig;
    require PVE::AccessControl;
    require PVE::Firewall;
}

sub _is_zombie {
    my ($conf) = @_;
    return 0 if ref($conf) ne 'HASH';
    my %fields = %$conf;
    # These are parser-derived fields, not stored VM settings.
    if (exists($fields{digest})) {
        return 0 if ($fields{digest} // '') !~ /^[a-f0-9]{40}$/;
        delete $fields{digest};
    }
    for my $key (qw(snapshots pending special-sections)) {
        next if !exists($fields{$key});
        return 0 if ref($fields{$key}) ne 'HASH' || keys %{$fields{$key}};
        delete $fields{$key};
    }
    return _canonical(\%fields) eq _canonical({ lock => 'destroyed' });
}

sub _config_volids {
    my ($vmid, $conf, $storecfg) = @_;
    die "VM destroy config inventory is unknown\n" if ref($conf) ne 'HASH';
    die "VM destroy refuses template/protection\n" if $conf->{template} || $conf->{protection};
    die "VM destroy refuses config lock\n" if exists($conf->{lock});
    die "VM destroy refuses pending configuration\n"
        if exists($conf->{pending}) && (ref($conf->{pending}) ne 'HASH' || keys %{$conf->{pending}});
    die "VM destroy snapshot inventory is unknown\n"
        if exists($conf->{snapshots}) && ref($conf->{snapshots}) ne 'HASH';
    my @sections = ($conf, values %{$conf->{snapshots} // {}});
    my %volids;
    for my $section (@sections) {
        die "VM destroy section is unknown\n" if ref($section) ne 'HASH';
        for my $key (keys %$section) {
            # Native qemu-server destroy performs SDN/IPAM release before
            # publishing the zombie config, and upstream deliberately catches
            # some failures. Until that external effect has its own durable
            # plan and postcondition, any NIC makes guarded destroy unsafe.
            # Refuse before PREPARED/native dispatch; never infer safety from
            # bridge/model syntax or from an empty IPAM configuration.
            die "VM destroy refuses unjournalled network/IPAM side effects\n"
                if $key =~ /^net\d+$/;
            die "VM destroy refuses unfinished/special/fleecing configuration\n"
                if $key eq 'snapstate' || $key eq 'lock' || $key =~ /fleec/i
                || $key =~ /^special:/;
            die "VM destroy refuses special configuration\n"
                if $key eq 'special-sections'
                && (ref($section->{$key}) ne 'HASH' || keys %{$section->{$key}});
            next if $key !~ /^(?:(?:ide|sata|scsi|virtio|unused)\d+|efidisk0|tpmstate0|vmstate)$/;
            my $drive = PVE::QemuConfig->parse_volume($key, $section->{$key}, 1);
            die "VM destroy volume parse is ambiguous for '$key'\n"
                if ref($drive) ne 'HASH' || !defined($drive->{file});
            # Match native destroy: cloud-init is NOT excluded as an optical
            # drive; ordinary optical media and host passthrough are NO_EFFECT.
            next if PVE::QemuServer::drive_is_cdrom($drive, 1)
                || $drive->{file} =~ m{^/};
            my ($sid, $name) = $drive->{file} =~ /^([A-Za-z0-9][A-Za-z0-9_.-]*):([^\s,]+)$/;
            die "VM destroy refuses unknown storage or foreign volume\n"
                if !defined($sid) || ref($storecfg->{ids}->{$sid}) ne 'HASH'
                || ($storecfg->{ids}->{$sid}->{type} // '') ne 'sharedlvmthin'
                || $name !~ /^vm-\Q$vmid\E-(?:disk-\d+|state-[A-Za-z0-9][A-Za-z0-9_.-]*|cloudinit)$/;
            die "VM destroy vmstate identity is not exact\n"
                if $key eq 'vmstate' && $name !~ /^vm-\Q$vmid\E-state-[A-Za-z0-9][A-Za-z0-9_.-]*$/;
            $volids{$drive->{file}} = 1;
        }
    }
    die "VM destroy refuses special snapshot section\n"
        if grep { /^(?:PENDING|special:)/ } keys %{$conf->{snapshots} // {}};
    return [sort keys %volids];
}

sub _validate_plan {
    my ($vmid, $conf, $storecfg, $volids, $plan) = @_;
    die "VM destroy plan does not bind exact raw/parsed config\n"
        if ref($plan) ne 'HASH' || !defined($plan->{raw_config}) || ref($plan->{raw_config})
        || !length($plan->{raw_config}) || length($plan->{raw_config}) > 4 * 1024 * 1024
        || ($plan->{config_sha256} // '') ne sha256_hex($plan->{raw_config})
        || ($conf->{digest} // '') ne sha1_hex($plan->{raw_config})
        || ($plan->{parsed_config_sha256} // '') ne sha256_hex(_canonical($conf));
    my $parsed = PVE::QemuServer::parse_vm_config(
        "/etc/pve/qemu-server/$vmid.conf", $plan->{raw_config}, 1,
    );
    die "VM destroy raw config does not parse to locked config\n"
        if ref($parsed) ne 'HASH' || _canonical($parsed) ne _canonical($conf);
    die "VM destroy plan volume inventory mismatch\n"
        if ref($plan->{volids}) ne 'ARRAY' || _canonical($plan->{volids}) ne _canonical($volids)
        || ref($plan->{volumes}) ne 'ARRAY';
    return _validate_v3_plan($vmid, $storecfg, $volids, $plan)
        if ($plan->{schema} // '') eq 'sharedlvmthin-vm-destroy-executor-plan/v3';
    return _validate_root_plan($vmid, $storecfg, $volids, $plan)
        if ($plan->{schema} // '') eq 'sharedlvmthin-vm-destroy-executor-plan/v2';
    die "unsupported VM destroy plan schema\n"
        if defined($plan->{schema}) && $plan->{schema} ne 'sharedlvmthin-vm-destroy-executor-plan/v1';
    my (%volumes, %ids, %identities, %names);
    my %expected = map { $_ => 1 } @$volids;
    for my $volume (@{$plan->{volumes}}) {
        die "VM destroy omitted/duplicate/foreign volume plan\n"
            if ref($volume) ne 'HASH' || !defined($volume->{volid})
            || !$expected{$volume->{volid}} || $volumes{$volume->{volid}}++;
        my ($sid) = split /:/, $volume->{volid}, 2;
        my $scfg = $storecfg->{ids}->{$sid};
        my $root = $volume->{root};
        die "VM destroy untyped or wrong root\n"
            if ref($root) ne 'HASH' || ($root->{type} // '') ne 'lvm-vg'
            || ($root->{name} // '') !~ /^[A-Za-z0-9+_.-]+$/
            || ($root->{uuid} // '') !~ /^[A-Za-z0-9-]+$/
            || $root->{name} ne ($scfg->{'slt-vgname'} // '')
            || $root->{uuid} ne ($scfg->{'slt-expected-vg-uuid'} // '')
            || ($volume->{kind} // '') !~ /^(?:thin|thick|lazy)$/;
        my $mode = $scfg->{'slt-allocation-mode'} // 'thin';
        die "VM destroy plan layout mismatch\n"
            if ($mode eq 'thin' && $volume->{kind} ne 'thin')
            || ($mode eq 'thick-generations' && $volume->{kind} ne 'thick')
            || ($mode eq 'thick-generations-lazy' && $volume->{kind} eq 'thin')
            || $mode !~ /^(?:thin|thick-generations|thick-generations-lazy)$/;
        die "VM destroy missing object/runtime plan\n"
            if ref($volume->{objects}) ne 'ARRAY' || !@{$volume->{objects}}
            || ref($volume->{runtime_ids}) ne 'ARRAY';
        for my $group (['objects', 'lvm-lv'], ['runtime_ids', 'dm']) {
            for my $object (@{$volume->{$group->[0]}}) {
                die "VM destroy untyped or swapped object\n"
                    if ref($object) ne 'HASH' || ($object->{type} // '') ne $group->[1]
                    || ($object->{volid} // '') ne $volume->{volid}
                    || ($object->{name} // '') !~ /^[A-Za-z0-9+_.-]+$/
                    || ($object->{uuid} // '') !~ /^[A-Za-z0-9-]+$/;
                my $identity = $object->{type} eq 'lvm-lv'
                    ? "lvm:$root->{uuid}:$object->{uuid}" : "dm:$object->{uuid}";
                my $name = "$object->{type}:$root->{uuid}:$object->{name}";
                die "VM destroy duplicate or incorrect object/runtime identity\n"
                    if ($object->{id} // '') ne $identity || $ids{$identity}++
                    || $identities{"$object->{type}:$object->{uuid}"}++ || $names{$name}++;
            }
        }
    }
    die "VM destroy omitted volume plan\n" if keys(%volumes) != @$volids;
    return [sort keys %ids];
}

sub _validate_v3_plan {
    my ($vmid, $storecfg, $volids, $plan) = @_;
    require PVE::SharedLvmThinVMDestroyPlanV3;
    my $rebuilt = PVE::SharedLvmThinVMDestroyPlanV3->validate($plan);
    die "VM destroy v3 executor context mismatch\n"
        if ($rebuilt->{authority} // '') ne 'NONE'
        || ($rebuilt->{vmid} // '') ne "$vmid"
        || ($rebuilt->{storecfg_sha256} // '') ne sha256_hex(_canonical($storecfg))
        || _canonical($rebuilt->{volids}) ne _canonical($volids)
        || ref($rebuilt->{object_ids}) ne 'ARRAY';
    return [@{$rebuilt->{object_ids}}];
}

sub _validate_root_plan {
    my ($vmid, $storecfg, $volids, $plan) = @_;
    die "VM destroy normalized root authority/identity mismatch\n"
        if ($plan->{authority} // '') ne 'NONE' || ($plan->{vmid} // '') ne "$vmid"
        || ref($plan->{adapter_inputs}) ne 'HASH' || ($plan->{adapter_inputs}->{plan_version} // '') ne '2'
        || ref($plan->{roots}) ne 'ARRAY';
    # V2 is a lossless normalization of a fully validated root-closure model,
    # not a second independently invented inventory schema. Rebuild validates
    # ownership, required members, snapshot origins and exact derived leaves.
    require PVE::SharedLvmThinVMDestroyPlanAdapter;
    my $rebuilt = PVE::SharedLvmThinVMDestroyPlanAdapter->adapt_plan(%{$plan->{adapter_inputs}});
    die "VM destroy normalized root closure changed\n" if _canonical($rebuilt) ne _canonical($plan);
    my (%roots, %volumes, %ids, %uuids, %names);
    my %expected = map { $_ => 1 } @$volids;
    for my $root (@{$plan->{roots}}) {
        die "VM destroy normalized root identity is ambiguous\n"
            if ref($root) ne 'HASH' || !defined($root->{id}) || $roots{$root->{id}}++
            || ($root->{owner_vmid} // '') ne "$vmid" || ref($root->{vg}) ne 'HASH'
            || ref($root->{volids}) ne 'ARRAY' || !@{$root->{volids}};
        for my $volid (@{$root->{volids}}) {
            die "VM destroy normalized volume coverage mismatch\n"
                if !$expected{$volid} || $volumes{$volid}++;
            my ($sid) = split /:/, $volid, 2;
            my $scfg = $storecfg->{ids}->{$sid};
            die "VM destroy normalized root storage mismatch\n"
                if ref($scfg) ne 'HASH' || ($scfg->{type} // '') ne 'sharedlvmthin'
                || $root->{vg}->{name} ne ($scfg->{'slt-vgname'} // '')
                || $root->{vg}->{uuid} ne ($scfg->{'slt-expected-vg-uuid'} // '')
                || ($scfg->{'slt-expected-pv-uuid'} // '') ne $plan->{adapter_inputs}->{expected_scope}->{pv_uuid}
                || ($scfg->{'slt-expected-wwid'} // '') ne $plan->{adapter_inputs}->{expected_scope}->{wwid};
            my $mode = $scfg->{'slt-allocation-mode'} // 'thin';
            die "VM destroy normalized layout mismatch\n"
                if ($mode eq 'thin' && $root->{kind} ne 'thin')
                || ($mode eq 'thick-generations' && $root->{kind} ne 'thick')
                || ($mode eq 'thick-generations-lazy' && $root->{kind} eq 'thin')
                || $mode !~ /^(?:thin|thick-generations|thick-generations-lazy)$/;
        }
        for my $object (@{$root->{objects}}, @{$root->{runtime_ids}}) {
            my $physical_name = $object->{type} eq 'dm' ? "dm:$object->{name}"
                : "lvm:$root->{vg}->{uuid}:$object->{name}";
            die "VM destroy duplicate normalized physical identity\n"
                if $ids{$object->{id}}++ || $uuids{"$object->{type}:$object->{uuid}"}++
                || $names{$physical_name}++;
        }
    }
    die "VM destroy normalized plan omits volume/root members\n"
        if keys(%volumes) != @$volids || _canonical([sort keys %ids]) ne _canonical($plan->{object_ids});
    return [sort keys %ids];
}

sub _record {
    my ($self, $stage, $receipt) = @_;
    my $ack = $self->{journal}->($stage, JSON::PP->new->decode(_canonical($receipt)));
    die "VM destroy journal did not confirm '$stage'\n"
        if !defined($ack) || ref($ack) || "$ack" ne '1';
}

sub _assert_finalization_identity {
    my ($state, $zombie) = @_;
    # ACL/firewall operations are VMID-scoped, not config-CAS operations. A
    # positively observed replacement config must block BEFORE either effect,
    # not merely before destroy_config. The native VM lock is still required;
    # these observations do not constitute a fencing mechanism.
    if ($state->{config} eq 'PRESENT') {
        die "VM destroy zombie identity changed during finalization\n"
            if $state->{config_sha256} ne sha256_hex(_canonical($zombie));
    } else {
        die "VM destroy config absent with remaining VM-scoped state; VMID reuse is ambiguous\n"
            if $state->{acl} ne 'ABSENT' || $state->{firewall} ne 'ABSENT';
    }
}

sub _run_finalization {
    my ($self, $vmid, $receipt, $evidence, $zombie) = @_;
    my @effects = (
        [acl => 'remove_vm_access', sub { PVE::AccessControl::remove_vm_access($vmid) }],
        [firewall => 'remove_vmfw_conf', sub { PVE::Firewall::remove_vmfw_conf($vmid) }],
    );
    for my $effect (@effects) {
        my ($field, $name, $code) = @$effect;
        my $state = $self->_finalization_state($vmid, $receipt);
        _assert_finalization_identity($state, $zombie);
        next if $state->{$field} eq 'ABSENT';
        $evidence->{$field} = 'INTENT';
        $code->();
        $state = $self->_finalization_state($vmid, $receipt);
        _assert_finalization_identity($state, $zombie);
        die "VM destroy $field absence is UNKNOWN after $name\n"
            if $state->{$field} ne 'ABSENT';
        $evidence->{$field} = 'ABSENT_VERIFIED';
    }

    my $state = $self->_finalization_state($vmid, $receipt);
    _assert_finalization_identity($state, $zombie);
    if ($state->{config} eq 'PRESENT') {
        die "VM destroy zombie identity changed during finalization\n"
            if $state->{config_sha256} ne sha256_hex(_canonical($zombie));
        $evidence->{config_remove} = 'INTENT';
        PVE::QemuConfig->destroy_config($vmid);
        $state = $self->_finalization_state($vmid, $receipt);
        _assert_finalization_identity($state, $zombie);
        die "VM destroy config absence is UNKNOWN after destroy_config\n"
            if $state->{config} ne 'ABSENT';
        $evidence->{config_remove} = 'ABSENT_VERIFIED';
    }
    die "VM destroy ACL/firewall absence changed before completion\n"
        if $state->{acl} ne 'ABSENT' || $state->{firewall} ne 'ABSENT';
}

sub _completion_context {
    my ($history, $vmid, $txid) = @_;
    die "VM destroy completion needs exact durable FINALIZING history\n"
        if ref($history) ne 'HASH' || ($history->{durable} // '') ne '1'
        || ($history->{stage} // '') ne 'FINALIZING'
        || ($history->{request_id} // '') ne $txid
        || ($history->{last_sha256} // '') !~ /\A[0-9a-f]{64}\z/
        || ref($history->{records}) ne 'ARRAY'
        || @{$history->{records}} < 4 || @{$history->{records}} >= 32;
    my @keys = qw(schema txid vmid config runtime plan);
    my ($context, $stage);
    my %next = (PREPARED => 'DISPATCHED', DISPATCHED => 'STORAGE_ABSENT',
        STORAGE_ABSENT => 'FINALIZING', FINALIZING => 'FINALIZING');
    for my $row (@{$history->{records}}) {
        die "VM destroy completion receipt is malformed\n"
            if ref($row) ne 'HASH' || ref($row->{receipt}) ne 'HASH';
        my $receipt = $row->{receipt};
        my $expected = defined($stage) ? $next{$stage} : 'PREPARED';
        die "VM destroy completion history stage/identity mismatch\n"
            if !defined($expected) || ($row->{stage} // '') ne $expected
            || ($receipt->{txid} // '') ne $txid || ($receipt->{vmid} // '') ne "$vmid"
            || ($receipt->{schema} // '') ne '1' || ref($receipt->{evidence}) ne 'HASH';
        my $current = { schema => 'sharedlvmthin-vm-destroy-executor-core/v1',
            receipt => { map { $_ => $receipt->{$_} } @keys } };
        $context //= $current;
        die "VM destroy completion immutable context changed\n" if _canonical($current) ne _canonical($context);
        $stage = $row->{stage};
    }
    my $digest = sha256_hex(JSON::PP->new->canonical->ascii->encode($context));
    die "VM destroy completion context digest mismatch\n"
        if $stage ne 'FINALIZING' || ($history->{context_sha256} // '') ne $digest;
    return JSON::PP->new->decode(_canonical($context));
}

sub _completion_observation {
    my ($proof, $context, $head, $txid) = @_;
    die "VM destroy completion requires absent-only OBSERVED proof\n"
        if ref($proof) ne 'HASH' || ($proof->{status} // '') ne 'OBSERVED'
        || ($proof->{purpose} // '') ne 'FINALIZATION_OBSERVATION_ONLY'
        || ($proof->{authority} // '') ne 'NONE'
        || ref($proof->{recovery}) ne 'HASH'
        || join(',', sort keys %{$proof->{recovery}}) ne 'context_sha256,journal_head_sha256,txid'
        || ($proof->{recovery}->{txid} // '') ne $txid
        || ($proof->{recovery}->{journal_head_sha256} // '') ne $head
        || ($proof->{recovery}->{context_sha256} // '') ne
            sha256_hex(JSON::PP->new->canonical->ascii->encode($context));
    my $runtime = JSON::PP->new->decode(_canonical($proof));
    delete @$runtime{qw(purpose recovery)};
    $runtime->{status} = 'QUALIFIED'; # identity comparison only, never returned
    die "VM destroy completion runtime identity mismatch\n"
        if _canonical($runtime) ne _canonical($context->{receipt}->{runtime});
}

sub complete_finalizing_absent {
    my ($self, $vmid, $txid) = @_;
    die "VM destroy requires root\n" if $> != 0 || $< != 0;
    die "invalid VM destroy completion identity\n"
        if !defined($vmid) || ref($vmid) || $vmid !~ /\A[1-9][0-9]{0,8}\z/
        || !defined($txid) || ref($txid) || $txid !~ /\A[0-9a-f]{32}\z/;
    for my $name (qw(read_journal verify_finalizing_absent journal_cas)) {
        die "missing VM destroy completion dependency '$name'\n" if ref($self->{$name}) ne 'CODE';
    }
    # Deliberately separate from execute/resume_finalizing. No storage/PVE
    # destroy/finalization helper is loaded or dispatched by this path.
    require PVE::Cluster;
    require PVE::QemuConfig;
    PVE::Cluster::cfs_update();
    return PVE::QemuConfig->lock_config($vmid, sub {
        my $history = $self->{read_journal}->($txid);
        my $context = _completion_context($history, $vmid, $txid);
        $history = JSON::PP->new->decode(_canonical($history));
        my $head = $history->{last_sha256};
        my $observe = sub {
            my $proof = $self->{verify_finalizing_absent}->($vmid, txid => $txid,
                expected_context => JSON::PP->new->decode(_canonical($context)));
            _completion_observation($proof, $context, $head, $txid);
            return JSON::PP->new->decode(_canonical($proof));
        };
        my $before = $observe->();
        my $again = $self->{read_journal}->($txid);
        _completion_context($again, $vmid, $txid);
        die "VM destroy completion journal changed during bracket\n" if _canonical($again) ne _canonical($history);
        my $after = $observe->();
        die "VM destroy completion observation changed during bracket\n" if _canonical($after) ne _canonical($before);
        my $receipt = JSON::PP->new->decode(_canonical($history->{records}->[-1]->{receipt}));
        $receipt->{evidence}->{journal_only_completion} = $after;
        $receipt->{resumed} = 1;
        # One append with the exact observed FINALIZING head. An exception or
        # lost acknowledgement is never retried or converted to another stage.
        my $ack = $self->{journal_cas}->('COMPLETE', $receipt, $head);
        die "VM destroy journal-only COMPLETE acknowledgement is UNKNOWN; do not retry\n"
            if !defined($ack) || ref($ack) || "$ack" ne '1';
        return { status => 'COMPLETE', txid => $txid, vmid => 0 + $vmid,
            journal_only => 1, previous_head_sha256 => $head };
    });
}

sub resume_finalizing {
    my ($self, $vmid, $txid) = @_;
    die "VM destroy requires root\n" if $> != 0;
    die "invalid VM destroy identity\n"
        if !defined($vmid) || $vmid !~ /^[1-9][0-9]*$/
        || !defined($txid) || $txid !~ /^[a-f0-9]{32}$/;
    die "VM destroy durable journal reader is unavailable\n"
        if ref($self->{read_journal}) ne 'CODE';
    _load_pve();
    PVE::Cluster::cfs_update();
    return PVE::QemuConfig->lock_config($vmid, sub {
        my $history = $self->{read_journal}->($txid);
        die "VM destroy journal history is not durably confirmed\n"
            if ref($history) ne 'HASH' || "$history->{durable}" ne '1'
            || ref($history->{records}) ne 'ARRAY';
        my ($storage, $finalizing, $latest);
        for my $entry (@{$history->{records}}) {
            die "VM destroy journal record is malformed\n"
                if ref($entry) ne 'HASH' || ref($entry->{receipt}) ne 'HASH';
            my $r = $entry->{receipt};
            die "VM destroy journal identity mismatch\n"
                if ($r->{txid} // '') ne $txid || ($r->{vmid} // '') ne "$vmid";
            $storage = $r if ($entry->{stage} // '') eq 'STORAGE_ABSENT';
            $finalizing = $r if ($entry->{stage} // '') eq 'FINALIZING';
            $latest = $entry->{stage};
        }
        die "VM destroy exact STORAGE_ABSENT/FINALIZING receipt is missing\n"
            if !$storage || !$finalizing || !_same_receipt_core($storage, $finalizing);
        die "VM destroy is not in a recoverable finalization state\n"
            if ($latest // '') ne 'FINALIZING';
        my $receipt = JSON::PP->new->decode(_canonical($finalizing));
        my $runtime = $self->{verify_runtime}->($vmid);
        die "VM destroy runtime/contract changed before resume\n"
            if ref($runtime) ne 'HASH' || _canonical($runtime) ne _canonical($receipt->{runtime});
        my $object_ids = _validate_plan($vmid, $receipt->{config},
            PVE::Storage::config(), $receipt->{plan}->{volids}, $receipt->{plan});
        my $proof = $self->{observe_plan}->($receipt->{plan}, $receipt);
        die "VM destroy storage absence is not reproducible during resume\n"
            if ref($proof) ne 'HASH' || ($proof->{status} // '') ne 'ABSENT'
            || ($proof->{foreign_unchanged} // '') ne 'YES'
            || _canonical($proof->{object_ids} // []) ne _canonical($object_ids);
        my $state = $self->_finalization_state($vmid, $receipt);
        my $zombie;
        if ($state->{config} eq 'PRESENT') {
            $zombie = PVE::QemuConfig->load_config($vmid);
            die "VM destroy exact zombie config is absent during resume\n" if !_is_zombie($zombie);
            die "VM destroy zombie digest does not match durable receipt\n"
                if ($receipt->{evidence}->{zombie_config_sha256} // '')
                    ne sha256_hex(_canonical($zombie));
        } else {
            $zombie = { lock => 'destroyed' };
        }
        my $evidence = JSON::PP->new->decode(_canonical($receipt->{evidence}));
        $self->_record('FINALIZING', { %$receipt, evidence => $evidence,
            resume => 1, recovery_required => 0 });
        my $ok = eval { $self->_run_finalization($vmid, $receipt, $evidence, $zombie); 1 };
        if (!$ok) {
            my $error = $@ || "unknown VM destroy resume error\n";
            eval { $self->_record('FINALIZING', { %$receipt,
                evidence => $evidence, error => "$error", resume => 1,
                recovery_required => 1 }); };
            die "VM destroy FINALIZING resume remains blocked; no redispatch: $error$@";
        }
        $self->_record('COMPLETE', { %$receipt, evidence => $evidence, resumed => 1 });
        return { status => 'COMPLETE', txid => $txid, vmid => 0 + $vmid, resumed => 1 };
    });
}

sub execute {
    my ($self, $vmid, $txid) = @_;
    die "VM destroy requires root\n" if $> != 0;
    die "invalid VM destroy identity\n"
        if !defined($vmid) || $vmid !~ /^[1-9][0-9]*$/
        || !defined($txid) || $txid !~ /^[a-f0-9]{32}$/;
    _load_pve();
    PVE::Cluster::cfs_update();
    return PVE::QemuConfig->lock_config($vmid, sub {
        my $runtime = $self->{verify_runtime}->($vmid);
        die "VM destroy runtime is not qualified\n"
            if ref($runtime) ne 'HASH' || ($runtime->{status} // '') ne 'QUALIFIED'
            || ($runtime->{contract_sha256} // '') !~ /^[a-f0-9]{64}$/;
        my $conf = PVE::QemuConfig->load_config($vmid);
        PVE::QemuConfig->check_lock($conf);
        PVE::QemuConfig->check_protection($conf, 'guarded VM destroy');
        die "VM destroy refuses running VM\n" if PVE::QemuServer::check_running($vmid);
        die "VM destroy refuses HA resource\n" if PVE::HA::Config::service_is_configured("vm:$vmid");
        PVE::ReplicationConfig->new()->check_for_existing_jobs($vmid);
        my $storecfg = PVE::Storage::config();
        die "VM destroy storage inventory is unknown\n"
            if ref($storecfg) ne 'HASH' || ref($storecfg->{ids}) ne 'HASH';
        my $volids = _config_volids($vmid, $conf, $storecfg);
        my $before = _canonical($conf);
        my $inputs = JSON::PP->new->decode(_canonical([$conf, $storecfg, $volids]));
        my $plan = $self->{prepare_plan}->($vmid, @$inputs);
        my $object_ids = _validate_plan($vmid, $conf, $storecfg, $volids, $plan);
        die "VM destroy config changed during preflight\n"
            if _canonical(PVE::QemuConfig->load_config($vmid)) ne $before;
        my $receipt = { schema => 1, txid => $txid, vmid => 0 + $vmid,
            config => $conf, runtime => $runtime, plan => $plan };
        # Clone so native routines cannot mutate the archived in-memory view.
        $receipt = JSON::PP->new->decode(_canonical($receipt));
        my %evidence = (locked_config_sha256 => sha256_hex($before),
            raw_config_sha256 => $plan->{config_sha256}, object_ids => $object_ids,
            native => 'NOT_DISPATCHED', acl => 'NOT_ATTEMPTED', firewall => 'NOT_ATTEMPTED',
            config_remove => 'NOT_ATTEMPTED');
        $self->_record('PREPARED', { %$receipt, evidence => \%evidence });
        my $fresh = PVE::QemuConfig->load_config($vmid);
        _config_volids($vmid, $fresh, $storecfg); # includes no fleecing/special evidence
        die "VM destroy config changed before dispatch\n" if _canonical($fresh) ne $before;
        $evidence{native} = 'DISPATCH_INTENT';
        $self->_record('DISPATCHED', { %$receipt, evidence => \%evidence });
        my $storage_absent = 0;
        my $ok = eval {
            my $dispatch_config = PVE::QemuConfig->load_config($vmid);
            _config_volids($vmid, $dispatch_config, $storecfg);
            die "VM destroy config changed after dispatch journal\n"
                if _canonical($dispatch_config) ne $before;
            # Planning and durable descriptor/journal IPC can take time. The
            # early runtime observation is not a lease across that interval.
            # Recheck boot/owner/quorum/package/contract immediately before the
            # first native effect; no changed or ambiguous proof is admissible.
            my $dispatch_runtime = $self->{verify_runtime}->($vmid);
            die "VM destroy runtime/contract changed before native dispatch\n"
                if ref($dispatch_runtime) ne 'HASH'
                || _canonical($dispatch_runtime) ne _canonical($receipt->{runtime});
            my ($cleanup_calls, $ipam_calls) = (0, 0);
            {
                # The qualified native helper reacquires this same VM lock.
                # Its only safe specialization is an exact no-op, locally
                # scoped to this one call after proving there is no work.
                no warnings 'redefine';
                local *PVE::QemuConfig::cleanup_fleecing_images = sub {
                    die "VM destroy fleecing cleanup contract drift\n"
                        if @_ != 2 || $_[0] != $vmid || $_[1] != $storecfg || $cleanup_calls++;
                    my $current = PVE::QemuConfig->load_config($vmid);
                    _config_volids($vmid, $current, $storecfg);
                    die "VM destroy config changed at fleecing boundary\n"
                        if _canonical($current) ne $before;
                    return;
                };
                # There are no netX entries by preflight invariant. Keep the
                # qualified native call ordering but turn its otherwise
                # unjournalled SDN/IPAM helper into a one-call verified no-op.
                local *PVE::QemuServer::Network::delete_ifaces_ipams_ips = sub {
                    die "VM destroy IPAM cleanup contract drift\n"
                        if @_ != 2 || ref($_[0]) ne 'HASH' || $_[1] != $vmid || $ipam_calls++;
                    die "VM destroy network appeared at IPAM boundary\n"
                        if grep { /^net\d+$/ } keys %{$_[0]};
                    my $current = PVE::QemuConfig->load_config($vmid);
                    _config_volids($vmid, $current, $storecfg);
                    die "VM destroy config changed at IPAM boundary\n"
                        if _canonical($current) ne $before;
                    return;
                };
                # Keep the qualified caller chain; no nested CLI/API worker.
                PVE::QemuServer::destroy_vm($storecfg, $vmid, 0, { lock => 'destroyed' }, 0);
            }
            $evidence{native} = 'RETURNED';
            $evidence{fleecing_noop_calls} = $cleanup_calls;
            $evidence{ipam_noop_calls} = $ipam_calls;
            die "VM destroy fleecing callback count changed\n" if $cleanup_calls != 1;
            die "VM destroy IPAM callback count changed\n" if $ipam_calls != 1;
            my $observe_receipt = JSON::PP->new->decode(_canonical($receipt));
            my $proof = $self->{observe_plan}->($observe_receipt->{plan}, $observe_receipt);
            $evidence{storage_proof} = $proof;
            die "VM destroy storage absence is UNKNOWN\n"
                if ref($proof) ne 'HASH' || ($proof->{status} // '') ne 'ABSENT'
                || ($proof->{foreign_unchanged} // '') ne 'YES'
                || ref($proof->{object_ids}) ne 'ARRAY'
                || _canonical($proof->{object_ids}) ne _canonical($object_ids);
            my $zombie = PVE::QemuConfig->load_config($vmid);
            die "VM destroy exact zombie config is absent\n"
                if !_is_zombie($zombie);
            $evidence{zombie_config_sha256} = sha256_hex(_canonical($zombie));
            $self->_record('STORAGE_ABSENT', { %$receipt, evidence => \%evidence });
            $storage_absent = 1;
            $self->_record('FINALIZING', { %$receipt, evidence => \%evidence,
                next_effects => [qw(remove_vm_access remove_vmfw_conf destroy_config)] });
            $self->_run_finalization($vmid, $receipt, \%evidence, $zombie);
            $self->_record('COMPLETE', { %$receipt, evidence => \%evidence });
            1;
        };
        if (!$ok) {
            my $error = $@ || "unknown VM destroy error\n";
            # Never restore the old config, delete a remaining volume, retry,
            # or remove the zombie on this path. Journal failure cannot grant.
            my $stage = $storage_absent ? 'FINALIZING' : 'PARTIAL_OR_UNKNOWN';
            eval { $self->_record($stage, { %$receipt, evidence => \%evidence,
                error => "$error", recovery_required => $storage_absent ? 1 : 0 }); };
            my $journal_error = $@;
            die "VM destroy " . ($storage_absent ? 'FINALIZING recovery required' : 'PARTIAL_OR_UNKNOWN')
                . "; no redispatch: $error$journal_error";
        }
        return { status => 'COMPLETE', txid => $txid, vmid => 0 + $vmid };
    });
}

1;
