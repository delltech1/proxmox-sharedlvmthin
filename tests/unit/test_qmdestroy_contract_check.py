import importlib.machinery
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmdestroy-contract-check"
loader = importlib.machinery.SourceFileLoader("qmdestroy_contract", str(CHECKER))
spec = importlib.util.spec_from_loader(loader.name, loader)
module = importlib.util.module_from_spec(spec)
loader.exec_module(module)

QEMU = r'''
sub destroy_vm {
my ($storecfg, $vmid, $skiplock, $replacement_conf, $purge_unreferenced) = @_;
my $remove_owned_drive = sub {
my ($path, $owner) = eval { PVE::Storage::path($storecfg, $volid) };
return if !$path || !$owner || ($owner != $vmid);
eval { PVE::Storage::vdisk_free($storecfg, $volid) };
log_warn("Could not remove disk '$volid', check manually: $@") if $@;
};
PVE::QemuConfig->foreach_volume_full($conf, $include_opts, $remove_owned_drive);
for my $snap (values %{ $conf->{snapshots} }) {
$remove_owned_drive->('vmstate', $drive);
}
PVE::QemuConfig->foreach_volume_full($conf->{pending}, $include_opts, $remove_owned_drive);
eval { PVE::Storage::vdisk_free($storecfg, $volid) };
if (defined $replacement_conf) {
PVE::QemuConfig->write_config($vmid, $replacement_conf);
} else {
PVE::QemuConfig->destroy_config($vmid);
}
}
my $fleecing_section_schema = {};
'''

API = r'''
name => 'destroy_vm',
parameters => { properties => { purge => {} } },
returns => {},
PVE::QemuConfig->lock_config(
my $ha_managed = $early_checks->();
PVE::QemuServer::destroy_vm(
{ lock => 'destroyed' }
PVE::AccessControl::remove_vm_access($vmid);
PVE::Firewall::remove_vmfw_conf($vmid);
PVE::QemuConfig->destroy_config($vmid);
name => 'unlink',
'''


class QMDestroyContractTests(unittest.TestCase):
    def test_exact_warn_continue_and_final_removal_order_is_qualified(self):
        destroy, api = module.qualify(QEMU, API)
        self.assertIn("vdisk_free", destroy)
        self.assertIn("destroy_config", api)

    def test_api14_direct_owner_lookup_and_warn_are_qualified(self):
        api14 = QEMU.replace(
            "my ($path, $owner) = eval { PVE::Storage::path($storecfg, $volid) };",
            "my ($path, $owner) = PVE::Storage::path($storecfg, $volid);",
        ).replace(
            "log_warn(\"Could not remove disk '$volid', check manually: $@\") if $@;",
            "warn \"Could not remove disk '$volid', check manually: $@\" if $@;",
        )
        destroy, _ = module.qualify(api14, API)
        self.assertIn("PVE::Storage::path", destroy)

    def test_ambiguous_owner_lookup_variants_are_retested(self):
        changed = QEMU.replace(
            "return if !$path || !$owner || ($owner != $vmid);",
            "my ($path, $owner) = PVE::Storage::path($storecfg, $volid);\n"
            "return if !$path || !$owner || ($owner != $vmid);",
        )
        with self.assertRaisesRegex(RuntimeError, "no single qualified upstream variant"):
            module.qualify(changed, API)

    def test_missing_warning_boundary_is_retested(self):
        with self.assertRaisesRegex(RuntimeError, "no single qualified upstream variant"):
            module.qualify(QEMU.replace("log_warn(\"Could not remove disk '$volid', check manually: $@\") if $@;", ""), API)

    def test_native_digest_contract_change_requires_requalification(self):
        changed = API.replace("purge => {}", "purge => {}, digest => {}")
        with self.assertRaisesRegex(RuntimeError, "digest parameter"):
            module.qualify(QEMU, changed)

    def test_config_removal_before_destroy_is_retested(self):
        changed = API.replace(
            "PVE::QemuServer::destroy_vm(\n{ lock => 'destroyed' }",
            "PVE::QemuConfig->destroy_config($vmid);\nPVE::QemuServer::destroy_vm(\n{ lock => 'destroyed' }",
        )
        with self.assertRaisesRegex(RuntimeError, "cardinality"):
            module.qualify(QEMU, changed)


if __name__ == "__main__":
    unittest.main()
