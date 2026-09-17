# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinPeerAudit;

use strict;
use warnings;
use Exporter qw(import);

our @EXPORT_OK = qw(select_peer_nodes evaluate_peer_mapper_evidence);

sub select_peer_nodes {
    my (%e) = @_;
    die "cluster membership is unavailable\n" if ref($e{members}) ne 'HASH';
    die "local node is malformed\n"
        if !defined($e{local_node}) || $e{local_node} !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
    my %selected;
    if (ref($e{nodes}) eq 'HASH') {
        %selected = map { $_ => 1 } grep { $e{nodes}->{$_} } keys %{$e{nodes}};
    } elsif (defined($e{nodes}) && !ref($e{nodes}) && $e{nodes} ne '') {
        %selected = map { $_ => 1 } split(/,/, $e{nodes});
    } elsif (defined($e{nodes}) && ref($e{nodes})) {
        die "storage node scope is malformed\n";
    } else {
        %selected = map { $_ => 1 } keys %{$e{members}};
    }
    delete $selected{$e{local_node}};
    die "no peer nodes are configured\n" if !keys(%selected);

    my @peers;
    for my $node (sort keys %selected) {
        die "peer node name is malformed\n" if $node !~ /^([A-Za-z0-9][A-Za-z0-9_.-]*)$/;
        my $member = $e{members}->{$node};
        die "peer '$node' is absent from cluster membership\n" if ref($member) ne 'HASH';
        die "peer '$node' is not positively online\n" if !$member->{online};
        my $ip = $member->{ip} // '';
        die "peer '$node' has no valid cluster address\n" if $ip !~ /^([A-Fa-f0-9:.]+)$/;
        push @peers, {node => $node, ip => $1};
    }
    return \@peers;
}

sub evaluate_peer_mapper_evidence {
    my (%e) = @_;
    die "mapper name is malformed\n"
        if !defined($e{mapper}) || $e{mapper} !~ /^[A-Za-z0-9_.+-]+$/;
    die "mapper UUID is malformed\n"
        if !defined($e{mapper_uuid}) || $e{mapper_uuid} !~ /^LVM-[A-Za-z0-9]+-tpool$/;
    die "peer evidence is unavailable\n" if ref($e{evidence}) ne 'ARRAY' || !@{$e{evidence}};
    my %seen;
    for my $item (@{$e{evidence}}) {
        die "peer evidence item is malformed\n" if ref($item) ne 'HASH';
        my $node = $item->{node} // '';
        die "peer evidence node is malformed or duplicated\n"
            if $node !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/ || $seen{$node}++;
        my $line = $item->{line} // '';
        die "peer '$node' returned ambiguous mapper evidence\n"
            if $line !~ /^BASTRIX_REMOTE_THIN_V1\|(ABSENT|PRESENT)\|\Q$e{mapper}\E\|\Q$e{mapper_uuid}\E$/;
        return {safe => 0, state => 'REMOTE_CONFLICT', node => $node,
            reason => "remote thin-pool mapper is present on peer '$node'"}
            if $1 eq 'PRESENT';
    }
    return {safe => 1, state => 'ALL_PEERS_ABSENT',
        reason => 'every configured online peer positively reports the exact mapper absent'};
}

1;
