package PVE::Storage;

use strict;
use warnings;

sub APIVER { return 15; }
sub config { return {}; }
sub storage_config { die "unexpected unmocked PVE::Storage::storage_config in portable unit test\n"; }

1;
