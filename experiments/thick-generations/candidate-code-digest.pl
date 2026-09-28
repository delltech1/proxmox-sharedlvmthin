#!/usr/bin/perl
use strict;
use warnings;

BEGIN {
    die "candidate digest requires PERL5LIB and PERL5OPT to be absent\n"
        if exists($ENV{PERL5LIB}) || exists($ENV{PERL5OPT});
}

use FindBin ();
use lib "$FindBin::Bin/../../usr/share/perl5";
use lib "$FindBin::Bin/lib";
use Digest::SHA qw(sha256_hex);
use PVE::Storage::Custom::SharedLvmThinPlugin;

sub file_digest {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "cannot read '$path': $!\n";
    binmode($fh);
    my $digest = Digest::SHA->new(256)->addfile($fh)->hexdigest;
    close($fh) or die "cannot close '$path': $!\n";
    return $digest;
}

my $plugin_key = 'PVE/Storage/Custom/SharedLvmThinPlugin.pm';
my $plugin = file_digest($INC{$plugin_key});
my @code = ("$plugin_key=$plugin");
for my $key (sort keys %INC) {
    next if $key !~ m{^PVE/SharedLvm};
    push @code, "$key=" . file_digest($INC{$key});
}
die "candidate safety modules were not loaded\n" if @code < 2;
my $code = sha256_hex(join("\n", @code) . "\n");
print "PLUGIN_SHA256=$plugin\n";
print "CANDIDATE_CODE_SHA256=$code\n";
