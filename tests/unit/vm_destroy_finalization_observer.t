use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";
use Digest::SHA qw(sha256_hex);
use JSON::PP ();

use PVE::SharedLvmThinVMDestroyFinalizationObserver;

sub fixture {
    my (%opts) = @_;
    my $conf = $opts{conf} // { lock => 'destroyed', snapshots => {} };
    my $usercfg = $opts{usercfg} // { acl_root => {}, vms => {}, pools => {} };
    my @calls;
    my $observer = PVE::SharedLvmThinVMDestroyFinalizationObserver->new(
        cfs_update => sub { push @calls, 'update'; die "no quorum\n" if $opts{update_fail} },
        get_vmlist => sub {
            push @calls, 'vmlist';
            return $opts{vmlist} if exists($opts{vmlist});
            return { ids => { 101 => { type => 'qemu', node => 'pve01' } } };
        },
        load_config => sub {
            push @calls, "config:$_[0]:$_[1]";
            die "config read failed\n" if $opts{config_fail};
            return $conf;
        },
        path_state => sub {
            push @calls, "path:$_[0]";
            die "symlink or lstat race\n" if $opts{path_fail};
            return $opts{firewall} // 'ABSENT';
        },
        lock_user_config => sub {
            push @calls, 'acl-lock';
            die "ACL lock failed\n" if $opts{lock_fail};
            $_[0]->();
        },
        read_user_config => sub {
            push @calls, 'usercfg';
            die "user.cfg read failed\n" if $opts{user_fail};
            return $usercfg;
        },
    );
    return ($observer, \@calls, $conf);
}

subtest 'present QEMU config has exact node and canonical digest' => sub {
    my ($observer, $calls, $conf) = fixture();
    my $state = $observer->observe(101);
    is($state->{config}, 'PRESENT', 'present');
    is($state->{config_node}, 'pve01', 'cluster owner bound');
    is($state->{config_sha256}, sha256_hex(JSON::PP->new->canonical->encode($conf)),
        'exact parsed config digest');
    is($state->{acl}, 'ABSENT', 'ACL absent');
    is($state->{firewall}, 'ABSENT', 'firewall absent');
};

subtest 'cluster-wide absence is accepted only when all finalization state is absent' => sub {
    my ($observer) = fixture(vmlist => { ids => {} });
    my $state = $observer->observe(101);
    is_deeply($state, { vmid => '101', config => 'ABSENT', acl => 'ABSENT', firewall => 'ABSENT' },
        'safe terminal absence');
};

for my $case (
    ['ACL path', { acl_root => { children => { vms => { children => { 101 => { role => 1 } } } } }, vms => {}, pools => {} }],
    ['direct VM map', { acl_root => {}, vms => { 101 => 'pool-a' }, pools => {} }],
    ['any pool membership', { acl_root => {}, vms => {}, pools => { a => { vms => { 101 => 1 } } } }],
) {
    subtest "$case->[0] is independently detected" => sub {
        my ($observer) = fixture(usercfg => $case->[1]);
        is($observer->observe(101)->{acl}, 'PRESENT', 'reference found');
    };
}

for my $case (
    ['quorum/cache refresh', { update_fail => 1 }],
    ['malformed vmlist', { vmlist => {} }],
    ['foreign LXC identity', { vmlist => { ids => { 101 => { type => 'lxc', node => 'pve01' } } } }],
    ['config parser/read', { config_fail => 1 }],
    ['firewall symlink/lstat', { path_fail => 1 }],
    ['ACL lock', { lock_fail => 1 }],
    ['user.cfg read', { user_fail => 1 }],
    ['malformed user map', { usercfg => { acl_root => {}, vms => [], pools => {} } }],
    ['malformed pool member map', { usercfg => { acl_root => {}, vms => {}, pools => { a => { vms => [] } } } }],
) {
    subtest "$case->[0] failure makes the complete observation UNKNOWN" => sub {
        my ($observer) = fixture(%{$case->[1]});
        my $state = $observer->observe(101);
        is_deeply([@$state{qw(config firewall acl)}], [qw(UNKNOWN UNKNOWN UNKNOWN)], 'fail closed');
        ok(length($state->{error} // ''), 'diagnostic retained');
    };
}

for my $field (qw(acl firewall)) {
    subtest "absent config plus remaining $field is VMID-reuse ambiguous" => sub {
        my %args = (vmlist => { ids => {} });
        $args{firewall} = 'PRESENT' if $field eq 'firewall';
        $args{usercfg} = { acl_root => {}, vms => { 101 => 'orphan' }, pools => {} }
            if $field eq 'acl';
        my ($observer) = fixture(%args);
        my $state = $observer->observe(101);
        is_deeply([@$state{qw(config firewall acl)}], [qw(UNKNOWN UNKNOWN UNKNOWN)],
            'observer cannot authorize cleanup after identity disappeared');
        like($state->{error}, qr/VMID reuse is ambiguous/, 'exact boundary reported');
    };
}

subtest 'invalid identity performs no reads' => sub {
    my ($observer, $calls) = fixture();
    my $state = $observer->observe('../101');
    is($state->{config}, 'UNKNOWN', 'refused');
    is_deeply($calls, [], 'zero external observations');
};

done_testing();
