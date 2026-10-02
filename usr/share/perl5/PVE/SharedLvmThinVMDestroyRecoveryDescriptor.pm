package PVE::SharedLvmThinVMDestroyRecoveryDescriptor;

use strict;
use warnings;
use JSON::PP ();
use Digest::SHA qw(sha256_hex);

my $HELPER = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-recovery';
my $LIMIT = 1024 * 1024;
my $SCHEMA = 'sharedlvmthin-vm-destroy-recovery/v1';
my $CORE_SCHEMA = 'sharedlvmthin-vm-destroy-executor-core/v1';

sub _json { JSON::PP->new->canonical->ascii->encode($_[0]) }
sub _copy { JSON::PP->new->decode(_json($_[0])) }
sub _need {
    die "VM destroy recovery descriptor refused: malformed validation call\n" if @_ != 2;
    die "VM destroy recovery descriptor refused: $_[1]\n" if !$_[0];
}

sub new {
    my ($class, %args) = @_;
    _need(!grep({ $_ ne 'txid' && $_ ne 'runner' } keys %args), 'unknown argument');
    _need(!ref($args{txid}) && scalar(($args{txid} // '') =~ /\A[0-9a-f]{32}\z/), 'invalid txid');
    _need(!exists($args{runner}) || ref($args{runner}) eq 'CODE', 'invalid runner');
    return bless { txid => $args{txid}, runner => $args{runner} // \&_run, attempted => 0 }, $class;
}

sub _context {
    my ($self, $context) = @_;
    _need(ref($context) eq 'HASH' && join(',', sort keys %$context) eq 'receipt,schema'
        && ($context->{schema} // '') eq $CORE_SCHEMA, 'executor context schema');
    my $core = $context->{receipt};
    _need(ref($core) eq 'HASH' && join(',', sort keys %$core) eq 'config,plan,runtime,schema,txid,vmid',
        'full executor core required');
    _need(_json($core->{schema}) eq '1' && ($core->{txid} // '') eq $self->{txid}, 'executor core identity');
    _need(scalar(_json($core->{vmid}) =~ /\A[1-9][0-9]{0,8}\z/), 'executor vmid');
    for my $key (qw(config runtime plan)) {
        _need(ref($core->{$key}) eq 'HASH' && scalar(keys %{$core->{$key}}), 'executor core objects missing');
    }
    return $context;
}

sub _descriptor {
    my ($self, $value) = @_;
    _need(ref($value) eq 'HASH' && join(',', sort keys %$value) eq 'authority,context,context_sha256,schema,txid',
        'descriptor fields');
    _need(($value->{schema} // '') eq $SCHEMA && ($value->{authority} // '') eq 'NONE'
        && ($value->{txid} // '') eq $self->{txid}, 'descriptor schema/authority/identity');
    $self->_context($value->{context});
    _need(($value->{context_sha256} // '') eq sha256_hex(_json($value->{context})), 'context digest mismatch');
    _need(length(_json($value)) + 1 <= $LIMIT, 'oversized descriptor');
    return $value;
}

sub _run {
    my ($argv, $input) = @_;
    local %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
    require PVE::Tools;
    my ($output, $errors) = ('', '');
    PVE::Tools::run_command($argv, input => $input, timeout => 30,
        outfunc => sub { $output .= $_[0] . "\n"; _need(length($output) <= $LIMIT + 1024, 'oversized response'); },
        errfunc => sub { $errors .= $_[0] . "\n"; _need(length($errors) <= 8192, 'oversized diagnostics'); });
    _need($errors eq '', 'helper diagnostics; no implicit retry');
    return $output;
}

sub _response {
    my ($self, $raw, $expected) = @_;
    _need(defined($raw) && !ref($raw) && length($raw) <= $LIMIT + 1024, 'invalid response bytes');
    my $value = eval { JSON::PP->new->decode($raw) };
    _need(!$@ && ref($value) eq 'HASH' && $raw eq _json($value) . "\n", 'noncanonical response');
    _need(join(',', sort keys %$value) eq 'authority,descriptor,descriptor_sha256,durable,schema,txid', 'response fields');
    _need(($value->{schema} // '') eq 'sharedlvmthin-vm-destroy-recovery-result/v1'
        && ($value->{authority} // '') eq 'NONE' && ($value->{txid} // '') eq $self->{txid}
        && JSON::PP::is_bool($value->{durable}) && $value->{durable}, 'response identity/durability');
    $self->_descriptor($value->{descriptor});
    _need(($value->{descriptor_sha256} // '') eq sha256_hex(_json($value->{descriptor})), 'descriptor digest mismatch');
    _need(!defined($expected) || _json($value->{descriptor}) eq _json($expected), 'created descriptor mismatch');
    return _copy($value);
}

sub create {
    my ($self, $context) = @_;
    _need($> == 0, 'root required');
    _need(!$self->{attempted}, 'create already attempted; never retry or adopt');
    $context = _copy($self->_context($context));
    my $descriptor = { schema => $SCHEMA, authority => 'NONE', txid => $self->{txid},
        context => $context, context_sha256 => sha256_hex(_json($context)) };
    $self->_descriptor($descriptor);
    # Set before IPC: an exception can mean a durable file with a lost ack.
    $self->{attempted} = 1;
    my $raw = $self->{runner}->(['/usr/bin/python3', '-I', $HELPER, '_create', '--txid', $self->{txid}],
        _json($descriptor) . "\n");
    return $self->_response($raw, $descriptor);
}

sub read {
    my ($self) = @_;
    _need($> == 0, 'root required');
    my $raw = $self->{runner}->(['/usr/bin/python3', '-I', $HELPER, '_read', '--txid', $self->{txid}], '');
    # A descriptor is an identity source only. It does not prove effects, grant
    # storage authority, relax journal CAS, or authorize PARTIAL_OR_UNKNOWN.
    return $self->_response($raw);
}

1;
