#!/usr/bin/perl
use strict;
use warnings;
use Test::More;
use Storable qw(dclone);
use PVE::Storage;
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::API2::Storage::Config;
{ package Local::FakeBlockStat; sub mode { return 0060000 } }

# Runs against real PVE base classes but substitutes a read-only configuration
# snapshot. No real PVE storage configuration or block device is written.
my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $common = {
    type => 'sharedlvmthin', shared => 1,
    'slt-vgname' => 'fixture-vg', 'slt-vg-layout' => 'isolated',
    'slt-expected-vg-uuid' => 'fixture-vg-uuid',
    'slt-expected-pv-uuid' => 'fixture-pv-uuid',
    'slt-expected-wwid' => '3600abcd', 'slt-vg-reserve-percent' => 5,
    nodes => 'node-a,node-b',
};
my $eager = { %$common, 'slt-allocation-mode' => 'thick-generations' };
my $lazy = { %$common, 'slt-allocation-mode' => 'thick-generations-lazy' };
my $saved = { ids => { eager => $eager } };
my $original = dclone($saved);

{
    my $volume = 'vm-100-disk-0';
    my $anchor = PVE::SharedLvmThinThick::anchor_name(lc($common->{'slt-expected-vg-uuid'}), $volume);
    my $key = PVE::SharedLvmThinThick::object_key(lc($common->{'slt-expected-vg-uuid'}), $volume);
    my $generation = "sltg-g-$key-00000001";
    my $rows = { 'fixture-vg' => { $anchor => { tags => 'fixture' },
        head => { tags => 'fixture' }, $generation => { tags => 'fixture' } } };
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::decode_anchor_tags = sub {
        return { sid => 'eager', vol => $volume, head => 'head', generation => 0 };
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::validate_generation_tags = sub { 1 };
    my @identity_checks;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub {
        push @identity_checks, [ @_ ]; return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { return $rows };
    my ($path_eager, $owner, $content) = $class->path($eager, $volume, 'eager', undef);
    like($path_eager, qr{^/dev/mapper/sltg-}, 'owner HEAD path retains raw mapper form');
    is($owner, 100, 'HEAD path returns VM owner');
    is($content, 'images', 'HEAD path returns content type');
    is($identity_checks[0]->[3], '/dev/mapper/3600abcd', 'path scopes physical identity to pinned WWID');
    is($identity_checks[0]->[4], 30, 'path bounds each physical identity probe with configured default');
    my $path_ok = eval { $class->path($lazy, $volume, 'lazy', undef); 1 };
    ok(!$path_ok, 'public HEAD path refuses sibling storage ID');
    my $ok = eval { $class->_thick_read_anchor('lazy', $lazy, $volume, $rows); 1 };
    ok(!$ok, 'authenticated anchor rejects a sibling alias for the same volume');
    like($@, qr/belongs to another storage or volume/, 'anchor ownership failure is explicit');
    local *PVE::Storage::Custom::SharedLvmThinPlugin::decode_generation_tags = sub {
        return { sid => 'eager', vol => $volume, role => 'snapshot', generation => 1, snapshot => 'snap1' };
    };
    is($class->path($eager, $volume, 'eager', 'snap1'), "/dev/fixture-vg/$generation",
        'owner snapshot path retains the generation format');
    my $snapshot_ok = eval { $class->path($lazy, $volume, 'lazy', 'snap1'); 1 };
    ok(!$snapshot_ok, 'public snapshot path refuses sibling storage ID');
    {
        no warnings 'once';
        local *File::stat::stat = sub { return bless {}, 'Local::FakeBlockStat' };
        my $block = $class->qemu_blockdev_options($eager, 'eager', $volume, '9.0', {});
        is($block->{driver}, 'host_device', 'real PVE blockdev construction accepts authorized Thick HEAD');
        is($block->{filename}, $path_eager, 'real PVE blockdev uses the verified mapper path');
        my $bad_block = eval { $class->qemu_blockdev_options($lazy, 'lazy', $volume, '9.0', {}); 1 };
        ok(!$bad_block, 'real PVE blockdev construction refuses sibling alias');
    }
    {
        local *PVE::Storage::Custom::SharedLvmThinPlugin::decode_anchor_tags = sub {
            return { sid => 'lazy', vol => $volume, head => 'head', generation => 0 };
        };
        $snapshot_ok = eval { $class->path($lazy, $volume, 'lazy', 'snap1'); 1 };
        ok(!$snapshot_ok, 'snapshot generation must match the authenticated owner independently');
        like($@, qr/missing or ambiguous/, 'foreign snapshot is not returned');
    }
    my $no_sid_ok = eval { $class->path($eager, $volume, undef, undef); 1 };
    ok(!$no_sid_ok, 'Thick public path refuses missing storage ID');
    {
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { die "VG UUID mismatch\n" };
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { die "unexpected LV inventory\n" };
        my $wrong_vg_ok = eval { $class->path($eager, $volume, 'eager', undef); 1 };
        ok(!$wrong_vg_ok, 'incorrect physical identity refuses path');
        like($@, qr/VG UUID mismatch/, 'physical identity is checked before LV inventory');
    }
    {
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { die "scoped inventory timeout\n" };
        my $timeout_ok = eval { $class->path($eager, $volume, 'eager', undef); 1 };
        ok(!$timeout_ok, 'unavailable scoped inventory does not return a guessed path');
        like($@, qr/scoped inventory timeout/, 'inventory failure is propagated');
    }
    delete $rows->{'fixture-vg'}->{$anchor};
    my $missing_ok = eval { $class->path($eager, $volume, 'eager', undef); 1 };
    ok(!$missing_ok, 'Thick public path refuses missing anchor evidence');
}

no warnings 'redefine';
local *PVE::Storage::config = sub { return dclone($saved) };
my $ok = eval { $class->on_add_hook('lazy', $lazy); 1 };
ok($ok, 'real PVE parent hook accepts a valid candidate pair') or diag($@);
is_deeply($saved, $original, 'candidate validation does not change saved inventory');

for my $case (
    [ 'reserve', { %$lazy, disable => 1, 'slt-vg-reserve-percent' => 7 }, qr/identical protected VG reserve/ ],
    [ 'scope', { %$lazy, nodes => 'node-a' }, qr/same PVE node scope/ ],
    [ 'identity', { %$lazy, 'slt-expected-pv-uuid' => 'other' }, qr/disagree/ ],
    [ 'VG UUID case', { %$lazy, 'slt-expected-vg-uuid' => 'Fixture-vg-uuid' }, qr/conflicts.*persistent namespace/ ],
    [ 'PV UUID case', { %$lazy, 'slt-expected-pv-uuid' => 'Fixture-pv-uuid' }, qr/disagree/ ],
) {
    my ($name, $candidate, $expected) = @$case;
    my $success = eval { $class->on_add_hook('lazy', $candidate); 1 };
    ok(!$success, "$name mismatch is refused by server hook");
    like($@, $expected, "$name mismatch is reported");
    is_deeply($saved, $original, "$name refusal preserves saved config");
}

my $thin = { %$common, 'slt-allocation-mode' => 'thin' };
my $thin_same = eval { $class->on_add_hook('thin', $thin); 1 };
ok(!$thin_same, 'adding Thin cannot introduce a conflict with existing Thick');
like($@, qr/VG_MODE_CONFLICT/, 'reverse-order failure identifies VG conflict');
is_deeply($saved, $original, 'reverse-order refusal does not change saved config');

my $collision = { %$lazy, 'slt-vgname' => 'other-vg',
    'slt-expected-vg-uuid' => 'Fixture-vg-uuid', disable => 1 };
my $collision_ok = eval { $class->on_add_hook('other', $collision); 1 };
ok(!$collision_ok, 'case-only VG UUID collision is refused across VG names');
like($@, qr/conflicts.*persistent namespace/, 'namespace collision is explicit');
is_deeply($saved, $original, 'namespace refusal leaves committed config unchanged');

my $same_uuid_other_vg = { %$lazy, 'slt-vgname' => 'another-vg', disable => 1 };
my $same_uuid_ok = eval { $class->on_add_hook('same-uuid-other-vg', $same_uuid_other_vg); 1 };
ok(!$same_uuid_ok, 'same VG UUID with a different VG name is refused');
like($@, qr/conflicts.*persistent namespace/, 'same UUID naming conflict is explicit');
is_deeply($saved, $original, 'same UUID naming refusal leaves committed config unchanged');
for my $candidate_mode ('thin', 'thick-generations') {
    for my $other_mode ('thin', 'thick-generations') {
        my $left = { %$common, 'slt-allocation-mode' => $candidate_mode };
        my $right = { %$common, 'slt-allocation-mode' => $other_mode,
            'slt-vgname' => 'another-vg', disable => 1 };
        my $config = { ids => { self => $left, other => $right } };
        my $ok = eval { $class->_reject_conflicting_vg_uuid_names('self', $left, $config); 1 };
        ok(!$ok, "$candidate_mode versus disabled $other_mode rejects equal UUID across VG names");
    }
}
{
    my $ok = eval { $class->_reject_conflicting_vg_uuid_names('eager', $eager, { ids => { eager => $eager } }); 1 };
    ok($ok, 'UUID name check ignores self') or diag($@);
    my $without_uuid = { %$same_uuid_other_vg };
    delete $without_uuid->{'slt-expected-vg-uuid'};
    $ok = eval { $class->_reject_conflicting_vg_uuid_names('other', $without_uuid, $saved); 1 };
    ok($ok, 'UUID name check does not introduce mandatory Thin UUID') or diag($@);
}
my $same_uuid_thin = { %$same_uuid_other_vg, 'slt-allocation-mode' => 'thin' };
$same_uuid_ok = eval { $class->on_add_hook('same-uuid-thin', $same_uuid_thin); 1 };
ok(!$same_uuid_ok, 'Thin add also refuses same UUID under a different VG name');
like($@, qr/conflicts.*persistent namespace/, 'Thin same UUID conflict is explicit');
{
    my $broken = dclone({ ids => { eager => $eager, other => $same_uuid_other_vg } });
    local *PVE::Storage::config = sub { return dclone($broken) };
    my $ok = eval { $class->on_update_hook_full('other', $broken->{ids}->{other}, { disable => 1 }, [], {}); 1 };
    ok($ok, 'pure disable remains available for a pre-existing naming conflict') or diag($@);
    $ok = eval { $class->on_update_hook_full('other', $broken->{ids}->{other}, { disable => 0 }, [], {}); 1 };
    ok(!$ok, 'non-disable update refuses a pre-existing naming conflict');
}
{
    my $existing_thin = { %$thin, disable => 1 };
    my $cfg = dclone({ ids => { thin => $existing_thin } });
    local *PVE::Storage::config = sub { return dclone($cfg) };
    my $ok = eval { $class->on_add_hook('other-thick', $same_uuid_other_vg); 1 };
    ok(!$ok, 'Thick add refuses conflicting UUID when Thin was configured first');
    like($@, qr/conflicts.*persistent namespace/, 'reverse add reports UUID conflict');
    my $separate = { %$same_uuid_other_vg, 'slt-expected-vg-uuid' => 'unrelated-vg-uuid' };
    $ok = eval { $class->on_add_hook('other-thick', $separate); 1 };
    ok($ok, 'Thick add accepts another VG with a distinct UUID') or diag($@);
}
{
    my $bad_thin = { %$thin, 'slt-vgname' => 'another-vg' };
    my $cfg = dclone({ ids => { eager => $eager, thin => $bad_thin } });
    local *PVE::Storage::config = sub { return dclone($cfg) };
    my $ok = eval { $class->on_update_hook_full('thin', $bad_thin, { disable => 1 }, [], {}); 1 };
    ok($ok, 'pure disable remains available for conflicting Thin') or diag($@);
    $ok = eval { $class->on_update_hook_full('thin', $bad_thin, { disable => 0 }, [], {}); 1 };
    ok(!$ok, 'Thin non-disable update refuses conflicting UUID');
    like($@, qr/conflicts.*persistent namespace/, 'Thin update reports UUID conflict');
}

my $thin_collision = { %$thin, 'slt-vgname' => 'other-vg',
    'slt-expected-vg-uuid' => 'Fixture-vg-uuid', disable => 1 };
my $thin_collision_ok = eval { $class->on_add_hook('thin-other', $thin_collision); 1 };
ok(!$thin_collision_ok, 'Thin creation refuses case-only namespace collision on a different VG');
like($@, qr/conflicts.*persistent namespace/, 'Thin collision refusal is explicit');
is_deeply($saved, $original, 'Thin collision refusal leaves committed config unchanged');

{
    my $pair = { ids => { eager => $eager, lazy => $lazy } };
    my $copy = dclone($pair);
    local *PVE::Storage::config = sub { return dclone($pair) };
    my $ok = eval { $class->on_update_hook_full('lazy', $lazy, { 'slt-vg-reserve-percent' => 7 }, [], {}); 1 };
    ok(!$ok, 'PUT cannot break reserve parity');
    like($@, qr/identical protected VG reserve/, 'PUT parity mismatch is explicit');
    $ok = eval { $class->on_update_hook_full('lazy', $lazy, {}, ['slt-expected-pv-uuid'], {}); 1 };
    ok(!$ok, 'PUT cannot delete required Thick identity');
    $ok = eval { $class->on_update_hook_full('lazy', $lazy, { disable => 1 }, [], {}); 1 };
    ok($ok, 'a pure disable is allowed');
    is_deeply($pair, $copy, 'PUT validation leaves committed config unchanged');
}
{
    my $broken = { %$lazy, 'slt-vg-reserve-percent' => 7 };
    my $pair = { ids => { eager => $eager, lazy => $broken } };
    local *PVE::Storage::config = sub { return dclone($pair) };
    my $ok = eval { $class->on_update_hook_full('lazy', $broken, { 'slt-vg-reserve-percent' => 5 }, [], {}); 1 };
    ok($ok, 'PUT may restore a broken pair in a single change');
    $ok = eval { $class->on_update_hook_full('lazy', $broken, { type => 'sharedlvmthin', disable => 1 }, [], {}); 1 };
    ok($ok, 'an already broken pair can be disabled with the PVE type field');
}
{
    my $delta = $class->check_config('lazy', { disable => 1 }, 0, 1);
    is($delta->{type}, 'sharedlvmthin', 'real PVE check_config supplies plugin type');
    my $broken = { %$lazy, 'slt-vg-reserve-percent' => 7 };
    local *PVE::Storage::config = sub { die 'disable should not read invalid config' };
    my $ok = eval { $class->on_update_hook_full('lazy', $broken, $delta, [], {}); 1 };
    ok($ok, 'real PVE decoded disable can safely bypass invalid parity') or diag($@);
    my $fixed_ok = eval { $class->check_config('lazy', { 'slt-vgname' => 'other' }, 0, 1); 1 };
    ok(!$fixed_ok, 'real PVE update schema rejects changing fixed VG');
    $fixed_ok = eval { $class->check_config('lazy', { 'slt-allocation-mode' => 'thin' }, 0, 1); 1 };
    ok(!$fixed_ok, 'real PVE update schema rejects changing fixed allocation mode');
}
{
    my $remote = { %$thin, 'slt-vgname' => 'thin-vg', nodes => 'another-node' };
    my $pair = { ids => { thin => $remote } };
    local *PVE::Storage::config = sub { return dclone($pair) };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_package_flavor = sub { 'thick-only' };
    my $ok = eval { $class->on_update_hook_full('thin', $remote, { type => 'sharedlvmthin', disable => 0 }, [], {}); 1 };
    ok($ok, 'Thick-only node accepts an ordinary remote Thin config update') or diag($@);
    $pair->{ids}->{eager} = $eager;
    $pair->{ids}->{thin}->{'slt-expected-vg-uuid'} = 'Fixture-vg-uuid';
    $ok = eval { $class->on_update_hook_full('thin', $pair->{ids}->{thin}, { type => 'sharedlvmthin', disable => 0 }, [], {}); 1 };
    ok(!$ok, 'Thin PUT refuses case-only VG UUID collision');
    like($@, qr/conflicts.*persistent namespace/, 'Thin PUT collision is explicit');
}
{
    my $memory = dclone({ ids => { eager => $eager, lazy => $lazy } });
    my $writes = 0;
    local *PVE::Storage::config = sub { return dclone($memory) };
    local *PVE::Storage::lock_storage_config = sub { $_[0]->() };
    local *PVE::Storage::write_config = sub { $memory = dclone($_[0]); $writes++ };
    local *PVE::Storage::Plugin::lookup = sub { return $class };
    my $ok = eval { PVE::API2::Storage::Config->update({ storage => 'lazy', 'slt-vg-reserve-percent' => 7 }); 1 };
    ok(!$ok, 'real PVE PUT plumbing rejects alias mismatch');
    like($@, qr/identical protected VG reserve/, 'PVE PUT reaches our alias-parity refusal');
    is($writes, 0, 'rejected PVE PUT does not write even the in-memory config');
    $memory->{ids}->{lazy}->{'slt-vg-reserve-percent'} = 7;
    $ok = eval { PVE::API2::Storage::Config->update({ storage => 'lazy', disable => 1 }); 1 };
    ok($ok, 'real PVE PUT plumbing allows pure disable of a broken pair') or diag($@);
    is($writes, 1, 'valid PVE PUT writes only the in-memory config');
    is($memory->{ids}->{lazy}->{disable}, 1, 'PVE PUT preserves decoded disable');
}
{
    my $memory = dclone({ ids => { eager => $eager } });
    my ($writes, $activation_checks) = (0, 0);
    local *PVE::Storage::config = sub { return dclone($memory) };
    local *PVE::Storage::lock_storage_config = sub { $_[0]->() };
    local *PVE::Storage::write_config = sub { $memory = dclone($_[0]); $writes++ };
    local *PVE::Storage::Plugin::lookup = sub { return $class };
    local *PVE::Storage::storage_check_enabled = sub { $activation_checks++; return 0 };
    my $bad = { %$lazy, storage => 'lazy', 'slt-vg-reserve-percent' => 7, disable => 1 };
    my $ok = eval { PVE::API2::Storage::Config->create($bad); 1 };
    ok(!$ok, 'real PVE POST plumbing rejects mismatched alias');
    like($@, qr/identical protected VG reserve/, 'PVE POST reaches server parity refusal');
    is($writes, 0, 'rejected PVE POST does not write config');
    is($activation_checks, 0, 'rejected PVE POST does not reach activation');
    my $good = { %$lazy, storage => 'lazy', disable => 1 };
    $ok = eval { PVE::API2::Storage::Config->create($good); 1 };
    ok($ok, 'real PVE POST plumbing accepts matching disabled alias') or diag($@);
    is($writes, 1, 'valid PVE POST writes only in-memory config');
    is($activation_checks, 1, 'valid POST reaches stubbed activation check');
}

{
    my $effects = 0;
    my $conflict = { %$collision, type => 'sharedlvmthin' };
    local *PVE::Storage::config = sub { return { ids => { eager => $eager, other => $conflict } } };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub { $effects++; die 'unexpected quorum call' };
    my $activated = eval { $class->_thick_activate_volume('eager', $eager, 'vm-100-disk-0', undef, {}); 1 };
    ok(!$activated, 'direct activation rejects namespace collision');
    like($@, qr/conflicts.*persistent namespace/, 'direct activation reports namespace collision');
    is($effects, 0, 'direct activation stops before quorum or device operations');
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_assert_no_active_storage_worker = sub { $effects++; die 'unexpected worker call' };
    my $materialized = eval { $class->_lazy_materialize_volume_locked($eager, 'eager', 'vm-100-disk-0'); 1 };
    ok(!$materialized, 'direct Lazy materialization rejects namespace collision');
    like($@, qr/conflicts.*persistent namespace/, 'materialization reports namespace collision');
    is($effects, 0, 'materialization stops before worker or device operations');
}


done_testing();
