#!/usr/bin/perl
use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib";
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

use PVE::SharedLvmThinThick qw(anchor_name anchor_tags clone_geometry lazy_object_tags);
require PVE::Storage::Custom::SharedLvmThinPlugin;

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $sid = 'lazy-test';
my $vol = 'vm-900099-disk-0';
my $vg = 'testvg';
my $namespace = 'vg-uuid';
my $tx = '0123456789abcdef0123456789abcdef';
my $bytes = 1024 * 1024 * 1024;
my $region = 2048;
my $head = 'lazy-data';
my $metadata = 'lazy-metadata';
my $data_uuid = 'data-uuid';
my $metadata_uuid = 'metadata-uuid';
my $anchor = anchor_name($namespace, $vol);
my $geometry = clone_geometry($bytes, $region);
my $scfg = {
    'slt-vgname' => $vg,
    'slt-allocation-mode' => 'thick-generations',
    'slt-expected-vg-uuid' => $namespace,
    'slt-expected-wwid' => 'a' x 32,
};

sub inventory {
    my ($phase) = @_;
    my $owned = $phase =~ /^(?:LAZY_CLAIMED|LAZY_ACTIVE|MATERIALIZING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
    my %owner = $owned ? (
        owner_node => 'node-a',
        owner_boot => 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        owner_epoch => '11111111111111111111111111111111',
        publication => 2,
    ) : (
        owner_node => 'none', owner_boot => 'none', owner_epoch => 'none',
        publication => $phase eq 'LAZY_PREPARED' ? 0
            : $phase eq 'MATERIALIZED' ? 2 : 1,
    );
    my %state = (
        v => 6, sid => $sid, vol => $vol, phase => $phase, tx => $tx,
        op => 'ALLOC', snapshot => 'none', source => $head, old => $head,
        new => $head, head => $head, generation => 0, region => $region,
        policy => 'lazy-zero', bytes => $bytes, metadata => $metadata,
        data_uuid => $data_uuid, metadata_uuid => $metadata_uuid,
        zero_source => 'dm-zero', %owner,
    );
    return {$vg => {
        $anchor => {tags => join(',', @{anchor_tags(%state)})},
        $head => {
            tags => join(',', @{lazy_object_tags(
                sid => $sid, vol => $vol, tx => $tx, kind => 'data',
                bytes => $bytes, region => $region,
            )}),
            lv_size => $bytes, lv_uuid => $data_uuid,
        },
        $metadata => {
            tags => join(',', @{lazy_object_tags(
                sid => $sid, vol => $vol, tx => $tx, kind => 'metadata',
                bytes => $geometry->{metadata_bytes}, region => $region,
            )}),
            lv_size => $geometry->{metadata_bytes}, lv_uuid => $metadata_uuid,
        },
    }};
}

for my $phase (qw(LAZY_DORMANT LAZY_ACTIVE)) {
    my $lvs = inventory($phase);
    my $effects = 0;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub { $effects++; die "unexpected effect\n" };
    is($class->volume_size_info($scfg, $sid, $vol, 7), $bytes,
        "$phase returns signed HEAD geometry");
    my @info = $class->volume_size_info($scfg, $sid, $vol, 7);
    is_deeply(\@info, [$bytes, 'raw', 0, undef],
        "$phase preserves the PVE list-context contract");
    is($effects, 0, "$phase geometry query has zero effects");

    for my $case (
        ['data size drift', sub { $_[0]->{$vg}->{$head}->{lv_size}-- }, qr/data size differs/],
        ['data UUID drift', sub { $_[0]->{$vg}->{$head}->{lv_uuid} = 'other' }, qr/data UUID differs/],
        ['metadata missing', sub { delete $_[0]->{$vg}->{$metadata} }, qr/metadata.*missing/],
        ['metadata UUID drift', sub { $_[0]->{$vg}->{$metadata}->{lv_uuid} = 'other' }, qr/metadata UUID differs/],
        ['data tag corruption', sub { $_[0]->{$vg}->{$head}->{tags} =~ s/slt_tgl_bytes=\d+/slt_tgl_bytes=512/ }, qr/ownership (?:digest|proof) mismatch/],
        ['anchor publication corruption', sub { $_[0]->{$vg}->{$anchor}->{tags} =~ s/slt_tg_publication=\d+/slt_tg_publication=9/ }, qr/(?:digest mismatch|publication)/],
        ['anchor owner corruption', sub { $_[0]->{$vg}->{$anchor}->{tags} =~ s/slt_tg_owner_node=[^,]+/slt_tg_owner_node=other/ }, qr/(?:digest mismatch|owner)/],
    ) {
        my $bad = inventory($phase);
        $case->[1]->($bad);
        local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $bad };
        eval { $class->volume_size_info($scfg, $sid, $vol, 7) };
        like($@, $case->[2], "$phase rejects $case->[0]");
    }
}

for my $phase (qw(
    LAZY_PREPARED LAZY_CLAIMED MATERIALIZING HYDRATION_COMPLETE LINEAR_PIVOTED
)) {
    my $lvs = inventory($phase);
    my $effects = 0;
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { $lvs };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::run_command = sub { $effects++; die "unexpected effect\n" };
    eval { $class->volume_size_info($scfg, $sid, $vol, 7) };
    like($@, qr/size is unavailable.*\Q$phase\E.*do not retry/s,
        "$phase remains unavailable as a transitional state");
    is($effects, 0, "$phase refusal has zero effects");
}

done_testing;
