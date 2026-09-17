#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinPeerAudit qw(select_peer_nodes evaluate_peer_mapper_evidence);

my $members = {
    node1 => {online => 1, ip => '2001:db8::1'},
    node2 => {online => 1, ip => '2001:db8::2'},
};
my $peers = select_peer_nodes(members => $members, local_node => 'node1');
is_deeply($peers, [{node => 'node2', ip => '2001:db8::2'}],
    'two-node cluster without qdevice audits its exact peer');

my %large;
for my $index (1 .. 500) {
    $large{"node$index"} = {online => 1, ip => sprintf('2001:db8::%x', $index)};
}
$peers = select_peer_nodes(members => \%large, local_node => 'node1');
is(scalar(@$peers), 499, 'peer selection has no small-cluster hardcoded ceiling');

my $mapper = 'vg--one-sltp--100-tpool';
my $uuid = 'LVM-abcdef-tpool';
my $r = evaluate_peer_mapper_evidence(mapper => $mapper, mapper_uuid => $uuid,
    evidence => [{node => 'node2', line => "BASTRIX_REMOTE_THIN_V1|ABSENT|$mapper|$uuid"}]);
ok($r->{safe}, 'exact peer absence is safe');
$r = evaluate_peer_mapper_evidence(mapper => $mapper, mapper_uuid => $uuid,
    evidence => [{node => 'node2', line => "BASTRIX_REMOTE_THIN_V1|PRESENT|$mapper|$uuid"}]);
is($r->{state}, 'REMOTE_CONFLICT', 'exact peer presence is a conflict');

for my $case (
    ['offline peer', sub {
        my %copy = map { $_ => {%{$members->{$_}}} } keys %$members;
        $copy{node2}->{online} = 0;
        select_peer_nodes(members => \%copy, local_node => 'node1');
    }],
    ['unknown scoped peer', sub {
        select_peer_nodes(members => $members, local_node => 'node1', nodes => 'node1,node3');
    }],
    ['malformed response', sub {
        evaluate_peer_mapper_evidence(mapper => $mapper, mapper_uuid => $uuid,
            evidence => [{node => 'node2', line => 'ABSENT'}]);
    }],
    ['duplicate peer', sub {
        evaluate_peer_mapper_evidence(mapper => $mapper, mapper_uuid => $uuid,
            evidence => [
                {node => 'node2', line => "BASTRIX_REMOTE_THIN_V1|ABSENT|$mapper|$uuid"},
                {node => 'node2', line => "BASTRIX_REMOTE_THIN_V1|ABSENT|$mapper|$uuid"},
            ]);
    }],
) {
    my $ok = eval { $case->[1]->(); 1 };
    ok(!$ok, "$case->[0] fails closed");
}

done_testing();
