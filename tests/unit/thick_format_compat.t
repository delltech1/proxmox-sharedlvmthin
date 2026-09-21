#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinThick qw(
    anchor_name anchor_tags decode_anchor_tags
    clone_geometry
    generation_name generation_tags decode_generation_tags
    mapper_name object_key transition_tags validate_transition_tags
    vg_intent_tags decode_vg_intent_tags
);

# These fixtures are the published TG32 persistent-format contract. A change
# here is not an ordinary refactor: it requires an explicit format migration,
# mixed-version recovery design and a new qualification cycle.
my $sid = 'store-a';
my $vol = 'vm-100-disk-0';
my $key = '2345f20287eadf3985496abe';
my $head = "sltg-g-$key-00000000";

is(object_key($sid, $vol), $key, 'published object-key derivation is stable');
is(anchor_name($sid, $vol), "sltg-a-$key", 'published anchor name is stable');
is(mapper_name($sid, $vol), "sltg-$key", 'published frontend name is stable');
is(generation_name($sid, $vol, 0), $head, 'published generation name is stable');

is_deeply(clone_geometry(1024 * 1024 * 1024), {
    region_sectors => 8,
    regions => 262144,
    metadata_bytes => 20971520,
}, 'published 1 GiB clone geometry remains stable');
is_deeply(clone_geometry(1024 * 1024 * 1024 * 1024), {
    region_sectors => 16,
    regions => 134217728,
    metadata_bytes => 150994944,
}, 'published 1 TiB clone geometry remains stable');

my $anchor = anchor_tags(
    sid => $sid, vol => $vol, phase => 'MATERIALIZED',
    tx => ('1' x 32), op => 'ALLOC', snapshot => 'none',
    source => $head, old => $head, new => $head, head => $head,
    generation => 0, region => 8,
);
is_deeply($anchor, [
    'slt_tg_v=5',
    'slt_tg_sid=store-a',
    'slt_tg_vol=vm-100-disk-0',
    'slt_tg_phase=MATERIALIZED',
    'slt_tg_tx=' . ('1' x 32),
    'slt_tg_op=ALLOC',
    'slt_tg_snapshot=none',
    "slt_tg_source=$head",
    "slt_tg_old=$head",
    "slt_tg_new=$head",
    "slt_tg_head=$head",
    'slt_tg_generation=0',
    'slt_tg_region=8',
    'slt_tg_sha256=3be3ec7d2f419f4811a41ff374a4e760',
], 'published signed anchor encoding is byte-for-byte stable');
is(decode_anchor_tags($anchor)->{head}, $head,
    'published anchor fixture remains decodable');

my $generation = generation_tags(
    sid => $sid, vol => $vol, role => 'head', generation => 0,
);
is_deeply($generation, [
    'slt_tgo_v=1',
    'slt_tgo_sid=store-a',
    'slt_tgo_vol=vm-100-disk-0',
    'slt_tgo_role=head',
    'slt_tgo_generation=0',
    'slt_tgo_sha256=8ca4bda12ba5cb67f66f8f330f751226',
], 'published signed generation encoding is byte-for-byte stable');
is(decode_generation_tags($generation)->{role}, 'head',
    'published generation fixture remains decodable');

my $transition = transition_tags(
    sid => $sid, vol => $vol, tx => ('2' x 32),
    kind => 'metadata', generation => 1, region => 8,
);
is_deeply($transition, [
    'slt_tgt_v=1',
    'slt_tgt_sid=store-a',
    'slt_tgt_vol=vm-100-disk-0',
    'slt_tgt_tx=' . ('2' x 32),
    'slt_tgt_kind=metadata',
    'slt_tgt_generation=1',
    'slt_tgt_region=8',
    'slt_tgt_sha256=e1aaa1141c921618df9c458413281305',
], 'published signed transition-artifact encoding is stable');
ok(validate_transition_tags($transition,
    sid => $sid, vol => $vol, tx => ('2' x 32),
    kind => 'metadata', generation => 1, region => 8),
    'published transition fixture validates');

my $intent = vg_intent_tags(
    tx => ('3' x 32), state => 'OPEN', op => 'DM_CUTOVER',
    object => "sltg-a-$key", before => ('4' x 32),
);
is_deeply($intent, [
    'slt_tg_vgi_v=1',
    'slt_tg_vgi_tx=' . ('3' x 32),
    'slt_tg_vgi_state=OPEN',
    'slt_tg_vgi_op=DM_CUTOVER',
    "slt_tg_vgi_object=sltg-a-$key",
    'slt_tg_vgi_before=' . ('4' x 32),
    'slt_tg_vgi_sha256=5a17db7aa4d403dfb30af2541c8a8edf',
], 'published signed VG-intent encoding is byte-for-byte stable');
is(decode_vg_intent_tags($intent)->{op}, 'DM_CUTOVER',
    'published VG-intent fixture remains decodable');

done_testing();
