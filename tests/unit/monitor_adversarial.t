#!/usr/bin/perl

use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Test::More;

my $monitor = "$FindBin::Bin/../../usr/libexec/pve-sharedlvmthin/pve-sharedlvmthin-monitor";
do $monitor or die $@ || $!;

sub run_case {
    my (%case) = @_;
    my @commands;
    my @reads;
    my $stderr = '';
    my $post_reads = 0;
    my $gib = 1024 * 1024 * 1024;

    no warnings 'redefine';
    local @ARGV = ('testvg/sltp-900001');
    local *PVE::Storage::config = sub { return {}; };
    local *PVE::Storage::storage_config = sub {
        my ($cfg, $storeid) = @_;
        die "unknown storage '$storeid'\n" if $storeid ne 'sharedthin-test';
        return {
            type => 'sharedlvmthin', shared => 1,
            'slt-vgname' => 'testvg',
            'slt-vg-reserve-percent' => 5,
            'slt-vg-reserve-gib' => 100,
            ($case{elastic} ? (
                'slt-initial-pool-mode' => 'elastic',
                'slt-burst-headroom-gib' => 64,
            ) : ()),
        };
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_mutation_quorum = sub {
        die "no quorum\n" if $case{quorum_fail};
        return 1;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub {
        die "identity mismatch\n" if $case{identity_fail};
        return 1;
    };
    local *PVE::Cluster::cfs_lock_storage = sub {
        my ($storeid, $timeout, $code) = @_;
        die "storage lock unavailable\n" if $case{lock_fail};
        return $code->();
    };
    local *main::read_growth_percent = sub { return 10; };
    local *main::read_data_threshold = sub { return 80; };
    local *main::read_fields = sub {
        my ($command) = @_;
        my $line = join(' ', @$command);
        push @reads, $line;
        return (
            $case{locked_tag} // 'pve-slt-sid-sharedthin-test',
            $case{needs_check} ? 'twi-cotz--'
                : $case{metadata_readonly} ? 'twi-aotzM-'
                : $case{inactive} ? 'twi---tz--' : 'twi-aotz--',
            $case{health_status} // '',
            $case{check_needed} // '',
        ) if $line =~ /lv_tags,lv_attr/;
        return ($case{data_percent} // 85) if $line =~ /data_percent/;
        return (1000 * $gib, ($case{vg_free} // 300) * $gib, 4 * 1024 * 1024)
            if $line =~ /vg_size,vg_free/;
        if ($line =~ /lv_size/) {
            $post_reads++;
            die "postcondition unavailable\n"
                if $case{post_unknown} && $post_reads > 1;
            return (($post_reads > 1 ? $case{post_size} : ($case{pool_size} // 100)) * $gib);
        }
        die "unexpected read_fields: $line\n";
    };
    local *main::run_command = sub {
        my ($command, %options) = @_;
        push @commands, join(' ', @$command);
        if (join(' ', @$command) =~ /lv_tags/) {
            $options{outfunc}->('pve-slt-sid-sharedthin-test') if $options{outfunc};
            return;
        }
        die "lvextend timeout\n" if $case{extend_error};
        return;
    };
    open(my $err, '>', \$stderr) or die $!;
    local *STDERR = $err;
    my ($rc, $died);
    eval { $rc = main::main(); 1 } or $died = $@;
    close($err);
    return {
        rc => $rc, died => $died, stderr => $stderr,
        extend_calls => scalar(grep { /\/sbin\/lvextend/ } @commands),
        commands => \@commands,
        reads => \@reads,
    };
}

subtest 'healthy event performs one cluster-locked growth' => sub {
    my $r = run_case();
    is($r->{rc}, 0, 'event succeeded');
    is($r->{extend_calls}, 1, 'exactly one lvextend');
    my ($usage_read) = grep { /-o data_percent/ } @{$r->{reads}};
    ok(defined($usage_read), 'current Data% is re-read under lock');
    unlike($usage_read, qr/--readonly/, 'live device-mapper Data% is not suppressed by lvs --readonly');
    unlike(
        join('\n', @{$r->{commands}}), qr/lvchange|setautoactivation/,
        'dmeventd/autogrow never changes the autoactivation policy',
    );
};

subtest 'elastic event grows to used plus absolute headroom' => sub {
    my $r = run_case(elastic => 1, pool_size => 100, data_percent => 85);
    is($r->{rc}, 0, 'elastic event succeeded');
    is($r->{extend_calls}, 1, 'elastic event issued exactly one grow');
    like(join('\n', @{$r->{commands}}), qr/lvextend -L 159987531776B testvg\/sltp-900001/,
        'target is rounded used plus 64 GiB, independent of virtual disk size');
};

subtest 'thin-pool health failures disable autogrow without repair' => sub {
    for my $case (
        ['needs_check', needs_check => 1],
        ['metadata read-only', metadata_readonly => 1],
        ['explicit health failure', health_status => 'needs_check'],
        ['explicit check-needed field', check_needed => 'yes'],
    ) {
        my ($name, %args) = @$case;
        my $r = run_case(%args);
        is($r->{rc}, 1, "$name returned controlled failure");
        like($r->{stderr}, qr/CRITICAL:.*autogrow disabled/, "$name reported");
        is($r->{extend_calls}, 0, "$name ran zero lvextend");
        unlike(join('\n', @{$r->{commands}}), qr/thin_(?:check|repair)|lvconvert/, "$name ran zero repair commands");
    }
};

subtest 'quorum, identity, and reserve gates run before lvextend' => sub {
    for my $case (
        ['quorum', quorum_fail => 1, qr/no quorum/],
        ['identity', identity_fail => 1, qr/identity mismatch/],
        ['reserve', vg_free => 105, qr/CRITICAL: refusing autogrow/],
    ) {
        my ($name, $key, $value, $error) = @$case;
        my $r = run_case($key => $value);
        is($r->{rc}, 1, "$name failure returned controlled error");
        is($r->{extend_calls}, 0, "$name failure ran zero lvextend");
        like($r->{stderr}, $error, "$name reason reported");
    }
};

subtest 'lock acquisition failure executes no mutation' => sub {
    my $r = run_case(lock_fail => 1);
    is($r->{rc}, 1, 'lock failure is controlled');
    is($r->{extend_calls}, 0, 'lock failure ran zero lvextend');
    like($r->{stderr}, qr/storage lock unavailable/, 'lock failure reported');
};

subtest 'uncertain lvextend outcome is classified without retry' => sub {
    my $completed = run_case(
        extend_error => 1, pool_size => 100, post_size => 110,
    );
    is($completed->{rc}, 0, 'size increase treated as partial/completed');
    is($completed->{extend_calls}, 1, 'completed outcome not retried');
    like($completed->{stderr}, qr/PARTIAL\/COMPLETED/, 'partial completion reported');

    my $unchanged = run_case(
        extend_error => 1, pool_size => 100, post_size => 100,
    );
    is($unchanged->{rc}, 1, 'unchanged size is controlled failure');
    is($unchanged->{extend_calls}, 1, 'unchanged outcome not immediately retried');
    like($unchanged->{stderr}, qr/controlled failure/, 'unchanged state reported');

    my $unknown = run_case(
        extend_error => 1, pool_size => 100, post_unknown => 1,
    );
    is($unknown->{rc}, 1, 'unknown outcome stops');
    is($unknown->{extend_calls}, 1, 'unknown outcome not retried');
    like($unknown->{stderr}, qr/UNKNOWN/, 'unknown state reported');
};

subtest 'inactive, foreign, and stale events never grow' => sub {
    my $inactive = run_case(inactive => 1);
    is($inactive->{rc}, 1, 'inactive pool refused');
    is($inactive->{extend_calls}, 0, 'inactive pool zero growth');
    like($inactive->{stderr}, qr/inactive/, 'inactive reason reported');

    my $foreign = run_case(locked_tag => 'pve-slt-sid-other-storage');
    is($foreign->{rc}, 1, 'ownership change refused');
    is($foreign->{extend_calls}, 0, 'foreign pool zero growth');
    like($foreign->{stderr}, qr/ownership .* changed/, 'ownership change reported');

    my $stale = run_case(data_percent => 70);
    is($stale->{rc}, 0, 'stale event safely coalesced');
    is($stale->{extend_calls}, 0, 'stale event zero growth');
    like($stale->{stderr}, qr/stale\/coalesced event/, 'stale event reported');
};

subtest 'sequential pool events recalculate reserve under the lock' => sub {
    my $first = run_case(vg_free => 150, pool_size => 400);
    is($first->{rc}, 0, 'pool A growth allowed');
    is($first->{extend_calls}, 1, 'pool A grew once');

    my $second = run_case(vg_free => 110, pool_size => 400);
    is($second->{rc}, 1, 'pool B blocked using refreshed free space');
    is($second->{extend_calls}, 0, 'pool B did not consume reserve');
    like($second->{stderr}, qr/projected free .* protected reserve/, 'fresh reserve decision reported');
};

subtest 'snapshot or concurrent allocation consumption is reflected under lock' => sub {
    my $before_snapshot = run_case(vg_free => 150, pool_size => 400);
    is($before_snapshot->{extend_calls}, 1, 'growth would fit before concurrent consumption');

    my $after_snapshot = run_case(vg_free => 130, pool_size => 400);
    is($after_snapshot->{rc}, 1, 'growth refused after refreshed free-space read');
    is($after_snapshot->{extend_calls}, 0, 'no stale pre-snapshot reserve decision used');
    like($after_snapshot->{stderr}, qr/protected reserve/, 'reserve conflict reported');
};

subtest 'event storm is coalesced by current usage under lock' => sub {
    my $trigger = run_case(data_percent => 85);
    is($trigger->{extend_calls}, 1, 'first threshold event grows once');

    for my $event (1 .. 4) {
        my $coalesced = run_case(data_percent => 72);
        is($coalesced->{rc}, 0, "event $event safely ignored after growth");
        is($coalesced->{extend_calls}, 0, "event $event issued no duplicate lvextend");
    }
};

done_testing();
