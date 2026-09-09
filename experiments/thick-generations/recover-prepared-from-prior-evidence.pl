#!/usr/bin/perl
use strict;
use warnings;

use Getopt::Long qw(GetOptions);
use JSON::PP qw(decode_json);
use PVE::SharedLvmThinThick qw(
    anchor_name anchor_tags decode_anchor_tags mapper_name object_key
    validate_generation_tags validate_transition_tags
);
use PVE::Storage::Custom::SharedLvmThinPlugin;
use PVE::Tools qw(run_command);

sub read_file {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "cannot read evidence file '$path'\n";
    local $/;
    my $data = <$fh>;
    close($fh) or die "cannot close evidence file '$path'\n";
    return $data;
}

sub evidence_anchor {
    my ($directory, $anchor) = @_;
    my $report = decode_json(read_file("$directory/probes/lvs.stdout"));
    my $rows = $report->{report}->[0]->{lv};
    die "evidence LVM report is malformed\n" if ref($rows) ne 'ARRAY';
    my @matches = grep { ($_->{lv_name} // '') eq $anchor } @$rows;
    die "evidence does not contain exactly one anchor\n" if @matches != 1;
    return decode_anchor_tags($matches[0]->{lv_tags} // '');
}

sub same_anchor {
    my ($left, $right) = @_;
    return join("\n", @{anchor_tags(%$left)}) eq join("\n", @{anchor_tags(%$right)});
}

my %option;
GetOptions(
    'current-evidence=s' => \$option{current_evidence},
    'prior-evidence=s' => \$option{prior_evidence},
    'store-id=s' => \$option{store_id}, 'volume=s' => \$option{volume},
    'vg=s' => \$option{vg}, 'vg-uuid=s' => \$option{vg_uuid},
    'pv-uuid=s' => \$option{pv_uuid}, 'wwid=s' => \$option{wwid},
    'ack=s' => \$option{ack},
) or die "invalid recovery arguments\n";

die "explicit prior-evidence recovery acknowledgement is required\n"
    if ($option{ack} // '') ne 'RESTORE-ONLY-EXACT-PRIOR-SIGNED-ANCHOR';
for my $field (qw(current_evidence prior_evidence store_id volume vg vg_uuid pv_uuid wwid)) {
    die "missing recovery option '$field'\n"
        if !defined($option{$field}) || $option{$field} eq '';
}

my $namespace = lc($option{vg_uuid});
my $anchor_name = anchor_name($namespace, $option{volume});
my $current_evidence = evidence_anchor($option{current_evidence}, $anchor_name);
my $prior_evidence = evidence_anchor($option{prior_evidence}, $anchor_name);
die "current evidence is not PREPARED\n" if $current_evidence->{phase} ne 'PREPARED';
die "prior evidence is not MATERIALIZED\n" if $prior_evidence->{phase} ne 'MATERIALIZED';
die "evidence identity mismatch\n"
    if $current_evidence->{sid} ne $option{store_id}
    || $prior_evidence->{sid} ne $option{store_id}
    || $current_evidence->{vol} ne $option{volume}
    || $prior_evidence->{vol} ne $option{volume};
die "prior evidence is not the exact predecessor of PREPARED\n"
    if $current_evidence->{old} ne $prior_evidence->{head}
    || $current_evidence->{head} ne $prior_evidence->{head}
    || $current_evidence->{generation} != $prior_evidence->{generation}
    || $current_evidence->{tx} eq $prior_evidence->{tx};

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $scfg = {
    shared => 1, 'slt-allocation-mode' => 'thick-generations',
    'slt-vgname' => $option{vg},
    'slt-expected-vg-uuid' => $option{vg_uuid},
    'slt-expected-pv-uuid' => $option{pv_uuid},
    'slt-expected-wwid' => $option{wwid},
    'slt-vg-reserve-percent' => 5,
};

$class->_require_thick_identity_config($option{store_id}, $scfg);
$class->_with_vg_lock($option{store_id}, $scfg, sub {
    my $intent = $class->_read_vg_intent($option{vg});
    die "no OPEN intent exists\n" if !$intent;
    die "OPEN intent and PREPARED evidence differ\n"
        if $intent->{tx} ne $current_evidence->{tx}
        || $intent->{object} ne $anchor_name;
    my $expected_intent = $current_evidence->{op} eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER';
    die "OPEN intent operation and PREPARED anchor differ\n"
        if $intent->{op} ne $expected_intent;

    my $lvs = PVE::Storage::LVMPlugin::lvm_list_volumes($option{vg});
    my $live_anchor = decode_anchor_tags($lvs->{$option{vg}}->{$anchor_name}->{tags} // '');
    die "live anchor changed after current evidence capture\n"
        if !same_anchor($live_anchor, $current_evidence);
    my $new_generation = int($current_evidence->{generation}) + 1;
    my $key = object_key($namespace, $option{volume});
    my $new = $current_evidence->{new};
    my $meta = sprintf('sltg-m-%s-%08d', $key, $new_generation);
    die "exact PREPARED object set is incomplete\n"
        if !$lvs->{$option{vg}}->{$new} || !$lvs->{$option{vg}}->{$meta};
    validate_generation_tags(
        $lvs->{$option{vg}}->{$new}->{tags} // '', sid => $option{store_id},
        vol => $option{volume}, role => 'head', generation => $new_generation,
    );
    validate_transition_tags(
        $lvs->{$option{vg}}->{$meta}->{tags} // '', sid => $option{store_id},
        vol => $option{volume}, tx => $current_evidence->{tx}, kind => 'metadata',
        generation => $new_generation, region => $current_evidence->{region},
    );
    $class->_verify_autoactivation_disabled($option{vg}, $new);
    $class->_verify_autoactivation_disabled($option{vg}, $meta);
    die "PREPARED transition object is active; refusing evidence rollback\n"
        if -b "/dev/$option{vg}/$new" || -b "/dev/$option{vg}/$meta";
    my $source_map = mapper_name($namespace, $option{volume})
        . sprintf('-src-%08d', $current_evidence->{generation});
    die "PREPARED source mapper exists; refusing evidence rollback\n"
        if -b "/dev/mapper/$source_map";
    my $head = $lvs->{$option{vg}}->{$current_evidence->{old}};
    die "authoritative old HEAD is missing\n" if !$head;
    $class->_thick_verify_frontend(
        $scfg, $option{volume}, $current_evidence->{old}, int($head->{lv_size} / 512),
    );

    # Publish the previously proven anchor first. A crash after this point is
    # the already-qualified C2 state: materialized HEAD plus unrecorded objects.
    $class->_change_exact_tags(
        $option{vg}, $anchor_name, anchor_tags(%$current_evidence),
        anchor_tags(%$prior_evidence), 'restoring exact prior anchor failed',
        "/dev/mapper/$option{wwid}",
    );
    run_command(
        ['/sbin/lvremove', '-f', "$option{vg}/$meta", "$option{vg}/$new"],
        errmsg => 'removing exact PREPARED transition objects failed',
    );
    my $after = PVE::Storage::LVMPlugin::lvm_list_volumes($option{vg});
    die "PREPARED evidence rollback postcondition failed\n"
        if $after->{$option{vg}}->{$new} || $after->{$option{vg}}->{$meta};
    my $restored = decode_anchor_tags($after->{$option{vg}}->{$anchor_name}->{tags} // '');
    die "prior anchor restore postcondition failed\n"
        if !same_anchor($restored, $prior_evidence);
    $class->_clear_vg_intent($option{vg}, %$intent);
    return;
});

print "RECOVERY=PREPARED_PRIOR_ANCHOR_RESTORED\n";
