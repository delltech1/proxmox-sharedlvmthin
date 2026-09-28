#!/usr/bin/perl -T
# Experimental, unpackaged. No SSH, service control, retry, hold release or fencing.
# Production invocation requires deterministic PVE set serialization:
#   PERL_HASH_SEED=0 PERL_PERTURB_KEYS=0 /usr/bin/perl -T layout-migration-storage-cas.pl inspect
#   PERL_HASH_SEED=0 PERL_PERTURB_KEYS=0 /usr/bin/perl -T layout-migration-storage-cas.pl execute-once
#   PERL_HASH_SEED=0 PERL_PERTURB_KEYS=0 /usr/bin/perl -T layout-migration-storage-cas.pl collect-serializer
# collect-serializer reads the installed runtime; it never opens the CAS root,
# reads storage.cfg, acquires a cluster lock or authorizes a write. Run it only
# after candidate unpack, using these identical helper bytes for later CAS.
# Inputs/records live ONLY in /var/lib/pve-sharedlvmthin/storage-config-cas.
# request.json must be canonical ASCII JSON in the controller request shape.
# A MODEL authorization is never permission for the production write.
# INTENT.json reserves this entire local CAS slot, across tx/generation/attempt
# changes. Neither command removes/replaces it, even after a failed write.
# External, independently verified settlement is required before archival/reuse.
# Admission evidence is an explicit root-supplied assertion, NOT a collector
# proving remote quiescence. This helper does not qualify that assertion.

package SLT::StorageCAS;
BEGIN {
    die "PERL5LIB/PERL5OPT must be absent at interpreter start\n"
        if exists($ENV{PERL5LIB}) || exists($ENV{PERL5OPT});
}
use strict;
use warnings;
use B ();
use Cwd qw(realpath);
use Digest::SHA qw(sha256_hex);
use Fcntl qw(:DEFAULT :flock :mode);
use IO::Handle ();
use JSON::PP ();
use Storable qw(dclone);
use Time::HiRes qw(gettimeofday clock_gettime CLOCK_MONOTONIC);

use constant ROOT => '/var/lib/pve-sharedlvmthin/storage-config-cas';
use constant MAX => 1024 * 1024;
my $JSON = JSON::PP->new->canonical(1)->ascii(1)->allow_nonref(1)->max_depth(20);
my $SHA = qr/\A[0-9a-f]{64}\z/;
my $ID = qr/\A[0-9a-f]{32}\z/;
my $NAME = qr/\A[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\z/;
my @CALLS = qw(observe_local_identity sample_clock verify_admission verify_serializer
    with_storage_config_lock read_native_config assert_if_modified read_raw_config
    render_native_config write_native_config_once persist_record_once);

sub need { die "REFUSED: $_[1]\n" if !$_[0]; }
sub exact {
    my ($v, $keys, $label) = @_;
    need(ref($v) eq 'HASH' && join('|', sort keys %$v) eq join('|', sort @$keys), "$label fields invalid");
}
sub string {
    my ($v, $rx, $label) = @_;
    need(defined($v) && !ref($v) && (B::svref_2object(\$v)->FLAGS & B::SVp_POK()) && $v =~ $rx, "$label invalid");
}
sub decimal {
    my ($v, $label) = @_;
    string($v, qr/\A(?:0|[1-9][0-9]{0,18})\z/, $label);
    need(length($v) < 19 || $v le '9223372036854775807', "$label exceeds bound");
    return 0 + $v;
}
sub canonical { return $JSON->encode($_[0]); }
sub digest { return sha256_hex(canonical($_[0])); }
sub decode_canonical {
    my ($raw) = @_;
    need(length($raw) > 0 && length($raw) <= 4 * MAX, 'JSON size invalid');
    my $v = $JSON->decode($raw);
    # Also rejects duplicate keys, non-ASCII encoding and alternate escaping.
    need(canonical($v) eq $raw, 'JSON must be exact canonical ASCII (no duplicate keys)');
    return $v;
}
sub raw_hex {
    my ($hex, $label) = @_;
    string($hex, qr/\A[0-9a-f]+\z/, $label);
    need(length($hex) % 2 == 0 && length($hex) <= MAX * 2, "$label size invalid");
    return pack('H*', $hex);
}
sub validate_request {
    my ($r, $execute) = @_;
    exact($r, [qw(schema tx generation attempt_id executor context_sha256 candidate config changes participants evidence serializer authorization)], 'request');
    need($r->{schema} eq 'slt-native-config-cas-controller/v1', 'request schema invalid');
    string($r->{$_}, $ID, $_) for qw(tx attempt_id);
    need(decimal($r->{generation}, 'generation') > 0, 'generation must be positive');
    string($r->{context_sha256}, $SHA, 'context SHA');
    my $c = $r->{candidate};
    exact($c, [qw(package version flavor deb_sha256 artifact_sha256)], 'candidate');
    need($c->{package} eq 'pve-sharedlvmthin' && $c->{flavor} eq 'dual', 'candidate must be Dual');
    string($c->{version}, qr/\A[A-Za-z0-9.+:~_-]{1,128}\z/, 'version');
    string($c->{$_}, $SHA, $_) for qw(deb_sha256 artifact_sha256);
    exact($r->{config}, [qw(baseline_hex target_hex baseline_native_digest)], 'config');
    my $base = raw_hex($r->{config}->{baseline_hex}, 'baseline');
    my $target = raw_hex($r->{config}->{target_hex}, 'target');
    need($base ne $target, 'baseline equals target');
    string($r->{config}->{baseline_native_digest}, qr/\A[0-9a-f]{40}\z/, 'native digest');
    need(ref($r->{changes}) eq 'ARRAY' && @{$r->{changes}} && @{$r->{changes}} <= 64, 'changes invalid');
    my $last = '';
    for my $change (@{$r->{changes}}) {
        exact($change, [qw(storage_id property old_value new_value)], 'change');
        string($change->{storage_id}, $NAME, 'storage ID');
        need($change->{storage_id} gt $last && $change->{property} eq 'slt-vg-layout'
             && !defined($change->{old_value}) && $change->{new_value} eq 'mixed', 'delta not permitted or unordered');
        $last = $change->{storage_id};
    }
    need(ref($r->{participants}) eq 'ARRAY' && @{$r->{participants}} == 4, 'four participants required');
    my (%boots, @san, @names); $last = '';
    for my $node (@{$r->{participants}}) {
        exact($node, [qw(node boot_id role)], 'participant');
        string($node->{node}, $NAME, 'node');
        string($node->{boot_id}, qr/\A[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\z/, 'boot');
        need($node->{node} gt $last && ($node->{role} eq 'SAN_PARTICIPANT' || $node->{role} eq 'CONTROL_ONLY'), 'participant topology invalid');
        $last = $node->{node}; $boots{$last} = $node->{boot_id}; push @names, $last;
        push @san, $last if $node->{role} eq 'SAN_PARTICIPANT';
    }
    need(@san == 3, 'exactly three SAN participants required');
    exact($r->{executor}, [qw(node boot_id)], 'executor');
    need(grep($_ eq $r->{executor}->{node}, @san), 'executor must be a SAN participant');
    need($r->{executor}->{boot_id} eq $boots{$r->{executor}->{node}}, 'executor boot differs');
    exact($r->{evidence}, [qw(barrier_sha256 preinst_sha256 payload_sha256 prepare_manifest_sha256)], 'evidence');
    string($r->{evidence}->{$_}, $SHA, $_) for qw(barrier_sha256 prepare_manifest_sha256);
    for my $pair ([preinst_sha256 => \@san], [payload_sha256 => \@names]) {
        exact($r->{evidence}->{$pair->[0]}, $pair->[1], $pair->[0]);
        string($_, $SHA, 'node evidence SHA') for values %{$r->{evidence}->{$pair->[0]}};
    }
    my $s = $r->{serializer};
    exact($s, [qw(helper_sha256 perl_sha256 perl_hash_seed perl_perturb_keys modules registered_plugins)], 'serializer');
    string($s->{$_}, $SHA, $_) for qw(helper_sha256 perl_sha256);
    need($s->{perl_hash_seed} eq '0' && $s->{perl_perturb_keys} eq '0', 'serializer hash-order controls invalid');
    # Real API15 candidate imports 247 modules; request bytes remain <= 4 MiB.
    need(ref($s->{modules}) eq 'ARRAY' && @{$s->{modules}} && @{$s->{modules}} <= 1024, 'module list invalid');
    my %modules; $last = '';
    for my $m (@{$s->{modules}}) {
        exact($m, [qw(name path sha256)], 'module');
        string($m->{name}, qr/\A[A-Za-z_][A-Za-z0-9_\/]*\.pm\z/, 'module name');
        string($m->{path}, qr/\A\/[A-Za-z0-9_.+\/-]+\z/, 'module path');
        need($m->{path} !~ m{//|/(?:\.|\.\.)(?:/|$)} && $m->{name} gt $last, 'module path/order invalid');
        string($m->{sha256}, $SHA, 'module SHA'); $modules{$m->{name}} = 1; $last = $m->{name};
    }
    need($modules{$_}, "required module missing: $_") for qw(PVE/Storage.pm PVE/Storage/Plugin.pm PVE/SectionConfig.pm PVE/Cluster.pm PVE/Tools.pm PVE/JSONSchema.pm PVE/Storage/Custom/SharedLvmThinPlugin.pm);
    need(ref($s->{registered_plugins}) eq 'ARRAY' && @{$s->{registered_plugins}}, 'plugin registry invalid');
    $last = ''; my $found = 0;
    for my $plugin (@{$s->{registered_plugins}}) {
        string($plugin, qr/\A[A-Za-z_][A-Za-z0-9_:]*\z/, 'plugin');
        need($plugin gt $last, 'plugin registry not uniquely sorted'); $last = $plugin;
        $found = 1 if $plugin eq 'PVE::Storage::Custom::SharedLvmThinPlugin';
    }
    need($found, 'candidate plugin absent');
    my $auth = $r->{authorization};
    exact($auth, [qw(action request_body_sha256 issued_wall_ns expires_wall_ns)], 'authorization');
    need($auth->{action} eq 'NATIVE_PVE_CONFIG_CAS_ONCE'
         || (!$execute && $auth->{action} eq 'CONTROLLER_MODEL_ONE_CAS_ONLY'), 'model token cannot authorize native write');
    my %body = %$r; delete $body{authorization};
    need($auth->{request_body_sha256} eq digest(\%body), 'authorization body SHA differs');
    my $issued = decimal($auth->{issued_wall_ns}, 'issued time');
    my $expires = decimal($auth->{expires_wall_ns}, 'expiry');
    need($expires > $issued && $expires - $issued <= 1800 * 1_000_000_000, 'authorization duration invalid');
    return ($base, $target);
}

# Internal dependency-injection boundary, not exposed as a CLI/env test mode.
# Loading this file via require does not import PVE or open /etc/pve.
sub run_with_backend {
    my ($request, $backend, $execute) = @_;
    my $r = decode_canonical(canonical($request));
    my ($base, $target) = validate_request($r, $execute);
    exact($backend, \@CALLS, 'backend');
    need(ref($backend->{$_}) eq 'CODE', 'backend callback invalid') for @CALLS;
    my $request_sha = digest($r);
    my ($wall_last, $mono_last, $mono_deadline);
    my ($intent, $entered, $outcome, $body_done, $closed, $calls, $poison) = (0, 0, 0, 0, 0, 0, 0);
    my ($write_status, $observation, $inline, $rendered);
    my $call = sub {
        my ($name, @args) = @_;
        need(!$poison, 'attempt poisoned');
        my @seals = map { canonical($_) } @args;
        my $value;
        my $good = eval { $value = $backend->{$name}->(@args); 1; };
        my $error = $@;
        for my $i (0 .. $#args) {
            if (canonical($args[$i]) ne $seals[$i]) { $poison = 1; die "REFUSED: backend mutated argument\n"; }
        }
        die $error if !$good;
        return $value;
    };
    my $clock = sub {
        my $v = $call->('sample_clock'); exact($v, [qw(wall_ns monotonic_ns)], 'clock');
        my ($wall, $mono) = @$v{qw(wall_ns monotonic_ns)};
        need(defined($wall) && defined($mono) && !ref($wall) && !ref($mono)
             && $wall =~ /\A[0-9]+\z/ && $mono =~ /\A[0-9]+\z/, 'clock invalid');
        my $auth = $r->{authorization};
        need($wall >= $auth->{issued_wall_ns} && $wall <= $auth->{expires_wall_ns}, 'authorization expired/future');
        $mono_deadline //= $mono + ($auth->{expires_wall_ns} - $wall);
        need(!defined($wall_last) || ($wall >= $wall_last && $mono >= $mono_last && $mono <= $mono_deadline), 'clock rollback/expiry');
        ($wall_last, $mono_last) = ($wall, $mono);
    };
    my $check = sub {
        $clock->();
        my $id = $call->('observe_local_identity'); exact($id, [qw(node boot_id quorate)], 'local identity');
        need($id->{node} eq $r->{executor}->{node} && $id->{boot_id} eq $r->{executor}->{boot_id}
             && JSON::PP::is_bool($id->{quorate}) && $id->{quorate}, 'node/boot/quorum differs');
        my $a = $call->('verify_admission', dclone($r)); exact($a, [qw(request_sha256 admitted)], 'admission');
        need($a->{request_sha256} eq $request_sha && JSON::PP::is_bool($a->{admitted}) && $a->{admitted}, 'admission unproven');
        my $s = $call->('verify_serializer', dclone($r->{serializer})); exact($s, [qw(manifest_sha256 verified)], 'serializer ACK');
        need($s->{manifest_sha256} eq digest($r->{serializer}) && JSON::PP::is_bool($s->{verified}) && $s->{verified}, 'serializer unproven');
    };
    my $persist = sub {
        my ($kind, $record) = @_;
        my $ack = $call->('persist_record_once', $kind, $record);
        exact($ack, [qw(record_sha256 created file_synced directory_synced)], 'durability ACK');
        need($ack->{record_sha256} eq digest($record), 'record ACK differs');
        need(JSON::PP::is_bool($ack->{$_}) && $ack->{$_}, 'durability unproven') for qw(created file_synced directory_synced);
    };
    my $ok = eval {
        $check->();
        $backend->{with_storage_config_lock}->(sub {
            if ($closed || $calls++) { $poison = 1; die "REFUSED: repeated/late lock callback\n"; }
            my $good = eval {
                $check->();
                need($call->('read_raw_config') eq $base, 'raw baseline differs');
                my $cfg = $call->('read_native_config');
                need(ref($cfg) eq 'HASH' && ref($cfg->{ids}) eq 'HASH', 'native config invalid');
                need($cfg->{digest} eq $r->{config}->{baseline_native_digest}, 'native digest differs');
                $call->('assert_if_modified', $cfg, $r->{config}->{baseline_native_digest});
                need($call->('read_raw_config') eq $base, 'baseline changed during native read');
                for my $delta (@{$r->{changes}}) {
                    my $section = $cfg->{ids}->{$delta->{storage_id}};
                    need(ref($section) eq 'HASH' && $section->{type} eq 'sharedlvmthin'
                         && !exists($section->{'slt-vg-layout'}), 'delta precondition differs');
                    $section->{'slt-vg-layout'} = 'mixed';
                }
                $rendered = $call->('render_native_config', $cfg);
                need($rendered eq $target, 'native serialized target differs');
                $check->();
                if ($execute) {
                    my $record = {schema => 'slt-controller-cas-intent/v1', request_sha256 => $request_sha,
                        tx => $r->{tx}, generation => $r->{generation}, attempt_id => $r->{attempt_id},
                        executor => $r->{executor}, reservation => "$r->{tx}:$r->{generation}:storage-config-cas"};
                    $intent = 1; # May have persisted even if its ACK raises.
                    $persist->('INTENT', $record);
                    $check->();
                    need($call->('read_raw_config') eq $base, 'baseline changed before write');
                    $clock->(); need(!$entered && !$poison, 'write latch consumed');
                    $entered = 1; $write_status = 'ENTERED_OUTCOME_UNKNOWN';
                    my $written = eval { $call->('write_native_config_once', $cfg); 1; };
                    $write_status = $written ? 'RETURNED' : 'RAISED';
                    $inline = $call->('read_raw_config');
                    $observation = $inline eq $target ? 'TARGET' : $inline eq $base ? 'BASELINE' : 'FOREIGN';
                    $check->();
                    need(!$written || $observation eq 'TARGET', 'write/readback contradiction');
                }
                $body_done = 1; 1;
            };
            if (!$good) { $poison = 1; die $@ || "REFUSED: lock body ambiguous\n"; }
        });
        $closed = 1;
        need($calls == 1 && $body_done && !$poison, 'lock completion unproven');
        if ($execute) {
            $clock->();
            $persist->('OUTCOME', {schema => 'slt-controller-cas-outcome/v1', request_sha256 => $request_sha,
                attempt_id => $r->{attempt_id}, write_entered => JSON::PP::true,
                write_outcome => $write_status, inline_observation => $observation,
                inline_raw_hex => unpack('H*', $inline), settlement => JSON::PP::false, authorization => 'NONE'});
            $outcome = 1;
        }
        1;
    };
    $closed = 1;
    return {schema => 'slt-native-storage-cas-result/v1', request_sha256 => $request_sha,
        classification => !$ok ? 'UNKNOWN_RETAIN' : $execute ? 'INLINE_OBSERVATION_ONLY' : 'INSPECTED_NO_WRITE',
        intent_reserved => $intent ? JSON::PP::true : JSON::PP::false,
        write_entered => $entered ? JSON::PP::true : JSON::PP::false,
        outcome_durable => $outcome ? JSON::PP::true : JSON::PP::false,
        write_outcome => $write_status, inline_observation => $observation,
        authorization => 'NONE', settlement_proven => JSON::PP::false,
        retry_authorized => JSON::PP::false, hold_transition_authorized => JSON::PP::false,
        release_authorized => JSON::PP::false, runtime_qualified => JSON::PP::false};
}

sub read_file {
    my ($path, $private, $maximum) = @_;
    # All callers select fixed paths or previously pinned module identities.
    # Capture only after rejecting traversal, to permit those paths under -T.
    need(defined($path) && $path !~ m{//|/(?:\.|\.\.)(?:/|$)}, 'unsafe evidence path');
    ($path) = $path =~ m{\A(/[A-Za-z0-9_.+/-]+)\z};
    need(defined($path), 'invalid evidence path');
    $maximum //= 4 * MAX;
    sysopen(my $fh, $path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK) or die "REFUSED: cannot open evidence\n";
    my @before = stat($fh);
    need(S_ISREG($before[2]) && $before[4] == 0 && !($before[2] & 022)
         && (!$private || (($before[2] & 0777) == 0600 && $before[3] == 1)), 'unsafe evidence inode');
    my $raw = '';
    while (1) { my $n = sysread($fh, my $buf, 65536); need(defined($n), 'evidence read failed'); last if !$n;
        $raw .= $buf; need(length($raw) <= $maximum, 'evidence exceeds bound'); }
    my @after = stat($fh); close($fh) or die "REFUSED: evidence close failed\n";
    need(join(':', @before[0,1,2,3,4,7,9,10]) eq join(':', @after[0,1,2,3,4,7,9,10])
         && ($path eq '/proc/sys/kernel/random/boot_id' || length($raw) == $before[7]), 'evidence changed during read');
    return $raw;
}
sub secure_root {
    my $path = '';
    for my $part (split('/', ROOT)) {
        next if !$part; $path .= "/$part";
        my @s = lstat($path);
        need(@s && S_ISDIR($s[2]) && $s[4] == 0 && !($s[2] & 022), 'unsafe/missing fixed-root ancestor');
    }
    my @s = lstat(ROOT); need(($s[2] & 0777) == 0700, 'fixed root must be 0700');
    sysopen(my $dir, ROOT, O_RDONLY | O_DIRECTORY | O_NOFOLLOW) or die "REFUSED: root open failed\n";
    return $dir;
}
sub pinned_root {
    my ($dir) = @_;
    my @fd = stat($dir); my @name = lstat(ROOT);
    need(@name && S_ISDIR($name[2]) && join(':', @fd[0,1,2,4]) eq join(':', @name[0,1,2,4]), 'fixed root namespace changed');
    return '/proc/self/fd/' . fileno($dir);
}
sub persist_create_only {
    my ($dir, $kind, $record) = @_;
    need($kind eq 'INTENT' || $kind eq 'OUTCOME', 'record kind invalid');
    my $path = pinned_root($dir) . "/$kind.json";
    sysopen(my $fh, $path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600)
        or die "REFUSED: durable reservation already exists or cannot be created\n";
    my $raw = canonical($record); my $offset = 0;
    while ($offset < length($raw)) {
        my $n = syswrite($fh, $raw, length($raw) - $offset, $offset);
        need(defined($n) && $n > 0, 'record write failed; preserve partial record'); $offset += $n;
    }
    $fh->sync or die "REFUSED: record fsync failed; no retry\n";
    my @fd = stat($fh); my @name = lstat($path);
    need(@name && $fd[0] == $name[0] && $fd[1] == $name[1] && $fd[3] == 1, 'record namespace changed');
    pinned_root($dir);
    $dir->sync or die "REFUSED: directory fsync failed; no retry\n";
    close($fh) or die "REFUSED: record close failed; no retry\n";
    return {record_sha256 => digest($record), created => JSON::PP::true,
            file_synced => JSON::PP::true, directory_synced => JSON::PP::true};
}

sub load_production_modules {
    require PVE::Storage;
    require PVE::SectionConfig;
    require PVE::INotify;
    require PVE::Cluster;
}

sub serializer_manifest {
    my @modules;
    for my $name (sort grep { /\.pm\z/ } keys %INC) {
        my $path = realpath($INC{$name} // '');
        need(defined($path), 'loaded module path unavailable');
        push @modules, {name => $name, path => $path, sha256 => sha256_hex(read_file($path, 0))};
    }
    my @plugins = sort map { PVE::Storage::Plugin->lookup($_) } @{PVE::Storage::Plugin->lookup_types()};
    return {helper_sha256 => sha256_hex(read_file(realpath(__FILE__), 0)),
        perl_sha256 => sha256_hex(read_file(realpath($^X), 0, 64 * MAX)),
        perl_hash_seed => '0', perl_perturb_keys => '0',
        modules => \@modules, registered_plugins => \@plugins};
}

sub verify_runtime_serializer {
    my ($s) = @_;
    need(canonical(serializer_manifest()) eq canonical($s), 'runtime serializer manifest differs');
    return {manifest_sha256 => digest($s), verified => JSON::PP::true};
}

sub collect_serializer {
    load_production_modules();
    my $s = serializer_manifest();
    # Re-read through the exact verifier used by the write path. A renderer's
    # extracted module paths/helper SHA are never translated into live evidence.
    verify_runtime_serializer($s);
    return {schema => 'slt-native-cas-serializer/v1', classification => 'READ_ONLY_RUNTIME_SERIALIZER',
        serializer => $s, serializer_sha256 => digest($s), authorization => 'NONE',
        mutation_performed => JSON::PP::false, runtime_qualified => JSON::PP::false};
}

sub production_backend {
    my ($dir) = @_;
    load_production_modules();
    my $read_raw = sub { read_file('/etc/pve/storage.cfg', 0) };
    return {
        sample_clock => sub { my ($s, $us) = gettimeofday(); return {wall_ns => $s * 1_000_000_000 + $us * 1000,
            monotonic_ns => int(clock_gettime(CLOCK_MONOTONIC) * 1_000_000_000)}; },
        observe_local_identity => sub { my $boot = read_file('/proc/sys/kernel/random/boot_id', 0); $boot =~ s/\n\z//;
            return {node => PVE::INotify::nodename(), boot_id => $boot, quorate => PVE::Cluster::check_cfs_quorum(1) ? JSON::PP::true : JSON::PP::false}; },
        verify_admission => sub {
            my ($r) = @_;
            my $root = pinned_root($dir);
            need(read_file("$root/request.json", 1) eq canonical($r), 'request changed');
            my %e = ("barrier.json" => $r->{evidence}->{barrier_sha256});
            for my $kind (qw(preinst payload)) {
                $e{"$kind-$_.json"} = $r->{evidence}->{"${kind}_sha256"}->{$_} for keys %{$r->{evidence}->{"${kind}_sha256"}};
            }
            need(sha256_hex(read_file("$root/$_", 1)) eq $e{$_}, 'admission artifact differs') for sort keys %e;
            my $hold_raw = read_file('/var/lib/pve-sharedlvmthin/maintenance/active.json', 1);
            need(sha256_hex($hold_raw) eq $r->{evidence}->{prepare_manifest_sha256}, 'PREPARE hold differs');
            my $hold = decode_canonical($hold_raw);
            need($hold->{schema} eq 'slt-package-maintenance/v1' && $hold->{phase} eq 'PREPARE_READY'
                 && $hold->{tx} eq $r->{tx} && "$hold->{generation}" eq $r->{generation}
                 && canonical($hold->{candidate}) eq canonical($r->{candidate})
                 && $hold->{baseline_storage_cfg_sha256} eq sha256_hex(pack('H*', $r->{config}->{baseline_hex}))
                 && $hold->{target_storage_cfg_sha256} eq sha256_hex(pack('H*', $r->{config}->{target_hex})), 'hold identity differs');
            need(read_file('/usr/share/pve-sharedlvmthin/package-artifact-sha256', 0) eq $r->{candidate}->{artifact_sha256} . "\n"
                 && read_file('/usr/share/pve-sharedlvmthin/package-flavor', 0) eq "dual\n", 'installed candidate differs');
            return {request_sha256 => digest($r), admitted => JSON::PP::true};
        },
        verify_serializer => \&verify_runtime_serializer,
        with_storage_config_lock => sub { PVE::Storage::lock_storage_config($_[0], 'native CAS storage lock'); },
        read_native_config => sub { local $SIG{__WARN__} = sub { die "REFUSED: native parser warning\n"; };
            return dclone(PVE::Storage::config()); },
        assert_if_modified => sub { PVE::SectionConfig::assert_if_modified($_[0], $_[1]); },
        read_raw_config => $read_raw,
        render_native_config => sub { local $SIG{__WARN__} = sub { die "REFUSED: native serializer warning\n"; };
            return PVE::Storage::Plugin->write_config('storage.cfg', $_[0]); },
        write_native_config_once => sub { local $SIG{__WARN__} = sub { die "REFUSED: native writer warning\n"; };
            PVE::Storage::write_config($_[0]); },
        persist_record_once => sub { return persist_create_only($dir, @_); },
    };
}

sub main {
    my ($command, @extra) = @_;
    need(($ENV{PERL_HASH_SEED} // '') eq '0' && ($ENV{PERL_PERTURB_KEYS} // '') eq '0',
        'production invocation requires PERL_HASH_SEED=0 and PERL_PERTURB_KEYS=0 at interpreter start');
    %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
    need(defined($command) && !@extra && ($command eq 'inspect' || $command eq 'execute-once' || $command eq 'collect-serializer'),
        'usage: inspect | execute-once | collect-serializer (no alternate paths/test flags)');
    need($> == 0 && ${^TAINT}, 'production invocation requires root and perl -T');
    if ($command eq 'collect-serializer') {
        print canonical(collect_serializer()), "\n";
        return 0;
    }
    my $dir = secure_root();
    my $root = pinned_root($dir);
    my $r = decode_canonical(read_file("$root/request.json", 1));
    validate_request($r, $command eq 'execute-once');
    my $lock;
    if ($command eq 'execute-once') {
        sysopen($lock, "$root/.cas.lock", O_CREAT | O_RDWR | O_NOFOLLOW, 0600) or die "REFUSED: local lock unavailable\n";
        my @s = stat($lock); need(S_ISREG($s[2]) && $s[4] == 0 && ($s[2] & 0777) == 0600 && $s[3] == 1, 'unsafe local lock');
        flock($lock, LOCK_EX | LOCK_NB) or die "REFUSED: another CAS executor is live\n";
        my @named = lstat("$root/.cas.lock"); need($s[0] == $named[0] && $s[1] == $named[1], 'lock namespace changed');
    }
    # No replay, including a different attempt/tx in this fixed reservation slot.
    if (lstat("$root/INTENT.json") || lstat("$root/OUTCOME.json")) {
        print canonical({classification => 'UNKNOWN_RETAIN', authorization => 'NONE', retry_authorized => JSON::PP::false,
                         reason => 'reservation exists; inspect records and settle externally; never redispatch'}), "\n";
        return 2;
    }
    my $result = run_with_backend($r, production_backend($dir), $command eq 'execute-once');
    print canonical($result), "\n";
    return $result->{classification} eq 'UNKNOWN_RETAIN' ? 2 : 0;
}

unless (caller) {
    my $rc = eval { main(@ARGV) };
    if ($@) { print canonical({classification => 'REFUSED', authorization => 'NONE', reason => 'invalid or unavailable boundary; no automatic retry'}), "\n"; $rc = 2; }
    exit($rc);
}
1;
