#!/usr/bin/perl -T
use strict;
use warnings;
# Compatibility locator only: the installed package owns the implementation.
BEGIN {
    die "root required\n" if $> != 0 || $< != 0;
    %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
}
exec '/usr/bin/perl', '-T', '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-dispatch', @ARGV;
die "cannot execute installed dispatcher: $!\n";
