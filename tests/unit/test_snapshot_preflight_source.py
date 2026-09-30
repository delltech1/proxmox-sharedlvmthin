import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-snapshot-preflight"
CLI = ROOT / "usr/sbin/sharedlvmthin"
BUILD = ROOT / "scripts/build.sh"


class SnapshotPreflightSourceTests(unittest.TestCase):
    def test_preflight_is_read_only_and_binds_vm_config(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("CONFIG_SHA256=", source)
        self.assertIn("_thick_read_anchor", source)
        self.assertIn("PVE::Storage::volume_has_feature", source)
        self.assertNotIn("run_command", source)
        self.assertNotIn("_with_vg_lock", source)
        self.assertNotIn("_set_vg_intent", source)
        self.assertNotIn("_lazy_materialize_volume", source)
        self.assertNotIn("activate_volume", source)

    def test_lazy_states_have_actionable_fail_closed_results(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("LAZY_ACTIVE", source)
        self.assertIn("LAZY_DORMANT", source)
        self.assertIn("thick-lazy-materialize", source)
        self.assertIn("does not perform implicitly", source)
        self.assertIn("SNAPSHOT_READY=NO", source)
        self.assertIn("RESULT=BLOCKED", source)

    def test_cli_and_package_expose_the_preflight(self):
        cli = CLI.read_text(encoding="utf-8")
        build = BUILD.read_text(encoding="utf-8")
        self.assertIn("snapshot-preflight)", cli)
        self.assertIn("sharedlvmthin snapshot-preflight <vmid> [--ram]", cli)
        self.assertIn("sharedlvmthin-snapshot-preflight", build)


if __name__ == "__main__":
    unittest.main()
