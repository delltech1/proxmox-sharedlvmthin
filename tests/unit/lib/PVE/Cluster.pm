package PVE::Cluster;

use strict;
use warnings;

sub check_cfs_quorum { return 1; }

sub cfs_lock_storage {
    my ($storeid, $timeout, $code) = @_;
    return $code->();
}

1;
