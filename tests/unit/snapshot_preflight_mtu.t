use strict;
use warnings;
use Test::More;
use lib 'usr/share/perl5';
use PVE::SharedLvmThinSnapshotPreflight;

sub evaluate {
    my ($conf, $raw, %options) = @_;
    return PVE::SharedLvmThinSnapshotPreflight::evaluate_ram_network(
        conf => $conf,
        parse => sub {
            die "parse failed\n" if $options{parse_error};
            my ($value) = @_;
            return if $options{parse_undef};
            return { model => $value =~ /^virtio=/ ? 'virtio' : 'e1000' };
        },
        probe => sub {
            die "probe failed\n" if $options{probe_error};
            return $raw;
        },
    );
}

ok(!evaluate({}, '')->{ok}, 'no-NIC result is refused');
ok(!evaluate({ net0 => 'e1000=x' }, '')->{ok}, 'e1000-only result is refused');
ok(evaluate({ net0 => 'virtio=x' }, 'net0=1500')->{ok}, 'one exact VirtIO MTU passes');
ok(evaluate({ net0 => 'virtio=x' }, 'net0=0')->{ok}, 'upstream zero omit semantic passes');
ok(evaluate({ net0 => 'virtio=x', net1 => 'virtio=y', net2 => 'e1000=z' },
    'net0=1500,net1=9000')->{ok}, 'two exact VirtIO keys with a mixed model pass');

for my $case (
    ['empty', { net0 => 'virtio=x' }, ''],
    ['undefined', { net0 => 'virtio=x' }, undef],
    ['partial', { net0 => 'virtio=x', net1 => 'virtio=y' }, 'net0=1500'],
    ['duplicate', { net0 => 'virtio=x' }, 'net0=1500,net0=1500'],
    ['unexpected', { net0 => 'virtio=x' }, 'net0=1500,net1=1500'],
    ['malformed value', { net0 => 'virtio=x' }, 'net0=-1'],
    ['malformed delimiter', { net0 => 'virtio=x' }, 'net0=1500,'],
) {
    my ($name, $conf, $raw) = @$case;
    ok(!evaluate($conf, $raw)->{ok}, "$name result is refused");
}
ok(!evaluate({ net0 => 'virtio=x' }, 'net0=1500', parse_error => 1)->{ok},
    'parse exception is refused');
ok(!evaluate({ net0 => 'virtio=x' }, 'net0=1500', parse_undef => 1)->{ok},
    'undefined parse result is refused');
ok(!evaluate({ net0 => 'virtio=x' }, 'net0=1500', probe_error => 1)->{ok},
    'probe exception is refused');

ok(PVE::SharedLvmThinSnapshotPreflight::evaluate_current_runtime_metadata({})->{ok},
    'ordinary current configuration has no stale runtime metadata');
for my $key (qw(vmstate runningmachine runningcpu running-nets-host-mtu)) {
    my $result = PVE::SharedLvmThinSnapshotPreflight::evaluate_current_runtime_metadata(
        { $key => $key eq 'running-nets-host-mtu' ? 'net0=1500' : 'value' },
    );
    ok(!$result->{ok}, "stale current $key is refused");
    like($result->{blocked}->[0], qr/\Q$key\E/, "$key refusal identifies the property");
}

done_testing();
