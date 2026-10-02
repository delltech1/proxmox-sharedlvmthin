import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SOURCE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-snapshot-preflight"
NETWORK_MODULE = ROOT / "usr/share/perl5/PVE/SharedLvmThinSnapshotPreflight.pm"
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

    def test_ram_preflight_reuses_upstream_mtu_probe_and_requires_full_coverage(self):
        source = SOURCE.read_text(encoding="utf-8")
        network = NETWORK_MODULE.read_text(encoding="utf-8")
        self.assertIn("use PVE::QemuServer::Network;", source)
        self.assertIn("PVE::QemuServer::Network::get_nets_host_mtu($vmid, $conf)", source)
        self.assertIn("upstream would write an empty running-nets-host-mtu value", network)
        self.assertIn("evaluate_ram_network", source)
        self.assertIn("exactly covers every VirtIO NIC", source)
        self.assertIn("duplicate key", network)
        self.assertIn("unexpected key", network)
        self.assertIn('if (!$running)', source)

    def test_preflight_parses_the_captured_bytes_strictly_and_rechecks_identity(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn(
            "PVE::QemuServer::parse_vm_config($config_path, $config_bytes, 1)",
            source,
        )
        self.assertNotIn("PVE::QemuConfig->load_config($vmid)", source)
        self.assertIn("configuration changed during preflight observation", source)
        self.assertGreaterEqual(source.count("sha256_hex("), 2)

    def test_preflight_refuses_stale_current_runtime_properties(self):
        source = SOURCE.read_text(encoding="utf-8")
        network = NETWORK_MODULE.read_text(encoding="utf-8")
        self.assertIn(
            "qw(vmstate runningmachine runningcpu running-nets-host-mtu)",
            network,
        )
        self.assertIn("stale current runtime property", network)
        self.assertIn("evaluate_current_runtime_metadata($conf)", source)

    def test_cli_and_package_expose_the_preflight(self):
        cli = CLI.read_text(encoding="utf-8")
        build = BUILD.read_text(encoding="utf-8")
        self.assertIn("snapshot-preflight)", cli)
        self.assertIn("sharedlvmthin snapshot-preflight <vmid> [--ram]", cli)
        self.assertIn("sharedlvmthin-snapshot-preflight", build)


if __name__ == "__main__":
    unittest.main()
