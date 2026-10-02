package PVE::SharedLvmThinVMDestroyStoragePlan;

use strict;
use warnings;
use JSON::PP ();
use Digest::SHA qw(sha256_hex);

# Pure, read-only model. The collector must provide a COMPLETE scoped inventory;
# this module neither obtains that proof nor runs PVE/LVM/device-mapper commands.
# A plan is diagnostic data (authority NONE), never a deletion capability.
# Inventory completeness is an injected trust boundary: a fabricated collector
# omitting an object from BOTH the inventory and root closure cannot be detected
# without an independent authoritative collector. Numeric complete=1 is required.
my $JSON = JSON::PP->new->canonical(1)->utf8(1);
my $SCHEMA = 'sharedlvmthin-vm-destroy-storage-plan/v1';
my %PHASE = (thin => 'OWNED_INACTIVE', thick => 'MATERIALIZED',
             lazy => 'LAZY_DORMANT');
my %ROOT_KIND = (thin => 'thin_pool', thick => 'anchor', lazy => 'anchor');
my %KINDS = (
    thin => { map { $_ => 1 } qw(thin_pool thin_data thin_metadata thin_lv snapshot) },
    thick => { map { $_ => 1 } qw(anchor generation snapshot) },
    lazy => { map { $_ => 1 } qw(anchor lazy_data lazy_metadata) },
);

sub _require ($$) { die "VM_DESTROY_STORAGE_UNKNOWN: " . (@_ == 2 ? $_[1] : 'invalid predicate arity') . "\n" if @_ != 2 || !$_[0]; }
sub _copy { return $JSON->decode($JSON->encode($_[0])); }
sub _digest { return sha256_hex($JSON->encode($_[0])); }
sub _same { return $JSON->encode($_[0]) eq $JSON->encode($_[1]); }
sub _id { return defined($_[0]) && !ref($_[0]) && $_[0] =~ /^[A-Za-z0-9][A-Za-z0-9_.:+\/-]{0,255}$/; }
sub _fields {
    my ($value, @fields) = @_;
    _require(ref($value) eq 'HASH' &&
        join('|', sort keys %$value) eq join('|', sort @fields), 'unexpected record fields');
}
sub _ids {
    my ($values, $label) = @_;
    _require(ref($values) eq 'ARRAY', "$label is not an array");
    my %seen;
    for my $id (@$values) {
        _require(_id($id) && !$seen{$id}++, "$label has invalid/duplicate identity");
    }
    return \%seen;
}

sub new {
    my ($class, %args) = @_;
    _require(keys(%args) == 1 && ref($args{collector}) eq 'CODE', 'collector required');
    return bless { collector => $args{collector} }, $class;
}

sub _inventory {
    my ($self) = @_;
    my $raw = eval { $self->{collector}->() };
    _require(!$@ && ref($raw) eq 'HASH', 'scoped collector failed');
    return _validate_inventory(_copy($raw));
}

sub _validate_inventory {
    my ($doc) = @_;
    _fields($doc, qw(schema complete scope roots objects runtimes),
        (exists($doc->{opaque_foreign_objects}) ? 'opaque_foreign_objects' : ()));
    _require($doc->{schema} eq 'sharedlvmthin-destroy-inventory/v1' &&
        defined($doc->{complete}) && !ref($doc->{complete}) && $doc->{complete} eq '1',
        'inventory completeness unproven');
    _fields($doc->{scope}, qw(node boot_id vg_uuid pv_uuid wwid storage_ids config_sha256));
    for my $field (qw(node boot_id vg_uuid pv_uuid wwid)) {
        _require(_id($doc->{scope}->{$field}), "scope $field invalid");
    }
    _require(scalar(($doc->{scope}->{config_sha256} // '') =~ /\A[0-9a-f]{64}\z/),
        'scope config digest invalid');
    my $storage_ids = _ids($doc->{scope}->{storage_ids}, 'scope storages');
    _require(scalar(keys(%$storage_ids)), 'empty storage scope');
    for my $field (qw(roots objects runtimes)) {
        _require(ref($doc->{$field}) eq 'ARRAY', "$field inventory unavailable");
    }
    my (%roots, %objects, %runtimes, %uuids, %dm_uuids, %volumes);
    for my $root (@{$doc->{roots}}) {
        _fields($root, qw(id mode phase owner_vmid namespace object_ids runtime_ids volume_ids));
        _require(_id($root->{id}) && !$roots{$root->{id}}, 'duplicate/invalid root ID');
        _require(exists($PHASE{$root->{mode} // ''}) &&
            ($root->{phase} // '') eq $PHASE{$root->{mode}}, 'unsupported root mode/phase');
        _require(scalar(($root->{owner_vmid} // '') =~ /\A[1-9][0-9]*\z/) &&
            _id($root->{namespace}), 'root ownership unknown');
        _ids($root->{object_ids}, 'root objects');
        _ids($root->{runtime_ids}, 'root runtimes');
        _ids($root->{volume_ids}, 'root volumes');
        _require(@{$root->{object_ids}} && @{$root->{volume_ids}}, 'empty root closure');
        for my $vol (@{$root->{volume_ids}}) {
            my ($sid, $name) = split(/:/, $vol, 2);
            _require(defined($name) && length($name) && $storage_ids->{$sid} &&
                !$volumes{$vol}++, 'volume outside scope or assigned to multiple roots');
        }
        $roots{$root->{id}} = $root;
    }
    for my $obj (@{$doc->{objects}}) {
        _fields($obj, qw(id uuid root_id kind owner_vmid namespace volume_id origin_uuid));
        _require(_id($obj->{id}) && !$objects{$obj->{id}}, 'duplicate/invalid object ID');
        _require(_id($obj->{uuid}) && !$uuids{$obj->{uuid}}++, 'duplicate/invalid persistent UUID');
        my $root = $roots{$obj->{root_id} // ''};
        _require($root && $KINDS{$root->{mode}}->{$obj->{kind} // ''}, 'unknown object root/kind');
        _require(($obj->{owner_vmid} // '') eq $root->{owner_vmid} &&
            ($obj->{namespace} // '') eq $root->{namespace}, 'foreign root member ownership');
        my %vols = map { $_ => 1 } @{$root->{volume_ids}};
        _require(defined($obj->{volume_id}) &&
            ($obj->{volume_id} eq '' || $vols{$obj->{volume_id}}), 'object volume ownership unknown');
        _require(defined($obj->{origin_uuid}) &&
            ($obj->{origin_uuid} eq '' || _id($obj->{origin_uuid})), 'invalid snapshot origin');
        if ($obj->{kind} eq 'snapshot') {
            _require(($root->{mode} ne 'thin' || $obj->{origin_uuid} ne '') && $obj->{volume_id} ne '',
                'snapshot ownership/origin omitted');
        } else {
            _require($obj->{origin_uuid} eq '', 'unexpected origin on non-snapshot');
        }
        $objects{$obj->{id}} = $obj;
    }
    for my $runtime (@{$doc->{runtimes}}) {
        _fields($runtime, qw(id dm_uuid root_id));
        _require(_id($runtime->{id}) && !$runtimes{$runtime->{id}}, 'duplicate runtime ID');
        _require(_id($runtime->{dm_uuid}) && !$dm_uuids{$runtime->{dm_uuid}}++,
            'duplicate/invalid DM UUID');
        _require($roots{$runtime->{root_id} // ''}, 'runtime root unknown');
        $runtimes{$runtime->{id}} = $runtime;
    }
    if (exists($doc->{opaque_foreign_objects})) {
        _require(ref($doc->{opaque_foreign_objects}) eq 'ARRAY', 'opaque foreign inventory unavailable');
        my (%names, %normalized);
        for my $uuid (keys %uuids) {
            (my $key = $uuid) =~ s/-//g;
            $normalized{$key} = 1;
        }
        for my $foreign (@{$doc->{opaque_foreign_objects}}) {
            _fields($foreign, qw(type name uuid scope_uuid evidence_sha256));
            _require(($foreign->{type} // '') eq 'lvm-lv' &&
                ($foreign->{name} // '') =~ /^[A-Za-z0-9+_.-]+$/ &&
                ($foreign->{uuid} // '') =~ /^[A-Za-z0-9-]+$/ &&
                ($foreign->{scope_uuid} // '') eq $doc->{scope}->{vg_uuid} &&
                ($foreign->{evidence_sha256} // '') =~ /^[0-9a-f]{64}$/,
                'opaque foreign identity invalid');
            (my $key = $foreign->{uuid}) =~ s/-//g;
            _require(!$names{$foreign->{name}}++ && !$normalized{$key}++,
                'opaque foreign duplicate name or UUID alias');
        }
        $doc->{opaque_foreign_objects} = [sort { $a->{name} cmp $b->{name} }
            @{$doc->{opaque_foreign_objects}}];
    }
    for my $root (values %roots) {
        my $object = $objects{$root->{id}};
        _require($object && $object->{root_id} eq $root->{id} &&
            $object->{kind} eq $ROOT_KIND{$root->{mode}}, 'persistent root object missing');
        my @objects = sort grep { $objects{$_}->{root_id} eq $root->{id} } keys %objects;
        my @runtimes = sort grep { $runtimes{$_}->{root_id} eq $root->{id} } keys %runtimes;
        _require(_same(\@objects, [sort @{$root->{object_ids}}]) &&
            _same(\@runtimes, [sort @{$root->{runtime_ids}}]), 'incomplete root member closure');
        my %count;
        $count{$objects{$_}->{kind}}++ for @objects;
        my @required = $root->{mode} eq 'thin' ? qw(thin_pool thin_data thin_metadata thin_lv)
            : $root->{mode} eq 'lazy' ? qw(anchor lazy_data lazy_metadata) : qw(anchor generation);
        _require($count{$_}, "required root member kind $_ omitted") for @required;
        _require(($count{$ROOT_KIND{$root->{mode}}} // 0) == 1, 'ambiguous root object count');
        my @single = $root->{mode} eq 'thin' ? qw(thin_data thin_metadata)
            : $root->{mode} eq 'lazy' ? qw(lazy_data lazy_metadata) : ();
        _require($count{$_} == 1, "ambiguous structural member $_") for @single;
        for my $vol (@{$root->{volume_ids}}) {
            _require(scalar(grep({ $objects{$_}->{volume_id} eq $vol } @objects)),
                'volume has no persistent member');
        }
        for my $id (@objects) {
            my $member = $objects{$id};
            next if $member->{origin_uuid} eq '';
            _require(scalar(grep({ $objects{$_}->{uuid} eq $member->{origin_uuid} &&
                $_ ne $id } @objects)), 'snapshot origin outside complete root');
            if ($root->{mode} eq 'thin') {
                my %by_uuid = map { $objects{$_}->{uuid} => $objects{$_} } @objects;
                my %visited;
                my $cursor = $member;
                while ($cursor->{kind} eq 'snapshot') {
                    _require(!$visited{$cursor->{uuid}}++, 'snapshot origin cycle');
                    $cursor = $by_uuid{$cursor->{origin_uuid}};
                    _require($cursor && $cursor->{volume_id} eq $member->{volume_id},
                        'snapshot origin ownership mismatch');
                }
                _require($cursor->{kind} eq 'thin_lv', 'snapshot origin is not a Thin disk');
            }
        }
    }
    # Canonical order makes harmless inventory ordering differences immaterial.
    for my $root (@{$doc->{roots}}) {
        $root->{$_} = [sort @{$root->{$_}}] for qw(object_ids runtime_ids volume_ids);
    }
    $doc->{scope}->{storage_ids} = [sort @{$doc->{scope}->{storage_ids}}];
    $doc->{$_} = [sort { $a->{id} cmp $b->{id} } @{$doc->{$_}}] for qw(roots objects runtimes);
    return $doc;
}

sub _build {
    my ($before, $vmid, $requested) = @_;
    _require(defined($vmid) && !ref($vmid) && scalar($vmid =~ /\A[1-9][0-9]*\z/), 'invalid VMID');
    my $requested_ids = _ids($requested, 'requested volumes');
    _require(scalar(keys(%$requested_ids)), 'empty destroy plan');
    my @roots = grep { $_->{owner_vmid} eq "$vmid" } @{$before->{roots}};
    my @owned = sort map { @{$_->{volume_ids}} } @roots;
    _require(_same(\@owned, [sort @$requested]), 'requested set omits/adds owned volume');
    my %selected = map { $_->{id} => 1 } @roots;
    my @volumes;
    for my $root (@roots) {
        for my $vol (@{$root->{volume_ids}}) {
            push @volumes, { volid => $vol, mode => $root->{mode}, root_id => $root->{id},
                # Shared Thin pool members/snapshots belong to the root closure,
                # not just to one disk. Never authorize a partial pool plan.
                persistent_objects => [grep { $_->{root_id} eq $root->{id} } @{$before->{objects}}],
                runtime_objects => [grep { $_->{root_id} eq $root->{id} } @{$before->{runtimes}}],
                complete_root_volume_ids => $root->{volume_ids} };
        }
    }
    my $foreign = { roots => [grep { !$selected{$_->{id}} } @{$before->{roots}}],
        objects => [grep { !$selected{$_->{root_id}} } @{$before->{objects}}],
        runtimes => [grep { !$selected{$_->{root_id}} } @{$before->{runtimes}}] };
    $foreign->{opaque_foreign_objects} = $before->{opaque_foreign_objects}
        if exists($before->{opaque_foreign_objects});
    my $plan = { schema => $SCHEMA, authority => 'NONE', vmid => "$vmid",
        scope => $before->{scope}, before => $before, root_ids => [sort keys %selected],
        volumes => [sort { $a->{volid} cmp $b->{volid} } @volumes],
        foreign_baseline => $foreign, foreign_sha256 => _digest($foreign) };
    $plan->{plan_sha256} = _digest($plan);
    return _copy($plan);
}

sub plan {
    my ($self, %args) = @_;
    _require(keys(%args) == 2 && exists($args{vmid}) && exists($args{volumes}), 'plan arguments invalid');
    return _build($self->_inventory(), $args{vmid}, $args{volumes});
}

sub observe {
    my ($self, $plan) = @_;
    _require(ref($plan) eq 'HASH' && ref($plan->{volumes}) eq 'ARRAY', 'invalid plan');
    my $before = _validate_inventory(_copy($plan->{before}));
    my $rebuilt = _build($before, $plan->{vmid}, [map { $_->{volid} } @{$plan->{volumes}}]);
    _require(_same($plan, $rebuilt), 'plan digest/schema/derived closure changed');
    my $after = $self->_inventory();
    _require(_same($plan->{scope}, $after->{scope}), 'observation scope/boot/config drift');
    my %roots = map { $_ => 1 } @{$plan->{root_ids}};
    my (%ids, %uuids, %runtime_ids, %dm_uuids);
    for my $object (@{$before->{objects}}) {
        next if !$roots{$object->{root_id}};
        $ids{$object->{id}} = 1; $uuids{$object->{uuid}} = 1;
    }
    for my $runtime (@{$before->{runtimes}}) {
        next if !$roots{$runtime->{root_id}};
        $runtime_ids{$runtime->{id}} = 1; $dm_uuids{$runtime->{dm_uuid}} = 1;
    }
    for my $object (@{$after->{objects}}) {
        _require(!$ids{$object->{id}} && !$uuids{$object->{uuid}},
            'planned object remains, name reused or UUID alias exists');
    }
    for my $runtime (@{$after->{runtimes}}) {
        _require(!$runtime_ids{$runtime->{id}} && !$dm_uuids{$runtime->{dm_uuid}},
            'planned runtime remains, name reused or DM UUID alias exists');
    }
    my $remaining = { map { $_ => $after->{$_} } qw(roots objects runtimes) };
    $remaining->{opaque_foreign_objects} = $after->{opaque_foreign_objects}
        if exists($after->{opaque_foreign_objects});
    _require(_same($remaining, $plan->{foreign_baseline}), 'foreign baseline drift or extra object');
    return { schema => 'sharedlvmthin-vm-destroy-storage-observation/v1', authority => 'NONE',
        result => 'EXACT_PLANNED_STORAGE_ABSENT', plan_sha256 => $plan->{plan_sha256},
        foreign_sha256 => $plan->{foreign_sha256}, observed_sha256 => _digest($after) };
}

1;
