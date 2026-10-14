use strict;
use warnings;
use Test::More;
use lib 'tests/unit/lib', 'usr/share/perl5';
use PVE::Storage::Custom::SharedLvmThinPlugin;
my $class = 'PVE::Storage::Custom::SharedLvmThinPlugin';
my $frames = [{ sub => 'PVE::QemuServer::ImportDisk::do_import' }];
{
    no warnings 'redefine';
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_call_frames = sub { $frames };
    my $ok = eval { $class->_lazy_refuse_unqualified_native_import(undef, undef); 1 };
    ok(!$ok, 'exact native import refused');
    like($@, qr/Lazy Thick native image import is not qualified/, 'diagnostic identifies unsupported import');
    ok(eval { $class->_lazy_refuse_unqualified_native_import({kind=>'move'}, undef); 1 }, 'qualified move context unaffected');
    ok(eval { $class->_lazy_refuse_unqualified_native_import(undef, {kind=>'restore'}); 1 }, 'qualified restore context unaffected');
    for my $name ('PVE::QemuServer::clone_disk', 'PVE::QemuServer::ImportDisk::do_import_other', '') {
        $frames = [{sub=>$name}];
        ok(eval { $class->_lazy_refuse_unqualified_native_import(undef, undef); 1 }, 'other caller not classified as native import');
    }
}
{
    no warnings 'redefine';
    my $transactions = 0;
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_require_thick_identity_config = sub { };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_efi_allocation_requires_eager = sub { 0 };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_prepare_move_allocation = sub { undef };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_lazy_prepare_restore_allocation = sub { undef };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_thick_destroy_call_frames = sub { [{sub=>'PVE::QemuServer::ImportDisk::do_import'}] };
    local *PVE::Storage::Custom::SharedLvmThinPlugin::_new_transaction_id = sub { $transactions++; die "allocation reached transaction\n" };
    my $ok = eval { $class->_lazy_alloc_image('lazy', {}, 910001, 'raw', 'vm-910001-disk-0', 65536); 1 };
    ok(!$ok, 'allocation entry refuses native import');
    like($@, qr/No Lazy destination allocation was issued/, 'refusal precedes any transaction');
    is($transactions, 0, 'no allocation transaction entered');
}
done_testing();
