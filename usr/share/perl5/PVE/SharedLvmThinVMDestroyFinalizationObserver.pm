package PVE::SharedLvmThinVMDestroyFinalizationObserver;

use strict;
use warnings;
use Digest::SHA qw(sha256_hex);
use Errno qw(ENOENT);
use JSON::PP ();

sub _canonical { JSON::PP->new->canonical->encode($_[0]) }

sub _production_dependencies {
    require PVE::AccessControl;
    require PVE::Cluster;
    require PVE::QemuConfig;
    return (
        cfs_update => sub { PVE::Cluster::cfs_update(1) },
        get_vmlist => sub { PVE::Cluster::get_vmlist() },
        load_config => sub { PVE::QemuConfig->load_config(@_) },
        lock_user_config => sub { PVE::AccessControl::lock_user_config($_[0], $_[1]) },
        read_user_config => sub { PVE::Cluster::cfs_read_file('user.cfg') },
        path_state => \&_production_path_state,
    );
}

sub new {
    my ($class, %args) = @_;
    %args = _production_dependencies() if !keys(%args);
    for my $name (qw(cfs_update get_vmlist load_config lock_user_config read_user_config path_state)) {
        die "missing VM destroy finalization observer dependency '$name'\n"
            if ref($args{$name}) ne 'CODE';
    }
    return bless \%args, $class;
}

sub _production_path_state {
    my ($path) = @_;
    die "unexpected VM firewall path\n"
        if $path !~ m{^/etc/pve/firewall/[1-9][0-9]*\.fw$};
    for my $parent ('/etc', '/etc/pve', '/etc/pve/firewall') {
        local $! = 0;
        my @st = lstat($parent);
        die "cannot inspect canonical firewall parent '$parent': $!\n" if !@st;
        die "firewall parent is not a real directory: '$parent'\n" if -l _ || !-d _;
    }
    local $! = 0;
    my @st = lstat($path);
    return 'ABSENT' if !@st && $! == ENOENT;
    die "cannot inspect VM firewall path '$path': $!\n" if !@st;
    die "VM firewall path is not a canonical regular file\n" if -l _ || !-f _;
    return 'PRESENT';
}

sub _acl_state {
    my ($usercfg, $vmid) = @_;
    die "user.cfg parse result is unknown\n" if ref($usercfg) ne 'HASH';
    for my $key (qw(acl_root vms pools)) {
        die "user.cfg '$key' structure is unknown\n" if ref($usercfg->{$key}) ne 'HASH';
    }
    my $present = exists($usercfg->{vms}->{$vmid});
    for my $pool (values %{$usercfg->{pools}}) {
        die "user.cfg pool structure is unknown\n" if ref($pool) ne 'HASH';
        next if !exists($pool->{vms});
        die "user.cfg pool VM membership is unknown\n" if ref($pool->{vms}) ne 'HASH';
        $present ||= exists($pool->{vms}->{$vmid});
    }
    my $node = $usercfg->{acl_root};
    for my $name ('children', 'vms', 'children') {
        last if !exists($node->{$name});
        die "user.cfg ACL tree is unknown\n" if ref($node->{$name}) ne 'HASH';
        $node = $node->{$name};
    }
    $present ||= exists($node->{$vmid}) if ref($node) eq 'HASH';
    return $present ? 'PRESENT' : 'ABSENT';
}

sub observe {
    my ($self, $vmid) = @_;
    return { vmid => defined($vmid) ? "$vmid" : '', acl => 'UNKNOWN',
        firewall => 'UNKNOWN', config => 'UNKNOWN', error => 'invalid VM identity' }
        if !defined($vmid) || $vmid !~ /^[1-9][0-9]*$/;
    my $result = { vmid => "$vmid", acl => 'UNKNOWN', firewall => 'UNKNOWN', config => 'UNKNOWN' };
    my $ok = eval {
        $self->{cfs_update}->();
        my $vmlist = $self->{get_vmlist}->();
        die "cluster VM list is unknown\n"
            if ref($vmlist) ne 'HASH' || ref($vmlist->{ids}) ne 'HASH';
        if (my $entry = $vmlist->{ids}->{$vmid}) {
            die "cluster VM identity is unknown\n"
                if ref($entry) ne 'HASH' || ($entry->{type} // '') ne 'qemu'
                || ($entry->{node} // '') !~ /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;
            my $conf = $self->{load_config}->($vmid, $entry->{node});
            die "cluster VM config parse result is unknown\n" if ref($conf) ne 'HASH';
            $result->{config} = 'PRESENT';
            $result->{config_sha256} = sha256_hex(_canonical($conf));
            $result->{config_node} = $entry->{node};
        } else {
            $result->{config} = 'ABSENT';
        }
        $result->{firewall} = $self->{path_state}->("/etc/pve/firewall/$vmid.fw");
        die "VM firewall state is unknown\n"
            if $result->{firewall} !~ /^(?:PRESENT|ABSENT)$/;
        my $usercfg;
        $self->{lock_user_config}->(sub { $usercfg = $self->{read_user_config}->() },
            "read-only VM access observation failed");
        $result->{acl} = _acl_state($usercfg, "$vmid");
        # Once the config disappeared, its VMID can be reused by native PVE.
        # Never mutate leftover ACL/firewall state without a separate durable
        # VMID reservation protocol. The safe terminal case is all absent.
        die "config absent but VM-scoped finalization state remains; VMID reuse is ambiguous\n"
            if $result->{config} eq 'ABSENT'
            && ($result->{acl} ne 'ABSENT' || $result->{firewall} ne 'ABSENT');
        1;
    };
    if (!$ok) {
        my $error = $@ || "unknown finalization observation failure\n";
        return { vmid => "$vmid", acl => 'UNKNOWN', firewall => 'UNKNOWN',
            config => 'UNKNOWN', error => "$error" };
    }
    return $result;
}

1;
