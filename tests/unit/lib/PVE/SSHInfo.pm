package PVE::SSHInfo;

use strict;
use warnings;

sub ssh_info_to_command {
    my ($info, @extra) = @_;
    return ['/usr/bin/ssh', @extra, "root\@$info->{ip}"];
}

1;
