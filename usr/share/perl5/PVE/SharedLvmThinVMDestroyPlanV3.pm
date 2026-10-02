package PVE::SharedLvmThinVMDestroyPlanV3;

use strict;
use warnings;
use Digest::SHA qw(sha256_hex);
use JSON::PP ();
use PVE::SharedLvmThinVMDestroyPlanAdapter;

our $MAX_BYTES = 768 * 1024;
my $JSON = JSON::PP->new->canonical;

sub _json { $JSON->encode($_[0]) }
sub _copy { $JSON->decode(_json($_[0])) }
sub _hash { sha256_hex(_json($_[0])) }
sub _sha256 { defined($_[0]) && !ref($_[0]) && length($_[0]) == 64 && scalar($_[0] =~ /\A[0-9a-f]+\z/) }
sub _need { die "VM_DESTROY_V3_UNKNOWN: $_[1]\n" if !$_[0] }
sub _exact {
    my ($value, @keys) = @_;
    _need(ref($value) eq 'HASH' && join('|', sort keys %$value) eq join('|', sort @keys),
        'unexpected record fields');
}

sub compose {
    my ($class, %args) = @_;
    _need(keys(%args) == 1 && ref($args{scopes}) eq 'ARRAY' && @{$args{scopes}} >= 1,
        'one or more exact scope envelopes required');
    my @inputs = map { _copy($_) } @{$args{scopes}};
    my ($first, %scope_ids, %storage_ids, %volids, %root_ids, %object_ids, %physical_names,
        %wwids, %vg_uuids, %pv_uuids);
    my (@scopes, @roots, @volumes, @objects);
    for my $envelope (@inputs) {
        _exact($envelope, qw(collector_sid executor_v2 foreign_evidence foreign_evidence_sha256));
        _need(scalar(($envelope->{collector_sid} // '') =~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/)
            && ref($envelope->{foreign_evidence}) eq 'HASH'
            && _sha256($envelope->{foreign_evidence_sha256})
            && _hash($envelope->{foreign_evidence}) eq $envelope->{foreign_evidence_sha256},
            'invalid collector or foreign evidence identity');
        my $plan = $envelope->{executor_v2};
        _need(ref($plan) eq 'HASH'
            && ($plan->{schema} // '') eq 'sharedlvmthin-vm-destroy-executor-plan/v2'
            && ($plan->{authority} // '') eq 'NONE'
            && ref($plan->{adapter_inputs}) eq 'HASH', 'input is not an exact v2 plan');
        my $rebuilt = PVE::SharedLvmThinVMDestroyPlanAdapter->adapt_plan(%{$plan->{adapter_inputs}});
        _need(_json($rebuilt) eq _json($plan), 'v2 plan failed lossless rebuild');
        my $scope = $plan->{adapter_inputs}->{expected_scope};
        _exact($scope, qw(node boot_id vg_uuid pv_uuid wwid storage_ids config_sha256));
        _need(ref($scope->{storage_ids}) eq 'ARRAY' && @{$scope->{storage_ids}}, 'empty scope aliases');
        my %vg_names = map { ($_->{vg}->{name} // '') => 1 } @{$plan->{roots}};
        _need(keys(%vg_names) == 1 && scalar((keys(%vg_names))[0] =~ /^[A-Za-z0-9+_.-]+$/),
            'scope has inconsistent VG names');
        my $vg_name = (keys %vg_names)[0];
        my $wwid = lc($scope->{wwid} // '');
        _need(scalar($wwid =~ /^[0-9a-f]+$/)
            && scalar(($scope->{vg_uuid} // '') =~ /^[A-Za-z0-9-]+$/)
            && scalar(($scope->{pv_uuid} // '') =~ /^[A-Za-z0-9-]+$/), 'invalid physical scope');
        my $physical = { vg_name => $vg_name, vg_uuid => $scope->{vg_uuid},
            pv_uuid => $scope->{pv_uuid}, wwid => $wwid };
        my $scope_id = _hash({ map { $_ => $physical->{$_} } qw(vg_uuid pv_uuid wwid) });
        _need(!$scope_ids{$scope_id}++, 'same physical scope supplied twice');
        _need(!$wwids{$wwid}++, 'same WWID appears in multiple physical scopes');
        _need(!$vg_uuids{$scope->{vg_uuid}}++, 'same VG UUID appears in multiple physical scopes');
        _need(!$pv_uuids{$scope->{pv_uuid}}++, 'same PV UUID appears in multiple physical scopes');
        my @aliases = sort @{$scope->{storage_ids}};
        my %alias_seen;
        for my $sid (@aliases) {
            _need(scalar($sid =~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/) && !$alias_seen{$sid}++
                && !$storage_ids{$sid}++, 'duplicate or invalid cross-scope storage alias');
            my $scfg = $plan->{adapter_inputs}->{storecfg}->{ids}->{$sid};
            _need(ref($scfg) eq 'HASH' && ($scfg->{type} // '') eq 'sharedlvmthin'
                && ($scfg->{'slt-vgname'} // '') eq $vg_name
                && ($scfg->{'slt-expected-vg-uuid'} // '') eq $scope->{vg_uuid}
                && ($scfg->{'slt-expected-pv-uuid'} // '') eq $scope->{pv_uuid}
                && lc($scfg->{'slt-expected-wwid'} // '') eq $wwid, 'alias physical identity mismatch');
        }
        _need($alias_seen{$envelope->{collector_sid}}, 'collector SID is outside exact scope aliases');
        my $common = { vmid => "$plan->{vmid}", raw_config => $plan->{raw_config},
            config_sha256 => $plan->{config_sha256}, parsed_config_sha256 => $plan->{parsed_config_sha256},
            storecfg_sha256 => _hash($plan->{adapter_inputs}->{storecfg}), node => $scope->{node},
            boot_id => $scope->{boot_id}, scope_config_sha256 => $scope->{config_sha256} };
        $first //= $common;
        _need(_json($common) eq _json($first), 'VM/config/runtime/storecfg differs across scopes');
        my @scope_volids = sort @{$plan->{volids}};
        for my $volid (@scope_volids) {
            my ($sid) = split /:/, $volid, 2;
            _need($alias_seen{$sid} && !$volids{$volid}++, 'volume outside scope or duplicated');
        }
        my %root_map;
        for my $root (@{$plan->{roots}}) {
            _need(($root->{vg}->{name} // '') eq $vg_name
                && ($root->{vg}->{uuid} // '') eq $scope->{vg_uuid}, 'root physical identity mismatch');
            my $global = "root:$scope_id:$root->{id}";
            _need(!$root_ids{$global}++, 'duplicate global root identity');
            $root_map{$root->{id}} = $global;
            push @roots, { id => $global, scope_id => $scope_id, source_id => $root->{id},
                kind => $root->{kind}, owner_vmid => "$root->{owner_vmid}" };
            for my $member (@{$root->{objects}}, @{$root->{runtime_ids}}) {
                _need(!$object_ids{$member->{id}}++, 'duplicate cross-scope object/runtime identity');
                my $type = $member->{type};
                _need($type eq 'lvm-lv' || $type eq 'dm', 'unknown member type');
                my $physical_name = "$type:$scope_id:$member->{name}";
                _need(!$physical_names{$physical_name}++, 'duplicate physical name in scope');
                push @objects, $member->{id};
            }
        }
        for my $volume (@{$plan->{volumes}}) {
            _need($root_map{$volume->{root_id}}, 'volume root outside scope');
            push @volumes, { volid => $volume->{volid}, scope_id => $scope_id,
                root_id => $root_map{$volume->{root_id}} };
        }
        push @scopes, { scope_id => $scope_id, physical => $physical,
            storage_ids => \@aliases, collector_sid => $envelope->{collector_sid},
            volids => \@scope_volids, executor_v2 => $plan,
            adapter_sha256 => $plan->{adapter_sha256},
            storage_plan_sha256 => $plan->{storage_plan_sha256},
            foreign_evidence => $envelope->{foreign_evidence},
            foreign_evidence_sha256 => $envelope->{foreign_evidence_sha256} };
    }
    my $output = { schema => 'sharedlvmthin-vm-destroy-executor-plan/v3', authority => 'NONE',
        vmid => $first->{vmid}, raw_config => $first->{raw_config},
        config_sha256 => $first->{config_sha256}, parsed_config_sha256 => $first->{parsed_config_sha256},
        storecfg_sha256 => $first->{storecfg_sha256}, node => $first->{node}, boot_id => $first->{boot_id},
        volids => [sort keys %volids], scope_ids => [sort keys %scope_ids],
        scopes => [sort { $a->{scope_id} cmp $b->{scope_id} } @scopes],
        roots => [sort { $a->{id} cmp $b->{id} } @roots],
        volumes => [sort { $a->{volid} cmp $b->{volid} } @volumes],
        object_ids => [sort @objects] };
    $output->{plan_sha256} = _hash($output);
    _need(length(_json($output)) <= $MAX_BYTES, 'canonical v3 plan exceeds journal budget');
    return _copy($output);
}

sub validate {
    my ($class, $plan) = @_;
    _need(ref($plan) eq 'HASH' && ($plan->{schema} // '') eq
        'sharedlvmthin-vm-destroy-executor-plan/v3' && ref($plan->{scopes}) eq 'ARRAY',
        'invalid v3 plan');
    my $rebuilt = $class->compose(scopes => [map { +{
        collector_sid => $_->{collector_sid}, executor_v2 => $_->{executor_v2},
        foreign_evidence => $_->{foreign_evidence},
        foreign_evidence_sha256 => $_->{foreign_evidence_sha256},
    } } @{$plan->{scopes}}]);
    _need(_json($rebuilt) eq _json($plan), 'v3 plan changed');
    return _copy($rebuilt);
}

sub inventory_bundle {
    my ($class, $plan, $scope_id) = @_;
    my $validated = $class->validate($plan);
    _need(defined($scope_id) && !ref($scope_id) && scalar($scope_id =~ /^[0-9a-f]{64}$/),
        'invalid scope selection');
    my @scope = grep { $_->{scope_id} eq $scope_id } @{$validated->{scopes}};
    _need(@scope == 1, 'scope selection is absent or ambiguous');
    my $entry = $scope[0];
    my $input = $entry->{executor_v2}->{adapter_inputs};
    return _copy({ authority => 'NONE', storage_plan => $input->{storage_plan},
        bindings => $input->{bindings}, foreign_evidence => $entry->{foreign_evidence},
        foreign_evidence_sha256 => $entry->{foreign_evidence_sha256} });
}

sub validate_profile {
    my ($class, $plan, $profile) = @_;
    _need(defined($profile) && !ref($profile)
        && ($profile eq 'dual' || $profile eq 'thick-only'), 'invalid package profile');
    my $validated = $class->validate($plan);
    if ($profile eq 'thick-only') {
        _need(!grep({ ($_->{kind} // '') eq 'thin' } @{$validated->{roots}}),
            'Thick-only profile refuses a Thin scope');
    }
    return _copy($validated);
}

1;
