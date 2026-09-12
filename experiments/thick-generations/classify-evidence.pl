#!/usr/bin/perl
use strict;
use warnings;

use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use JSON::PP qw(decode_json);
use PVE::SharedLvmThinThick qw(
    classify_recovery decode_anchor_tags decode_generation_tags
    decode_vg_intent_tags validate_transition_tags
);

sub fail {
    my ($reason) = @_;
    $reason =~ s/[\r\n]+/ /g;
    print "STATE=RECOVERY_REQUIRED\nSAFE_FOR_MUTATION=NO\nREASON=$reason\n";
    exit 2;
}

@ARGV == 1 or fail('usage: classify-evidence.pl EVIDENCE_DIR');
my $dir = $ARGV[0];
-d $dir or fail('evidence directory does not exist');

sub slurp {
    my ($path) = @_;
    open(my $fh, '<', $path) or fail("cannot read evidence file '$path'");
    local $/;
    my $data = <$fh>;
    close($fh) or fail("cannot close evidence file '$path'");
    return $data;
}

my %manifest;
for my $line (split(/\n/, slurp("$dir/manifest"))) {
    next if $line eq '';
    $line =~ /^([A-Z0-9_]+)=(.*)$/ or fail('manifest contains a malformed field');
    fail("manifest field '$1' is duplicated") if exists($manifest{$1});
    $manifest{$1} = $2;
}
fail('unsupported or incomplete evidence manifest')
    if ($manifest{EVIDENCE_VERSION} // '') ne '1'
    || ($manifest{EVIDENCE_STATE} // '') ne 'CAPTURED'
    || grep { !defined($manifest{$_}) || $manifest{$_} eq '' }
        qw(VG STORE_ID VOLUME MAPPER);
fail('LVM evidence did not complete')
    if ($manifest{VGS_EXIT} // 'x') ne '0' || ($manifest{LVS_EXIT} // 'x') ne '0';

my $lvs_report = eval { decode_json(slurp("$dir/probes/lvs.stdout")) };
fail("invalid LVM JSON evidence: $@") if $@ || ref($lvs_report) ne 'HASH';
my $vgs_report = eval { decode_json(slurp("$dir/probes/vgs.stdout")) };
fail("invalid VG JSON evidence: $@") if $@ || ref($vgs_report) ne 'HASH';

my $lvs = $lvs_report->{report}->[0]->{lv};
my $vgs = $vgs_report->{report}->[0]->{vg};
fail('LVM evidence has an unexpected shape') if ref($lvs) ne 'ARRAY';
fail('VG evidence has an unexpected shape') if ref($vgs) ne 'ARRAY' || @$vgs != 1;
fail('VG evidence identifies another VG')
    if ($vgs->[0]->{vg_name} // '') ne $manifest{VG};

my %lv;
for my $item (@$lvs) {
    my $name = $item->{lv_name} // '';
    fail('LVM evidence contains an unnamed or duplicate LV')
        if $name eq '' || exists($lv{$name});
    $lv{$name} = $item;
}

$manifest{MAPPER} =~ /^sltg-([0-9a-f]{24})$/
    or fail('frontend mapper does not contain an exact Thick Generations object key');
my $object_key = $1;
my $anchor_lv = "sltg-a-$object_key";
fail('the exact anchor LV is missing') if !exists($lv{$anchor_lv});
my $anchor = eval { decode_anchor_tags($lv{$anchor_lv}->{lv_tags} // '') };
fail("anchor proof is invalid: $@") if $@;
fail('anchor identity differs from the evidence request')
    if $anchor->{sid} ne $manifest{STORE_ID} || $anchor->{vol} ne $manifest{VOLUME};

sub owned_generation {
    my ($name, $expected_role) = @_;
    return 0 if !exists($lv{$name});
    my $generation = eval { decode_generation_tags($lv{$name}->{lv_tags} // '') };
    fail("generation '$name' proof is invalid: $@") if $@;
    fail("generation '$name' belongs to another object")
        if $generation->{sid} ne $manifest{STORE_ID}
        || $generation->{vol} ne $manifest{VOLUME};
    fail("generation '$name' has unexpected role")
        if defined($expected_role) && $generation->{role} ne $expected_role;
    return $generation;
}

my $head = owned_generation($anchor->{head}, 'head');
my $source = $anchor->{source} eq $anchor->{head} ? $head
    : owned_generation($anchor->{source}, 'snapshot');
my $old = $anchor->{old} eq $anchor->{head} ? $head
    : $anchor->{old} eq $anchor->{source} ? $source
    : owned_generation($anchor->{old}, undef);
my $new = $anchor->{new} eq $anchor->{head} ? $head
    : owned_generation($anchor->{new}, 'head');

my $intent;
my $intent_object_present;
my $vg_tags = $vgs->[0]->{vg_tags} // '';
if ($vg_tags =~ /(?:^|,)slt_tg_vgi_/) {
    $intent = eval { decode_vg_intent_tags($vg_tags) };
    fail("VG intent proof is invalid: $@") if $@;
    if ($intent->{op} eq 'REMOVE_SNAPSHOT') {
        $intent_object_present = exists($lv{$intent->{object}}) ? 1 : 0;
        owned_generation($intent->{object}, 'snapshot') if $intent_object_present;
    }
}

my $unrecorded_prepare = $intent && $intent->{tx} ne $anchor->{tx};
my $transition_tx = $unrecorded_prepare ? $intent->{tx} : $anchor->{tx};
my $new_generation = ($anchor->{phase} eq 'PREPARED' || $unrecorded_prepare)
    ? $anchor->{generation} + 1 : $anchor->{generation};
my $meta_name = sprintf(
    'sltg-m-%s-%08d', $object_key, $new_generation,
);
my $meta = exists($lv{$meta_name}) ? 1 : 0;
if ($meta) {
    eval {
        validate_transition_tags(
            $lv{$meta_name}->{lv_tags} // '', sid => $manifest{STORE_ID},
            vol => $manifest{VOLUME}, tx => $transition_tx, kind => 'metadata',
            generation => $new_generation, region => $anchor->{region},
        );
    };
    fail("transition metadata proof is invalid: $@") if $@;
}
my $candidate_new_name = sprintf('sltg-g-%s-%08d', $object_key, $new_generation);
my $candidate_new = 0;
if ($unrecorded_prepare && exists($lv{$candidate_new_name})) {
    my $candidate = owned_generation($candidate_new_name, 'head');
    fail('unrecorded destination generation number is inconsistent')
        if $candidate->{generation} != $new_generation;
    $candidate_new = 1;
}

sub dm_escape {
    my ($value) = @_;
    $value =~ s/-/--/g;
    return $value;
}
sub lv_dm_name {
    my ($name) = @_;
    return dm_escape($manifest{VG}) . '-' . dm_escape($name);
}

my $runtime = 'unknown';
my $clone_status = 'none';
my $clone_source = 'none';
my $runtime_suspended = 0;
if (($manifest{DM_INFO_EXIT} // 'x') ne '0') {
    $runtime = 'absent';
} else {
    my $info = slurp("$dir/dm-info.stdout");
    $info =~ /:([^:\r\n]+)\s*$/ or fail('device-mapper suspension evidence is malformed');
    my $dm_state = $1;
    $runtime_suspended = 1 if $dm_state eq 'Suspended';
    fail('device-mapper suspension state is unknown')
        if $dm_state ne 'Active' && $dm_state ne 'Suspended';
    my $table = slurp("$dir/dm-table.stdout");
    my $deps = slurp("$dir/dm-deps.stdout");
    if ($table =~ /^0\s+\d+\s+linear\s+/m) {
        if ($anchor->{phase} eq 'MATERIALIZED' && $head
            && $deps =~ /\(\Q@{[lv_dm_name($anchor->{head})]}\E\)/) {
            $runtime = 'linear-head';
        } elsif ($anchor->{phase} eq 'PREPARED' && $old
            && $deps =~ /\(\Q@{[lv_dm_name($anchor->{old})]}\E\)/) {
            $runtime = 'linear-old';
        } elsif ($new && $deps =~ /\(\Q@{[lv_dm_name($anchor->{new})]}\E\)/) {
            $runtime = 'linear-new';
        } elsif ($old && $deps =~ /\(\Q@{[lv_dm_name($anchor->{old})]}\E\)/) {
            $runtime = 'linear-old';
        } elsif ($head && $deps =~ /\(\Q@{[lv_dm_name($anchor->{head})]}\E\)/) {
            $runtime = 'linear-head';
        }
    } elsif ($table =~ /^0\s+\d+\s+clone\s+/m) {
        $runtime = 'clone';
        my $source_map = sprintf(
            '%s-src-%08d', $manifest{MAPPER}, $source->{generation},
        );
        $clone_source = 'source' if $deps =~ /\(\Q$source_map\E\)/;
        $clone_source = 'old'
            if $clone_source ne 'source' && $deps =~ /\(\Q@{[lv_dm_name($anchor->{old})]}\E\)/;
        $clone_source = 'unknown' if $clone_source eq 'none';
        my $status = slurp("$dir/dm-status.stdout");
        if ($status =~ /^0\s+\d+\s+clone\s+\S+\s+\S+\s+\S+\s+(\d+)\/(\d+)\s+(\d+)(?:\s|$)/m) {
            $clone_status = ($1 == $2 && $3 == 0) ? 'complete' : 'incomplete';
        } else {
            $clone_status = 'unknown';
        }
    }
}

my $classification = classify_recovery(
    anchor => $anchor, intent => $intent,
    objects => {
        head => $head ? 1 : 0, source => $source ? 1 : 0,
        old => $old ? 1 : 0, new => $new ? 1 : 0, meta => $meta,
        candidate_new => $candidate_new,
    },
    runtime => $runtime, clone_status => $clone_status,
    clone_source => $clone_source, runtime_suspended => $runtime_suspended,
    expected_anchor => $anchor_lv,
    intent_object_present => $intent_object_present,
);

print "STATE=$classification->{state}\n";
print 'SAFE_FOR_MUTATION=' . ($classification->{safe_for_mutation} ? 'YES' : 'NO') . "\n";
print "DATA_STATE=$classification->{data_state}\n";
print "TRANSACTION_STATE=$classification->{transaction_state}\n";
print "MATERIALIZATION_STATE=$classification->{materialization_state}\n";
print "RUNTIME_STATE=$runtime\n";
print 'RUNTIME_SUSPENDED=' . ($runtime_suspended ? 'YES' : 'NO') . "\n";
print "CLONE_SOURCE=$clone_source\n";
print "REASON=$classification->{reason}\n";
exit($classification->{safe_for_mutation} ? 0 : 1);
