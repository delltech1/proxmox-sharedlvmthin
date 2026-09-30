package PVE::Storage::Plugin;

use strict;
use warnings;

sub parse_lvm_name {
    my ($name, $noerr) = @_;
    if (!defined($name) || length($name) < 2) {
        return undef if $noerr;
        die "lvm name is shorter than 2 characters\n";
    }
    if ($name !~ /^[a-z0-9][a-z0-9_.-]*[a-z0-9]$/i) {
        return undef if $noerr;
        die "lvm name contains illegal characters\n";
    }
    return $name;
}

1;
