# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinGuardClient;

use strict;
use warnings;
use IO::Socket::UNIX;
use JSON::PP ();
use Socket qw(SOCK_STREAM);

sub new {
    my ($class, %args) = @_;
    return bless({
        socket_factory => $args{socket_factory},
        allow_real_socket => $args{allow_real_socket} ? 1 : 0,
        socket_path => $args{socket_path} // '/run/pve-sharedlvmthin/thin-guard.sock',
        request_timeout => $args{request_timeout} // 35,
    }, $class);
}

sub _connect {
    my ($self) = @_;
    return $self->{socket_factory}->() if defined($self->{socket_factory});
    die "real ThinGuard socket access is disabled\n" if !$self->{allow_real_socket};
    die "refusing non-canonical ThinGuard socket path\n"
        if $self->{socket_path} ne '/run/pve-sharedlvmthin/thin-guard.sock';
    return IO::Socket::UNIX->new(Type => SOCK_STREAM(), Peer => $self->{socket_path})
        || die "cannot connect to ThinGuard daemon: $!\n";
}

sub request {
    my ($self, $request, $expected_action) = @_;
    die "ThinGuard client request must be a hash\n" if ref($request) ne 'HASH';
    my @expected = ref($expected_action) eq 'ARRAY' ? @$expected_action : ($expected_action);
    die "ThinGuard expected action is malformed\n"
        if !@expected || grep { !defined($_) || $_ !~ /^[A-Z_]+$/ } @expected;
    my %expected_action = map { $_ => 1 } @expected;
    my $payload = JSON::PP->new->canonical(1)->encode({version => 1, %$request}) . "\n";
    die "ThinGuard client request exceeds protocol limit\n" if length($payload) > 16384;
    my $response = '';
    my $transport_error = '';
    my $attempts = $self->{allow_real_socket} ? 2 : 1;
    for my $attempt (1 .. $attempts) {
        $response = '';
        my $socket;
        eval {
            $socket = $self->_connect();
            die "ThinGuard socket factory returned no socket\n" if !defined($socket);
            local $SIG{ALRM} = sub { die "ThinGuard request timeout\n" };
            die "invalid ThinGuard request timeout\n"
                if $self->{request_timeout} !~ /^\d+$/
                || $self->{request_timeout} < 1 || $self->{request_timeout} > 1310;
            alarm($self->{request_timeout});
            my $written = $socket->syswrite($payload, length($payload));
            die "ThinGuard request write failed\n"
                if !defined($written) || $written != length($payload);
            while (length($response) <= 16384 && $response !~ /\n/) {
                my $chunk = '';
                my $count = $socket->sysread($chunk, 4096);
                die "ThinGuard response read failed\n" if !defined($count) || $count == 0;
                $response .= $chunk;
            }
            alarm(0);
        };
        $transport_error = $@;
        alarm(0);
        close($socket) if defined($socket);
        last if !$transport_error;
        select(undef, undef, undef, 0.1) if $attempt < $attempts;
    }
    die $transport_error if $transport_error;
    die "ThinGuard response contains multiple lines or exceeds protocol limit\n"
        if length($response) > 16384 || $response !~ /\n\z/ || $response =~ /\n./s;
    my $decoded = eval { JSON::PP::decode_json($response) };
    die "ThinGuard response is invalid JSON\n" if $@ || ref($decoded) ne 'HASH';
    my %expected = map { $_ => 1 } qw(version ok request_id state action message);
    die "ThinGuard response contains unexpected fields\n"
        if grep { !$expected{$_} } keys %$decoded;
    die "ThinGuard response is incomplete\n"
        if grep { !exists($decoded->{$_}) } keys %expected;
    die "ThinGuard response request ID mismatch\n"
        if ($decoded->{request_id} // '') ne ($request->{request_id} // '');
    die "ThinGuard daemon refused request: $decoded->{message}\n" if !$decoded->{ok};
    die "ThinGuard daemon returned unexpected action '$decoded->{action}'\n"
        if !$expected_action{$decoded->{action} // ''};
    return $decoded;
}

1;
