package PVE::SharedLvmThinVMDestroyRuntime;

use strict;
use warnings;
use Digest::SHA qw(sha256_hex);
use Fcntl qw(O_RDONLY O_NOFOLLOW :mode);
use JSON::PP ();
use Time::HiRes qw(clock_gettime CLOCK_MONOTONIC);

# Source-only observer, not an admission token or dispatcher. The caller must
# hold the native VM lock and compare two verify() results immediately before
# dispatch. No timestamp/PID/counter is included in the comparable evidence.
# DI callbacks are trusted test boundaries, never CLI/user-supplied options.
my $JSON = JSON::PP->new->canonical->allow_nonref;
my $BASE = '/usr/share/pve-sharedlvmthin';
my $CHECKER = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmdestroy-contract-check';
my @PACKAGES = qw(pve-manager libpve-storage-perl qemu-server pve-qemu-kvm libpve-common-perl);
my @OPERATIONS = qw(read-only-inventory package-lifecycle activation-deactivation allocation-removal
    snapshot-vmstate resize move-import-restore migration thin-ownership-autogrow thick-lazy-transition);
my %CONTRACT = (
    14 => '06e848db8abaf5e765845577b90d38f97d8d3e0d29e24212504d77ee60c0c1ab',
    15 => 'd52049347b091b6091671887b8c5087e9001445c793c1a719823fbbb047a220e',
);
my @FILES = ("$BASE/package-flavor", "$BASE/runtime-build-id", "$BASE/package-artifact-sha256", "$BASE/pve-qualified-tuples.json",
    '/var/lib/pve-sharedlvmthin/update-guard/runtime-release.json', '/etc/pve/corosync.conf',
    '/proc/sys/kernel/random/boot_id', $CHECKER, '/usr/share/perl5/PVE/Storage.pm',
    '/usr/share/perl5/PVE/QemuServer.pm', '/usr/share/perl5/PVE/API2/Qemu.pm');
my %FILES = map { $_ => 1 } @FILES;
sub _need ($$) { die "VM_DESTROY_RUNTIME_REFUSED: $_[1]\n" if !$_[0] }
sub _canonical { $JSON->encode($_[0]) }
sub _policy_hash { sha256_hex(JSON::PP->new->canonical->ascii->encode($_[0]) . "\n") }
sub _clock { clock_gettime(CLOCK_MONOTONIC) }
sub _name { defined($_[0]) && !ref($_[0]) && scalar($_[0] =~ /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\z/) }

# JSON::PP otherwise accepts duplicate object keys. Validate the token tree
# before decoding, including escaped-key aliases; bound depth and token count.
sub _strict_json {
    my ($raw) = @_;
    my @tokens;
    pos($raw) = 0;
    while (pos($raw) < length($raw)) {
        last if $raw =~ /\G\s+\z/gc;
        _need(scalar($raw =~ /\G\s*("(?:[^"\\\x00-\x1f]|\\(?:["\\\/bfnrt]|u[0-9a-fA-F]{4}))*"|[{}\[\]:,]|-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|true|false|null)/gc),
            'invalid JSON token');
        push @tokens, $1;
        _need(@tokens <= 100000, 'JSON token budget');
    }
    my ($i, $walk) = (0);
    $walk = sub {
        my ($depth) = @_;
        _need($depth <= 24 && $i < @tokens, 'JSON depth/truncation');
        my $t = $tokens[$i++];
        if ($t eq '{' || $t eq '[') {
            my $end = $t eq '{' ? '}' : ']';
            my %seen;
            if (($tokens[$i] // '') eq $end) { $i++; return }
            while (1) {
                if ($t eq '{') {
                    my $key = $tokens[$i++];
                    _need(defined($key) && scalar($key =~ /^"/), 'JSON object key');
                    $key = $JSON->decode($key);
                    _need(!$seen{$key}++, 'duplicate JSON key');
                    _need(($tokens[$i++] // '') eq ':', 'JSON colon');
                }
                $walk->($depth + 1);
                my $separator = $tokens[$i++];
                last if defined($separator) && $separator eq $end;
                _need(defined($separator) && $separator eq ',', 'JSON separator');
            }
        } else { _need(scalar($t !~ /^[}\]:,]$/), 'JSON value') }
    };
    $walk->(0);
    _need($i == @tokens, 'JSON trailing tokens');
    return $JSON->decode($raw);
}

sub _read_file {
    my ($path) = @_;
    _need($FILES{$path}, 'unexpected evidence path');
    my @parts = split('/', $path); pop @parts; shift @parts;
    my $parent = '';
    for my $part (@parts) {
        $parent .= "/$part";
        my @st = lstat($parent);
        _need(@st && S_ISDIR($st[2]) && $st[4] == 0 && !($st[2] & 0022), 'unsafe evidence parent');
    }
    my @before = lstat($path);
    _need(@before && S_ISREG($before[2]) && $before[4] == 0 && !($before[2] & 0022)
        && $before[3] == 1 && $before[7] <= 8 * 1024 * 1024, 'unsafe evidence file');
    sysopen(my $fh, $path, O_RDONLY | O_NOFOLLOW) or die "cannot open runtime evidence: $!\n";
    my @opened = stat($fh);
    _need($opened[0] == $before[0] && $opened[1] == $before[1], 'evidence inode changed');
    my $raw = '';
    while (1) {
        my $count = sysread($fh, my $chunk, 65536);
        _need(defined($count), 'evidence read failed');
        last if !$count;
        $raw .= $chunk;
        _need(length($raw) <= 8 * 1024 * 1024, 'evidence size budget');
    }
    my @after = stat($fh);
    close($fh) or die "cannot close runtime evidence: $!\n";
    my @path_after = lstat($path);
    _need(@path_after && join(':', @before[0,1,2,3,4,7,9,10]) eq join(':', @after[0,1,2,3,4,7,9,10])
        && join(':', @before[0,1,2,3,4,7,9,10]) eq join(':', @path_after[0,1,2,3,4,7,9,10]), 'evidence changed while read');
    _need(length($raw) && index($raw, "\0") == -1, 'empty/binary evidence');
    return $raw;
}

sub _run {
    my ($argv, $timeout) = @_;
    require PVE::Tools;
    my ($out, $err) = ('', '');
    local %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
    PVE::Tools::run_command($argv, timeout => $timeout,
        outfunc => sub { $out .= $_[0] . "\n"; _need(length($out) <= 1024 * 1024, 'command output budget') },
        errfunc => sub { $err .= $_[0]; _need(length($err) <= 8192, 'command diagnostic budget') });
    _need($err eq '', 'command returned diagnostics');
    return $out;
}

sub _pve_state {
    my ($vmid) = @_;
    require PVE::Cluster; require PVE::INotify; require PVE::Storage;
    require PVE::Storage::Custom::SharedLvmThinPlugin; require POSIX;
    PVE::Cluster::cfs_update(1);
    PVE::Cluster::check_cfs_quorum();
    my $plugin = 'PVE::Storage::Custom::SharedLvmThinPlugin';
    $plugin->_assert_package_operations_released('guarded vm destroy');
    my $list = PVE::Cluster::get_vmlist();
    _need(ref($list) eq 'HASH' && ref($list->{ids}) eq 'HASH', 'cluster VM list unavailable');
    return { node => PVE::INotify::nodename(), owner => $list->{ids}->{$vmid},
        nodes => PVE::Cluster::get_nodelist(), members => PVE::Cluster::get_members(),
        apiver => PVE::Storage::APIVER(), apiage => PVE::Storage::APIAGE(),
        kernel => (POSIX::uname())[2], loaded_build_id => $plugin->_loaded_runtime_build_id(),
        package_operations_released => 1, quorate => 1 };
}

sub new {
    my ($class, %args) = @_;
    my %allowed = map { $_ => 1 } qw(reader runner pve_reader clock journal_reader finalization_reader fresh_process_reader);
    _need(!grep({ !$allowed{$_} || ref($args{$_}) ne 'CODE' } keys %args), 'invalid dependency');
    return bless { reader => \&_read_file, runner => \&_run, pve_reader => \&_pve_state,
        journal_reader => \&_read_finalizing_journal,
        finalization_reader => sub {
            require PVE::SharedLvmThinVMDestroyFinalizationObserver;
            return PVE::SharedLvmThinVMDestroyFinalizationObserver->new()->observe($_[0]);
        },
        clock => \&_clock, %args }, $class;
}

sub _read_finalizing_journal {
    my ($txid) = @_;
    _need(defined($txid) && !ref($txid) && scalar($txid =~ /\A[0-9a-f]{32}\z/), 'invalid recovery txid');
    require PVE::Tools;
    my ($raw, $errors) = ('', '');
    local %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
    PVE::Tools::run_command(['/usr/bin/python3', '-I', '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy',
        '_observe-durable', '--request-id', $txid], input => '', timeout => 30,
        outfunc => sub { $raw .= $_[0] . "\n"; _need(length($raw) <= 32 * 1024 * 1024 + 4096, 'journal observation budget') },
        errfunc => sub { $errors .= $_[0]; _need(length($errors) <= 8192, 'journal diagnostic budget') });
    _need($errors eq '', 'journal observation diagnostics');
    my $value = _strict_json($raw);
    _need($raw eq JSON::PP->new->canonical->ascii->encode($value) . "\n", 'noncanonical journal observation');
    return $value;
}

sub _finalizing_head {
    my ($value, $txid, $context) = @_;
    my $json = JSON::PP->new->canonical->ascii;
    _need(ref($value) eq 'HASH' && join(',', sort keys %$value) eq
        'authority,durable,last_sha256,records,schema,stage', 'recovery journal fields');
    _need(($value->{schema} // '') eq 'sharedlvmthin-vm-destroy-journal/v1'
        && ($value->{authority} // '') eq 'NONE' && JSON::PP::is_bool($value->{durable}) && $value->{durable}
        && ($value->{stage} // '') eq 'FINALIZING', 'recovery journal not durable FINALIZING');
    _need(ref($value->{records}) eq 'ARRAY' && @{$value->{records}} >= 4
        && @{$value->{records}} <= 32, 'recovery journal record count');
    my ($previous, $stage, $sequence) = (undef, undef, 0);
    my %next = (PREPARED => 'DISPATCHED', DISPATCHED => 'STORAGE_ABSENT', STORAGE_ABSENT => 'FINALIZING',
        FINALIZING => 'FINALIZING');
    for my $row (@{$value->{records}}) {
        _need(ref($row) eq 'HASH' && join(',', sort keys %$row) eq
            'authority,context,context_sha256,evidence,previous_sha256,request_id,schema,sequence,stage',
            'recovery journal record fields');
        _need(($row->{schema} // '') eq 'sharedlvmthin-vm-destroy-journal/v1'
            && ($row->{authority} // '') eq 'NONE' && ($row->{request_id} // '') eq $txid
            && _canonical($row->{sequence}) eq '' . ++$sequence, 'recovery journal record identity');
        my $expected_stage = defined($stage) ? $next{$stage} : 'PREPARED';
        _need(defined($expected_stage) && ($row->{stage} // '') eq $expected_stage, 'recovery journal stage/terminal mismatch');
        _need($json->encode($row->{previous_sha256}) eq $json->encode($previous), 'recovery journal broken chain');
        _need($json->encode($row->{context}) eq $json->encode($context)
            && ($row->{context_sha256} // '') eq sha256_hex($json->encode($context)), 'recovery journal context mismatch');
        _need(ref($row->{evidence}) eq 'HASH' && length($json->encode($row)) + 1 <= 1024 * 1024,
            'recovery journal evidence/size');
        $previous = sha256_hex($json->encode($row));
        $stage = $row->{stage};
    }
    _need($stage eq 'FINALIZING' && ($value->{last_sha256} // '') eq $previous, 'recovery journal head mismatch');
    return $previous;
}

sub _all_finalization_absent {
    my ($state, $vmid) = @_;
    _need(ref($state) eq 'HASH' && join(',', sort keys %$state) eq 'acl,config,firewall,vmid'
        && ($state->{vmid} // '') eq "$vmid", 'finalization observation identity/shape');
    _need(!grep({ ($state->{$_} // '') ne 'ABSENT' } qw(acl firewall config)),
        'finalization is PRESENT/UNKNOWN; absent-only observation refused');
}

sub verify_finalizing_absent {
    my ($self, $vmid, %args) = @_;
    _need($> == 0 && $< == 0, 'root required');
    _need(defined($vmid) && !ref($vmid) && scalar($vmid =~ /\A[1-9][0-9]{0,8}\z/), 'invalid VM identity');
    _need(join(',', sort keys %args) eq 'expected_context,txid', 'recovery accepts identity/context only');
    my ($txid, $context) = @args{qw(txid expected_context)};
    _need(defined($txid) && !ref($txid) && scalar($txid =~ /\A[0-9a-f]{32}\z/), 'invalid recovery txid');
    _need(ref($context) eq 'HASH' && join(',', sort keys %$context) eq 'receipt,schema'
        && ($context->{schema} // '') eq 'sharedlvmthin-vm-destroy-executor-core/v1', 'recovery immutable context');
    my $core = $context->{receipt};
    _need(ref($core) eq 'HASH' && join(',', sort keys %$core) eq 'config,plan,runtime,schema,txid,vmid'
        && _canonical($core->{schema}) eq '1' && ($core->{txid} // '') eq $txid
        && _canonical($core->{vmid}) eq "$vmid"
        && ref($core->{config}) eq 'HASH' && ref($core->{plan}) eq 'HASH', 'recovery core identity');
    _need(ref($core->{runtime}) eq 'HASH'
        && ($core->{runtime}->{schema} // '') eq 'sharedlvmthin-vm-destroy-runtime/v1'
        && ($core->{runtime}->{status} // '') eq 'QUALIFIED'
        && ($core->{runtime}->{authority} // '') eq 'NONE', 'recovery original runtime identity');
    $context = $JSON->decode(_canonical($context));
    my $fresh;
    if (exists($context->{receipt}->{runtime}->{fresh_process}) || exists($self->{fresh_process_reader})) {
        _need(ref($self->{fresh_process_reader}) eq 'CODE'
            && ref($context->{receipt}->{runtime}->{fresh_process}) eq 'HASH', 'fresh-process recovery reader/receipt missing');
        $fresh = $self->{fresh_process_reader}->();
        _need(ref($fresh) eq 'HASH'
            && _canonical($fresh) eq _canonical($context->{receipt}->{runtime}->{fresh_process}),
            'fresh-process source identity differs from immutable receipt');
        $fresh = $JSON->decode(_canonical($fresh));
    }
    my $journal = $self->{journal_reader}->($txid);
    my $head = _finalizing_head($journal, $txid, $context);
    my $state = $self->{finalization_reader}->($vmid);
    _all_finalization_absent($state, $vmid);
    my $observed = $self->_observe_runtime($vmid, 1);
    $observed->{fresh_process} = $fresh if defined($fresh);
    my $comparable = $JSON->decode(_canonical($observed));
    delete $comparable->{purpose};
    $comparable->{status} = 'QUALIFIED'; # comparison only, never returned/admitted
    _need(_canonical($comparable) eq _canonical($context->{receipt}->{runtime}),
        'absent recovery node/boot/package/tuple/contract differs from immutable receipt');
    my $again = $self->{finalization_reader}->($vmid);
    _all_finalization_absent($again, $vmid);
    _need(_canonical($again) eq _canonical($state), 'finalization observation changed');
    my $journal_again = $self->{journal_reader}->($txid);
    _need(_finalizing_head($journal_again, $txid, $context) eq $head
        && _canonical($journal_again) eq _canonical($journal), 'recovery journal changed during observation');
    if (defined($fresh)) {
        my $fresh_again = $self->{fresh_process_reader}->();
        _need(ref($fresh_again) eq 'HASH' && _canonical($fresh_again) eq _canonical($fresh),
            'fresh-process source changed during absent observation');
    }
    $observed->{recovery} = { txid => $txid, context_sha256 =>
        sha256_hex(JSON::PP->new->canonical->ascii->encode($context)), journal_head_sha256 => $head };
    return $observed;
}

sub _contract_output {
    my ($api) = @_;
    return "QMDestroy_CONTRACT_VERSION=1\nNATIVE_CONFIG_CAS=ABSENT\n"
        . "VDISK_FREE_FAILURE_POLICY=WARN_AND_CONTINUE\nFINAL_CONFIG_REMOVAL=AFTER_DESTROY_VM\n"
        . "FLEECING_CLEANUP_CALL=PINNED\nIPAM_CLEANUP_CALL=PINNED\n"
        . "CONTRACT_SHA256=$CONTRACT{$api}\nCONTRACT_VARIANT=API$api\n"
        . "MUTATION_ADAPTER=QUALIFIED\nQMDestroy_CONTRACT=QUALIFIED\n";
}

sub verify {
    my ($self, $vmid) = @_;
    return $self->_observe_runtime($vmid, 0);
}

sub _observe_runtime {
    my ($self, $vmid, $finalizing_absent) = @_;
    _need($> == 0 && $< == 0, 'root required');
    _need(defined($vmid) && !ref($vmid) && scalar($vmid =~ /^[1-9][0-9]{0,8}\z/), 'invalid VM identity');
    my $deadline = $self->{clock}->() + 45;
    my $budget = sub { my $left = $deadline - $self->{clock}->(); _need($left > 0, 'observation deadline'); return $left < 10 ? $left : 10 };
    my (%raw, %hashes);
    for my $path (@FILES) {
        $budget->(); $raw{$path} = $self->{reader}->($path);
        _need(defined($raw{$path}) && !ref($raw{$path}) && length($raw{$path}) > 0
            && length($raw{$path}) <= 8 * 1024 * 1024 && index($raw{$path}, "\0") == -1, 'invalid evidence bytes');
        $hashes{$path} = sha256_hex($raw{$path});
    }
    my $state = $self->{pve_reader}->($vmid); $budget->();
    _need(ref($state) eq 'HASH' && _name($state->{node}), 'local node unknown');
    _need(($state->{quorate} // '') eq '1' && ($state->{package_operations_released} // '') eq '1', 'quorum/package admission not released');
    if ($finalizing_absent) {
        _need(!defined($state->{owner}), 'VMID was reused or still has an owner');
    } else {
        _need(ref($state->{owner}) eq 'HASH' && ($state->{owner}->{type} // '') eq 'qemu'
            && ($state->{owner}->{node} // '') eq $state->{node}, 'VM is not exactly locally owned');
    }
    my ($api, $age) = @{$state}{qw(apiver apiage)};
    _need(defined($api) && !ref($api) && exists($CONTRACT{$api}) && defined($age) && !ref($age)
        && scalar($age =~ /^\d+\z/) && $age == $api - 9, 'unsupported APIVER/APIAGE');
    my $boot = $raw{'/proc/sys/kernel/random/boot_id'};
    _need(scalar($boot =~ /^([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\n?\z/), 'invalid boot identity'); $boot = $1;
    my $flavor = $raw{"$BASE/package-flavor"};
    _need(scalar($flavor =~ /^(dual|thick-only)\n?\z/), 'invalid package profile marker'); $flavor = $1;
    my $build = $raw{"$BASE/runtime-build-id"};
    _need(scalar($build =~ /^([a-f0-9]{64})\n?\z/), 'invalid installed build identity'); $build = $1;
    _need(($state->{loaded_build_id} // '') eq $build, 'loaded/installed build mismatch');
    my $release = _strict_json($raw{'/var/lib/pve-sharedlvmthin/update-guard/runtime-release.json'});
    _need(ref($release) eq 'HASH' && ($release->{schema} // '') eq '1' && ($release->{qualified} // '') eq 'QUALIFIED'
        && ($release->{runtime_build_id} // '') eq $build && ($release->{boot_id} // '') eq $boot
        && ($release->{kernel_release} // '') eq ($state->{kernel} // '')
        && scalar(($release->{plan_digest} // '') =~ /^[a-f0-9]{64}\z/), 'runtime release identity mismatch');

    my $dpkg = $self->{runner}->(['/usr/bin/dpkg-query', '-W',
        '-f=${Package}\t${Version}\t${Architecture}\t${db:Status-Status}\t${db:Status-Eflag}\n',
        @PACKAGES, 'pve-sharedlvmthin*'], $budget->());
    _need(defined($dpkg) && !ref($dpkg) && length($dpkg) <= 65536, 'package evidence budget');
    my %packages;
    my %wanted = map { $_ => 1 } (@PACKAGES, 'pve-sharedlvmthin', 'pve-sharedlvmthin-thick');
    for my $line (split(/\n/, $dpkg)) {
        my @f = split(/\t/, $line, -1);
        _need(@f == 5 && $wanted{$f[0]} && !exists($packages{$f[0]})
            && scalar($f[1] =~ /^[A-Za-z0-9.+:~_-]+\z/) && scalar($f[2] =~ /^(?:all|amd64)\z/)
            && $f[4] eq 'ok', 'ambiguous package identity/state');
        $packages{$f[0]} = { version => $f[1], arch => $f[2], state => $f[3] };
    }
    my $package = $flavor eq 'dual' ? 'pve-sharedlvmthin' : 'pve-sharedlvmthin-thick';
    my $other = $flavor eq 'dual' ? 'pve-sharedlvmthin-thick' : 'pve-sharedlvmthin';
    _need(!exists($packages{$other}) || scalar($packages{$other}->{state} =~ /^(?:config-files|not-installed)\z/), 'conflicting profile package');
    for my $name (@PACKAGES, $package) { _need(exists($packages{$name}) && $packages{$name}->{state} eq 'installed', 'required package not installed') }
    _need($packages{$package}->{arch} eq 'all', 'plugin architecture mismatch');
    _need(($release->{plugin_package} // '') eq $package
        && ($release->{plugin_version} // '') eq $packages{$package}->{version}, 'release package/version mismatch');
    my $artifact = $raw{"$BASE/package-artifact-sha256"};
    _need(scalar($artifact =~ /^([a-f0-9]{64})\n?\z/), 'invalid installed artifact identity'); $artifact = $1;
    _need(($release->{artifact_sha256} // '') eq $artifact, 'release artifact mismatch');
    for my $argv (['/usr/bin/dpkg', '--audit'], ['/usr/bin/dpkg', '--verify', $package]) {
        my $output = $self->{runner}->($argv, $budget->());
        _need(defined($output) && !ref($output) && $output eq '', 'dpkg audit/payload verification not clean');
    }

    my $manifest = _strict_json($raw{"$BASE/pve-qualified-tuples.json"});
    _need(ref($manifest) eq 'HASH' && ($manifest->{schema} // '') eq '1'
        && ($manifest->{catalogue} // '') eq 'pve-qualified-tuples-v1'
        && ref($manifest->{packages}) eq 'ARRAY'
        && _canonical([sort @{$manifest->{packages}}]) eq _canonical([sort @PACKAGES])
        && ref($manifest->{tuples}) eq 'ARRAY' && @{$manifest->{tuples}} <= 256, 'unsupported tuple manifest');
    my @matches;
    my %ids;
    for my $item (@{$manifest->{tuples}}) {
        _need(ref($item) eq 'HASH' && _name($item->{id}) && !$ids{$item->{id}}++
            && ref($item->{packages}) eq 'HASH' && _canonical([sort keys %{$item->{packages}}]) eq _canonical([sort @PACKAGES])
            && ref($item->{profiles}) eq 'ARRAY' && ref($item->{plugin_versions}) eq 'ARRAY', 'incomplete/ambiguous tuple');
        next if ($item->{api} // '') ne $api || ($item->{running_kernel} // '') ne ($state->{kernel} // '')
            || !scalar(grep({ $_ eq $flavor } @{$item->{profiles}}))
            || !scalar(grep({ $_ eq $packages{$package}->{version} } @{$item->{plugin_versions}}))
            || scalar(grep({ ($item->{packages}->{$_} // '') ne $packages{$_}->{version} } @PACKAGES));
        push @matches, $item;
    }
    _need(@matches == 1, 'tuple unlisted or ambiguous');
    my $tuple = $matches[0];
    _need(($tuple->{status} // '') eq 'EXACT_LAB_TESTED' && ref($tuple->{scopes}) eq 'ARRAY'
        && scalar(grep({ $_ eq 'san-dataplane' } @{$tuple->{scopes}})), 'tuple not SAN qualified');
    _need(scalar(grep({ $_ eq 'api-hooks' } @{$tuple->{scopes}})), 'tuple lacks API hooks qualification');
    _need(!exists($tuple->{required_tests}) || (ref($tuple->{required_tests}) eq 'ARRAY' && !@{$tuple->{required_tests}}), 'tuple has outstanding tests');
    _need(!grep({ $_ eq 'san-dataplane' || $_ eq 'allocation-removal' } @{$tuple->{excluded_scopes} // []}), 'tuple excludes destroy scope');
    if (exists($tuple->{operation_qualifications})) {
        _need(ref($tuple->{operation_qualifications}) eq 'HASH'
            && ref($manifest->{operation_groups}) eq 'ARRAY'
            && _canonical($manifest->{operation_groups}) eq _canonical(\@OPERATIONS)
            && _canonical([sort keys %{$tuple->{operation_qualifications}}]) eq _canonical([sort @OPERATIONS])
            && ($manifest->{operation_qualification_mode} // '') eq 'ADVISORY_ONLY'
            && ($manifest->{operation_authorization} // '') eq 'NONE'
            && ref($manifest->{operation_evidence_ids}) eq 'ARRAY', 'allocation-removal matrix schema unqualified');
        my $op = $tuple->{operation_qualifications}->{'allocation-removal'};
        _need(ref($op) eq 'HASH' && ($op->{status} // '') eq 'QUALIFIED'
            && ref($op->{evidence}) eq 'ARRAY' && @{$op->{evidence}}
            && (!exists($op->{required_tests}) || (ref($op->{required_tests}) eq 'ARRAY' && !@{$op->{required_tests}})),
            'allocation-removal remains unqualified');
        my %registry = map { $_ => 1 } @{$manifest->{operation_evidence_ids}};
        _need(!grep({ ref($_) || !$registry{$_} } @{$op->{evidence}}), 'allocation-removal evidence unregistered');
    }
    _need(($release->{runtime_tuple_id} // '') eq $tuple->{id}
        && ($release->{runtime_tuple_status} // '') eq $tuple->{status}
        && ($release->{runtime_tuple_sha256} // '') eq _policy_hash($tuple)
        && ($release->{runtime_manifest_sha256} // '') eq _policy_hash($manifest), 'release tuple/manifest binding mismatch');

    # Existing project policy: docs/cluster-quorum.md permits healthy 2/2
    # without qdevice. This observer is intentionally stricter for destroy:
    # require every configured peer online, unit votes, no quorum overrides.
    my $config = $raw{'/etc/pve/corosync.conf'};
    _need(scalar($config !~ /^\s*(?:two_node|expected_votes|quorum_votes|last_man_standing|auto_tie_breaker)\s*:/m),
        'unsupported quorum override');
    my $configured = () = $config =~ /^\s*node\s*\{/mg;
    my @blocks = $config =~ /^\s*node\s*\{([^{}]*)\}/mg;
    my %configured_names;
    _need(@blocks == $configured, 'ambiguous configured node blocks');
    for my $block (@blocks) {
        my @names = $block =~ /^\s*name:\s*([A-Za-z0-9][A-Za-z0-9_.-]*)\s*$/mg;
        _need(@names == 1 && !$configured_names{$names[0]}++, 'configured node name missing/duplicate');
    }
    _need($configured >= 2 && $configured <= 16 && ref($state->{nodes}) eq 'ARRAY'
        && @{$state->{nodes}} == $configured && ref($state->{members}) eq 'HASH', 'cluster topology unknown');
    my %nodes;
    for my $node (@{$state->{nodes}}) {
        _need(_name($node) && $configured_names{$node} && !$nodes{$node}++ && ref($state->{members}->{$node}) eq 'HASH'
            && ($state->{members}->{$node}->{online} // '') eq '1', 'cluster member offline/ambiguous');
    }
    _need($nodes{$state->{node}} && !grep({ !$nodes{$_} } keys %{$state->{members}}), 'cluster membership mismatch');
    my $votes = $self->{runner}->(['/usr/bin/pvecm', 'status'], $budget->());
    _need(defined($votes) && !ref($votes) && length($votes) <= 65536, 'vote evidence unavailable');
    my %votes;
    for my $field ('Nodes', 'Expected votes', 'Total votes', 'Quorate') {
        my @found = $votes =~ /^\Q$field\E:\s*([^\r\n]+)$/mg;
        _need(@found == 1, 'vote evidence missing/duplicate');
        $found[0] =~ s/\s+$//;
        $votes{$field} = $found[0];
    }
    my $qdevice = $config =~ /^\s*device\s*\{/m ? 1 : 0;
    _need($votes{Quorate} eq 'Yes' && $votes{Nodes} eq "$configured"
        && $votes{'Expected votes'} eq '' . ($configured + $qdevice)
        && $votes{'Total votes'} eq $votes{'Expected votes'}, 'quorum/vote topology not qualified');
    my $output = $self->{runner}->([$CHECKER], $budget->());
    _need(defined($output) && !ref($output) && $output eq _contract_output($api), 'destroy contract output/digest/variant mismatch');

    # Bracket all source/marker/manifest reads and cluster-owner observations.
    # Stable bytes are evidence, never a lock or old-writer fence.
    for my $path (@FILES) { $budget->(); _need($self->{reader}->($path) eq $raw{$path}, 'runtime evidence changed during observation') }
    my $again = $self->{pve_reader}->($vmid); $budget->();
    _need(_canonical($again) eq _canonical($state), 'PVE runtime changed during observation');
    my $evidence = { schema => 'sharedlvmthin-vm-destroy-runtime/v1',
        status => $finalizing_absent ? 'OBSERVED' : 'QUALIFIED', authority => 'NONE',
        ($finalizing_absent ? (purpose => 'FINALIZATION_OBSERVATION_ONLY') : ()),
        vmid => "$vmid", node => $state->{node}, boot_id => $boot, kernel => $state->{kernel},
        apiver => 0 + $api, apiage => 0 + $age, profile => $flavor, package => $package,
        plugin_version => $packages{$package}->{version}, packages => \%packages, runtime_build_id => $build,
        tuple_id => $tuple->{id}, tuple_sha256 => sha256_hex(_canonical($tuple)),
        contract_variant => "API$api", contract_sha256 => $CONTRACT{$api},
        contract_output_sha256 => sha256_hex($output), evidence_file_sha256 => \%hashes,
        topology => { nodes => [sort keys %nodes], quorate => 1, expected_votes => 0 + $votes{'Expected votes'},
            total_votes => 0 + $votes{'Total votes'}, qdevice => $qdevice,
            policy => $configured == 2 && !$qdevice ? 'PROJECT_CONSTRAINED_2_OF_2' : 'ALL_CONFIGURED_PEERS_ONLINE' } };
    _need(length(_canonical($evidence)) <= 65536, 'canonical evidence budget');
    return $evidence;
}

sub assert_same {
    my ($class, $before, $after) = @_;
    for my $value ($before, $after) {
        _need(ref($value) eq 'HASH' && ($value->{schema} // '') eq 'sharedlvmthin-vm-destroy-runtime/v1'
            && ($value->{status} // '') eq 'QUALIFIED' && ($value->{authority} // '') eq 'NONE', 'invalid runtime comparison');
    }
    _need(_canonical($before) eq _canonical($after), 'runtime changed between VM-lock observations');
    return 1;
}

1;
