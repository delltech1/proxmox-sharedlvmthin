use strict;
use warnings;
use Test::More;
use JSON::PP qw(decode_json);
use lib 'tests/unit/lib', 'usr/share/perl5';
use PVE::Cluster;
use PVE::Storage;
use PVE::Tools ();
use PVE::SSHInfo;
use PVE::SharedLvmThinGuardInventory ();
use PVE::SharedLvmThinPeerAudit ();

BEGIN {
    $INC{'PVE/INotify.pm'} = 1;
    $INC{'PVE/QemuConfig.pm'} = 1;
    $INC{'PVE/QemuServer.pm'} = 1;
}

sub run_fixture {
    my ($ssh_fails) = @_;
    pipe(my $reader, my $writer) or die "pipe: $!";
    my $pid = fork();
    die "fork: $!" if !defined($pid);
    if (!$pid) {
        close $reader;
        open STDOUT, '>&', $writer or die "stdout: $!";
        close $writer;
        no warnings 'redefine';
        local *PVE::INotify::nodename = sub { 'local' };
        local *PVE::QemuServer::check_running = sub { 0 };
        local *PVE::Cluster::check_cfs_quorum = sub { 1 };
        local *PVE::Cluster::cfs_update = sub { 1 };
        local *PVE::Cluster::get_members = sub { {} };
        local *PVE::Storage::config = sub { { ids => { test => {
            type => 'sharedlvmthin', 'slt-allocation-mode' => 'thin',
            'slt-vgname' => 'testvg', 'slt-expected-wwid' => '3600abcd',
        } } } };
        local *PVE::SharedLvmThinGuardInventory::build_guard_inventory = sub { { pools => [{
            pool_uuid => 'pool-uuid', owner_epoch => 'a' x 32, owner_node => 'local',
            mapper_name => 'test-mapper', mapper_uuid => 'LVM-abc-tpool', locally_active => 0,
        }] } };
        local *PVE::SharedLvmThinGuardInventory::storage_identity_matches = sub { 1 };
        local *PVE::SharedLvmThinPeerAudit::select_peer_nodes = sub { [{ node => 'peer', ip => '192.0.2.2' }] };
        local *PVE::SharedLvmThinPeerAudit::evaluate_peer_mapper_evidence = sub { { safe => 1 } };
        local *PVE::Tools::run_command = sub {
            my ($argv, %opts) = @_;
            if ($argv->[0] eq '/usr/bin/ssh') {
                for my $option ('StrictHostKeyChecking=yes', 'ControlMaster=no', 'ControlPath=none') {
                    die "missing SSH option $option\n" if !grep { $_ eq $option } @$argv;
                }
                die "simulated SSH host key failure\n" if $ssh_fails;
                $opts{outfunc}->('BASTRIX_REMOTE_THIN_V1|ABSENT|test-mapper|LVM-abc-tpool');
            } elsif ($argv->[0] eq '/usr/sbin/lvs') {
                $opts{outfunc}->('{"report":[{"lv":[]}]}');
            } elsif ($argv->[0] ne '/usr/sbin/dmsetup') {
                $opts{outfunc}->('fixture');
            }
        };
        @ARGV = ('--prepare', 'test', 'pool-uuid', 'a' x 32, 'LVM-abc-tpool');
        do './usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory';
        die $@ || $! || 'inventory returned unexpectedly';
    }
    close $writer;
    local $/;
    my $output = <$reader>;
    close $reader;
    waitpid($pid, 0);
    return ($? >> 8, decode_json($output));
}

my ($ok_exit, $ok) = run_fixture(0);
is($ok_exit, 0, 'guard inventory accepts canonical mocked peer proof with hardened SSH argv');
is($ok->{admission}->{remote_runtime_absent}, 1, 'peer absence reaches admission');
my ($failed_exit, $failed) = run_fixture(1);
isnt($failed_exit, 0, 'SSH host key failure exits nonzero');
is($failed->{inventory_ok}, 0, 'SSH failure does not produce successful guard inventory');
like($failed->{error}, qr/simulated SSH host key failure/, 'failure remains visible');
done_testing();
