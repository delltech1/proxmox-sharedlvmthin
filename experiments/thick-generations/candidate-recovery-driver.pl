#!/usr/bin/perl
use strict;
use warnings;

BEGIN {
    die "candidate recovery requires PERL5LIB and PERL5OPT to be absent at interpreter start\n"
        if exists($ENV{PERL5LIB}) || exists($ENV{PERL5OPT});
}

use FindBin ();
use lib "$FindBin::Bin/../../usr/share/perl5";
use lib "$FindBin::Bin/lib";

use Getopt::Long qw(GetOptions);
use PVE::Storage;
use PVE::Storage::Custom::SharedLvmThinPlugin;
use SharedLvmCandidateProvenance qw(verify_candidate_provenance);

my %option;
GetOptions(
    'mode=s' => \$option{mode},
    'store-id=s' => \$option{store_id},
    'volume=s' => \$option{volume},
    'plugin-sha256=s' => \$option{plugin_sha256},
    'candidate-code-sha256=s' => \$option{candidate_code_sha256},
    'ack=s' => \$option{ack},
) or die "invalid candidate recovery-driver arguments\n";

die "explicit disposable-recovery acknowledgement is required\n"
    if ($option{ack} // '') ne 'DISPOSABLE-CANDIDATE-RECOVERY';
die "invalid candidate recovery mode\n"
    if ($option{mode} // '') !~ /^(?:resume|recover-resize|lazy-activate-close|lazy-materialize)$/;
die "invalid storage identifier\n"
    if ($option{store_id} // '') !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
die "invalid Thick Generations volume name\n"
    if ($option{volume} // '') !~ /^(?:vm|base)-\d+-disk-\d+$/;
verify_candidate_provenance(
    $FindBin::Bin, $option{plugin_sha256}, $option{candidate_code_sha256},
);

my $cfg = PVE::Storage::config();
my $scfg = PVE::Storage::storage_config($cfg, $option{store_id});
die "storage '$option{store_id}' is not SharedLvmThin\n"
    if ($scfg->{type} // '') ne 'sharedlvmthin';

my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $allocation = $class->_allocation_mode($scfg);
die "storage '$option{store_id}' is not configured for the requested Thick mode\n"
    if ($option{mode} =~ /^lazy-(?:activate-close|materialize)$/
        ? $allocation ne 'thick-generations-lazy'
        : !$class->_is_thick_mode($scfg));
$class->_require_thick_identity_config($option{store_id}, $scfg);

if ($option{mode} eq 'lazy-activate-close') {
    my ($state) = $class->_thick_read_anchor(
        $option{store_id}, $scfg, $option{volume},
    );
    die "Lazy candidate recovery requires LAZY_CLAIMED on this node and boot\n"
        if ($state->{phase} // '') ne 'LAZY_CLAIMED';
    print "LAZY_CANDIDATE_ACTIVATE_START storage=$option{store_id} volume=$option{volume}\n";
    $class->_lazy_activate_volume(
        $option{store_id}, $scfg, $option{volume}, undef, undef,
    );
    print "LAZY_CANDIDATE_ACTIVATE_COMPLETE storage=$option{store_id} volume=$option{volume}\n";
    $class->_lazy_deactivate_volume(
        $option{store_id}, $scfg, $option{volume}, undef, undef,
    );
    my ($closed) = $class->_thick_read_anchor(
        $option{store_id}, $scfg, $option{volume},
    );
    die "Lazy candidate recovery did not converge to LAZY_DORMANT\n"
        if ($closed->{phase} // '') ne 'LAZY_DORMANT';
    print "LAZY_CANDIDATE_CLOSE_COMPLETE storage=$option{store_id} volume=$option{volume}\n";
    exit 0;
}

if ($option{mode} eq 'lazy-materialize') {
    print "LAZY_CANDIDATE_MATERIALIZE_START storage=$option{store_id} volume=$option{volume}\n";
    my $result = $class->_lazy_materialize_volume(
        $scfg, $option{store_id}, $option{volume},
    );
    die "Lazy candidate materialization did not converge to canonical v5\n"
        if ($result // '') ne 'LAZY_MATERIALIZED_V5';
    print "LAZY_CANDIDATE_MATERIALIZE_COMPLETE storage=$option{store_id} volume=$option{volume}\n";
    exit 0;
}

if ($option{mode} eq 'recover-resize') {
    my $vg = $scfg->{'slt-vgname'};
    my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
    my $intent = $class->_read_vg_intent($scfg, $vg, $device);
    die "volume '$option{store_id}:$option{volume}' has no recoverable OPEN EXTEND intent\n"
        if !$intent || ($intent->{state} // '') ne 'OPEN'
        || ($intent->{op} // '') ne 'EXTEND';
    my $expected_tx = $intent->{tx};
    my $expected_object = $intent->{object};
    die "candidate resize recovery intent has invalid transaction identity\n"
        if !defined($expected_tx) || $expected_tx !~ /^[0-9a-f]{32}$/;
    die "candidate resize recovery intent has invalid anchor identity\n"
        if !defined($expected_object)
        || $expected_object !~ /^sltg-a-[0-9a-f]{24}$/;
    print "RESIZE_RECOVERY_START storage=$option{store_id} volume=$option{volume}\n";
    my $result = $class->_thick_recover_resize(
        $scfg, $option{store_id}, $option{volume}, $expected_tx, $expected_object,
    );
    print "$result storage=$option{store_id} volume=$option{volume}\n";
    exit 0;
}

my ($state) = $class->_thick_read_anchor(
    $option{store_id}, $scfg, $option{volume},
);
my $phase = $state->{phase} // '';
die "volume '$option{store_id}:$option{volume}' has no resumable materialization transition\n"
    if $phase ne 'PREPARED'
    && $phase ne 'SOURCE_READY'
    && $phase ne 'COMMITTED'
    && $phase ne 'HYDRATING'
    && $phase ne 'HYDRATION_COMPLETE'
    && $phase ne 'LINEAR_PIVOTED';
my $snapshot = $state->{snapshot};
my $operation = $state->{op};
my $transaction = $state->{tx};
die "candidate recovery anchor has invalid snapshot identity\n"
    if !defined($snapshot) || $snapshot !~ /^[A-Za-z0-9_.+-]+$/;
die "candidate recovery anchor has invalid operation\n"
    if !defined($operation)
    || ($operation ne 'SNAPSHOT' && $operation ne 'ROLLBACK');
die "candidate recovery anchor has invalid transaction UUID\n"
    if !defined($transaction) || $transaction !~ /^[0-9a-f]{32}$/;

my $vg = $scfg->{'slt-vgname'};
my $device = "/dev/mapper/$scfg->{'slt-expected-wwid'}";
my $intent = $class->_read_vg_intent($scfg, $vg, $device);
my $expected_intent_op = $operation eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER';
my $exact_vg_intent = defined($intent)
    && ($intent->{tx} // '') eq $transaction
    && ($intent->{state} // '') eq 'OPEN'
    && ($intent->{op} // '') eq $expected_intent_op;
if (!$exact_vg_intent) {
    my ($current, undef, $anchor) = $class->_thick_read_anchor(
        $option{store_id}, $scfg, $option{volume},
    );
    die "a different VG intent targets this materialization anchor\n"
        if defined($intent) && ($intent->{object} // '') eq $anchor;
    die "anchor-scoped materialization does not match transaction '$transaction'\n"
        if ($current->{tx} // '') ne $transaction
        || ($current->{op} // '') ne $operation
        || ($current->{snapshot} // '') ne $snapshot
        || ($current->{phase} // '') !~ /^(?:HYDRATING|HYDRATION_COMPLETE|LINEAR_PIVOTED)$/;
}

print "MATERIALIZATION_START transaction=$transaction storage=$option{store_id} volume=$option{volume}\n";
$class->_thick_volume_snapshot(
    $scfg, $option{store_id}, $option{volume}, $snapshot, $operation, 1,
    $transaction,
);
print "MATERIALIZATION_COMPLETE transaction=$transaction storage=$option{store_id} volume=$option{volume}\n";

exit 0;
