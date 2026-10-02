import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "usr" / "share" / "perl5" / "PVE" / "Storage" / "Custom" / "SharedLvmThinPlugin.pm"


class ScopedVgStatusSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = PLUGIN.read_text(encoding="utf-8")

    def test_storage_hooks_do_not_use_global_upstream_lvm_vgs_parser(self):
        self.assertNotIn("PVE::Storage::LVMPlugin::lvm_vgs()", self.source)
        self.assertIn("sub _scoped_vg_status", self.source)

    def test_probe_is_read_only_json_device_and_vg_scoped(self):
        start = self.source.index("sub _scoped_vg_status")
        end = self.source.index("\nsub list_images", start)
        helper = self.source[start:end]
        for token in (
            "--readonly", "--reportformat", "json", "--devices",
            "vg_name=$vg", "vg_uuid", "vg_size", "vg_free",
        ):
            self.assertIn(token, helper)
        for mutator in ("lvcreate", "lvremove", "lvchange", "vgchange", "dmsetup"):
            self.assertNotIn(mutator, helper)

    def test_capacity_and_identity_fail_closed(self):
        start = self.source.index("sub _scoped_vg_status")
        end = self.source.index("\nsub list_images", start)
        helper = self.source[start:end]
        self.assertIn("UUID mismatch", helper)
        self.assertIn("overflowing", helper)
        self.assertIn("impossible capacity", helper)
        self.assertIn("state => 'ABSENT'", helper)
        self.assertIn("state => 'FOUND'", helper)


if __name__ == "__main__":
    unittest.main()
