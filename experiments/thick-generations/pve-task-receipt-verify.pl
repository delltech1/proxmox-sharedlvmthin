#!/usr/bin/perl
use strict;
use warnings;

use JSON::PP qw(decode_json);

@ARGV == 5 or die "usage: $0 BEFORE.json AFTER.json NODE VMID TYPE\n";
my ($before_path, $after_path, $node, $vmid, $type) = @ARGV;

$node =~ /^[A-Za-z0-9][A-Za-z0-9.-]*$/
    or die "unsafe PVE node identity\n";
$vmid =~ /^[1-9][0-9]{2,8}$/ or die "invalid VMID\n";
$type =~ /^[a-z][a-z0-9_-]*$/ or die "invalid task type\n";

sub load_rows {
    my ($path) = @_;
    open(my $fh, '<', $path) or die "cannot read task evidence: $!\n";
    local $/;
    my $decoded = eval { decode_json(<$fh>) };
    die "malformed task evidence\n" if $@ || ref($decoded) ne 'ARRAY';
    my %seen;
    for my $row (@$decoded) {
        die "malformed task evidence row\n" if ref($row) ne 'HASH';
        my $upid = $row->{upid} // '';
        die "task evidence contains an invalid UPID\n"
            if $upid !~ /^UPID:[A-Za-z0-9][A-Za-z0-9.-]*:/;
        die "task evidence contains a duplicate UPID\n" if $seen{$upid}++;
    }
    return $decoded;
}

my $before = load_rows($before_path);
my $after = load_rows($after_path);
my %old = map { $_->{upid} => 1 } @$before;
my @new = grep {
    !$old{$_->{upid}}
        && ($_->{type} // '') eq $type
        && "" . ($_->{id} // '') eq "$vmid"
        && ($_->{node} // '') eq $node
} @$after;

die "task correlation is ambiguous\n" if @new != 1;
my $row = $new[0];
die "task did not terminate successfully\n"
    if ($row->{status} // '') ne 'OK';
my $upid = $row->{upid};
die "task UPID does not match the reported node\n"
    if $upid !~ /^UPID:\Q$node\E:/;

print "$upid\n";
