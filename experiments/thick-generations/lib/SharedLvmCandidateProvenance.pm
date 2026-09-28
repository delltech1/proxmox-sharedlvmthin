package SharedLvmCandidateProvenance;

use strict;
use warnings;

use Cwd qw(realpath);
use Digest::SHA qw(sha256_hex);
use Exporter qw(import);

our @EXPORT_OK = qw(verify_candidate_provenance);

sub _file_sha256 {
    my ($path, $label) = @_;
    open(my $fh, '<', $path) or die "cannot open $label '$path': $!\n";
    binmode($fh);
    my $digest = Digest::SHA->new(256)->addfile($fh)->hexdigest;
    close($fh) or die "cannot close $label '$path': $!\n";
    return $digest;
}

sub verify_candidate_provenance {
    my ($bin, $expected_plugin_digest, $expected_code_digest) = @_;
    die "invalid expected plugin SHA-256\n"
        if ($expected_plugin_digest // '') !~ /^[0-9a-f]{64}$/;
    die "invalid expected candidate code SHA-256\n"
        if ($expected_code_digest // '') !~ /^[0-9a-f]{64}$/;

    my $root = realpath("$bin/../..");
    die "candidate checkout root is unavailable\n" if !defined($root);
    my $candidate_perl = realpath("$root/usr/share/perl5");
    die "candidate Perl root is unavailable\n" if !defined($candidate_perl);
    die "candidate Perl root escaped the candidate checkout\n"
        if index($candidate_perl, "$root/") != 0;

    my $plugin_key = 'PVE/Storage/Custom/SharedLvmThinPlugin.pm';
    die "candidate plugin may not be a symlink\n"
        if -l "$candidate_perl/$plugin_key";
    my $plugin_path = realpath($INC{$plugin_key} // '');
    my $expected_plugin = "$candidate_perl/$plugin_key";
    die "candidate plugin provenance mismatch\n"
        if !defined($plugin_path) || $plugin_path ne $expected_plugin;
    my $plugin_digest = _file_sha256($plugin_path, 'candidate plugin');
    die "candidate plugin SHA-256 mismatch\n"
        if $plugin_digest ne $expected_plugin_digest;

    my @candidate_modules;
    my @candidate_code = ("$plugin_key=$plugin_digest");
    for my $key (sort keys %INC) {
        next if $key !~ m{^PVE/SharedLvm};
        die "candidate module '$key' may not be a symlink\n"
            if -l "$candidate_perl/$key";
        my $loaded = realpath($INC{$key} // '');
        my $expected = "$candidate_perl/$key";
        die "candidate module '$key' has no canonical path\n" if !defined($loaded);
        die "candidate module '$key' escaped its exact candidate path\n"
            if $loaded ne $expected;
        my $module_digest = _file_sha256($loaded, "candidate module '$key'");
        push @candidate_modules, "$key=$loaded";
        push @candidate_code, "$key=$module_digest";
    }
    die "candidate safety modules were not loaded\n" if !@candidate_modules;
    my $candidate_code_digest = sha256_hex(join("\n", @candidate_code) . "\n");
    die "candidate code-set SHA-256 mismatch\n"
        if $candidate_code_digest ne $expected_code_digest;

    print "CANDIDATE_ROOT=$root\n";
    print "PLUGIN_PATH=$plugin_path\n";
    print "PLUGIN_SHA256=$plugin_digest\n";
    print "CANDIDATE_CODE_SHA256=$candidate_code_digest\n";
    print "CANDIDATE_MODULE=$_\n" for @candidate_modules;
    return 1;
}

1;
