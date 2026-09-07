package PVE::Storage::LVMPlugin;

use strict;
use warnings;

sub lvm_list_volumes {
    die "unexpected unmocked PVE::Storage::LVMPlugin::lvm_list_volumes in portable unit test\n";
}

sub lvm_vgs {
    die "unexpected unmocked PVE::Storage::LVMPlugin::lvm_vgs in portable unit test\n";
}

1;
