#!/usr/bin/perl
use strict;
use warnings;
use Test::More;
use FindBin;
use File::Spec;
use File::Temp qw(tempdir);
use Fcntl qw(O_RDONLY O_DIRECTORY);
use Storable qw(dclone);
use JSON::PP ();
use Digest::SHA qw(sha256_hex);

# `prove -I...` exports PERL5LIB to each test process.  The production CAS
# helper deliberately refuses that environment, so remove only the harness
# injection before requiring it in the dependency-injected unit test.
delete local $ENV{PERL5LIB};
delete local $ENV{PERL5OPT};
require File::Spec->catfile($FindBin::Bin, '..', '..', 'experiments', 'thick-generations', 'layout-migration-storage-cas.pl');
ok(!exists($INC{'PVE/Storage.pm'}), 'injected test mode never loads native PVE');
my $base = "sharedlvmthin: alpha\n\tvgname vgtest\n";
my $target = "sharedlvmthin: alpha\n\tvgname vgtest\n\tslt-vg-layout mixed\n";
my $boot = '12345678-1234-4123-8123-123456789abc';
my @modules = sort qw(PVE/Storage.pm PVE/Storage/Plugin.pm PVE/SectionConfig.pm PVE/Cluster.pm PVE/Tools.pm PVE/JSONSchema.pm PVE/Storage/Custom/SharedLvmThinPlugin.pm);
sub request {
    my $r = {
        schema => 'slt-native-config-cas-controller/v1', tx => 'a' x 32, generation => '1', attempt_id => 'b' x 32,
        executor => {node => 'pve01', boot_id => $boot}, context_sha256 => 'c' x 64,
        candidate => {package => 'pve-sharedlvmthin', version => '5.13-TG35', flavor => 'dual', deb_sha256 => 'd' x 64, artifact_sha256 => 'e' x 64},
        config => {baseline_hex => unpack('H*', $base), target_hex => unpack('H*', $target), baseline_native_digest => 'f' x 40},
        changes => [{storage_id => 'alpha', property => 'slt-vg-layout', old_value => undef, new_value => 'mixed'}],
        participants => [map {{node => sprintf('pve%02d', $_), boot_id => $boot, role => $_ == 4 ? 'CONTROL_ONLY' : 'SAN_PARTICIPANT'}} 1..4],
        evidence => {barrier_sha256 => 'a' x 64, prepare_manifest_sha256 => 'b' x 64,
            preinst_sha256 => {map {sprintf('pve%02d', $_) => 'c' x 64} 1..3},
            payload_sha256 => {map {sprintf('pve%02d', $_) => 'd' x 64} 1..4}},
        serializer => {helper_sha256 => 'e' x 64, perl_sha256 => 'f' x 64,
            perl_hash_seed => '0', perl_perturb_keys => '0',
            modules => [map {{name => $_, path => '/usr/share/perl5/' . $_, sha256 => 'a' x 64}} @modules],
            registered_plugins => ['PVE::Storage::Custom::SharedLvmThinPlugin']},
    };
    $r->{authorization} = {action => 'NATIVE_PVE_CONFIG_CAS_ONCE', request_body_sha256 => SLT::StorageCAS::digest($r),
        issued_wall_ns => '1000000000', expires_wall_ns => '9000000000'};
    return $r;
}
sub resign {
    my ($r) = @_;
    my %body = %$r; delete $body{authorization};
    $r->{authorization}->{request_body_sha256} = SLT::StorageCAS::digest(\%body);
}
sub fixture {
    my $s = {raw => $base, writes => 0, clock => 2000000000, checks => 0, records => {}, events => [], locked => 0};
    my $b = {
        sample_clock => sub { return {wall_ns => $s->{clock}, monotonic_ns => $s->{clock}}; },
        observe_local_identity => sub {return {node => 'pve01', boot_id => $boot, quorate => JSON::PP::true};},
        verify_admission => sub {$s->{checks}++; return {request_sha256 => SLT::StorageCAS::digest($_[0]), admitted => JSON::PP::true};},
        verify_serializer => sub {return {manifest_sha256 => SLT::StorageCAS::digest($_[0]), verified => JSON::PP::true};},
        with_storage_config_lock => sub {$s->{locked} = 1; push @{$s->{events}}, 'lock'; $_[0]->(); $s->{locked} = 0; push @{$s->{events}}, 'unlock';},
        read_raw_config => sub {return $s->{raw};},
        read_native_config => sub {return {ids => {alpha => {type => 'sharedlvmthin', vgname => 'vgtest'}}, digest => 'f' x 40, order => {alpha => 1}};},
        assert_if_modified => sub {die 'not locked' if !$s->{locked}; push @{$s->{events}}, 'assert'; die 'digest' if $_[0]->{digest} ne $_[1];},
        render_native_config => sub {die 'wrong delta' if $_[0]->{ids}->{alpha}->{'slt-vg-layout'} ne 'mixed'; return $target;},
        write_native_config_once => sub {die 'not locked' if !$s->{locked}; $s->{writes}++; push @{$s->{events}}, 'write'; $s->{raw} = $target;},
        persist_record_once => sub {
            my ($kind, $record) = @_;
            die 'create-only reservation exists' if exists($s->{records}->{$kind});
            $s->{records}->{$kind} = dclone($record); push @{$s->{events}}, $kind;
            return {record_sha256 => SLT::StorageCAS::digest($record), created => JSON::PP::true,
                file_synced => JSON::PP::true, directory_synced => JSON::PP::true};
        },
    };
    return ($s, $b);
}
sub attempt {
    my ($modify, $inspect) = @_;
    my ($s, $b) = fixture(); my $r = request(); $modify->($s, $b, $r) if $modify;
    my $v = SLT::StorageCAS::run_with_backend($r, $b, !$inspect);
    return ($v, $s, $b, $r);
}
{
    my ($v, $s) = attempt(undef, 1);
    is($v->{classification}, 'INSPECTED_NO_WRITE', 'inspect proves native target');
    is($s->{writes}, 0, 'inspect never writes'); is_deeply($s->{records}, {}, 'inspect creates no durable records');
    is($v->{authorization}, 'NONE', 'inspection grants no authority');
}
{
    my ($v, $s, $b, $r) = attempt();
    is($v->{classification}, 'INLINE_OBSERVATION_ONLY', 'success remains observation only');
    is($s->{writes}, 1, 'one native write');
    is_deeply($s->{events}, [qw(lock assert INTENT write unlock OUTCOME)], 'intent synced before write; outcome after lock');
    ok($v->{outcome_durable} && !$v->{release_authorized} && !$v->{runtime_qualified}, 'durability is not settlement or runtime qualification');
    my $again = SLT::StorageCAS::run_with_backend($r, $b, 1);
    is($again->{classification}, 'UNKNOWN_RETAIN', 'replay never grants retry'); is($s->{writes}, 1, 'replay performs no second write');
}
for my $case (
    ['raw drift', sub {$_[0]->{raw} = 'foreign';}],
    ['digest drift', sub {$_[1]->{read_native_config} = sub {{ids => {}, digest => '0' x 40}};}],
    ['serializer proof false', sub {$_[1]->{verify_serializer} = sub {{manifest_sha256 => SLT::StorageCAS::digest($_[0]), verified => JSON::PP::false}};}],
    ['render mismatch', sub {$_[1]->{render_native_config} = sub {'foreign'};}],
    ['existing property', sub {$_[1]->{read_native_config} = sub {{ids => {alpha => {type => 'sharedlvmthin', 'slt-vg-layout' => 'mixed'}}, digest => 'f' x 40}};}],
    ['native parser exception', sub {$_[1]->{read_native_config} = sub {die 'parse warning'};}],
    ['missing callback invocation', sub {$_[1]->{with_storage_config_lock} = sub {};}],
) {
    my ($v, $s) = attempt($case->[1]);
    is($v->{classification}, 'UNKNOWN_RETAIN', "$case->[0] refuses"); is($s->{writes}, 0, "$case->[0] no write");
}
{
    my ($v, $s) = attempt(sub {$_[1]->{persist_record_once} = sub {die 'fsync uncertain'};});
    ok($v->{intent_reserved}, 'failed intent ACK conservatively reserves slot'); is($s->{writes}, 0, 'no write without proven intent durability');
}
for my $effect (0, 1) {
    my ($v, $s) = attempt(sub {my ($s, $b) = @_; $b->{write_native_config_once} = sub {$s->{writes}++; $s->{raw} = $target if $effect; die 'native write raised';};});
    is($s->{writes}, 1, 'raising writer invoked once'); is($v->{write_outcome}, 'RAISED', 'exception recorded, never retried');
    is($v->{inline_observation}, $effect ? 'TARGET' : 'BASELINE', 'effect classified by readback'); ok(!$v->{retry_authorized}, 'raised write never authorizes redispatch');
}
{
    my ($v, $s) = attempt(sub {my ($s, $b) = @_; $b->{write_native_config_once} = sub {$s->{writes}++; $s->{raw} = 'foreign';};});
    is($v->{classification}, 'UNKNOWN_RETAIN', 'returned writer with foreign bytes is ambiguous'); ok(!exists($s->{records}->{OUTCOME}), 'contradiction does not receive durable outcome');
}
{
    my ($v, $s) = attempt(sub {my ($s, $b) = @_; my $persist = $b->{persist_record_once}; $b->{persist_record_once} = sub {my $ack = $persist->(@_); $s->{clock} = 10000000000; return $ack;};});
    is($s->{writes}, 0, 'expired after durable intent never writes'); ok($v->{intent_reserved}, 'expiry retains intent');
}
{
    my ($v, $s) = attempt(sub {my ($s, $b) = @_; $b->{write_native_config_once} = sub {$s->{writes}++; $_[0]->{evil} = 1; die 'mutated and raised';};});
    is($v->{classification}, 'UNKNOWN_RETAIN', 'mutation even with exception poisons attempt'); is($s->{writes}, 1, 'poisoned writer not retried'); ok(!exists($s->{records}->{OUTCOME}), 'poisoned writer has no outcome');
}
{
    my ($v, $s) = attempt(sub {my ($s, $b) = @_; $b->{with_storage_config_lock} = sub {$s->{locked} = 1; $_[0]->(); eval {$_[0]->()};};});
    is($v->{classification}, 'UNKNOWN_RETAIN', 'repeated callback poisons even when swallowed'); is($s->{writes}, 1, 'duplicate callback no duplicate native write');
}
{
    my ($v, $s) = attempt(sub {$_[1]->{with_storage_config_lock} = sub {eval {$_[0]->()};}; $_[1]->{read_native_config} = sub {die 'fail'};});
    is($v->{classification}, 'UNKNOWN_RETAIN', 'swallowed callback failure still refuses'); is($s->{writes}, 0, 'swallowed failure no write');
}
for my $bad (
    ['model token', sub {$_[0]->{authorization}->{action} = 'CONTROLLER_MODEL_ONE_CAS_ONLY';}],
    ['body hash drift', sub {$_[0]->{attempt_id} = 'c' x 32;}],
    ['unknown request field', sub {$_[0]->{retry} = 1; resign($_[0]);}],
    ['missing participant', sub {pop @{$_[0]->{participants}}; resign($_[0]);}],
    ['wrong hash-order controls', sub {$_[0]->{serializer}->{perl_hash_seed} = '1'; resign($_[0]);}],
) {
    my $r = request(); $bad->[1]->($r); my ($s, $b) = fixture();
    ok(!eval {SLT::StorageCAS::run_with_backend($r, $b, 1); 1}, "$bad->[0] rejected at admission");
    is($s->{writes}, 0, "$bad->[0] never writes");
}
{
    my $r = request(); $r->{authorization}->{action} = 'CONTROLLER_MODEL_ONE_CAS_ONLY'; my ($s, $b) = fixture();
    is(SLT::StorageCAS::run_with_backend($r, $b, 0)->{classification}, 'INSPECTED_NO_WRITE', 'model token permits read-only inspection only');
    ok(!eval {SLT::StorageCAS::decode_canonical('{"a":1,"a":2}'); 1}, 'duplicate keys rejected');
    ok(!eval {SLT::StorageCAS::main('execute-once', '--test'); 1}, 'no CLI test bypass');
}
{
    local $ENV{PERL_HASH_SEED};
    local $ENV{PERL_PERTURB_KEYS};
    my $root_calls = 0;
    no warnings 'redefine';
    local *SLT::StorageCAS::secure_root = sub {$root_calls++; die 'root touched';};
    ok(!eval {SLT::StorageCAS::main('inspect'); 1},
        'missing deterministic startup controls refuse');
    is($root_calls, 0, 'startup controls are checked before opening CAS root');
}
SKIP: {
    skip 'POSIX fsync fixture requires Linux /proc; no real PVE writes', 4 if $^O ne 'linux';
    my $dir = tempdir(CLEANUP => 1); chmod(0700, $dir) or die 'chmod';
    sysopen(my $fd, $dir, O_RDONLY | O_DIRECTORY) or die 'open temporary directory';
    no warnings 'redefine';
    local *SLT::StorageCAS::pinned_root = sub {return $dir;};
    my $ack = SLT::StorageCAS::persist_create_only($fd, 'INTENT', {reservation => 'test'});
    ok($ack->{file_synced} && $ack->{directory_synced}, 'real temporary file and parent directory fsynced');
    ok(!eval {SLT::StorageCAS::persist_create_only($fd, 'INTENT', {reservation => 'replacement'}); 1}, 'existing intent cannot be overwritten');
    open(my $read, '<', "$dir/INTENT.json") or die 'read'; local $/; my $raw = <$read>; close($read);
    is($raw, SLT::StorageCAS::canonical({reservation => 'test'}), 'original reservation retained');
    ok(SLT::StorageCAS::persist_create_only($fd, 'OUTCOME', {authorization => 'NONE'})->{created}, 'separate create-only outcome');
    close($fd);
}
for my $count (247, 1024, 1025) {
    my $r = request();
    my @all = (@modules, map {sprintf('Extra/Module%04d.pm', $_)} 1 .. ($count - @modules));
    $r->{serializer}->{modules} = [map {{name => $_, path => '/usr/share/perl5/' . $_,
        sha256 => 'a' x 64}} sort @all];
    resign($r);
    my $ok = eval {SLT::StorageCAS::validate_request($r, 1); 1};
    if ($count <= 1024) {ok($ok, "$count exact pinned modules permitted");}
    else {ok(!$ok, '1025 modules refused');}
}
ok(!eval {SLT::StorageCAS::decode_canonical('"' . ('a' x (4 * 1024 * 1024)) . '"'); 1},
    '4 MiB aggregate JSON bound retained');
{
    # Exercise the real manifest builder and the real production verifier with
    # inert files/registry only. No PVE imports, config reads, locks or writers.
    no warnings qw(redefine once);
    local %INC = map {$_ => '/usr/share/perl5/' . $_} @modules;
    my ($loads, $effects, $drift) = (0, 0, 0);
    local *SLT::StorageCAS::load_production_modules = sub {$loads++};
    local *SLT::StorageCAS::realpath = sub {return $_[0]};
    local *SLT::StorageCAS::read_file = sub {return $_[0] . ($drift ? ':changed' : '')};
    local *SLT::StorageCAS::secure_root = sub {$effects++; die 'CAS root accessed'};
    local *PVE::Storage::config = sub {$effects++; die 'config accessed'};
    local *PVE::Storage::write_config = sub {$effects++; die 'writer invoked'};
    local *PVE::Storage::lock_storage_config = sub {$effects++; die 'lock acquired'};
    local *PVE::Storage::Plugin::lookup_types = sub {return ['sharedlvmthin']};
    local *PVE::Storage::Plugin::lookup = sub {return 'PVE::Storage::Custom::SharedLvmThinPlugin'};
    my $collected = SLT::StorageCAS::collect_serializer();
    is($collected->{classification}, 'READ_ONLY_RUNTIME_SERIALIZER', 'runtime collector classified read-only');
    is($collected->{authorization}, 'NONE', 'collector grants no write authority');
    ok(!$collected->{mutation_performed} && !$collected->{runtime_qualified}, 'collector not mutation or qualification');
    is($loads, 1, 'collector loads same production modules');
    is($effects, 0, 'collector never accesses CAS root/config/lock/writer');
    my $manifest = $collected->{serializer};
    is($collected->{serializer_sha256}, SLT::StorageCAS::digest($manifest), 'manifest digest binds exact runtime');
    like($manifest->{modules}->[0]->{path}, qr{\A/usr/share/perl5/}, 'actual loaded runtime paths retained');
    my $backend = SLT::StorageCAS::production_backend(undef);
    ok($backend->{verify_serializer}->($manifest)->{verified}, 'collector manifest accepted by actual production callback');
    for my $case (
        ['candidate renderer helper hash', sub {$_[0]->{helper_sha256} = 'a' x 64}],
        ['candidate extraction path', sub {$_[0]->{modules}->[0]->{path} = '/run/slt-candidate-render-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/payload/usr/share/perl5/PVE/Cluster.pm'}],
        ['changed interpreter', sub {$_[0]->{perl_sha256} = 'b' x 64}],
        ['changed module bytes', sub {$_[0]->{modules}->[0]->{sha256} = 'c' x 64}],
        ['missing loaded module', sub {pop @{$_[0]->{modules}}}],
        ['extra module', sub {push @{$_[0]->{modules}}, {name => 'Z.pm', path => '/usr/share/perl5/Z.pm', sha256 => 'd' x 64}}],
        ['reordered modules', sub {$_[0]->{modules} = [reverse @{$_[0]->{modules}}]}],
        ['changed registry', sub {$_[0]->{registered_plugins} = ['Foreign']}],
        ['changed seed', sub {$_[0]->{perl_hash_seed} = '1'}],
        ['extra manifest field', sub {$_[0]->{authorization} = 'WRITE'}],
    ) {
        my $bad = dclone($manifest); $case->[1]->($bad);
        ok(!eval {$backend->{verify_serializer}->($bad); 1}, "$case->[0] refused by production verifier");
    }
    $drift = 1;
    ok(!eval {$backend->{verify_serializer}->($manifest); 1}, 'runtime bytes changing after collection refused');
    $drift = 0;
    {
        my $reads = 0;
        local *SLT::StorageCAS::read_file = sub {return $_[0] . ':' . ++$reads};
        ok(!eval {SLT::StorageCAS::collect_serializer(); 1}, 'drift between collection and verification refuses receipt');
    }
    is($effects, 0, 'adversarial runtime checks never invoke effects');
}
done_testing();
