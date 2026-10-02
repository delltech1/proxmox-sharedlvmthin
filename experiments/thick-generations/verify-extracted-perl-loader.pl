#!/usr/bin/perl
# SPDX-License-Identifier: GPL-3.0-only

use strict;
use warnings;
use Cwd qw(abs_path);

my $root = shift // die "usage: $0 EXTRACTED-PERL5-ROOT\n";
$root = abs_path($root) // die "candidate Perl root does not exist\n";
unshift @INC, $root;

require PVE::Storage::Custom::SharedLvmThinPlugin;

my @candidate = sort grep {
    $_ eq 'PVE/Storage/Custom/SharedLvmThinPlugin.pm'
        || m{^PVE/SharedLvm[^/]*[.]pm$}
} keys %INC;
die "candidate plugin was not loaded\n"
    if !grep { $_ eq 'PVE/Storage/Custom/SharedLvmThinPlugin.pm' } @candidate;

for my $module (@candidate) {
    my $loaded = abs_path($INC{$module}) // die "loaded module disappeared: $module\n";
    die "candidate module escaped extracted payload: $module => $loaded\n"
        if index($loaded, "$root/") != 0;
    print "$module=$loaded\n";
}
print "EXTRACTED_PERL_LOADER=PASS\n";
