#!/usr/bin/perl -T

use strict;
use warnings;

use FindBin;
BEGIN {
    die "unsafe test path\n" if $FindBin::Bin !~ m{^([A-Za-z0-9_./-]+)$};
    require lib;
    lib->import("$1/../../usr/share/perl5", "$1/lib");
}
use Scalar::Util qw(tainted);
use Test::More;

use PVE::SharedLvmThinThick qw(object_key vg_intent_tags);
use PVE::Storage::Custom::SharedLvmThinPlugin;

ok(${^TAINT}, 'test itself runs with Perl taint checks enabled');

my $dirty = substr($ENV{PATH} // die("PATH is unavailable\n"), 0, 0);
ok(tainted($dirty), 'fixture is tainted before validation');

my $key = object_key($dirty . 'store', $dirty . 'vm-1-disk-0');
ok(!tainted($key), 'object key is untainted after strict token validation');

my $tags = vg_intent_tags(
    tx => $dirty . '0123456789abcdef0123456789abcdef',
    state => $dirty . 'OPEN',
    op => $dirty . 'EXTEND',
    object => $dirty . 'sltg-a-0123456789abcdef01234567',
    before => $dirty . '0123456789abcdef0123456789abcdef',
);
ok(!grep { tainted($_) } @$tags,
    'every validated VG transaction tag is safe for exec arguments');

{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_command_lines = sub {
        return [$dirty . 'vg-uuid|7|8|4096'];
    };
    my $digest = PVE::Storage::Custom::SharedLvmThinPlugin->_vg_state_digest(
        'vg', undef,
    );
    ok(!tainted($digest), 'VG state digest is untainted after grammar validation');
}

my $tx = PVE::Storage::Custom::SharedLvmThinPlugin->_new_transaction_id();
ok(!tainted($tx), 'kernel transaction identifier is untainted after validation');

my $argv = PVE::Storage::Custom::SharedLvmThinPlugin::_validated_exec_argv([
    '/sbin/lvextend', '-L', $dirty . '4294967296B',
    $dirty . 'vg/sltg-g-0123456789abcdef01234567-00000001',
]);
ok(!grep { tainted($_) } @$argv,
    'the central argv boundary untaints every checked command argument');

eval {
    PVE::Storage::Custom::SharedLvmThinPlugin::_validated_exec_argv([
        '/sbin/lvextend', $dirty . '--config=devices/use_devicesfile=0',
    ]);
};
like($@, qr/tainted external command argument .* may not be an option/,
    'tainted values cannot inject command options');

eval {
    PVE::Storage::Custom::SharedLvmThinPlugin::_validated_exec_argv([
        '/sbin/lvextend', $dirty . "vg/lv\n--force",
    ]);
};
like($@, qr/control character/,
    'tainted values cannot inject additional command content');

done_testing();
