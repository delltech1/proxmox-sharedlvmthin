#!/usr/bin/perl

use strict;
use warnings;

use lib '/usr/share/perl5';
use PVE::QemuServer::Network ();

my $failures = 0;

sub run_case {
    my ($name, $conf, $answers, $expected, $warning_count) = @_;
    my @warnings;
    no warnings 'redefine';
    local *PVE::QemuServer::Network::log_warn = sub { push @warnings, $_[0] };
    local *PVE::QemuServer::Network::mon_cmd = sub {
        my ($vmid, $command, %args) = @_;
        die "unexpected QMP command\n" if $vmid != 999999 || $command ne 'qom-get';
        my ($net) = ($args{path} // '') =~ m{/machine/peripheral/(net\d+)$};
        die "unexpected QMP path\n" if !defined($net) || !exists($answers->{$net});
        my $answer = $answers->{$net};
        die "$answer->{error}\n" if exists($answer->{error});
        return $answer->{value};
    };
    my $actual = PVE::QemuServer::Network::get_nets_host_mtu(999999, $conf);
    if ($actual ne $expected || scalar(@warnings) != $warning_count) {
        print "CASE=$name RESULT=FAIL ACTUAL=$actual EXPECTED=$expected ",
            "WARNINGS=", scalar(@warnings), " EXPECTED_WARNINGS=$warning_count\n";
        $failures++;
        return;
    }
    print "CASE=$name RESULT=PASS VALUE=$actual WARNINGS=", scalar(@warnings), "\n";
}

my $virtio0 = 'virtio=02:00:00:00:00:10,bridge=vmbr0';
my $virtio1 = 'virtio=02:00:00:00:00:11,bridge=vmbr1';
my $e1000 = 'e1000=02:00:00:00:00:12,bridge=vmbr0';
my $virtio_down = 'virtio=02:00:00:00:00:13,bridge=vmbr0,link_down=1';
my $virtio_nat = 'virtio=02:00:00:00:00:14';
my $virtio_policy = 'virtio=02:00:00:00:00:15,bridge=vmbr0,tag=123,firewall=1,rate=10';

run_case('no-nic', {}, {}, '', 0);
run_case('e1000-only', { net0 => $e1000 }, {}, '', 0);
run_case('virtio-1500', { net0 => $virtio0 }, { net0 => { value => 1500 } }, 'net0=1500', 0);
run_case('virtio-zero', { net0 => $virtio0 }, { net0 => { value => 0 } }, 'net0=0', 0);
run_case('virtio-qmp-error', { net0 => $virtio0 }, { net0 => { error => 'injected' } }, '', 1);
run_case('virtio-undefined', { net0 => $virtio0 }, { net0 => { value => undef } }, '', 1);
run_case(
    'two-virtio-partial',
    { net0 => $virtio0, net1 => $virtio1 },
    { net0 => { value => 1500 }, net1 => { error => 'injected' } },
    'net0=1500', 1,
);
run_case(
    'two-virtio-complete',
    { net0 => $virtio0, net1 => $virtio1 },
    { net0 => { value => 1500 }, net1 => { value => 9000 } },
    'net0=1500,net1=9000', 0,
);
run_case(
    'two-virtio-partial-undefined',
    { net0 => $virtio0, net1 => $virtio1 },
    { net0 => { value => 1500 }, net1 => { value => undef } },
    'net0=1500', 1,
);
run_case(
    'mixed-e1000-virtio',
    { net0 => $e1000, net1 => $virtio1 },
    { net1 => { value => 9000 } },
    'net1=9000', 0,
);
run_case('virtio-link-down', { net0 => $virtio_down }, { net0 => { value => 1500 } }, 'net0=1500', 0);
run_case('virtio-no-bridge-zero', { net0 => $virtio_nat }, { net0 => { value => 0 } }, 'net0=0', 0);
run_case('virtio-policy-options', { net0 => $virtio_policy }, { net0 => { value => 1500 } }, 'net0=1500', 0);

if ($failures) {
    print "MTU_FIXTURE=FAIL FAILURES=$failures\n";
    exit 1;
}
print "MTU_FIXTURE=PASS CASES=13\n";
exit 0;
