use strict;
use warnings;
use FindBin;
use lib "$FindBin::Bin/lib", "$FindBin::Bin/../../usr/share/perl5";
use Test::More;
use JSON::PP;
use Digest::SHA qw(sha256_hex);
use PVE::SharedLvmThinVMDestroyRuntime;

my $CLASS = 'PVE::SharedLvmThinVMDestroyRuntime';
my $JSON = JSON::PP->new->canonical;
my $BASE = '/usr/share/pve-sharedlvmthin';
my $CHECKER = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmdestroy-contract-check';
my $RELEASE = '/var/lib/pve-sharedlvmthin/update-guard/runtime-release.json';
my @PACKAGES = qw(pve-manager libpve-storage-perl qemu-server pve-qemu-kvm libpve-common-perl);
my %DIGEST = (14 => '06e848db8abaf5e765845577b90d38f97d8d3e0d29e24212504d77ee60c0c1ab',
    15 => 'd52049347b091b6091671887b8c5087e9001445c793c1a719823fbbb047a220e');
sub copy { $JSON->decode($JSON->encode($_[0])) }
sub policy_hash { sha256_hex(JSON::PP->new->canonical->ascii->encode($_[0]) . "\n") }
sub refused {
    my ($code, $regex, $label) = @_;
    my $ok = eval { $code->(); 1 }; my $error = $@;
    ok(!$ok, "$label refused"); like($error, $regex, "$label diagnostic");
}

sub fixture {
    my ($api, $flavor, $count) = @_;
    $api //= 15; $flavor //= 'dual'; $count //= 3;
    my $boot = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';
    my $build = 'a' x 64;
    my @nodes = map { "node-$_" } 1 .. $count;
    my $state = { node => 'node-1', owner => {node => 'node-1', type => 'qemu'},
        nodes => \@nodes, members => {map { $_ => {online => 1} } @nodes},
        apiver => $api, apiage => $api - 9, kernel => '7.0.14-17-pve', loaded_build_id => $build,
        package_operations_released => 1, quorate => 1 };
    my $tuple = { id => 'fixture-qualified', profiles => ['dual','thick-only'], plugin_versions => ['0.9.0~fixture'],
        api => $api, running_kernel => $state->{kernel}, packages => {map { $_ => '1.2.3' } @PACKAGES},
        status => 'EXACT_LAB_TESTED', scopes => ['api-hooks','san-dataplane'] };
    my $manifest = { schema => 1, catalogue => 'pve-qualified-tuples-v1', packages => \@PACKAGES, tuples => [$tuple] };
    my $plugin = $flavor eq 'dual' ? 'pve-sharedlvmthin' : 'pve-sharedlvmthin-thick';
    my $release = {schema => 1, qualified => 'QUALIFIED', runtime_build_id => $build, plan_digest => 'b' x 64,
        boot_id => $boot, kernel_release => $state->{kernel}, plugin_package => $plugin,
        plugin_version => '0.9.0~fixture', artifact_sha256 => 'd' x 64,
        runtime_tuple_id => $tuple->{id}, runtime_tuple_status => $tuple->{status},
        runtime_tuple_sha256 => policy_hash($tuple), runtime_manifest_sha256 => policy_hash($manifest)};
    my %files = ("$BASE/package-flavor" => "$flavor\n", "$BASE/runtime-build-id" => "$build\n",
        "$BASE/package-artifact-sha256" => ('d' x 64) . "\n",
        "$BASE/pve-qualified-tuples.json" => $JSON->encode($manifest), $RELEASE => $JSON->encode($release),
        '/proc/sys/kernel/random/boot_id' => "$boot\n",
        '/etc/pve/corosync.conf' => "nodelist {\n" . join('', map { " node {\n  name: $_\n }\n" } @nodes) . "}\nquorum {\n provider: corosync_votequorum\n}\n",
        $CHECKER => "fixture checker\n", '/usr/share/perl5/PVE/Storage.pm' => "fixture storage\n",
        '/usr/share/perl5/PVE/QemuServer.pm' => "fixture qemu\n", '/usr/share/perl5/PVE/API2/Qemu.pm' => "fixture API\n");
    my $dpkg = join('', map { "$_\t1.2.3\tamd64\tinstalled\tok\n" } @PACKAGES)
        . "$plugin\t0.9.0~fixture\tall\tinstalled\tok\n";
    my $contract = "QMDestroy_CONTRACT_VERSION=1\nNATIVE_CONFIG_CAS=ABSENT\n"
        . "VDISK_FREE_FAILURE_POLICY=WARN_AND_CONTINUE\nFINAL_CONFIG_REMOVAL=AFTER_DESTROY_VM\n"
        . "FLEECING_CLEANUP_CALL=PINNED\nIPAM_CLEANUP_CALL=PINNED\n"
        . "CONTRACT_SHA256=$DIGEST{$api}\nCONTRACT_VARIANT=API$api\n"
        . "MUTATION_ADAPTER=QUALIFIED\nQMDestroy_CONTRACT=QUALIFIED\n";
    return {files => \%files, state => $state, manifest => $manifest, release => $release, dpkg => $dpkg,
        votes => "Nodes: $count\nExpected votes: $count\nTotal votes: $count\nQuorate: Yes\n",
        contract => $contract, calls => [], reads => {}, pve_reads => 0, clock => 0};
}

sub verifier {
    my ($f) = @_;
    return $CLASS->new(reader => sub {
        my ($path) = @_;
        die "unexpected file $path" if !exists($f->{files}->{$path});
        $f->{reads}->{$path}++;
        return $f->{files}->{$path} . 'changed' if ($f->{file_drift} // '') eq $path && $f->{reads}->{$path} > 1;
        return $f->{files}->{$path};
    }, pve_reader => sub {
        is($_[0], 101, 'exact VM requested');
        $f->{pve_reads}++;
        my $state = copy($f->{state});
        $state->{owner}->{node} = 'node-2' if $f->{owner_drift} && $f->{pve_reads} > 1;
        return $state;
    }, runner => sub {
        my ($argv, $timeout) = @_;
        die 'injected timeout' if $f->{timeout};
        ok($timeout > 0 && $timeout <= 10, 'bounded command');
        push @{$f->{calls}}, [@$argv];
        return $f->{dpkg} if $argv->[0] eq '/usr/bin/dpkg-query';
        return $f->{votes} if $argv->[0] eq '/usr/bin/pvecm';
        return $f->{contract} if $argv->[0] eq $CHECKER;
        return $f->{audit} // '' if $argv->[0] eq '/usr/bin/dpkg' && $argv->[1] eq '--audit';
        return $f->{verify} // '' if $argv->[0] eq '/usr/bin/dpkg' && $argv->[1] eq '--verify';
        die 'unexpected command';
    }, clock => sub { $f->{clock} += $f->{clock_step} // 0; return $f->{clock} },
    journal_reader => sub {
        is($_[0], 'c' x 32, 'exact recovery txid requested');
        $f->{journal_reads}++;
        die "journal observation failed\n" if $f->{journal_error};
        $f->{journal_mutate}->($f, $f->{journal_reads}) if $f->{journal_mutate};
        return copy($f->{journal});
    }, finalization_reader => sub {
        is($_[0], 101, 'exact finalization VM requested');
        $f->{finalization_reads}++;
        die "finalization observation failed\n" if $f->{finalization_error};
        $f->{finalization_mutate}->($f, $f->{finalization_reads}) if $f->{finalization_mutate};
        return copy($f->{finalization});
    }, ($f->{fresh_process_reader} ? (fresh_process_reader => $f->{fresh_process_reader}) : ()));
}
sub update_manifest { $_[0]->{files}->{"$BASE/pve-qualified-tuples.json"} = $JSON->encode($_[0]->{manifest}) }
sub update_release { $_[0]->{files}->{$RELEASE} = $JSON->encode($_[0]->{release}) }

# Verifier must enforce real/effective root itself, not through a supplied probe.
if ($> != 0 || $< != 0) {
    refused(sub { verifier(fixture())->verify(101) }, qr/root required/, 'nonroot invocation');
    done_testing(); exit;
}

for my $api (14, 15) {
    for my $profile ('dual', 'thick-only') {
        subtest "API$api $profile exact repeatable evidence" => sub {
            my $f = fixture($api, $profile); my $v = verifier($f);
            my $before = $v->verify(101); my $after = $v->verify(101);
            is($before->{authority}, 'NONE', 'observation has no mutation authority');
            is($before->{profile}, $profile, 'exact profile');
            is($before->{apiage}, $api - 9, 'exact APIAGE');
            is($before->{contract_sha256}, $DIGEST{$api}, 'qualified source contract digest');
            ok($CLASS->assert_same($before, $after), 'byte-exact repeated observation');
            is_deeply($f->{calls}->[0], ['/usr/bin/dpkg-query','-W',
                '-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\t${db:Status-Eflag}\n',
                @PACKAGES,'pve-sharedlvmthin*'], 'fixed package argv; pattern not expanded by shell');
            is_deeply($f->{calls}->[1], ['/usr/bin/dpkg','--audit'], 'read-only package audit');
            is_deeply($f->{calls}->[2], ['/usr/bin/dpkg','--verify',$before->{package}], 'exact profile payload audit');
            is_deeply($f->{calls}->[3], ['/usr/bin/pvecm','status'], 'read-only quorum argv');
            is_deeply($f->{calls}->[4], [$CHECKER], 'fixed checker without overrides');
        };
    }
}

my $api15_digest = $DIGEST{15};
my @cases = (
    ['API16', sub { $_[0]->{state}->{apiver} = 16 }, qr/APIVER/],
    ['wrong APIAGE', sub { $_[0]->{state}->{apiage} = 5 }, qr/APIVER/],
    ['unknown owner', sub { delete $_[0]->{state}->{owner} }, qr/locally owned/],
    ['foreign owner', sub { $_[0]->{state}->{owner}->{node} = 'node-2' }, qr/locally owned/],
    ['LXC owner', sub { $_[0]->{state}->{owner}->{type} = 'lxc' }, qr/locally owned/],
    ['no quorum', sub { $_[0]->{state}->{quorate} = 0 }, qr/quorum/],
    ['maintenance pending', sub { $_[0]->{state}->{package_operations_released} = 0 }, qr/admission/],
    ['loaded old plugin', sub { $_[0]->{state}->{loaded_build_id} = 'c' x 64 }, qr/loaded\/installed/],
    ['stale boot release', sub { $_[0]->{release}->{boot_id} = 'b' x 36; update_release($_[0]) }, qr/release identity/],
    ['stale kernel release', sub { $_[0]->{release}->{kernel_release} = 'old'; update_release($_[0]) }, qr/release identity/],
    ['unqualified runtime release', sub { $_[0]->{release}->{qualified} = 'UNQUALIFIED'; update_release($_[0]) }, qr/release identity/],
    ['profile whitespace ambiguity', sub { $_[0]->{files}->{"$BASE/package-flavor"} = "dual \n" }, qr/profile marker/],
    ['invalid boot UUID', sub { $_[0]->{files}->{'/proc/sys/kernel/random/boot_id'} = "garbage\n" }, qr/boot identity/],
    ['invalid build SHA', sub { $_[0]->{files}->{"$BASE/runtime-build-id"} = "garbage\n" }, qr/build identity/],
    ['invalid plan SHA', sub { $_[0]->{release}->{plan_digest} = 'garbage'; update_release($_[0]) }, qr/release identity/],
    ['profile package mismatch', sub { $_[0]->{files}->{"$BASE/package-flavor"} = "thick-only\n" }, qr/conflicting profile/],
    ['half-configured package', sub { $_[0]->{dpkg} =~ s/installed/half-configured/ }, qr/not installed/],
    ['reinstreq package', sub { $_[0]->{dpkg} =~ s/\tok\n/\treinstreq\n/ }, qr/package identity/],
    ['duplicate package', sub { $_[0]->{dpkg} .= "pve-manager\t1.2.3\tamd64\tinstalled\tok\n" }, qr/package identity/],
    ['multiarch unexpected', sub { $_[0]->{dpkg} =~ s/amd64/arm64/ }, qr/package identity/],
    ['unknown plugin package', sub { $_[0]->{dpkg} .= "pve-sharedlvmthin-extra\t1\tall\tinstalled\tok\n" }, qr/package identity/],
    ['plugin version drift', sub { $_[0]->{dpkg} =~ s/0.9.0~fixture/0.9.0~other/ }, qr/release package\/version/],
    ['dirty dpkg audit', sub { $_[0]->{audit} = 'half-configured unrelated package' }, qr/not clean/],
    ['modified installed plugin', sub { $_[0]->{verify} = 'modified package payload' }, qr/not clean/],
    ['release artifact drift', sub { $_[0]->{release}->{artifact_sha256} = 'e' x 64; update_release($_[0]) }, qr/artifact mismatch/],
    ['release tuple replay', sub { $_[0]->{release}->{runtime_tuple_id} = 'old'; update_release($_[0]) }, qr/tuple\/manifest binding/],
    ['release manifest replay', sub { $_[0]->{release}->{runtime_manifest_sha256} = 'e' x 64; update_release($_[0]) }, qr/tuple\/manifest binding/],
    ['partial manifest package set', sub { delete $_[0]->{manifest}->{tuples}->[0]->{packages}->{'qemu-server'}; update_manifest($_[0]) }, qr/incomplete/],
    ['ambiguous tuples', sub { my $t = copy($_[0]->{manifest}->{tuples}->[0]); $t->{id} = 'second'; push @{$_[0]->{manifest}->{tuples}}, $t; update_manifest($_[0]) }, qr/tuple unlisted or ambiguous/],
    ['RETEST tuple', sub { $_[0]->{manifest}->{tuples}->[0]->{status} = 'RETEST_REQUIRED'; update_manifest($_[0]) }, qr/not SAN qualified/],
    ['control-plane only', sub { $_[0]->{manifest}->{tuples}->[0]->{scopes} = ['api-hooks']; update_manifest($_[0]) }, qr/not SAN qualified/],
    ['outstanding tests', sub { $_[0]->{manifest}->{tuples}->[0]->{required_tests} = ['not-run']; update_manifest($_[0]) }, qr/outstanding/],
    ['excluded SAN scope', sub { $_[0]->{manifest}->{tuples}->[0]->{excluded_scopes} = ['san-dataplane']; update_manifest($_[0]) }, qr/excludes/],
    ['advisory allocation blocked', sub { $_[0]->{manifest}->{tuples}->[0]->{operation_qualifications} = {'allocation-removal' => {status => 'BLOCKED'}}; update_manifest($_[0]) }, qr/allocation-removal/],
    ['duplicate JSON escaped key', sub { $_[0]->{files}->{"$BASE/pve-qualified-tuples.json"} = '{"schema":1,"sch\\u0065ma":1}' }, qr/duplicate JSON/],
    ['contract duplicate line', sub { $_[0]->{contract} .= "QMDestroy_CONTRACT=QUALIFIED\n" }, qr/contract output/],
    ['contract unknown digest', sub { $_[0]->{contract} =~ s/\Q$api15_digest\E/@{['a' x 64]}/ }, qr/contract output/],
    ['contract wrong variant', sub { $_[0]->{contract} =~ s/API15/API14/ }, qr/contract output/],
    ['contract warning policy changed', sub { $_[0]->{contract} =~ s/WARN_AND_CONTINUE/ABORT/ }, qr/contract output/],
    ['offline member', sub { $_[0]->{state}->{members}->{'node-2'}->{online} = 0 }, qr/member offline/],
    ['duplicate member', sub { $_[0]->{state}->{nodes}->[2] = 'node-2' }, qr/member offline/],
    ['unknown extra member', sub { $_[0]->{state}->{members}->{'node-4'} = {online=>1} }, qr/membership mismatch/],
    ['configured membership mismatch', sub { $_[0]->{files}->{'/etc/pve/corosync.conf'} =~ s/name: node-2/name: foreign/ }, qr/member offline/],
    ['forced expected votes', sub { $_[0]->{votes} =~ s/Expected votes: 3/Expected votes: 1/ }, qr/vote topology/],
    ['duplicate quorum field', sub { $_[0]->{votes} .= "Quorate: Yes\n" }, qr/missing\/duplicate/],
    ['two_node override', sub { $_[0]->{files}->{'/etc/pve/corosync.conf'} .= "two_node: 1\n" }, qr/quorum override/],
    ['probe timeout', sub { $_[0]->{timeout} = 1 }, qr/injected timeout/],
    ['total deadline', sub { $_[0]->{clock_step} = 5 }, qr/deadline/],
    ['source drift', sub { $_[0]->{file_drift} = '/usr/share/perl5/PVE/QemuServer.pm' }, qr/evidence changed/],
    ['owner drift', sub { $_[0]->{owner_drift} = 1 }, qr/PVE runtime changed/],
    ['empty file', sub { $_[0]->{files}->{$CHECKER} = '' }, qr/evidence bytes/],
);
for my $case (@cases) {
    subtest $case->[0] => sub { my $f = fixture(); $case->[1]->($f);
        refused(sub { verifier($f)->verify(101) }, $case->[2], $case->[0]);
    };
}

subtest 'invalid identities refuse before every probe' => sub {
    my $probes = 0;
    my $v = $CLASS->new(reader => sub { $probes++; die 'unexpected probe' },
        runner => sub { $probes++; die 'unexpected probe' },
        pve_reader => sub { $probes++; die 'unexpected probe' },
        clock => sub { $probes++; die 'unexpected probe' });
    for my $vmid (undef, '', '0', '-1', '101;id', "101\n", '1010000000', []) {
        refused(sub { $v->verify($vmid) }, qr/invalid VM identity/, 'invalid VM identity');
    }
    is($probes, 0, 'no command/file/PVE/clock probes');
};

subtest 'project constrained 2-of-2 policy, never 1-of-2 forced quorum' => sub {
    my $f = fixture(15, 'dual', 2);
    is(verifier($f)->verify(101)->{topology}->{policy}, 'PROJECT_CONSTRAINED_2_OF_2', 'existing policy honored');
    $f->{state}->{members}->{'node-2'}->{online} = 0;
    $f->{votes} = "Nodes: 1\nExpected votes: 1\nTotal votes: 1\nQuorate: Yes\n";
    refused(sub { verifier($f)->verify(101) }, qr/member offline/, 'forced quorum is not fencing');
};

subtest 'advisory operation matrix cannot promote an unqualified tuple' => sub {
    my $f = fixture(); my $manifest = $f->{manifest}; my $tuple = $manifest->{tuples}->[0];
    my @groups = qw(read-only-inventory package-lifecycle activation-deactivation allocation-removal
        snapshot-vmstate resize move-import-restore migration thin-ownership-autogrow thick-lazy-transition);
    $manifest->{operation_groups} = \@groups;
    $manifest->{operation_qualification_mode} = 'ADVISORY_ONLY';
    $manifest->{operation_authorization} = 'NONE';
    $manifest->{operation_evidence_ids} = ['fixture-evidence'];
    $tuple->{operation_qualifications} = {map { $_ => {status=>'QUALIFIED', evidence=>['fixture-evidence']} } @groups};
    update_manifest($f);
    $f->{release}->{runtime_tuple_sha256} = policy_hash($tuple);
    $f->{release}->{runtime_manifest_sha256} = policy_hash($manifest); update_release($f);
    is(verifier($f)->verify(101)->{status}, 'QUALIFIED', 'matching global tuple plus advisory group');
    $tuple->{status} = 'RETEST_REQUIRED'; update_manifest($f);
    refused(sub { verifier($f)->verify(101) }, qr/not SAN qualified/, 'group never promotes RETEST tuple');
    $tuple->{status} = 'EXACT_LAB_TESTED';
    $tuple->{operation_qualifications}->{'allocation-removal'}->{evidence} = ['invented']; update_manifest($f);
    refused(sub { verifier($f)->verify(101) }, qr/evidence unregistered/, 'unregistered evidence');
};

subtest 'second verification refuses stable-but-changed package/source/boot evidence' => sub {
    my $f = fixture(); my $v = verifier($f); my $before = $v->verify(101);
    $f->{files}->{$CHECKER} .= '# source changed';
    my $after = $v->verify(101);
    refused(sub { $CLASS->assert_same($before,$after) }, qr/between VM-lock observations/, 'source replay');
    refused(sub { $CLASS->assert_same({},$after) }, qr/invalid runtime comparison/, 'caller supplied status');
};

sub recovery_journal {
    my ($context) = @_;
    my $json = JSON::PP->new->canonical->ascii;
    my ($previous, @rows);
    for my $stage (qw(PREPARED DISPATCHED STORAGE_ABSENT FINALIZING)) {
        my $row = { schema => 'sharedlvmthin-vm-destroy-journal/v1', authority => 'NONE',
            request_id => 'c' x 32, sequence => scalar(@rows) + 1, stage => $stage,
            previous_sha256 => $previous, context => copy($context),
            context_sha256 => sha256_hex($json->encode($context)), evidence => { evidence => { stage => $stage } } };
        push @rows, $row;
        $previous = sha256_hex($json->encode($row));
    }
    return { schema => 'sharedlvmthin-vm-destroy-journal/v1', authority => 'NONE',
        durable => JSON::PP::true, records => \@rows, stage => 'FINALIZING', last_sha256 => $previous };
}

sub absent_fixture {
    my ($api, $profile) = @_;
    my $f = fixture($api, $profile);
    my $runtime = verifier($f)->verify(101);
    my $context = { schema => 'sharedlvmthin-vm-destroy-executor-core/v1', receipt => {
        schema => 1, txid => 'c' x 32, vmid => 101, config => { digest => 'a' x 40 },
        plan => { authority => 'NONE' }, runtime => $runtime } };
    $f->{state}->{owner} = undef;
    $f->{journal} = recovery_journal($context);
    $f->{finalization} = { vmid => '101', acl => 'ABSENT', firewall => 'ABSENT', config => 'ABSENT' };
    return ($f, $context);
}

sub absent_observation {
    my ($f, $context) = @_;
    return verifier($f)->verify_finalizing_absent(101, txid => 'c' x 32, expected_context => $context);
}

for my $api (14, 15) {
    for my $profile ('dual', 'thick-only') {
        subtest "API$api $profile absent finalization is observation only" => sub {
            my ($f, $context) = absent_fixture($api, $profile);
            my $result = absent_observation($f, $context);
            is($result->{status}, 'OBSERVED', 'not qualified for mutation');
            is($result->{purpose}, 'FINALIZATION_OBSERVATION_ONLY', 'single narrow purpose');
            is($result->{authority}, 'NONE', 'no authority');
            is($result->{recovery}->{journal_head_sha256}, $f->{journal}->{last_sha256}, 'exact durable head pinned');
            is($f->{journal_reads}, 2, 'journal brackets complete observation');
            is($f->{finalization_reads}, 2, 'all-ABSENT state rechecked');
            refused(sub { $CLASS->assert_same($result, $result) }, qr/invalid runtime comparison/,
                'observation cannot satisfy mutation-runtime comparison');
            refused(sub { verifier($f)->verify(101) }, qr/not exactly locally owned/,
                'normal execute qualification still refuses absent VM');
        };
    }
}

for my $case (
    ['local VMID reuse', sub { $_[0]->{state}->{owner} = { node => 'node-1', type => 'qemu' } }, qr/VMID was reused/],
    ['remote VMID reuse', sub { $_[0]->{state}->{owner} = { node => 'node-2', type => 'qemu' } }, qr/VMID was reused/],
    ['remaining ACL', sub { $_[0]->{finalization}->{acl} = 'PRESENT' }, qr/PRESENT\/UNKNOWN/],
    ['remaining config', sub { $_[0]->{finalization}->{config} = 'PRESENT' }, qr/PRESENT\/UNKNOWN/],
    ['unknown firewall', sub { $_[0]->{finalization}->{firewall} = 'UNKNOWN' }, qr/PRESENT\/UNKNOWN/],
    ['unverified durability', sub { $_[0]->{journal}->{durable} = JSON::PP::false }, qr/not durable FINALIZING/],
    ['terminal UNKNOWN', sub { $_[0]->{journal}->{stage} = 'PARTIAL_OR_UNKNOWN' }, qr/not durable FINALIZING/],
    ['terminal COMPLETE', sub { $_[0]->{journal}->{stage} = 'COMPLETE' }, qr/not durable FINALIZING/],
    ['stale head', sub { $_[0]->{journal}->{last_sha256} = '0' x 64 }, qr/head mismatch/],
    ['broken chain', sub { $_[0]->{journal}->{records}->[2]->{previous_sha256} = '0' x 64 }, qr/broken chain/],
    ['swapped request', sub { $_[0]->{journal}->{records}->[0]->{request_id} = 'd' x 32 }, qr/record identity/],
    ['changed context', sub { $_[0]->{journal}->{records}->[0]->{context}->{receipt}->{vmid} = 999 }, qr/context mismatch/],
    ['node drift', sub { $_[0]->{state}->{node} = 'node-2' }, qr/differs from immutable receipt/],
    ['source drift', sub { $_[0]->{files}->{$CHECKER} .= '# newer' }, qr/differs from immutable receipt/],
    ['boot drift', sub {
        my ($f) = @_;
        $f->{release}->{boot_id} = 'bbbbbbbb-bbbb-cccc-dddd-eeeeeeeeeeee';
        $f->{files}->{'/proc/sys/kernel/random/boot_id'} = $f->{release}->{boot_id} . "\n";
        update_release($f);
    }, qr/differs from immutable receipt/],
    ['tuple drift', sub {
        my ($f) = @_;
        $f->{manifest}->{tuples}->[0]->{id} = 'new-qualified-tuple';
        $f->{release}->{runtime_tuple_id} = 'new-qualified-tuple';
        $f->{release}->{runtime_tuple_sha256} = policy_hash($f->{manifest}->{tuples}->[0]);
        $f->{release}->{runtime_manifest_sha256} = policy_hash($f->{manifest});
        update_manifest($f); update_release($f);
    }, qr/differs from immutable receipt/],
    ['journal helper timeout', sub { $_[0]->{journal_error} = 1 }, qr/journal observation failed/],
    ['ambiguous finalization helper', sub { $_[0]->{finalization_error} = 1 }, qr/finalization observation failed/],
) {
    subtest "absent finalization refuses $case->[0]" => sub {
        my ($f, $context) = absent_fixture();
        $case->[1]->($f);
        refused(sub { absent_observation($f, $context) }, $case->[2], $case->[0]);
    };
}

subtest 'journal advance and VMID reuse during observation are not stale successes' => sub {
    my ($f, $context) = absent_fixture();
    $f->{journal_mutate} = sub {
        my ($f, $count) = @_;
        return if $count != 2;
        my $next = copy($f->{journal}->{records}->[-1]);
        $next->{sequence}++;
        $next->{previous_sha256} = $f->{journal}->{last_sha256};
        $next->{evidence} = { evidence => { later => 1 } };
        push @{$f->{journal}->{records}}, $next;
        $f->{journal}->{last_sha256} = sha256_hex(JSON::PP->new->canonical->ascii->encode($next));
    };
    refused(sub { absent_observation($f, $context) }, qr/journal changed/, 'legitimate concurrent checkpoint');
    ($f, $context) = absent_fixture();
    $f->{finalization_mutate} = sub {
        my ($f, $count) = @_;
        $f->{finalization}->{config} = 'PRESENT' if $count == 2;
    };
    refused(sub { absent_observation($f, $context) }, qr/PRESENT\/UNKNOWN/, 'config appeared during runtime reads');
};

subtest 'caller blobs cannot bypass dependency readers' => sub {
    my ($f, $context) = absent_fixture();
    refused(sub { verifier($f)->verify_finalizing_absent(101, txid => 'c' x 32,
        expected_context => $context, journal_observation => $f->{journal}) }, qr/identity\/context only/,
        'caller supplied journal');
    my $bad_context = copy($context);
    $bad_context->{receipt}->{runtime}->{status} = 'OBSERVED';
    refused(sub { absent_observation($f, $bad_context) }, qr/original runtime identity/,
        'observation recycled as initial qualification');
};

subtest 'fresh-process sources are trusted-reader bracketed and never stripped from recovery' => sub {
    my ($f, $context) = absent_fixture();
    my $fresh = { schema => 'sharedlvmthin-vm-destroy-fresh-process/v1',
        source_sha256 => 'a' x 64, launcher_sha256 => 'b' x 64,
        dispatcher_sha256 => 'd' x 64, module_count => 12 };
    $context->{receipt}->{runtime}->{fresh_process} = copy($fresh);
    $f->{journal} = recovery_journal($context);
    refused(sub { absent_observation($f, $context) }, qr/fresh-process recovery reader\/receipt missing/,
        'persisted source identity without trusted reader');
    my $reads = 0;
    $f->{fresh_process_reader} = sub { $reads++; return copy($fresh) };
    my $result = absent_observation($f, $context);
    is($reads, 2, 'loaded source identity read before and after entire observation');
    is_deeply($result->{fresh_process}, $fresh, 'source identity retained');
    is($result->{status}, 'OBSERVED', 'source reader never promotes observation');
    refused(sub { $CLASS->assert_same($result, $result) }, qr/invalid runtime comparison/,
        'source-pinned observation still cannot authorize execute');
    for my $field (qw(source_sha256 launcher_sha256 dispatcher_sha256)) {
        $f->{fresh_process_reader} = sub { my $changed = copy($fresh); $changed->{$field} = '0' x 64; return $changed };
        refused(sub { absent_observation($f, $context) }, qr/fresh-process source identity differs/,
            "$field changed before observation");
    }
    $reads = 0;
    $f->{fresh_process_reader} = sub {
        my $value = copy($fresh); $value->{launcher_sha256} = '0' x 64 if ++$reads == 2; return $value;
    };
    refused(sub { absent_observation($f, $context) }, qr/source changed during absent observation/,
        'loaded source changed during observation');
    delete($context->{receipt}->{runtime}->{fresh_process});
    $f->{journal} = recovery_journal($context);
    refused(sub { absent_observation($f, $context) }, qr/fresh-process recovery reader\/receipt missing/,
        'trusted reader cannot invent missing original source identity');
};

subtest 'root check happens before all probes' => sub {
    my $pid = fork();
    if (!defined($pid)) { plan skip_all => 'fork unavailable'; return }
    if (!$pid) {
        $> = 65534;
        exit 4 if $> == 0;
        my $v = $CLASS->new(reader => sub { die 'probe executed' });
        eval { $v->verify(101) };
        exit($@ =~ /root required/ ? 0 : 3);
    }
    waitpid($pid,0);
    is($?,0,'nonroot child refused without calling dependency');
};

done_testing();
