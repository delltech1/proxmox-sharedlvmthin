package PVE::SharedLvmThinSnapshotPreflight;

use strict;
use warnings;

sub evaluate_current_runtime_metadata {
    my ($conf) = @_;
    die "runtime metadata evaluator requires a configuration hash\n"
        if ref($conf) ne 'HASH';

    my @blocked;
    for my $key (qw(vmstate runningmachine runningcpu running-nets-host-mtu)) {
        push @blocked, "stale current runtime property '$key' is present"
            if defined($conf->{$key});
    }
    return { ok => @blocked ? 0 : 1, blocked => \@blocked };
}

sub evaluate_ram_network {
    my (%args) = @_;
    my $conf = $args{conf};
    my $parse = $args{parse};
    my $probe = $args{probe};
    die "RAM network evaluator requires a configuration hash\n"
        if ref($conf) ne 'HASH';
    die "RAM network evaluator requires parse and probe callbacks\n"
        if ref($parse) ne 'CODE' || ref($probe) ne 'CODE';

    my (@virtio, @blocked);
    for my $key (sort keys %$conf) {
        next if $key !~ /^net\d+$/;
        my $net = eval { $parse->($conf->{$key}) };
        my $error = $@;
        if ($error || ref($net) ne 'HASH') {
            $error =~ s/\s+$// if $error;
            push @blocked, "cannot parse '$key'" . ($error ? ": $error" : '');
            next;
        }
        push @virtio, $key if ($net->{model} // '') eq 'virtio';
    }
    if (!@virtio) {
        push @blocked,
            'upstream would write an empty running-nets-host-mtu value; '
            . 'configure at least one working VirtIO NIC or use a disk-only snapshot';
        return { ok => 0, blocked => \@blocked, virtio => \@virtio, observed => {} };
    }

    my $raw = eval { $probe->() };
    my $probe_error = $@;
    if ($probe_error) {
        $probe_error =~ s/\s+$//;
        push @blocked, "host-MTU QMP probe failed: $probe_error";
        return { ok => 0, blocked => \@blocked, virtio => \@virtio, observed => {} };
    }

    my %expected = map { $_ => 1 } @virtio;
    my %observed;
    if (!defined($raw) || $raw eq '') {
        push @blocked, 'host-MTU QMP probe returned an empty value';
    } else {
        for my $entry (split(/,/, $raw, -1)) {
            if ($entry !~ /^(net\d+)=(\d+)$/) {
                push @blocked, "host-MTU QMP probe returned malformed entry '$entry'";
                next;
            }
            my ($key, $value) = ($1, $2);
            if (!$expected{$key}) {
                push @blocked, "host-MTU QMP probe returned unexpected key '$key'";
                next;
            }
            if (exists($observed{$key})) {
                push @blocked, "host-MTU QMP probe returned duplicate key '$key'";
                next;
            }
            # Upstream explicitly defines zero as a valid request to omit the
            # host_mtu parameter.  Preserve that exact semantic; completeness,
            # not positivity, is the safety property here.
            $observed{$key} = $value;
        }
    }
    my @missing = grep { !exists($observed{$_}) } @virtio;
    push @blocked, 'host-MTU QMP probe returned no value for ' . join(',', @missing)
        if @missing;

    return {
        ok => @blocked ? 0 : 1,
        blocked => \@blocked,
        virtio => \@virtio,
        observed => \%observed,
    };
}

1;
