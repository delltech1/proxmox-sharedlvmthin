package PVE::SharedLvmThinVMDestroyInventory;

use strict;
use warnings;
use JSON::PP ();
use Digest::SHA qw(sha256_hex);
use PVE::Tools ();
use PVE::Storage ();
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::SharedLvmThinThick qw(decode_anchor_tags decode_generation_tags
    decode_lazy_object_tags decode_vg_intent_tags object_key mapper_name);
use PVE::SharedLvmThinVMDestroyStoragePlan;

# Read-only, single-VG collector. It does not establish cluster-wide absence,
# acquire destroy authority, activate a device or recover any state. Unknown
# foreign LVs are retained as typed opaque baseline objects without invented
# VM ownership. Unclassified plugin-tagged objects are REFUSED. Callbacks are trusted DI
# boundaries for unit tests; production defaults execute fixed read-only probes.
my $PLUGIN = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $JSON = JSON::PP->new->canonical;
my @LV_FIELDS = qw(vg_name lv_name lv_uuid lv_size lv_attr lv_tags pool_lv origin data_lv metadata_lv);
sub _need ($$) { die "VM_DESTROY_INVENTORY_UNKNOWN: " . (@_ == 2 ? $_[1] : 'invalid predicate arity') . "\n" if @_ != 2 || !$_[0] }
sub _json { $JSON->encode($_[0]) }
sub _copy { $JSON->decode(_json($_[0])) }
sub _hash { sha256_hex(_json($_[0])) }
sub _name { defined($_[0]) && !ref($_[0]) && $_[0] =~ /^[A-Za-z0-9+_.-]+$/ }
sub _uuid { defined($_[0]) && !ref($_[0]) && $_[0] =~ /^[A-Za-z0-9-]+$/ }
sub _trim { my $v = $_[0]; _need(defined($v) && !ref($v), 'non-scalar report field'); $v =~ s/^\s+|\s+$//g; return $v }
sub _lvname { my $v = _trim($_[0]); $v =~ s/^\[([^\[\]]+)\]$/$1/; _need($v eq '' || _name($v), 'invalid reported LV name'); return $v }
sub _dmname { my ($vg, $lv) = @_; $vg =~ s/-/--/g; $lv =~ s/-/--/g; return "$vg-$lv" }
sub _lvm_uuid { my ($vg, $lv) = @_; s/-//g for ($vg, $lv); return "LVM-$vg$lv" }

sub new {
    my ($class, %args) = @_;
    my %allowed = map { $_ => 1 } qw(storage_id vmid config_reader identity_reader runner dm_reader);
    _need(!grep({ !$allowed{$_} } keys %args), 'unknown collector option');
    _need(_name($args{storage_id}) && scalar(($args{vmid} // '') =~ /\A[1-9][0-9]*\z/), 'invalid storage/VM identity');
    for my $key (qw(config_reader identity_reader runner dm_reader)) {
        _need(!exists($args{$key}) || ref($args{$key}) eq 'CODE', "invalid $key");
    }
    $args{config_reader} //= sub { PVE::Storage::config() };
    $args{identity_reader} //= sub { [$PLUGIN->_lazy_local_identity()] };
    $args{runner} //= sub {
        my ($argv) = @_;
        my (@out, @err);
        PVE::Tools::run_command($argv, timeout => 30,
            outfunc => sub { push @out, $_[0]; _need(@out < 32768, 'oversized LVM report') },
            errfunc => sub { push @err, $_[0] });
        _need(!@err, 'LVM report contains diagnostics');
        return join("\n", @out);
    };
    $args{dm_reader} //= sub { $PLUGIN->_thick_tree_kernel_rows() };
    return bless \%args, $class;
}

sub _report {
    my ($self, $tool, $kind, $fields, $device, @scope) = @_;
    my @argv = ("/sbin/$tool", '--readonly', '--reportformat', 'json', '--units', 'b',
        '--nosuffix', '--devices', $device);
    push @argv, '--all' if $tool eq 'lvs';
    push @argv, '-o', join(',', @$fields), @scope;
    my $raw = $self->{runner}->(\@argv);
    _need(defined($raw) && !ref($raw) && length($raw) <= 8 * 1024 * 1024, 'report unavailable/oversized');
    my $doc = eval { $JSON->decode($raw) };
    _need(!$@ && ref($doc) eq 'HASH' && ref($doc->{report}) eq 'ARRAY' &&
        @{$doc->{report}} == 1 && ref($doc->{report}->[0]->{$kind}) eq 'ARRAY', 'malformed report');
    my $rows = $doc->{report}->[0]->{$kind};
    _need(@$rows <= 8192, 'report object budget exceeded');
    for my $row (@$rows) {
        _need(ref($row) eq 'HASH', 'malformed report row');
        $row->{$_} = _trim($row->{$_}) for @$fields;
    }
    return $rows;
}

sub _sample {
    my ($self, $cfg, $sid, $scfg) = @_;
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $vgs = $self->_report('vgs', 'vg', [qw(vg_name vg_uuid vg_tags)], $device, $vg);
    _need(@$vgs == 1 && $vgs->[0]->{vg_name} eq $vg &&
        $vgs->[0]->{vg_uuid} eq $scfg->{'slt-expected-vg-uuid'}, 'VG identity mismatch');
    _need(!decode_vg_intent_tags($vgs->[0]->{vg_tags}), 'unresolved VG intent');
    _need($vgs->[0]->{vg_tags} !~ /(?:^|,)pve-slt-bridge-/, 'bridge reservation present');
    my $pvs = $self->_report('pvs', 'pv', [qw(pv_uuid pv_name vg_uuid)], $device,
        '--select', "vg_name=$vg");
    _need(@$pvs == 1 && $pvs->[0]->{pv_uuid} eq $scfg->{'slt-expected-pv-uuid'} &&
        $pvs->[0]->{pv_name} eq $device && $pvs->[0]->{vg_uuid} eq $scfg->{'slt-expected-vg-uuid'},
        'PV/WWID identity mismatch');
    my $rows = $self->_report('lvs', 'lv', \@LV_FIELDS, $device, $vg);
    my (%lvs, %uuids);
    for my $row (@$rows) {
        _need($row->{vg_name} eq $vg, 'LV outside pinned VG');
        $row->{$_} = _lvname($row->{$_}) for qw(lv_name pool_lv origin data_lv metadata_lv);
        my $name = $row->{lv_name};
        _need($name ne '' && !$lvs{$name} && _uuid($row->{lv_uuid}) &&
            $row->{lv_size} =~ /^[1-9][0-9]*$/ && length($row->{lv_attr}) >= 5, 'incomplete/duplicate LV identity');
        (my $normalized = $row->{lv_uuid}) =~ s/-//g;
        _need(!$uuids{$normalized}++, 'duplicate normalized LV UUID');
        $lvs{$name} = { %$row, tags => $row->{lv_tags}, lv_type => substr($row->{lv_attr}, 0, 1),
            lv_state => substr($row->{lv_attr}, 4, 1) };
    }
    my $dm = $self->{dm_reader}->();
    _need(ref($dm) eq 'ARRAY' && @$dm <= 8192, 'DM inventory unavailable');
    my (%names, %dm_uuids);
    for my $row (@$dm) {
        _need(ref($row) eq 'ARRAY' && @$row == 3 && _name($row->[0]) &&
            defined($row->[1]) && !ref($row->[1]) && $row->[1] =~ /^[A-Za-z0-9_.+-]*$/ &&
            ($row->[2] // '') =~ /^\d+$/ && !$names{$row->[0]}++, 'malformed/duplicate DM row');
        _need(!$dm_uuids{$row->[1]}++, 'duplicate DM UUID') if $row->[1] ne '';
    }
    return { lvs => \%lvs, dm => [sort { $a->[0] cmp $b->[0] } @$dm], vg => $vgs->[0], pv => $pvs->[0] };
}

sub collect {
    my ($self) = @_;
    my $cfg = _copy($self->{config_reader}->());
    _need(ref($cfg) eq 'HASH' && ref($cfg->{ids}) eq 'HASH', 'storage config unavailable');
    my $sid = $self->{storage_id};
    my $scfg = $cfg->{ids}->{$sid};
    _need(ref($scfg) eq 'HASH' && ($scfg->{type} // '') eq 'sharedlvmthin' && $scfg->{shared}, 'storage unsupported');
    _need(_name($scfg->{'slt-vgname'}) && _uuid($scfg->{'slt-expected-vg-uuid'}) &&
        _uuid($scfg->{'slt-expected-pv-uuid'}) && ($scfg->{'slt-expected-wwid'} // '') =~ /^[0-9A-Fa-f]+$/,
        'storage identity not pinned');
    my @aliases = sort grep { ref($cfg->{ids}->{$_}) eq 'HASH' &&
        ($cfg->{ids}->{$_}->{type} // '') eq 'sharedlvmthin' &&
        ($cfg->{ids}->{$_}->{'slt-vgname'} // '') eq $scfg->{'slt-vgname'} } keys %{$cfg->{ids}};
    for my $alias (@aliases) {
        _need($cfg->{ids}->{$alias}->{shared}, 'unshared sibling alias');
        _need(($cfg->{ids}->{$alias}->{$_} // '') eq $scfg->{$_}, 'sibling physical identity mismatch')
            for qw(slt-expected-vg-uuid slt-expected-pv-uuid slt-expected-wwid);
    }
    my $identity = $self->{identity_reader}->();
    _need(ref($identity) eq 'ARRAY' && @$identity == 2 && _name($identity->[0]) &&
        ($identity->[1] // '') =~ /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/, 'node/boot unknown');
    my $sample = $self->_sample($cfg, $sid, $scfg);
    # Perl validators can change scalar numeric/string flags or normalize
    # decoder inputs. Preserve the raw canonical sample BEFORE invoking any
    # plugin helper; decoding must not masquerade as an external inventory race.
    my $sample_canonical = _json($sample);
    my $vg = $scfg->{'slt-vgname'};
    my $lvs = _copy($sample->{lvs});
    my $doc = { schema => 'sharedlvmthin-destroy-inventory/v1', complete => 1,
        scope => { node => $identity->[0], boot_id => $identity->[1], storage_ids => \@aliases,
            vg_uuid => $scfg->{'slt-expected-vg-uuid'}, pv_uuid => $scfg->{'slt-expected-pv-uuid'},
            wwid => $scfg->{'slt-expected-wwid'}, config_sha256 => _hash($cfg) },
        roots => [], objects => [], runtimes => [] };
    my (%claimed, %expected_dm, %dm_names, %bindings, %root_owner);
    $bindings{objects} = {}; $bindings{runtimes} = {};
    my $add_root = sub {
        my ($root_name, $mode, $owner, $volids, $kinds, $origins, $member_volids) = @_;
        $volids = [$volids] if !ref($volids);
        $member_volids //= {};
        my $root_id = "lv:$root_name";
        _need(!$root_owner{$root_id}, 'root claimed twice');
        $root_owner{$root_id} = "$owner";
        my $root = { id => $root_id, mode => $mode, owner_vmid => "$owner", namespace => $doc->{scope}->{vg_uuid},
            phase => $mode eq 'thin' ? 'OWNED_INACTIVE' : $mode eq 'lazy' ? 'LAZY_DORMANT' : 'MATERIALIZED',
            object_ids => [], runtime_ids => [], volume_ids => [sort @$volids] };
        for my $name (sort keys %$kinds) {
            my $info = $lvs->{$name};
            _need($info && !$claimed{$name}++, 'missing or multiply owned LV');
            my $id = "lv:$name";
            push @{$root->{object_ids}}, $id;
            push @{$doc->{objects}}, { id => $id, uuid => $info->{lv_uuid}, root_id => $root_id,
                kind => $kinds->{$name}, owner_vmid => "$owner", namespace => $root->{namespace},
                volume_id => $name eq $root_name || $kinds->{$name} =~ /^thin_(?:data|metadata)$/ ? ''
                    : ($member_volids->{$name} // $volids->[0]),
                origin_uuid => $origins->{$name} // '' };
            $bindings{objects}->{$id} = { name => $name, uuid => $info->{lv_uuid} } if "$owner" eq "$self->{vmid}";
            my $dm_name = _dmname($vg, $name);
            _need(!$expected_dm{$dm_name}, 'duplicate expected DM name');
            $expected_dm{$dm_name} = { uuid => _lvm_uuid($doc->{scope}->{vg_uuid}, $info->{lv_uuid}), root => $root };
        }
        push @{$doc->{roots}}, $root;
        return $root;
    };

    # Anchors are discovered through the real strict tag decoder, never by
    # inventing object names. The plugin revalidates full HEAD/tree ownership.
    for my $name (sort keys %$lvs) {
        next if $lvs->{$name}->{tags} !~ /(?:^|,)slt_tg_v=/;
        my $state = decode_anchor_tags($lvs->{$name}->{tags});
        my $alias = $state->{sid};
        _need(scalar(grep({ $_ eq $alias } @aliases)), 'anchor belongs to foreign storage');
        my $acfg = $cfg->{ids}->{$alias};
        _need($PLUGIN->_allocation_mode($acfg) ne 'thin', 'Thick anchor under Thin alias');
        my ($verified, undef, $anchor) = $PLUGIN->_thick_read_anchor($alias, $acfg, $state->{vol}, {$vg => _copy($lvs)});
        _need($anchor eq $name, 'anchor name/tag identity mismatch');
        my (undef, undef, $owner, undef, undef, $base) = $PLUGIN->parse_volname($state->{vol});
        _need(!$base, 'base volume unsupported');
        my ($mode, %kinds, %origins);
        if ($verified->{v} == 6) {
            _need($PLUGIN->_allocation_mode($acfg) eq 'thick-generations-lazy' &&
                $verified->{phase} eq 'LAZY_DORMANT' && $verified->{owner_node} eq 'none' &&
                $verified->{owner_boot} eq 'none' && $verified->{owner_epoch} eq 'none', 'Lazy state not unowned DORMANT');
            $mode = 'lazy';
            %kinds = ($anchor => 'anchor', $verified->{head} => 'lazy_data', $verified->{metadata} => 'lazy_metadata');
            my @forbidden = $PLUGIN->_lazy_runtime_names($acfg, $state->{vol});
            my %forbidden = map { $_ => 1 } @forbidden;
            _need(!grep({ $forbidden{$_->[0]} || $_->[1] eq "SLT-TG6-ZERO-$verified->{tx}" ||
                $_->[1] eq "SLT-TG6-CLONE-$verified->{tx}" } @{$sample->{dm}}), 'DORMANT Lazy runtime exists');
        } else {
            _need($verified->{v} == 5 && $verified->{phase} eq 'MATERIALIZED', 'Thick transition incomplete');
            my $tree = $PLUGIN->_thick_tree_plan($alias, $acfg, $state->{vol}, {$vg => _copy($lvs)});
            $mode = 'thick';
            %kinds = map { $_ => ($_ eq $anchor ? 'anchor' : $_ eq $tree->{head} ? 'generation' : 'snapshot') } keys %{$tree->{entries}};
        }
        my $root = $add_root->($anchor, $mode, $owner, "$alias:$state->{vol}", \%kinds, \%origins);
        my $front = mapper_name($PLUGIN->_thick_namespace($acfg), $state->{vol});
        $expected_dm{$front} = { uuid => 'SLT-TG2-' . object_key($PLUGIN->_thick_namespace($acfg), $state->{vol}), root => $root }
            if $mode eq 'thick';
    }
    for my $pool (sort keys %$lvs) {
        next if $lvs->{$pool}->{lv_type} ne 't';
        my @tags = grep { /^pve-slt-sid-/ } split(/,/, $lvs->{$pool}->{tags});
        _need(@tags == 1, 'Thin storage ownership ambiguous');
        (my $alias = $tags[0]) =~ s/^pve-slt-sid-//;
        _need(scalar(grep({ $_ eq $alias } @aliases)), 'Thin foreign storage ownership');
        _need($PLUGIN->_allocation_mode($cfg->{ids}->{$alias}) eq 'thin', 'Thin pool under Thick alias');
        my $ownership = PVE::Storage::Custom::SharedLvmThinPlugin::_thin_owner_state_from_tags($lvs->{$pool}->{tags});
        _need($ownership->{schema} == 1 && !defined($ownership->{owner}) && !defined($ownership->{epoch}),
            'Thin owner is foreign, active or legacy');
        my @members = grep { $lvs->{$_}->{pool_lv} eq $pool } sort keys %$lvs;
        my @heads = grep { $lvs->{$_}->{origin} eq '' && $_ =~ /^(?:vm|base)-/ } @members;
        _need(scalar(@heads), 'Thin root has no authoritative head');
        my ($owner, %head_volids);
        for my $head (@heads) {
            my (undef, undef, $head_owner, undef, undef, $base) = $PLUGIN->parse_volname($head);
            _need(!$base && $pool eq "sltp-$head_owner" &&
                (!defined($owner) || $owner eq $head_owner), 'Thin pool/VM ownership mismatch');
            $owner = $head_owner;
            $head_volids{$head} = "$alias:$head";
        }
        my $data = $lvs->{$pool}->{data_lv}; my $meta = $lvs->{$pool}->{metadata_lv};
        _need($data ne '' && $meta ne '' && $data ne $meta && $data ne $pool && $meta ne $pool,
            'Thin structural LV references unavailable');
        my %kinds = ($pool => 'thin_pool', $data => 'thin_data', $meta => 'thin_metadata',
            map { $_ => 'thin_lv' } @heads);
        my %origins;
        my %member_volids = %head_volids;
        for my $member (@members) {
            _need($lvs->{$member}->{lv_type} eq 'V', 'Thin member has unexpected LV type');
            next if exists($head_volids{$member});
            my $head = $lvs->{$member}->{origin};
            _need(exists($head_volids{$head}), 'Thin snapshot origin is not an owned head');
            _need(scalar($member =~ /\Asnap_\Q$head\E_[A-Za-z0-9][A-Za-z0-9_.-]*\z/) &&
                $lvs->{$member}->{origin} eq $head, 'Thin snapshot ownership/origin ambiguous');
            $kinds{$member} = 'snapshot'; $origins{$member} = $lvs->{$head}->{lv_uuid};
            $member_volids{$member} = $head_volids{$head};
        }
        my $root = $add_root->($pool, 'thin', $owner, [sort values %head_volids], \%kinds, \%origins, \%member_volids);
        # Same canonical hidden-pool identity used by _thin_pool_dm_identity.
        $expected_dm{_dmname($vg, $pool) . '-tpool'} = {
            uuid => _lvm_uuid($doc->{scope}->{vg_uuid}, $lvs->{$pool}->{lv_uuid}) . '-tpool', root => $root };
    }
    $doc->{opaque_foreign_objects} = [];
    my (%opaque_dm_names, %opaque_dm_uuids);
    for my $name (sort grep { !$claimed{$_} } keys %$lvs) {
        my $row = $lvs->{$name};
        _need($row->{tags} !~ /(?:^|,)(?:slt_|pve-slt-)/ &&
            $name !~ /^(?:sltg-|sltp-|vm-|base-|snap_)/,
            'unclassified plugin-owned object');
        push @{$doc->{opaque_foreign_objects}}, { type => 'lvm-lv', name => $name,
            uuid => $row->{lv_uuid}, scope_uuid => $doc->{scope}->{vg_uuid}, evidence_sha256 => _hash($row) };
        my $dm_name = _dmname($vg, $name);
        my $uuid = _lvm_uuid($doc->{scope}->{vg_uuid}, $row->{lv_uuid});
        $opaque_dm_names{$dm_name} = $uuid;
        $opaque_dm_uuids{$uuid} = $dm_name;
    }
    my %uuid_to_name;
    for my $name (keys %expected_dm) {
        _need(!$uuid_to_name{$expected_dm{$name}->{uuid}}, 'ambiguous expected DM UUID');
        $uuid_to_name{$expected_dm{$name}->{uuid}} = $name;
    }
    my $vg_prefix = _lvm_uuid($doc->{scope}->{vg_uuid}, '');
    my $vg_name_prefix = _dmname($vg, '');
    for my $row (@{$sample->{dm}}) {
        my ($name, $uuid, $opens) = @$row;
        my $expected = $expected_dm{$name};
        if (!$expected) {
            if (exists($opaque_dm_names{$name}) || exists($opaque_dm_uuids{$uuid})) {
                _need(($opaque_dm_names{$name} // '') eq $uuid &&
                    ($opaque_dm_uuids{$uuid} // '') eq $name, 'opaque foreign DM name/UUID conflict');
                next; # Complete raw DM identity/open count retained in foreign evidence.
            }
            _need(!$uuid_to_name{$uuid} && index($uuid, $vg_prefix) != 0 &&
                index($name, $vg_name_prefix) != 0 && $name !~ /^sltg-/ && $uuid !~ /^SLT-TG/,
                'unclassified or renamed scoped DM object');
            next;
        }
        _need($uuid ne '' && $uuid eq $expected->{uuid}, 'DM name/UUID conflict');
        _need($opens == 0, 'open backing/runtime unsupported; explicit quiescence required');
        _need($expected->{root}->{mode} ne 'lazy' && $expected->{root}->{mode} ne 'thin',
            'inactive Thin/DORMANT Lazy retains kernel mapping');
        my $id = "dm:$name";
        push @{$expected->{root}->{runtime_ids}}, $id;
        push @{$doc->{runtimes}}, { id => $id, dm_uuid => $uuid, root_id => $expected->{root}->{id} };
        $bindings{runtimes}->{$id} = { name => $name, uuid => $uuid }
            if $expected->{root}->{owner_vmid} eq "$self->{vmid}";
    }
    # Two complete samples bracket decoding. This detects observed races, not
    # a lock/fence; an executor must still revalidate under its own admission.
    _need($sample_canonical eq _json($self->_sample($cfg, $sid, $scfg)), 'LV/DM inventory changed during collection');
    _need(_json($cfg) eq _json($self->{config_reader}->()) &&
        _json($identity) eq _json($self->{identity_reader}->()), 'config/node/boot changed during collection');
    $doc = PVE::SharedLvmThinVMDestroyStoragePlan::_validate_inventory($doc);
    my %target = map { substr($_, 3) => 1 } keys %{$bindings{objects}};
    my $raw_sample = $JSON->decode($sample_canonical);
    my %foreign = map { $_ => $raw_sample->{lvs}->{$_} } grep { !$target{$_} } keys %$lvs;
    my %target_dm = map { substr($_, 3) => 1 } keys %{$bindings{runtimes}};
    my $foreign = { lvs => \%foreign, dm => [grep { !$target_dm{$_->[0]} } @{$raw_sample->{dm}}],
        vg_tags => $raw_sample->{vg}->{vg_tags}, pv => $raw_sample->{pv} };
    return { authority => 'NONE', inventory => $doc, bindings => \%bindings,
        foreign_evidence => $foreign, foreign_evidence_sha256 => _hash($foreign) };
}

sub plan {
    my ($self, $volumes) = @_;
    my $collected = $self->collect();
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $collected->{inventory} });
    return { %$collected, storage_plan => $planner->plan(vmid => $self->{vmid}, volumes => $volumes) };
}

sub observe {
    my ($self, $bundle) = @_;
    _need(ref($bundle) eq 'HASH' && ($bundle->{authority} // '') eq 'NONE' &&
        _hash($bundle->{foreign_evidence}) eq ($bundle->{foreign_evidence_sha256} // ''), 'invalid baseline bundle');
    my $now = $self->collect();
    _need($now->{foreign_evidence_sha256} eq $bundle->{foreign_evidence_sha256}, 'raw foreign namespace drift');
    my $planner = PVE::SharedLvmThinVMDestroyStoragePlan->new(collector => sub { $now->{inventory} });
    return { %{$planner->observe($bundle->{storage_plan})},
        foreign_evidence_sha256 => $now->{foreign_evidence_sha256} };
}

1;
