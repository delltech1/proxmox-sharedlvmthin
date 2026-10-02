package PVE::SharedLvmThinVMDestroyPlanAdapter;

use strict;
use warnings;
use JSON::PP ();
use Digest::SHA qw(sha1_hex sha256_hex);
use PVE::SharedLvmThinVMDestroyStoragePlan;

# Pure diagnostic translation. No PVE calls, effects or authority issuance.
# Bindings must come from the same complete, scoped collector: opaque planner
# IDs are NOT LV/DM names. V1 refuses shared multi-volume roots; explicit V2
# stores each root closure once and maps volume leaves without inventing
# per-volume ownership. expected_scope is an independently pinned collection
# scope; its config digest must not silently become the changing VM config.
sub _json { JSON::PP->new->canonical->encode($_[0]) }
sub _copy { JSON::PP->new->decode(_json($_[0])) }
sub _need ($$) { die "VM_DESTROY_ADAPTER_UNKNOWN: " . (@_ == 2 ? $_[1] : 'invalid predicate arity') . "\n" if @_ != 2 || !$_[0] }

sub adapt_plan {
    my ($class, %args) = @_;
    my @fields = qw(storage_plan raw_config locked_config storecfg bindings expected_scope);
    push @fields, 'plan_version' if exists($args{plan_version});
    _need(join('|', sort keys %args) eq join('|', sort @fields),
        'unexpected adapter inputs');
    my $version = $args{plan_version} // 1;
    _need(!ref($version) && scalar($version =~ /\A(?:1|2)\z/), 'unsupported executor plan version');
    my $input = _copy(\%args);
    my ($source, $conf, $cfg, $bindings) = @{$input}{qw(storage_plan locked_config storecfg bindings)};
    _need(ref($source) eq 'HASH' && ($source->{authority} // '') eq 'NONE', 'source authority must be NONE');
    _need(ref($source->{volumes}) eq 'ARRAY', 'source volumes missing');
    # Rebuild all derived closures/digests; do not trust a supplied digest alone.
    my $validator = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $source->{before} });
    my $rebuilt = $validator->plan(vmid => $source->{vmid}, volumes => [map { $_->{volid} } @{$source->{volumes}}]);
    _need(_json($rebuilt) eq _json($source), 'source plan changed');
    _need(ref($input->{expected_scope}) eq 'HASH' && _json($source->{scope}) eq _json($input->{expected_scope}),
        'scope not independently pinned');
    my $raw = $input->{raw_config};
    _need(defined($raw) && !ref($raw) && length($raw) && length($raw) <= 4 * 1024 * 1024
        && ref($conf) eq 'HASH' && ($conf->{digest} // '') eq sha1_hex($raw), 'raw/locked digest mismatch');
    _need(ref($cfg) eq 'HASH' && ref($cfg->{ids}) eq 'HASH', 'storage configuration missing');
    _need(ref($bindings) eq 'HASH' && join('|', sort keys %$bindings) eq 'objects|runtimes'
        && ref($bindings->{objects}) eq 'HASH' && ref($bindings->{runtimes}) eq 'HASH', 'typed bindings missing');
    return $class->_normalized_roots($input) if $version == 2;
    my (@volumes, %object_keys, %runtime_keys, %identities, %physical_names, @ids);
    for my $volume (@{$source->{volumes}}) {
        _need(@{$volume->{complete_root_volume_ids}} == 1
            && $volume->{complete_root_volume_ids}->[0] eq $volume->{volid}, 'shared multi-volume root unsupported');
        my ($sid) = split /:/, $volume->{volid}, 2;
        my $scfg = $cfg->{ids}->{$sid};
        _need(ref($scfg) eq 'HASH' && ($scfg->{type} // '') eq 'sharedlvmthin', 'unknown storage');
        for my $pair ([qw(vg_uuid slt-expected-vg-uuid)], [qw(pv_uuid slt-expected-pv-uuid)], [qw(wwid slt-expected-wwid)]) {
            _need(($scfg->{$pair->[1]} // '') eq $source->{scope}->{$pair->[0]}, 'storage scope identity mismatch');
        }
        my $vg = $scfg->{'slt-vgname'} // '';
        _need(scalar($vg =~ /\A[A-Za-z0-9+_.-]+\z/), 'VG name missing');
        my $output = { volid => $volume->{volid}, kind => $volume->{mode},
            root => { type => 'lvm-vg', name => $vg, uuid => $source->{scope}->{vg_uuid} },
            objects => [], runtime_ids => [] };
        for my $group ([qw(persistent_objects objects objects uuid lvm-lv)], [qw(runtime_objects runtimes runtime_ids dm_uuid dm)]) {
            my ($field, $binding_group, $out_field, $uuid_field, $type) = @$group;
            for my $object (@{$volume->{$field}}) {
                my $binding = $bindings->{$binding_group}->{$object->{id}};
                _need(ref($binding) eq 'HASH' && join('|', sort keys %$binding) eq 'name|uuid'
                    && ($binding->{name} // '') =~ /^[A-Za-z0-9+_.-]+$/
                    && ($binding->{uuid} // '') =~ /^[A-Za-z0-9-]+$/
                    && $binding->{uuid} eq $object->{$uuid_field}, 'missing or inconsistent physical name/UUID binding');
                _need(!defined($object->{volume_id}) || $object->{volume_id} eq '' || $object->{volume_id} eq $volume->{volid},
                    'foreign member volume binding');
                my $id = $type eq 'lvm-lv' ? "lvm:$source->{scope}->{vg_uuid}:$binding->{uuid}" : "dm:$binding->{uuid}";
                _need(!$identities{$id}++ && !$physical_names{"$type:$vg:$binding->{name}"}++, 'duplicate physical identity');
                $binding_group eq 'objects' ? $object_keys{$object->{id}}++ : $runtime_keys{$object->{id}}++;
                push @{$output->{$out_field}}, { type => $type, id => $id, name => $binding->{name},
                    uuid => $binding->{uuid}, volid => $volume->{volid} };
                push @ids, $id;
            }
        }
        push @volumes, $output;
    }
    _need(_json([sort keys %object_keys]) eq _json([sort keys %{$bindings->{objects}}])
        && _json([sort keys %runtime_keys]) eq _json([sort keys %{$bindings->{runtimes}}]), 'extra or omitted bindings');
    my $output = { schema => 'sharedlvmthin-vm-destroy-executor-plan/v1', authority => 'NONE',
        vmid => $source->{vmid}, raw_config => $raw, config_sha256 => sha256_hex($raw),
        parsed_config_sha256 => sha256_hex(_json($conf)),
        volids => [sort map { $_->{volid} } @volumes], volumes => \@volumes,
        object_ids => [sort @ids], storage_plan_sha256 => $source->{plan_sha256}, adapter_inputs => $input };
    $output->{adapter_sha256} = sha256_hex(_json($output));
    return _copy($output);
}

sub _normalized_roots {
    my ($class, $input) = @_;
    my ($source, $cfg, $bindings) = @{$input}{qw(storage_plan storecfg bindings)};
    my %selected = map { $_ => 1 } @{$source->{root_ids}};
    my (@roots, @volumes, @ids, %seen, %names, %object_keys, %runtime_keys);
    for my $source_root (@{$source->{before}->{roots}}) {
        next if !$selected{$source_root->{id}};
        my $root = { id => $source_root->{id}, kind => $source_root->{mode},
            phase => $source_root->{phase}, owner_vmid => $source_root->{owner_vmid},
            namespace => $source_root->{namespace}, volids => _copy($source_root->{volume_ids}),
            objects => [], runtime_ids => [] };
        for my $volid (@{$root->{volids}}) {
            my ($sid) = split /:/, $volid, 2;
            my $scfg = $cfg->{ids}->{$sid};
            _need(ref($scfg) eq 'HASH' && ($scfg->{type} // '') eq 'sharedlvmthin', 'unknown storage');
            for my $pair ([qw(vg_uuid slt-expected-vg-uuid)], [qw(pv_uuid slt-expected-pv-uuid)], [qw(wwid slt-expected-wwid)]) {
                _need(($scfg->{$pair->[1]} // '') eq $source->{scope}->{$pair->[0]}, 'storage scope identity mismatch');
            }
            my $vg = $scfg->{'slt-vgname'} // '';
            _need(scalar($vg =~ /\A[A-Za-z0-9+_.-]+\z/), 'VG name missing');
            my $identity = { type => 'lvm-vg', name => $vg, uuid => $source->{scope}->{vg_uuid} };
            _need(!exists($root->{vg}) || _json($root->{vg}) eq _json($identity), 'root spans inconsistent VG aliases');
            $root->{vg} = $identity;
        }
        for my $group ([qw(objects objects uuid lvm-lv)], [qw(runtimes runtime_ids dm_uuid dm)]) {
            my ($field, $out, $uuid_field, $type) = @$group;
            for my $object (@{$source->{before}->{$field}}) {
                next if $object->{root_id} ne $root->{id};
                my $binding = $bindings->{$field}->{$object->{id}};
                _need(ref($binding) eq 'HASH' && join('|', sort keys %$binding) eq 'name|uuid'
                    && ($binding->{name} // '') =~ /^[A-Za-z0-9+_.-]+$/
                    && ($binding->{uuid} // '') =~ /^[A-Za-z0-9-]+$/
                    && $binding->{uuid} eq $object->{$uuid_field}, 'missing or inconsistent physical name/UUID binding');
                my $id = $type eq 'lvm-lv' ? "lvm:$root->{vg}->{uuid}:$binding->{uuid}" : "dm:$binding->{uuid}";
                _need(!$seen{$id}++ && !$names{"$type:$root->{vg}->{name}:$binding->{name}"}++, 'duplicate physical identity');
                my $member = { type => $type, id => $id, name => $binding->{name}, uuid => $binding->{uuid}, source_id => $object->{id} };
                if ($type eq 'lvm-lv') {
                    @{$member}{qw(kind volid origin_uuid)} = @{$object}{qw(kind volume_id origin_uuid)};
                    $object_keys{$object->{id}}++;
                } else {
                    $runtime_keys{$object->{id}}++;
                }
                push @{$root->{$out}}, $member;
                push @ids, $id;
            }
        }
        for my $volid (@{$root->{volids}}) {
            my @leaves = sort map { $_->{id} } grep { $_->{volid} eq $volid } @{$root->{objects}};
            _need(@leaves, 'volume has no exact leaf members');
            push @volumes, { volid => $volid, root_id => $root->{id}, object_ids => \@leaves };
        }
        push @roots, $root;
    }
    _need(_json([sort keys %object_keys]) eq _json([sort keys %{$bindings->{objects}}])
        && _json([sort keys %runtime_keys]) eq _json([sort keys %{$bindings->{runtimes}}]), 'extra or omitted bindings');
    my $output = { schema => 'sharedlvmthin-vm-destroy-executor-plan/v2', authority => 'NONE',
        vmid => $source->{vmid}, raw_config => $input->{raw_config}, config_sha256 => sha256_hex($input->{raw_config}),
        parsed_config_sha256 => sha256_hex(_json($input->{locked_config})),
        volids => [sort map { $_->{volid} } @volumes],
        volumes => [sort { $a->{volid} cmp $b->{volid} } @volumes], roots => \@roots,
        object_ids => [sort @ids], storage_plan_sha256 => $source->{plan_sha256}, adapter_inputs => $input };
    $output->{adapter_sha256} = sha256_hex(_json($output));
    return _copy($output);
}

sub observe {
    my ($class, $planner, $adapted) = @_;
    _need(ref($adapted) eq 'HASH' && ref($adapted->{adapter_inputs}) eq 'HASH', 'adapted plan missing');
    my $rebuilt = $class->adapt_plan(%{$adapted->{adapter_inputs}});
    _need(_json($rebuilt) eq _json($adapted), 'adapted plan changed');
    _need(ref($planner) && $planner->isa('PVE::SharedLvmThinVMDestroyStoragePlan'), 'invalid observer');
    my $proof = $planner->observe(_copy($adapted->{adapter_inputs}->{storage_plan}));
    _need(ref($proof) eq 'HASH' && ($proof->{authority} // '') eq 'NONE'
        && ($proof->{result} // '') eq 'EXACT_PLANNED_STORAGE_ABSENT'
        && ($proof->{plan_sha256} // '') eq $adapted->{storage_plan_sha256}, 'observation does not bind source plan');
    return { authority => 'NONE', status => 'ABSENT', foreign_unchanged => 'YES',
        object_ids => _copy($adapted->{object_ids}), source_observation => _copy($proof),
        adapter_sha256 => $adapted->{adapter_sha256} };
}

1;
