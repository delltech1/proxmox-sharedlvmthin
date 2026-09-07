package PVE::Storage::Plugin;

use strict;
use warnings;

sub parse_lvm_name {
    my ($name) = @_;
    die "invalid LVM name\n"
        if !defined($name) || $name !~ /^[A-Za-z0-9][A-Za-z0-9+_.-]*$/;
    return 1;
}

1;
