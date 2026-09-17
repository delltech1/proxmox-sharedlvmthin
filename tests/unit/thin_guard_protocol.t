#!/usr/bin/perl

use strict;
use warnings;
use Test::More;
use FindBin;
use lib "$FindBin::Bin/../../usr/share/perl5";

use PVE::SharedLvmThinGuardProtocol qw(decode_guard_request encode_guard_response);

my $id = '1' x 32;
my $prepare = qq|{"version":1,"op":"PREPARE","request_id":"$id","storage_id":"slt-thin","pool_uuid":"abc-123","owner_epoch":"| . ('a' x 32) . qq|","mapper_uuid":"LVM-abcdef-tpool"}\n|;
my $request = decode_guard_request($prepare);
is($request->{op}, 'PREPARE', 'strict PREPARE request decodes');

my $status = decode_guard_request(qq|{"version":1,"op":"STATUS","request_id":"$id"}\n|);
is($status->{op}, 'STATUS', 'strict STATUS request decodes');

my $release = decode_guard_request(qq|{"version":1,"op":"RELEASE","request_id":"$id","pool_uuid":"abc-123","owner_epoch":"| . ('a' x 32) . qq|"}\n|);
is($release->{op}, 'RELEASE', 'strict RELEASE request decodes');

my @bad = (
    ['missing newline', substr($prepare, 0, -1)],
    ['multiple lines', $prepare . "{}\n"],
    ['NUL', "{}\0\n"],
    ['wrong version', qq|{"version":2,"op":"STATUS","request_id":"$id"}\n|],
    ['unknown operation', qq|{"version":1,"op":"ARM","request_id":"$id"}\n|],
    ['extra field', qq|{"version":1,"op":"STATUS","request_id":"$id","safe":true}\n|],
    ['bad request ID', qq|{"version":1,"op":"STATUS","request_id":"no"}\n|],
    ['bad storage ID', $prepare =~ s/slt-thin/slt thin/r],
    ['bad owner epoch', $prepare =~ s/"a{32}"/"xyz"/r],
    ['bad mapper UUID', $prepare =~ s/LVM-abcdef-tpool/not-lvm/r],
);
for my $case (@bad) {
    my $ok = eval { decode_guard_request($case->[1]); 1 };
    ok(!$ok, "$case->[0] fails closed");
}

my $encoded = encode_guard_response(
    ok => 1, request_id => $id, state => 'ARMED_PENDING',
    action => 'ACK_PREPARED', message => 'watchdog armed',
);
like($encoded, qr/^\{"action":"ACK_PREPARED"/, 'response JSON is canonical');
is(decode_guard_request(qq|{"version":1,"op":"STATUS","request_id":"$id"}\n|)->{request_id},
    $id, 'request ID round-trips exactly');
eval { encode_guard_response(ok => 1, request_id => $id, state => 'OK',
    action => 'NONE', message => "bad\nmessage") };
like($@, qr/response message/, 'control characters are refused in responses');

done_testing();
