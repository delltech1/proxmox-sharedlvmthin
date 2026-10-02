package PVE::SharedLvmThinVMDestroyJournal;

use strict;
use warnings;
use JSON::PP ();
use Digest::SHA qw(sha256_hex);

my $LIMIT = 1024 * 1024;
my $OBSERVE_LIMIT = 32 * $LIMIT + 4096;
my $HELPER = '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy';
my @CORE = qw(schema txid vmid config runtime plan);
my @EXTRA = qw(evidence current_effect next_effects error resume resumed recovery_required);

sub _json { JSON::PP->new->canonical->ascii->encode($_[0]) }
sub _copy { JSON::PP->new->decode(_json($_[0])) }
sub _need ($$) { die "VM destroy journal bridge refused: " . (@_ == 2 ? $_[1] : 'invalid predicate arity') . "\n" if @_ != 2 || !$_[0] }
sub _decode {
    my ($raw, $limit) = @_;
    _need(defined($raw) && !ref($raw) && length($raw) <= $limit, 'invalid response bytes');
    my $value = eval { JSON::PP->new->decode($raw) };
    _need(!$@ && ref($value) eq 'HASH' && $raw eq _json($value) . "\n", 'noncanonical response');
    return $value;
}

sub _advance {
    my ($previous, $stage) = @_;
    return 0 if !defined($stage) || ref($stage);
    return defined($stage) && $stage eq 'PREPARED' if !defined($previous);
    return 0 if $previous eq 'COMPLETE' || $previous eq 'PARTIAL_OR_UNKNOWN';
    return 1 if $stage eq 'PARTIAL_OR_UNKNOWN';
    return 1 if $previous eq 'FINALIZING' && $stage eq 'FINALIZING';
    my %next = (PREPARED => 'DISPATCHED', DISPATCHED => 'STORAGE_ABSENT',
        STORAGE_ABSENT => 'FINALIZING', FINALIZING => 'COMPLETE');
    return defined($next{$previous}) && $next{$previous} eq $stage;
}

sub new {
    my ($class, %args) = @_;
    _need(!grep({ $_ ne 'request_id' && $_ ne 'runner' } keys %args), 'unknown argument');
    _need(scalar(($args{request_id} // '') =~ /\A[0-9a-f]{32}\z/), 'invalid request ID');
    _need(!exists($args{runner}) || ref($args{runner}) eq 'CODE', 'invalid runner');
    return bless { request_id => $args{request_id}, runner => $args{runner} // \&_run,
        previous => undef, sequence => 0, poisoned => 0 }, $class;
}

sub open_existing {
    my ($class, %args) = @_;
    my $expected = delete $args{expected_context};
    _need(ref($expected) eq 'HASH', 'exact expected context required for recovery');
    my $self = $class->new(%args);
    $self->{context} = _copy($expected);
    $self->_load_existing();
    return $self;
}

sub _load_existing {
    my ($self) = @_;
    _need($> == 0, 'root required');
    _need(!$self->{poisoned}, 'prior uncertain append; use a new recovery process');
    _need(ref($self->{context}) eq 'HASH', 'recovery context missing');
    my $raw = $self->{runner}->(['/usr/bin/python3', '-I', $HELPER, '_observe-durable',
        '--request-id', $self->{request_id}], '');
    my $value = _decode($raw, $OBSERVE_LIMIT);
    _need(join(',', sort keys %$value) eq 'authority,durable,last_sha256,records,schema,stage', 'observation fields');
    _need(($value->{schema} // '') eq 'sharedlvmthin-vm-destroy-journal/v1'
        && ($value->{authority} // '') eq 'NONE'
        && JSON::PP::is_bool($value->{durable}) && $value->{durable}, 'not durable NONE journal');
    _need(ref($value->{records}) eq 'ARRAY' && @{$value->{records}} >= 4
        && @{$value->{records}} <= 32, 'invalid recovery record count');
    my ($previous, $stage, $sequence, @records) = (undef, undef, 0);
    my %allowed = map { $_ => 1 } @EXTRA;
    for my $row (@{$value->{records}}) {
        _need(ref($row) eq 'HASH' && join(',', sort keys %$row) eq
            'authority,context,context_sha256,evidence,previous_sha256,request_id,schema,sequence,stage', 'record fields');
        _need(($row->{schema} // '') eq 'sharedlvmthin-vm-destroy-journal/v1'
            && ($row->{authority} // '') eq 'NONE' && ($row->{request_id} // '') eq $self->{request_id}, 'record identity');
        _need(_json($row->{sequence}) eq '' . ++$sequence && _advance($stage, $row->{stage}), 'record sequence/stage');
        _need(_json($row->{previous_sha256}) eq _json($previous), 'record chain');
        _need(_json($row->{context}) eq _json($self->{context})
            && ($row->{context_sha256} // '') eq sha256_hex(_json($self->{context})), 'record context');
        my $context = $row->{context};
        _need(join(',', sort keys %$context) eq 'receipt,schema'
            && ($context->{schema} // '') eq 'sharedlvmthin-vm-destroy-executor-core/v1'
            && ref($context->{receipt}) eq 'HASH'
            && join(',', sort keys %{$context->{receipt}}) eq join(',', sort @CORE)
            && ($context->{receipt}->{txid} // '') eq $self->{request_id}, 'executor core');
        _need(ref($row->{evidence}) eq 'HASH' && ref($row->{evidence}->{evidence}) eq 'HASH'
            && !grep({ !$allowed{$_} } keys %{$row->{evidence}}), 'stage evidence');
        push @records, { stage => $row->{stage}, receipt => { %{$context->{receipt}}, %{$row->{evidence}} } };
        $previous = sha256_hex(_json($row));
        $stage = $row->{stage};
    }
    _need(($value->{stage} // '') eq $stage && ($value->{last_sha256} // '') eq $previous, 'observed head mismatch');
    _need($stage eq 'FINALIZING', 'only exact FINALIZING is recoverable; terminal/other states refused');
    $self->{previous} = $previous;
    $self->{sequence} = $sequence;
    $self->{stage} = $stage;
    return { durable => 1, records => _copy(\@records), stage => $stage,
        request_id => $self->{request_id}, last_sha256 => $previous,
        context_sha256 => sha256_hex(_json($self->{context})) };
}

sub read_journal {
    my ($self, $request_id) = @_;
    _need(($request_id // '') eq $self->{request_id}, 'read request mismatch');
    return $self->_load_existing();
}

sub reader {
    my ($self) = @_;
    return sub { $self->read_journal(@_) };
}

sub _run {
    my ($argv, $input) = @_;
    local %ENV = (PATH => '/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL => 'C', LANG => 'C');
    require PVE::Tools;
    my ($output, $errors) = ('', '');
    PVE::Tools::run_command($argv, input => $input, timeout => 30,
        outfunc => sub { $output .= $_[0] . "\n"; _need(length($output) <= $OBSERVE_LIMIT, 'oversized response'); },
        errfunc => sub { $errors .= $_[0] . "\n"; _need(length($errors) <= 8192, 'oversized diagnostics'); });
    _need($errors eq '', 'helper diagnostics leave publication uncertain');
    return $output;
}

sub callback {
    my ($self) = @_;
    return sub { $self->append(@_) };
}

sub cas_callback {
    my ($self) = @_;
    return sub { $self->append_cas(@_) };
}

sub append_cas {
    my ($self, $stage, $receipt, $expected_previous) = @_;
    _need(@_ == 4 && defined($expected_previous) && !ref($expected_previous)
        && scalar($expected_previous =~ /\A[0-9a-f]{64}\z/), 'explicit CAS head required');
    _need(defined($self->{previous}) && $self->{previous} eq $expected_previous,
        'explicit CAS differs from observed journal head');
    # append still supplies this pinned previous hash to the private helper,
    # which rechecks it under the durable journal lock before O_EXCL creation.
    return $self->append($stage, $receipt);
}

sub append {
    my ($self, $stage, $receipt) = @_;
    _need($> == 0, 'root required');
    _need(!$self->{poisoned}, 'prior uncertain append; no retry');
    _need($self->{sequence} < 32, 'record budget exhausted');
    _need(_advance($self->{stage}, $stage), 'stage skip, replay or terminal state');
    _need(defined($stage) && scalar($stage =~ /\A(?:PREPARED|DISPATCHED|STORAGE_ABSENT|FINALIZING|COMPLETE|PARTIAL_OR_UNKNOWN)\z/),
        'invalid stage');
    _need(ref($receipt) eq 'HASH', 'receipt missing');
    my %allowed = map { $_ => 1 } (@CORE, @EXTRA);
    _need(!grep({ !$allowed{$_} } keys %$receipt), 'unknown receipt fields');
    _need(exists($receipt->{$_}), "missing core $_") for @CORE;
    _need(($receipt->{txid} // '') eq $self->{request_id}, 'receipt request mismatch');
    _need(ref($receipt->{evidence}) eq 'HASH', 'stage evidence missing');
    my $context = { schema => 'sharedlvmthin-vm-destroy-executor-core/v1',
        receipt => { map { $_ => $receipt->{$_} } @CORE } };
    _need(!defined($self->{context}) || _json($self->{context}) eq _json($context), 'immutable context changed');
    my $evidence = { map { exists($receipt->{$_}) ? ($_ => $receipt->{$_}) : () } @EXTRA };
    my $request = { schema => 'sharedlvmthin-vm-destroy-append/v1', authority => 'NONE',
        request_id => $self->{request_id}, stage => $stage, expected_previous => $self->{previous},
        context => $context, evidence => $evidence };
    my $input = _json($request) . "\n";
    _need(length($input) <= $LIMIT, 'oversized append');
    my $row = { schema => 'sharedlvmthin-vm-destroy-journal/v1', authority => 'NONE',
        sequence => $self->{sequence} + 1, request_id => $self->{request_id}, stage => $stage,
        previous_sha256 => $self->{previous}, context_sha256 => sha256_hex(_json($context)),
        context => $context, evidence => $evidence };
    _need(length(_json($row)) + 1 <= $LIMIT, 'oversized journal record');
    # Set BEFORE invoking the helper. Missing/invalid acknowledgement can mean
    # a fully fsynced publication. Never redispatch automatically, even UNKNOWN.
    $self->{poisoned} = 1;
    my $raw = $self->{runner}->(['/usr/bin/python3', '-I', $HELPER, '_append', '--request-id', $self->{request_id}], $input);
    _need(defined($raw) && !ref($raw) && length($raw) <= $LIMIT, 'invalid acknowledgement bytes');
    my $ack = _decode($raw, $LIMIT);
    my $expected = { schema => 'sharedlvmthin-vm-destroy-append-result/v1', authority => 'NONE',
        request_id => $self->{request_id}, stage => $stage, previous_sha256 => $self->{previous},
        request_sha256 => sha256_hex(_json($request)), last_sha256 => sha256_hex(_json($row)) };
    _need(_json($ack) eq _json($expected), 'acknowledgement identity/digest mismatch');
    $self->{previous} = $ack->{last_sha256};
    $self->{sequence}++;
    $self->{stage} = $stage;
    $self->{context} = _copy($context);
    $self->{poisoned} = 0;
    return 1;
}

1;
