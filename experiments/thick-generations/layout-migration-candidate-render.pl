#!/usr/bin/perl -T
# Read-only candidate serializer. This helper never calls a cluster writer.
BEGIN {
    die "REFUSED: deterministic fresh interpreter required\n"
        if ($ENV{PERL_HASH_SEED} // '') ne '0' || ($ENV{PERL_PERTURB_KEYS} // '') ne '0'
        || exists($ENV{PERL5LIB}) || exists($ENV{PERL5OPT});
    $SIG{__WARN__} = sub { die "REFUSED: warning: $_[0]" };
}
use strict;
use warnings;
use Cwd qw(realpath);
use Digest::SHA qw(sha256_hex);
use Fcntl qw(:DEFAULT :mode);
use JSON::PP ();
use Scalar::Util qw(refaddr);
use Storable qw(dclone);
my $json = JSON::PP->new->canonical(1)->ascii(1);
sub need { die "REFUSED: $_[1]\n" if !$_[0] }
sub read_file {
    my ($path) = @_;
    sysopen(my $fh, $path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK) or die "REFUSED: read failed\n";
    my @before = stat($fh);
    need(S_ISREG($before[2]) && $before[4] == 0 && !($before[2] & 022), 'unsafe file inode');
    local $/; my $raw = <$fh>; my @after = stat($fh); close($fh) or die "REFUSED: close failed\n";
    need(join(':', @before[0,1,2,3,4,7,9,10]) eq join(':', @after[0,1,2,3,4,7,9,10])
         && length($raw) == $before[7], 'file changed');
    return $raw;
}
sub modules {
    return [map {
        my $path = realpath($INC{$_}); need(defined($path) && $path =~ m{\A/[A-Za-z0-9_.+/-]+\z}, 'module path invalid');
        {name => $_, path => $path, sha256 => sha256_hex(read_file($path))}
    } sort grep { /\.pm\z/ } keys %INC];
}
sub registry {
    return {map { $_ => PVE::Storage::Plugin->lookup($_) } sort @{PVE::Storage::Plugin->lookup_types()}};
}
need($> == 0 && ${^TAINT} && @ARGV == 1, 'root/taint/exact root required');
my ($root) = $ARGV[0] =~ m{\A(/run/slt-candidate-render-[0-9a-f]{32})\z};
need(defined($root), 'root path invalid');
my @st = lstat($root);
need(@st && S_ISDIR($st[2]) && $st[4] == 0 && ($st[2] & 0777) == 0700, 'root inode invalid');
my $lib = "$root/payload/usr/share/perl5";
unshift @INC, $lib;
need(!exists($INC{'PVE/Storage.pm'}) && !exists($INC{'PVE/Storage/Custom/SharedLvmThinPlugin.pm'}), 'plugin already loaded');
# Match the production CAS startup order exactly. PVE::Storage discovers and
# loads the candidate plugin from the prepended private payload. Loading the
# plugin first changes Perl hash insertion order and can produce bytewise
# different (although semantically equal) list serialization.
require PVE::Storage;
need(exists($INC{'PVE/Storage/Custom/SharedLvmThinPlugin.pm'}), 'candidate plugin was not discovered');
need(realpath($INC{'PVE/Storage/Custom/SharedLvmThinPlugin.pm'}) eq "$lib/PVE/Storage/Custom/SharedLvmThinPlugin.pm", 'candidate plugin not selected');
need(PVE::Storage::Plugin->lookup('sharedlvmthin') eq 'PVE::Storage::Custom::SharedLvmThinPlugin', 'candidate registry differs');
my $raw = read_file("$root/baseline.cfg");
need(read_file('/etc/pve/storage.cfg') eq $raw, 'live read-only baseline differs');
# The production CAS uses PVE::Storage::config(), whose normalized in-memory
# representation is not byte-order equivalent to a direct parse_config() for
# set-like properties such as `nodes`. Use the same read-only entry point here
# so the pre-PREPARE target is byte-identical to the later locked serializer.
my $cfg = PVE::Storage::config();
my $before_modules = modules(); my $before_registry = registry();
my %method_refs = map { $_ => refaddr(PVE::Storage::Plugin->can($_)) } qw(parse_config write_config lookup lookup_types);
my $baseline_digest = $cfg->{digest};
my $copy = dclone($cfg);
for my $id (qw(slt-scale-thick slt-scale-thin slt-tg-thick slt-tg-thin)) {
    my $section = $copy->{ids}->{$id};
    need(ref($section) eq 'HASH' && ($section->{type} // '') eq 'sharedlvmthin'
         && !exists($section->{'slt-vg-layout'}), 'delta precondition differs');
    $section->{'slt-vg-layout'} = 'mixed';
}
my $expected_semantics = dclone($copy); delete $expected_semantics->{digest};
my $target = PVE::Storage::Plugin->write_config('storage.cfg', $copy);
my $parsed = PVE::Storage::Plugin->parse_config('storage.cfg', $target);
my $semantics = dclone($parsed); delete $semantics->{digest};
need($json->encode($semantics) eq $json->encode($expected_semantics), 'target semantic roundtrip differs');
need(PVE::Storage::Plugin->write_config('storage.cfg', dclone($parsed)) eq $target, 'target rerender differs');
need(read_file("$root/baseline.cfg") eq $raw && read_file('/etc/pve/storage.cfg') eq $raw, 'baseline changed');
need($json->encode(modules()) eq $json->encode($before_modules), 'module identity drift');
need($json->encode(registry()) eq $json->encode($before_registry), 'registry drift');
need(refaddr(PVE::Storage::Plugin->can($_)) == $method_refs{$_}, 'serializer reference drift') for keys %method_refs;
print $json->encode({schema => 'slt-candidate-render/v1', classification => 'READ_ONLY_CANDIDATE_TARGET',
    authorization => 'NONE', mutation_performed => JSON::PP::false, runtime_qualified => JSON::PP::false,
    baseline_sha256 => sha256_hex($raw), baseline_native_digest => $baseline_digest,
    target_hex => unpack('H*', $target), target_sha256 => sha256_hex($target),
    perl_sha256 => sha256_hex(read_file(realpath($^X))), perl_hash_seed => '0', perl_perturb_keys => '0',
    modules => $before_modules, registry => $before_registry}), "\n";
