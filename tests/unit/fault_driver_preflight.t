#!/usr/bin/perl
use strict;
use warnings;

use FindBin qw($Bin);
use Cwd qw(realpath);
use Digest::SHA qw(sha256_hex);
use File::Basename qw(dirname);
use Test::More;
use lib "$Bin/../../usr/share/perl5", "$Bin/lib";

use PVE::Storage::Custom::SharedLvmThinPlugin;

my $root = "$Bin/../..";

sub run_driver {
    my (%args) = @_;
    my $mutation_calls = 0;
    my $seen_cfg;
    local @ARGV = @{$args{argv}};
    local $ENV{PERL5LIB};
    local $ENV{PERL5OPT};
    delete $ENV{PERL5LIB};
    delete $ENV{PERL5OPT};
    no warnings 'redefine';
    local $FindBin::Bin = dirname(realpath($args{path}));
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_verify_storage_identity = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_no_vg_intent = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_list_volumes_scoped = sub { {} };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_anchor = sub {
        return ({ phase => 'MATERIALIZED', head => 'head' }, { lv_size => 4096 }, 'anchor');
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_frontend_present = sub {
        return $args{frontend_present};
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_verify_frontend = sub { 1 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_filesystem_path = sub {
        return '/dev/mapper/sltg-0123456789abcdef01234567';
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_command_deadline = sub { 30 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_frontend_open_count = sub {
        return $args{open_count} // 0;
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_volume_snapshot = sub {
        $mutation_calls++;
        $seen_cfg = $_[1];
        die "MUTATION_REACHED\n";
    };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_volume_resize = sub {
        $mutation_calls++;
        $seen_cfg = $_[1];
        die "MUTATION_REACHED\n";
    };

    my $result = do $args{path};
    my $error = $@;
    $error = $! if !defined($result) && $error eq '';
    return ($error, $mutation_calls, $seen_cfg);
}

my @identity = (
    '--store-id', 'thick-test', '--volume', 'vm-900001-disk-0',
    '--vg', 'testvg', '--vg-uuid', 'vg-uuid', '--pv-uuid', 'pv-uuid',
    '--wwid', '3600abcd',
);

my $candidate_perl = realpath("$root/usr/share/perl5");
my $plugin_key = 'PVE/Storage/Custom/SharedLvmThinPlugin.pm';
open(my $plugin_fh, '<', "$candidate_perl/$plugin_key") or die $!;
binmode($plugin_fh);
my $plugin_sha = Digest::SHA->new(256)->addfile($plugin_fh)->hexdigest;
close($plugin_fh) or die $!;
my @candidate_code = ("$plugin_key=$plugin_sha");
for my $key (sort grep { m{^PVE/SharedLvm} } keys %INC) {
    open(my $fh, '<', "$candidate_perl/$key") or die $!;
    binmode($fh);
    push @candidate_code, "$key=" . Digest::SHA->new(256)->addfile($fh)->hexdigest;
    close($fh) or die $!;
}
my $candidate_code_sha = sha256_hex(join("\n", @candidate_code) . "\n");
push @identity,
    '--plugin-sha256', $plugin_sha,
    '--candidate-code-sha256', $candidate_code_sha;

my ($error, $calls) = run_driver(
    path => "$root/experiments/thick-generations/fault-driver.pl",
    frontend_present => 1,
    open_count => 1,
    argv => [
        '--point', 'C10', '--operation', 'SNAPSHOT', '--snapshot', 'snap1',
        @identity, '--ack', 'DISPOSABLE-DATA-WILL-BE-LEFT-INCOMPLETE',
    ],
);
like($error, qr/requires an active zero-open frontend/,
    'C10 driver rejects an open frontend before mutation');
is($calls, 0, 'C10 open-frontend refusal dispatches no snapshot mutation');

my $cfg;
($error, $calls, $cfg) = run_driver(
    path => "$root/experiments/thick-generations/fault-driver.pl",
    frontend_present => 1,
    open_count => 0,
    argv => [
        '--point', 'C10', '--operation', 'SNAPSHOT', '--snapshot', 'snap1',
        @identity, '--ack', 'DISPOSABLE-DATA-WILL-BE-LEFT-INCOMPLETE',
    ],
);
like($error, qr/MUTATION_REACHED/, 'C10 exact zero-open fixture reaches the mocked operation');
is($calls, 1, 'C10 driver invokes exactly one snapshot operation');
is($cfg->{'slt-tg-online-materialization'}, 'synchronous',
    'C10 driver cannot hand off to an asynchronous worker');

($error, $calls) = run_driver(
    path => "$root/experiments/thick-generations/resize-fault-driver.pl",
    frontend_present => 0,
    argv => [
        '--point', 'R0', '--size-bytes', '8192', @identity,
        '--ack', 'DISPOSABLE-RESIZE-WILL-BE-LEFT-INCOMPLETE',
    ],
);
like($error, qr/requires an exact published frontend/,
    'R0 driver rejects an absent frontend before mutation');
is($calls, 0, 'R0 absent-frontend refusal dispatches no resize mutation');

($error, $calls) = run_driver(
    path => "$root/experiments/thick-generations/resize-fault-driver.pl",
    frontend_present => 1,
    open_count => 1,
    argv => [
        '--point', 'R0', '--size-bytes', '8192', @identity,
        '--ack', 'DISPOSABLE-RESIZE-WILL-BE-LEFT-INCOMPLETE',
    ],
);
like($error, qr/requires an active zero-open frontend/,
    'R0 driver rejects an open frontend before mutation');
is($calls, 0, 'R0 open-frontend refusal dispatches no resize mutation');

done_testing();
