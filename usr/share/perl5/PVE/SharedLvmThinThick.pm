# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinThick;

use strict;
use warnings;

use Digest::SHA qw(sha256_hex);
use Exporter qw(import);

our @EXPORT_OK = qw(
    anchor_name anchor_tags decode_anchor_tags generation_name generation_tags
    decode_generation_tags mapper_name object_key validate_generation_tags vg_intent_tags
    decode_vg_intent_tags validate_anchor_transition clone_geometry
    transition_tags validate_transition_tags classify_recovery materialized_rebase_state
);

my @ANCHOR_FIELDS = qw(v sid vol phase tx op snapshot source old new head generation region);
my %PHASE = map { $_ => 1 } qw(
    PREPARED SOURCE_READY COMMITTED HYDRATING HYDRATION_COMPLETE LINEAR_PIVOTED MATERIALIZED
);
my $TOKEN = qr/[A-Za-z0-9_.+-]+/;
my $TX = qr/[0-9a-f]{32}/;
my %ALLOWED_TRANSITION = (
    PREPARED => { SOURCE_READY => 1, MATERIALIZED => 1 },
    SOURCE_READY => { COMMITTED => 1 },
    COMMITTED => { HYDRATING => 1, HYDRATION_COMPLETE => 1 },
    HYDRATING => { HYDRATION_COMPLETE => 1 },
    HYDRATION_COMPLETE => { LINEAR_PIVOTED => 1 },
    LINEAR_PIVOTED => { MATERIALIZED => 1 },
    MATERIALIZED => { PREPARED => 1 },
);

sub _token {
    my ($label, $value) = @_;
    die "$label is missing\n" if !defined($value) || $value eq '';
    die "$label contains characters unsafe for an LVM tag\n"
        if $value !~ /^($TOKEN)$/;
    return $1;
}

sub object_key {
    my ($storeid, $volname) = @_;
    $storeid = _token('storage ID', $storeid);
    $volname = _token('volume name', $volname);
    return substr(sha256_hex(lc($storeid) . "\0" . $volname), 0, 24);
}

sub anchor_name {
    return 'sltg-a-' . object_key(@_);
}

sub mapper_name {
    return 'sltg-' . object_key(@_);
}

sub generation_name {
    my ($storeid, $volname, $generation) = @_;
    die "generation must be an integer from 0 through 99999999\n"
        if !defined($generation) || $generation !~ /^\d+$/ || $generation > 99_999_999;
    return sprintf('sltg-g-%s-%08d', object_key($storeid, $volname), $generation);
}

sub clone_geometry {
    my ($bytes) = @_;
    die "clone size must be a positive sector-aligned integer\n"
        if !defined($bytes) || $bytes !~ /^\d+$/ || $bytes < 512 || $bytes % 512;

    # Keep the three in-core dm-clone bitmaps bounded while avoiding a large
    # region for ordinary VM disks.  The chosen value is persisted in the
    # anchor, so future code changes cannot silently alter recovery geometry.
    my $max_regions = 134_217_728;
    my $sectors = int($bytes / 512);
    my $region = 8;
    while (int(($sectors + $region - 1) / $region) > $max_regions) {
        $region *= 2;
        die "clone size exceeds supported dm-clone geometry\n" if $region > 2_097_152;
    }
    my $regions = int(($sectors + $region - 1) / $region);

    # dm-clone stores a persistent bitset using dm-persistent-data.  Reserve
    # one byte per region plus 16 MiB structural headroom, then round to an
    # LVM-friendly 4 MiB boundary.  Runtime creation and status gates must
    # still positively verify the actual metadata device before publication.
    my $metadata = 16 * 1024 * 1024 + $regions;
    my $extent = 4 * 1024 * 1024;
    $metadata = int(($metadata + $extent - 1) / $extent) * $extent;
    die "calculated dm-clone metadata exceeds the supported 16 GiB limit\n"
        if $metadata > 16 * 1024 * 1024 * 1024;
    return {
        region_sectors => $region,
        regions => $regions,
        metadata_bytes => $metadata,
    };
}

sub _canonical {
    my ($values) = @_;
    return join('|', map { "$_=$values->{$_}" } @ANCHOR_FIELDS);
}

sub anchor_tags {
    my (%values) = @_;
    $values{v} = 5 if !defined($values{v});
    die "unsupported Thick Generations anchor version\n" if "$values{v}" ne '5';
    for my $field (qw(sid vol snapshot source old new head)) {
        _token("anchor $field", $values{$field});
    }
    $values{op} = uc(_token('anchor operation', $values{op}));
    die "unknown Thick Generations operation '$values{op}'\n"
        if $values{op} !~ /^(?:ALLOC|SNAPSHOT|ROLLBACK)$/;
    $values{phase} = uc(_token('anchor phase', $values{phase}));
    die "unknown Thick Generations phase '$values{phase}'\n" if !$PHASE{$values{phase}};
    die "invalid Thick Generations transaction ID\n"
        if !defined($values{tx}) || $values{tx} !~ /^$TX$/;
    die "generation must be an integer from 0 through 99999999\n"
        if !defined($values{generation}) || $values{generation} !~ /^\d+$/
        || $values{generation} > 99_999_999;
    $values{generation} = int($values{generation});
    die "invalid dm-clone region size in Thick Generations anchor\n"
        if !defined($values{region}) || $values{region} !~ /^\d+$/
        || $values{region} < 8 || $values{region} > 2_097_152
        || ($values{region} & ($values{region} - 1));
    $values{region} = int($values{region});
    if ($values{phase} eq 'PREPARED' || $values{phase} eq 'SOURCE_READY') {
        die "$values{phase} anchor must keep the old generation authoritative\n"
            if $values{head} ne $values{old};
    } else {
        die "$values{phase} anchor must publish the new generation as HEAD\n"
            if $values{head} ne $values{new};
        die "$values{phase} transition cannot have identical old and new generations\n"
            if $values{phase} ne 'MATERIALIZED' && $values{old} eq $values{new};
    }
    if ($values{op} eq 'ALLOC') {
        die "ALLOC anchor must use the reserved snapshot marker\n"
            if $values{snapshot} ne 'none';
        die "ALLOC anchor must use one identical source, old, new, and head generation\n"
            if $values{source} ne $values{old} || $values{old} ne $values{new}
            || $values{head} ne $values{old};
    } else {
        die "$values{op} anchor must persist an exact snapshot name\n"
            if $values{snapshot} eq 'none';
        die "$values{op} transition requires distinct old and new generations\n"
            if $values{old} eq $values{new};
        die "$values{op} transition source cannot be the new destination\n"
            if $values{source} eq $values{new};
        die "SNAPSHOT transition must clone the previous HEAD\n"
            if $values{op} eq 'SNAPSHOT' && $values{source} ne $values{old};
        die "ROLLBACK transition must clone a retained snapshot, not the previous HEAD\n"
            if $values{op} eq 'ROLLBACK' && $values{source} eq $values{old};
    }
    my $digest = substr(sha256_hex(_canonical(\%values)), 0, 32);
    return [
        (map { "slt_tg_$_=$values{$_}" } @ANCHOR_FIELDS),
        "slt_tg_sha256=$digest",
    ];
}

sub decode_anchor_tags {
    my ($tags) = @_;
    my @tags = ref($tags) eq 'ARRAY' ? @$tags : split(/,/, $tags // '');
    my %values;
    for my $tag (@tags) {
        $tag =~ s/^\s+|\s+$//g;
        next if $tag !~ /^slt_tg_/;
        die "malformed Thick Generations anchor tag\n"
            if $tag !~ /^slt_tg_([A-Za-z0-9_]+)=($TOKEN)$/;
        my ($field, $value) = ($1, $2);
        die "duplicate Thick Generations anchor field '$field'\n"
            if exists($values{$field});
        die "unknown Thick Generations anchor field '$field'\n"
            if !grep { $_ eq $field } (@ANCHOR_FIELDS, 'sha256');
        $values{$field} = $value;
    }
    my %expected = map { $_ => 1 } (@ANCHOR_FIELDS, 'sha256');
    die "incomplete Thick Generations anchor\n"
        if keys(%values) != keys(%expected)
        || grep { !exists($values{$_}) } keys(%expected);
    my $expected_tags = anchor_tags(%values);
    my ($expected_digest) = map { /^slt_tg_sha256=(.*)$/ ? $1 : () } @$expected_tags;
    die "Thick Generations anchor digest mismatch\n"
        if $values{sha256} ne $expected_digest;
    return \%values;
}

sub validate_anchor_transition {
    my ($before, $after) = @_;
    die "anchor transition requires before and after state\n"
        if ref($before) ne 'HASH' || ref($after) ne 'HASH';

    # Re-encode both sides so callers cannot bypass schema or digest rules with
    # partially decoded data.
    anchor_tags(%$before);
    anchor_tags(%$after);

    my $from = uc($before->{phase});
    my $to = uc($after->{phase});
    if ($from eq $to) {
        die "idempotent anchor transition changed persistent state\n"
            if _canonical($before) ne _canonical($after);
        return 1;
    }
    die "illegal Thick Generations anchor phase transition '$from->$to'\n"
        if !$ALLOWED_TRANSITION{$from} || !$ALLOWED_TRANSITION{$from}->{$to};

    for my $field (qw(v sid vol)) {
        die "anchor transition changed immutable field '$field'\n"
            if "$before->{$field}" ne "$after->{$field}";
    }

    if ($from eq 'MATERIALIZED' && $to eq 'PREPARED') {
        die "new transition must use a fresh transaction ID\n"
            if $after->{tx} eq $before->{tx};
        die "new transition must preserve the authoritative old HEAD\n"
            if $after->{old} ne $before->{head} || $after->{head} ne $before->{head};
        die "new transition must name a distinct destination generation\n"
            if $after->{new} eq $before->{head};
        die "new transition cannot advance generation before commit\n"
            if int($after->{generation}) != int($before->{generation});
    } else {
        for my $field (qw(tx op snapshot source old new region)) {
            die "anchor transition changed immutable field '$field'\n"
                if "$before->{$field}" ne "$after->{$field}";
        }
    }

    if ($from eq 'SOURCE_READY' && $to eq 'COMMITTED') {
        die "commit must move HEAD from the old generation to the new generation\n"
            if $before->{head} ne $before->{old} || $after->{head} ne $after->{new};
        die "commit generation must advance exactly once\n"
            if int($after->{generation}) != int($before->{generation}) + 1;
    } elsif ($from eq 'PREPARED' && $to eq 'MATERIALIZED') {
        die "initial materialization requires an ALLOC transaction\n"
            if $before->{op} ne 'ALLOC';
        die "initial materialization requires one identical source/old/new/head generation\n"
            if $before->{source} ne $before->{old}
            || $before->{old} ne $before->{new}
            || $before->{head} ne $before->{old}
            || $after->{head} ne $before->{head}
            || int($after->{generation}) != int($before->{generation});
    } else {
        die "post-commit transition changed HEAD or generation\n"
            if $after->{head} ne $before->{head}
            || int($after->{generation}) != int($before->{generation});
    }
    return 1;
}

sub materialized_rebase_state {
    my ($before, $tx) = @_;
    die "materialized anchor rebase requires an anchor state\n"
        if ref($before) ne 'HASH';
    anchor_tags(%$before);
    die "only a MATERIALIZED anchor can be rebased\n"
        if $before->{phase} ne 'MATERIALIZED';
    die "materialized anchor rebase requires a fresh transaction ID\n"
        if !defined($tx) || $tx !~ /^$TX$/ || $tx eq $before->{tx};

    my %after = (
        %$before,
        tx => $tx,
        op => 'ALLOC',
        snapshot => 'none',
        source => $before->{head},
        old => $before->{head},
        new => $before->{head},
    );
    anchor_tags(%after);
    return \%after;
}

sub generation_tags {
    my (%values) = @_;
    for my $field (qw(sid vol role)) {
        _token("generation $field", $values{$field});
    }
    die "invalid generation role\n" if $values{role} !~ /^(?:head|snapshot)$/;
    die "generation must be an integer from 0 through 99999999\n"
        if !defined($values{generation}) || $values{generation} !~ /^\d+$/
        || $values{generation} > 99_999_999;
    my @tags = (
        'slt_tgo_v=1',
        "slt_tgo_sid=$values{sid}",
        "slt_tgo_vol=$values{vol}",
        "slt_tgo_role=$values{role}",
        'slt_tgo_generation=' . int($values{generation}),
    );
    if ($values{role} eq 'snapshot') {
        _token('snapshot name', $values{snapshot});
        push @tags, "slt_tgo_snapshot=$values{snapshot}";
    } elsif (defined($values{snapshot})) {
        die "head generation cannot carry a snapshot name\n";
    }
    my $canonical = join('|', map { s/^slt_tgo_//r } @tags);
    push @tags, 'slt_tgo_sha256=' . substr(sha256_hex($canonical), 0, 32);
    return \@tags;
}

sub validate_generation_tags {
    my ($tags, %expected) = @_;
    my $observed = decode_generation_tags($tags);
    my $wanted = decode_generation_tags(generation_tags(%expected));
    for my $field (keys %$wanted) {
        die "generation ownership proof mismatch\n"
            if !exists($observed->{$field}) || "$observed->{$field}" ne "$wanted->{$field}";
    }
    return 1;
}

sub decode_generation_tags {
    my ($tags) = @_;
    my @tags = ref($tags) eq 'ARRAY' ? @$tags : split(/,/, $tags // '');
    my %values;
    for my $tag (@tags) {
        $tag =~ s/^\s+|\s+$//g;
        next if $tag !~ /^slt_tgo_/;
        die "malformed generation ownership tag\n"
            if $tag !~ /^slt_tgo_([A-Za-z0-9_]+)=($TOKEN)$/;
        my ($field, $value) = ($1, $2);
        die "duplicate generation ownership field '$field'\n" if exists($values{$field});
        die "unknown generation ownership field '$field'\n"
            if !grep { $_ eq $field } qw(v sid vol role generation snapshot sha256);
        $values{$field} = $value;
    }
    my @required = qw(v sid vol role generation sha256);
    push @required, 'snapshot' if ($values{role} // '') eq 'snapshot';
    die "incomplete generation ownership proof\n"
        if keys(%values) != @required || grep { !exists($values{$_}) } @required;
    my $wanted = generation_tags(%values);
    my ($digest) = map { /^slt_tgo_sha256=(.*)$/ ? $1 : () } @$wanted;
    die "generation ownership digest mismatch\n" if $values{sha256} ne $digest;
    return \%values;
}

sub transition_tags {
    my (%values) = @_;
    for my $field (qw(sid vol kind)) {
        _token("transition $field", $values{$field});
    }
    die "invalid transition artifact kind\n" if $values{kind} ne 'metadata';
    die "invalid transition transaction ID\n"
        if !defined($values{tx}) || $values{tx} !~ /^$TX$/;
    die "invalid transition generation\n"
        if !defined($values{generation}) || $values{generation} !~ /^\d+$/
        || $values{generation} > 99_999_999;
    die "invalid transition region size\n"
        if !defined($values{region}) || $values{region} !~ /^\d+$/
        || $values{region} < 8 || $values{region} > 2_097_152
        || ($values{region} & ($values{region} - 1));
    my @fields = qw(v sid vol tx kind generation region);
    $values{v} = 1 if !defined($values{v});
    die "unsupported transition artifact version\n" if "$values{v}" ne '1';
    my @tags = map { "slt_tgt_$_=$values{$_}" } @fields;
    my $canonical = join('|', map { "$_=$values{$_}" } @fields);
    push @tags, 'slt_tgt_sha256=' . substr(sha256_hex($canonical), 0, 32);
    return \@tags;
}

sub validate_transition_tags {
    my ($tags, %expected) = @_;
    my @observed = ref($tags) eq 'ARRAY' ? @$tags : split(/,/, $tags // '');
    @observed = map { s/^\s+|\s+$//gr } grep { /^\s*slt_tgt_/ } @observed;
    my $wanted = transition_tags(%expected);
    die "transition artifact ownership tag count mismatch\n" if @observed != @$wanted;
    my %observed = map { $_ => 1 } @observed;
    die "transition artifact ownership proof mismatch\n"
        if grep { !$observed{$_} } @$wanted;
    return 1;
}

sub classify_recovery {
    my (%evidence) = @_;
    my $anchor = $evidence{anchor};
    my $intent = $evidence{intent};
    my $objects = $evidence{objects};
    my $runtime = $evidence{runtime} // 'unknown';
    my $clone_status = $evidence{clone_status} // 'none';
    my $clone_source = $evidence{clone_source} // 'none';
    my $runtime_suspended = $evidence{runtime_suspended} // 0;
    my $expected_anchor = $evidence{expected_anchor};

    my $blocked = sub {
        my ($reason) = @_;
        return {
            state => 'RECOVERY_REQUIRED', safe_for_mutation => 0,
            data_state => 'AMBIGUOUS', transaction_state => 'AMBIGUOUS',
            materialization_state => 'UNKNOWN', reason => $reason,
        };
    };
    return $blocked->('anchor evidence is missing') if ref($anchor) ne 'HASH';
    return $blocked->('object evidence is missing') if ref($objects) ne 'HASH';
    eval { anchor_tags(%$anchor); };
    return $blocked->("anchor evidence is invalid: $@") if $@;
    return $blocked->('runtime evidence is invalid')
        if $runtime !~ /^(?:absent|linear-old|linear-head|linear-new|clone|unknown)$/;
    return $blocked->('clone source evidence is invalid')
        if $clone_source !~ /^(?:none|source|old|unknown)$/;
    return $blocked->('runtime suspension evidence is invalid')
        if "$runtime_suspended" !~ /^(?:0|1)$/;
    return $blocked->('authoritative HEAD object is missing') if !$objects->{head};

    my $require_stable_objects = sub {
        return 'materialized transition metadata still exists' if $objects->{meta};
        return 'materialized snapshot source is missing'
            if $anchor->{op} ne 'ALLOC' && !$objects->{source};
        return 'materialized snapshot lost its retained source generation'
            if $anchor->{op} eq 'SNAPSHOT' && !$objects->{old};
        return 'materialized rollback retained the superseded HEAD'
            if $anchor->{op} eq 'ROLLBACK' && $objects->{old};
        return undef;
    };

    my $result = sub {
        my ($state, $transaction, $materialization, $reason) = @_;
        return {
            state => $state, safe_for_mutation => ($state eq 'HEALTHY' ? 1 : 0),
            data_state => 'VALID', transaction_state => $transaction,
            materialization_state => $materialization, reason => $reason,
        };
    };

    if ($anchor->{phase} eq 'MATERIALIZED' && !defined($intent)) {
        my $object_error = $require_stable_objects->();
        return $blocked->($object_error) if defined($object_error);
        return $blocked->('materialized HEAD has an unexpected runtime mapping')
            if $runtime !~ /^(?:absent|linear-head|linear-new)$/;
        return $blocked->('materialized frontend is suspended') if $runtime_suspended;
        return $result->('HEALTHY', 'COMMITTED', 'MATERIALIZED',
            'materialized anchor and authoritative HEAD agree');
    }

    if ($anchor->{phase} eq 'MATERIALIZED' && defined($intent)) {
        eval { vg_intent_tags(%$intent); };
        return $blocked->("VG intent is invalid: $@") if $@;
        if ($intent->{op} eq 'REMOVE_SNAPSHOT') {
            return $blocked->('snapshot-delete intent object evidence is missing')
                if !defined($evidence{intent_object_present});
            my $present = $evidence{intent_object_present} ? 1 : 0;
            if ($anchor->{tx} eq $intent->{tx}) {
                return $blocked->('snapshot-delete anchor rebase is not canonical')
                    if $anchor->{op} ne 'ALLOC' || $anchor->{snapshot} ne 'none'
                    || $anchor->{source} ne $anchor->{head}
                    || $anchor->{old} ne $anchor->{head}
                    || $anchor->{new} ne $anchor->{head};
                my $object_error = $require_stable_objects->();
                return $blocked->($object_error) if defined($object_error);
                return $result->(
                    'RECOVERY_REQUIRED',
                    ($present ? 'SNAPSHOT_DELETE_READY' : 'SNAPSHOT_DELETE_FINALIZE'),
                    'MATERIALIZED',
                    ($present
                        ? 'canonical HEAD is stable and the exact signed snapshot awaits deletion'
                        : 'exact snapshot deletion completed and the OPEN intent awaits clearing'),
                );
            }
            return $blocked->('snapshot-delete object disappeared before anchor rebase')
                if !$present;
            my $object_error = $require_stable_objects->();
            return $blocked->($object_error) if defined($object_error);
            return $result->(
                'RECOVERY_REQUIRED', 'SNAPSHOT_DELETE_PREPARED', 'MATERIALIZED',
                'OPEN intent exists and the exact signed snapshot remains before anchor rebase',
            );
        }
        return $blocked->('VG intent refers to another anchor')
            if !defined($expected_anchor) || $intent->{object} ne $expected_anchor;
        return $blocked->('materialized object has an unsupported OPEN operation')
            if $intent->{op} !~ /^(?:DM_CUTOVER|DM_PIVOT)$/;
        if ($intent->{tx} ne $anchor->{tx}) {
            return $blocked->('new transition has modified runtime before PREPARED was recorded')
                if $runtime !~ /^(?:absent|linear-head)$/;
            return $result->('RECOVERY_REQUIRED', 'PREPARE_UNRECORDED', 'NOT_STARTED',
                'OPEN intent and signed transition objects exist but PREPARED anchor was not recorded')
                if $objects->{candidate_new} && $objects->{meta};
            return $result->('RECOVERY_REQUIRED', 'PREPARE_PARTIAL', 'NOT_STARTED',
                'OPEN intent has an incomplete set of pre-PREPARED transition objects')
                if $objects->{candidate_new} || $objects->{meta};
            return $result->('RECOVERY_REQUIRED', 'PREPARE_INCOMPLETE', 'MATERIALIZED',
                'OPEN cutover intent exists but PREPARED anchor was not recorded');
        }
        my $expected_op = $anchor->{op} eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER';
        return $blocked->('finalized transition and VG intent operations differ')
            if $anchor->{op} ne 'ALLOC' && $intent->{op} ne $expected_op;
        return $blocked->('finalized transition has an unexpected runtime mapping')
            if $runtime !~ /^(?:absent|linear-head|linear-new)$/;
        return $blocked->('finalized frontend is suspended') if $runtime_suspended;
        my $object_error = $require_stable_objects->();
        return $blocked->($object_error) if defined($object_error);
        return $result->('RECOVERY_REQUIRED', 'FINALIZE_PENDING', 'MATERIALIZED',
            'materialization is proven but OPEN intent remains');
    }

    return $blocked->('transition phase has no matching OPEN intent')
        if ref($intent) ne 'HASH';
    eval { vg_intent_tags(%$intent); };
    return $blocked->("VG intent is invalid: $@") if $@;
    return $blocked->('transition transaction and VG intent differ')
        if $intent->{tx} ne $anchor->{tx};
    my $expected_op = $anchor->{op} eq 'ROLLBACK' ? 'DM_PIVOT'
        : $anchor->{op} eq 'SNAPSHOT' ? 'DM_CUTOVER' : '';
    return $blocked->('transition anchor operation is unsupported') if !$expected_op;
    return $blocked->('transition and VG intent operations differ')
        if $intent->{op} ne $expected_op;
    return $blocked->('VG intent refers to another anchor')
        if !defined($expected_anchor) || $intent->{object} ne $expected_anchor;
    return $blocked->('transition source, old HEAD, or destination is missing')
        if !$objects->{source} || !$objects->{old} || !$objects->{new};

    if ($anchor->{phase} eq 'PREPARED') {
        return $blocked->('PREPARED transition metadata is missing') if !$objects->{meta};
        return $blocked->('PREPARED transition published an unexpected runtime mapping')
            if $runtime !~ /^(?:absent|linear-old|linear-head)$/;
        return $result->('RECOVERY_REQUIRED', 'PREPARED', 'NOT_STARTED',
            'persistent objects are prepared; cutover has not committed');
    }
    if ($anchor->{phase} eq 'SOURCE_READY') {
        return $blocked->('SOURCE_READY transition metadata is missing') if !$objects->{meta};
        return $blocked->('SOURCE_READY transition published an unexpected runtime mapping')
            if $runtime !~ /^(?:linear-old|linear-head)$/;
        return $result->('RECOVERY_REQUIRED', 'SOURCE_READY', 'READY',
            'metadata and immutable source view are ready; cutover has not committed');
    }
    if ($anchor->{phase} eq 'COMMITTED' || $anchor->{phase} eq 'HYDRATING') {
        return $blocked->('committed transition metadata is missing') if !$objects->{meta};
        if ($anchor->{phase} eq 'COMMITTED' && $runtime eq 'linear-old') {
            return $blocked->('committed old-generation frontend is unexpectedly active')
                if !$runtime_suspended;
            return $result->('RECOVERY_REQUIRED', 'COMMITTED', 'PUBLISH_REQUIRED',
                'cutover is committed and the suspended old-generation frontend awaits exact clone publication');
        }
        return $result->('RECOVERY_REQUIRED', $anchor->{phase}, 'RECONSTRUCT_REQUIRED',
            'runtime clone mapping is absent and must be reconstructed from exact evidence')
            if $runtime eq 'absent';
        return $blocked->('committed transition has an unexpected runtime mapping')
            if $runtime ne 'clone';
        return $blocked->('committed clone frontend is suspended') if $runtime_suspended;
        return $blocked->('clone runtime dependency does not prove the signed source generation')
            if $clone_source ne 'source';
        return $blocked->('clone target reports failed or unknown metadata state')
            if $clone_status !~ /^(?:incomplete|complete)$/;
        return $result->('RECOVERY_REQUIRED', $anchor->{phase},
            ($clone_status eq 'complete' ? 'COMPLETE' : 'HYDRATING'),
            'clone mapping remains authoritative pending explicit recovery');
    }
    if ($anchor->{phase} eq 'HYDRATION_COMPLETE') {
        return $blocked->('hydration-complete transition metadata is missing') if !$objects->{meta};
        return $result->('RECOVERY_REQUIRED', 'HYDRATION_COMPLETE', 'REVERIFY_REQUIRED',
            'runtime mapping is absent; persistent clone metadata must be reopened and verified')
            if $runtime eq 'absent';
        if ($runtime eq 'clone') {
            return $blocked->('hydration-complete clone frontend is suspended')
                if $runtime_suspended;
            return $blocked->('clone runtime dependency does not prove the signed source generation')
                if $clone_source ne 'source';
            return $blocked->('anchor claims complete hydration but clone status does not')
                if $clone_status ne 'complete';
            return $result->('RECOVERY_REQUIRED', 'HYDRATION_COMPLETE', 'PIVOT_READY',
                'complete clone mapping is ready for an explicit linear pivot');
        }
        return $result->('RECOVERY_REQUIRED', 'PIVOT_UNRECORDED', 'MATERIALIZED',
            'linear destination is live but LINEAR_PIVOTED was not recorded')
            if $runtime eq 'linear-new';
        return $blocked->('hydration-complete transition has an unexpected runtime mapping');
    }
    if ($anchor->{phase} eq 'LINEAR_PIVOTED') {
        return $blocked->('linear-pivoted transition has an unexpected runtime mapping')
            if $runtime !~ /^(?:absent|linear-new|linear-head)$/;
        return $blocked->('linear-pivoted frontend is suspended') if $runtime_suspended;
        return $result->('RECOVERY_REQUIRED', 'LINEAR_PIVOTED', 'FINALIZE_READY',
            'destination is authoritative; detached transition artifacts require exact cleanup');
    }
    return $blocked->('unsupported recovery phase');
}

sub vg_intent_tags {
    my (%values) = @_;
    $values{v} = 1 if !defined($values{v});
    die "unsupported VG intent version\n" if "$values{v}" !~ /^(1)$/;
    $values{v} = $1;
    die "invalid VG intent transaction ID\n"
        if !defined($values{tx}) || $values{tx} !~ /^($TX)$/;
    $values{tx} = $1;
    die "invalid VG intent state\n" if ($values{state} // '') !~ /^(OPEN)$/;
    $values{state} = $1;
    die "invalid VG intent operation\n"
        if ($values{op} // '') !~ /^(ALLOC|EXTEND|REMOVE|REMOVE_SNAPSHOT|DM_CUTOVER|DM_PIVOT)$/;
    $values{op} = $1;
    $values{object} = _token('VG intent object', $values{object});
    die "invalid VG intent before digest\n"
        if ($values{before} // '') !~ /^([0-9a-f]{16,64})$/;
    $values{before} = $1;
    my @fields = qw(v tx state op object before);
    my $canonical = join('|', map { "$_=$values{$_}" } @fields);
    my @tags = map { "slt_tg_vgi_$_=$values{$_}" } @fields;
    push @tags, 'slt_tg_vgi_sha256=' . substr(sha256_hex($canonical), 0, 32);
    return \@tags;
}

sub decode_vg_intent_tags {
    my ($tags) = @_;
    my @tags = ref($tags) eq 'ARRAY' ? @$tags : split(/,/, $tags // '');
    my %values;
    for my $tag (@tags) {
        $tag =~ s/^\s+|\s+$//g;
        next if $tag !~ /^slt_tg_vgi_/;
        die "malformed VG intent tag\n"
            if $tag !~ /^slt_tg_vgi_([A-Za-z0-9_]+)=($TOKEN)$/;
        my ($field, $value) = ($1, $2);
        die "duplicate VG intent field '$field'\n" if exists($values{$field});
        die "unknown VG intent field '$field'\n"
            if !grep { $_ eq $field } qw(v tx state op object before sha256);
        $values{$field} = $value;
    }
    return undef if !keys(%values);
    die "incomplete VG intent\n"
        if keys(%values) != 7
        || grep { !exists($values{$_}) } qw(v tx state op object before sha256);
    my $wanted = vg_intent_tags(%values);
    my ($digest) = map { /^slt_tg_vgi_sha256=(.*)$/ ? $1 : () } @$wanted;
    die "VG intent digest mismatch\n" if $values{sha256} ne $digest;
    return \%values;
}

1;
