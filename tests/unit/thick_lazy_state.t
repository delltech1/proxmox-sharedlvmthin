use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinThick qw(
    anchor_tags decode_anchor_tags validate_anchor_transition
    lazy_object_tags decode_lazy_object_tags validate_lazy_object_tags
    materialized_rebase_state classify_recovery
);

my $tx = '0123456789abcdef0123456789abcdef';
sub lazy_state {
    return {
        v => 6, sid => 'lazy-a', vol => 'vm-900-disk-0',
        phase => 'LAZY_PREPARED', tx => $tx, op => 'ALLOC', snapshot => 'none',
        source => 'data0', old => 'data0', new => 'data0', head => 'data0',
        generation => 0, region => 2048, policy => 'lazy-zero',
        bytes => 8 * 1024 * 1024 * 1024, metadata => 'meta0',
        data_uuid => 'AAAA-bbbb-1111', metadata_uuid => 'CCCC-dddd-2222',
        zero_source => 'dm-zero', publication => 0,
        owner_node => 'none', owner_boot => 'none', owner_epoch => 'none',
        @_,
    };
}

my $prepared = lazy_state();
my $encoded = anchor_tags(%$prepared);
my $decoded = decode_anchor_tags($encoded);
is_deeply(
    { map { $_ => "$prepared->{$_}" } keys %$prepared },
    { map { $_ => "$decoded->{$_}" } keys %$prepared },
    'v6 Lazy anchor round-trips every authoritative field',
);

my $dormant = lazy_state(phase => 'LAZY_DORMANT', publication => 1);
ok(validate_anchor_transition($prepared, $dormant),
    'prepared Lazy allocation can publish one dormant epoch');

my %owner = (
    owner_node => 'node-a',
    owner_boot => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    owner_epoch => '11111111111111111111111111111111',
);
my $claimed = lazy_state(
    phase => 'LAZY_CLAIMED', publication => 2, %owner,
);
my $active = lazy_state(
    phase => 'LAZY_ACTIVE', publication => 2, %owner,
);
my $materializing = lazy_state(
    phase => 'MATERIALIZING', publication => 2, %owner,
);
my $complete = lazy_state(
    phase => 'HYDRATION_COMPLETE', publication => 2, %owner,
);
my $pivoted = lazy_state(
    phase => 'LINEAR_PIVOTED', publication => 2, %owner,
);
my $materialized = lazy_state(
    phase => 'MATERIALIZED', publication => 2,
);

ok(validate_anchor_transition($dormant, $claimed), 'dormant Lazy volume can be claimed');
ok(validate_anchor_transition($claimed, $active), 'claimed Lazy volume can publish active');
ok(validate_anchor_transition($active, $materializing), 'active Lazy volume can materialize');
ok(validate_anchor_transition($materializing, $complete), 'materialization can complete');
ok(validate_anchor_transition($complete, $pivoted), 'complete clone can pivot');
ok(validate_anchor_transition($pivoted, $materialized), 'pivot can settle materialized');

my $closed = lazy_state(phase => 'LAZY_DORMANT', publication => 2);
ok(validate_anchor_transition($active, $closed),
    'clean deactivation can return active Lazy volume to dormant');

eval { anchor_tags(%$dormant, owner_node => 'node-a') };
like($@, qr/must clear owner identity/, 'dormant state cannot retain partial owner evidence');
eval { anchor_tags(%$active, owner_epoch => 'none') };
like($@, qr/requires an exact owner identity/, 'active state requires complete owner evidence');
eval { anchor_tags(%$prepared, publication => 1) };
like($@, qr/cannot have a publication epoch/, 'prepared state cannot masquerade as published');
eval { validate_anchor_transition($dormant, { %$claimed, publication => 3 }) };
like($@, qr/advance the publication epoch exactly once/,
    'claim cannot skip publication epochs');
eval { validate_anchor_transition($active, { %$materializing, metadata_uuid => 'other' }) };
like($@, qr/immutable field 'metadata_uuid'/,
    'active transition cannot change persistent clone metadata identity');
eval { validate_anchor_transition($dormant, $materialized) };
like($@, qr/illegal Lazy Thick anchor phase transition/,
    'dormant Lazy disk cannot be relabeled materialized without hydration and pivot');

my @unknown = @$encoded;
$unknown[0] = 'slt_tg_v=7';
eval { decode_anchor_tags(\@unknown) };
like($@, qr/unsupported/, 'unknown future anchor version fails closed');

for my $kind (qw(data metadata)) {
    my $tags = lazy_object_tags(
        sid => 'lazy-a', vol => 'vm-900-disk-0', tx => $tx,
        kind => $kind, bytes => ($kind eq 'data' ? 8 * 1024**3 : 20 * 1024**2),
        region => 2048,
    );
    my $object = decode_lazy_object_tags($tags);
    is($object->{kind}, $kind, "strict Lazy $kind object role round-trips");
    ok(validate_lazy_object_tags($tags,
        sid => 'lazy-a', vol => 'vm-900-disk-0', tx => $tx,
        kind => $kind, bytes => ($kind eq 'data' ? 8 * 1024**3 : 20 * 1024**2),
        region => 2048,
    ), "strict Lazy $kind object role validates exactly");
}
my $lazy_data = lazy_object_tags(
    sid => 'lazy-a', vol => 'vm-900-disk-0', tx => $tx,
    kind => 'data', bytes => 8 * 1024**3, region => 2048,
);
eval { validate_lazy_object_tags($lazy_data,
    sid => 'lazy-a', vol => 'vm-900-disk-0', tx => $tx,
    kind => 'metadata', bytes => 8 * 1024**3, region => 2048,
) };
like($@, qr/ownership proof mismatch/, 'data object cannot be reinterpreted as metadata');
eval { lazy_object_tags(
    v => 2, sid => 'lazy-a', vol => 'vm-900-disk-0', tx => $tx,
    kind => 'data', bytes => 8 * 1024**3, region => 2048,
) };
like($@, qr/unsupported Lazy Thick object version/,
    'future Lazy object schema is rejected explicitly');

eval { materialized_rebase_state($materialized, 'abcdef0123456789abcdef0123456789') };
like($@, qr/unavailable for Lazy Thick v6/,
    'generic v5 materialized rebase cannot reinterpret Lazy state');
my $classification = classify_recovery(
    anchor => $materialized, objects => { head => 1 }, runtime => 'absent',
);
is($classification->{state}, 'RECOVERY_REQUIRED',
    'generic recovery classifier fails closed for Lazy state');
like($classification->{reason}, qr/dedicated identity and owner classifier/,
    'generic recovery refusal names the missing Lazy classifier');

done_testing();
