# Copyright (C) 2026 BASTRIX Project Contributors
# SPDX-License-Identifier: GPL-3.0-only

package PVE::SharedLvmThinGuardProtocol;

use strict;
use warnings;
use Exporter qw(import);
use JSON::PP ();

our @EXPORT_OK = qw(decode_guard_request encode_guard_response);

my %fields = (
    STATUS => {version => 1, op => 1, request_id => 1},
    PREPARE => {
        version => 1, op => 1, request_id => 1, storage_id => 1,
        pool_uuid => 1, owner_epoch => 1, mapper_uuid => 1,
    },
    RELEASE => {
        version => 1, op => 1, request_id => 1,
        pool_uuid => 1, owner_epoch => 1,
    },
);

sub decode_guard_request {
    my ($line) = @_;
    die "guardian request is missing\n" if !defined($line);
    die "guardian request exceeds 16384 bytes\n" if length($line) > 16384;
    die "guardian request contains a NUL byte\n" if index($line, "\0") >= 0;
    die "guardian request must contain exactly one JSON line\n"
        if $line =~ /[\r\n]./s || $line !~ /\n\z/;
    $line =~ s/\r?\n\z//;
    my $request = eval { JSON::PP::decode_json($line) };
    die "guardian request is not valid JSON\n" if $@ || ref($request) ne 'HASH';
    die "unsupported guardian protocol version\n"
        if !defined($request->{version}) || $request->{version} !~ /^1$/;
    my $op = $request->{op} // '';
    die "unsupported guardian operation\n" if !exists($fields{$op});
    for my $name (keys %$request) {
        die "unexpected guardian request field '$name'\n" if !$fields{$op}->{$name};
    }
    for my $name (keys %{$fields{$op}}) {
        die "missing guardian request field '$name'\n" if !exists($request->{$name});
    }
    die "malformed guardian request ID\n"
        if $request->{request_id} !~ /^[a-f0-9]{32}$/;
    if ($op eq 'PREPARE') {
        die "malformed guardian storage ID\n"
            if $request->{storage_id} !~ /^[A-Za-z0-9_.-]+$/;
        die "malformed guardian pool UUID\n"
            if $request->{pool_uuid} !~ /^[A-Za-z0-9-]+$/;
        die "malformed guardian owner epoch\n"
            if $request->{owner_epoch} !~ /^[a-f0-9]{32}$/;
        die "malformed guardian mapper UUID\n"
            if $request->{mapper_uuid} !~ /^LVM-[A-Za-z0-9]+-tpool$/;
    } elsif ($op eq 'RELEASE') {
        die "malformed guardian pool UUID\n"
            if $request->{pool_uuid} !~ /^[A-Za-z0-9-]+$/;
        die "malformed guardian owner epoch\n"
            if $request->{owner_epoch} !~ /^[a-f0-9]{32}$/;
    }
    return $request;
}

sub encode_guard_response {
    my (%response) = @_;
    for my $name (qw(ok request_id state action message)) {
        die "missing guardian response field '$name'\n" if !exists($response{$name});
    }
    die "invalid guardian response result\n" if $response{ok} !~ /^(?:0|1)$/;
    die "invalid guardian response request ID\n"
        if $response{request_id} !~ /^[a-f0-9]{32}$/;
    for my $name (qw(state action)) {
        die "invalid guardian response field '$name'\n"
            if !defined($response{$name}) || $response{$name} !~ /^[A-Z_]+$/;
    }
    die "invalid guardian response message\n"
        if !defined($response{message}) || ref($response{message})
        || $response{message} =~ /[\x00-\x1f\x7f]/ || length($response{message}) > 2048;
    return JSON::PP->new->canonical(1)->encode({
        version => 1,
        map { $_ => $response{$_} } qw(ok request_id state action message),
    }) . "\n";
}

1;
