#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use Socket qw(AF_UNIX SOCK_STREAM PF_UNSPEC);
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinGuardClient;

sub pair_with_response {
    my ($response) = @_;
    socketpair(my $client, my $server, AF_UNIX, SOCK_STREAM, PF_UNSPEC) or die $!;
    $server->syswrite($response, length($response));
    return ($client, $server);
}

my $id = 'a' x 32;
my $response = qq|{"action":"ACK_PREPARED","message":"armed","ok":1,"request_id":"$id","state":"ARMED_PENDING","version":1}\n|;
my ($client, $server) = pair_with_response($response);
my $guard = PVE::SharedLvmThinGuardClient->new(socket_factory => sub { $client });
my $result = $guard->request({op => 'PREPARE', request_id => $id}, 'ACK_PREPARED');
is($result->{state}, 'ARMED_PENDING', 'exact daemon response is accepted');
my $sent = '';
$server->sysread($sent, 4096);
like($sent, qr/"op":"PREPARE"/, 'canonical request is sent to daemon');

for my $case (
    ['wrong ID', qq|{"action":"ACK_PREPARED","message":"armed","ok":1,"request_id":"| . ('b' x 32) . qq|","state":"ARMED_PENDING","version":1}\n|],
    ['refusal', qq|{"action":"REFUSED","message":"unsafe","ok":0,"request_id":"$id","state":"IDLE","version":1}\n|],
    ['wrong action', qq|{"action":"NONE","message":"wrong","ok":1,"request_id":"$id","state":"IDLE","version":1}\n|],
    ['extra field', qq|{"action":"ACK_PREPARED","message":"armed","ok":1,"request_id":"$id","state":"ARMED_PENDING","version":1,"trust":true}\n|],
) {
    ($client, $server) = pair_with_response($case->[1]);
    $guard = PVE::SharedLvmThinGuardClient->new(socket_factory => sub { $client });
    my $ok = eval { $guard->request({op => 'PREPARE', request_id => $id}, 'ACK_PREPARED'); 1 };
    ok(!$ok, "$case->[0] fails closed");
}

$guard = PVE::SharedLvmThinGuardClient->new();
eval { $guard->request({op => 'STATUS', request_id => $id}, 'NONE') };
like($@, qr/real ThinGuard socket access is disabled/, 'real daemon is opt-in');

done_testing();
