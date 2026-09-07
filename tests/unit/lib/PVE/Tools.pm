package PVE::Tools;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(run_command trim);

sub run_command {
    die "unexpected unmocked PVE::Tools::run_command in portable unit test\n";
}

sub trim {
    my ($value) = @_;
    return undef if !defined($value);
    $value =~ s/^\s+|\s+$//g;
    return $value;
}

1;
