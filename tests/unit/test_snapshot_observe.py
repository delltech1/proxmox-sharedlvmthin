import importlib.machinery
import importlib.util
from pathlib import Path
import unittest
import tempfile
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-snapshot-observe"


def load_observer():
    loader = importlib.machinery.SourceFileLoader("snapshot_observer", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class SnapshotObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_observer()
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_qmp_states_are_conservative(self):
        classify = self.module.classify_qmp
        self.assertEqual(classify({}), "IDLE_OBSERVED")
        self.assertEqual(classify({"status": "active"}), "ACTIVE")
        self.assertEqual(classify({"status": "completed"}), "COMPLETED")
        self.assertEqual(classify({"status": "failed"}), "FAILED")
        self.assertEqual(classify({"bytes": 4096}), "UNKNOWN")
        self.assertEqual(classify([]), "UNKNOWN")

    def test_runtime_calls_are_read_only_and_bounded(self):
        self.assertIn("timeout=min(remaining, 5)", self.source)
        self.assertIn("STORAGE_MUTATION_AUTHORIZED=NO", self.source)
        self.assertIn("SAVEVM_END_SENT=NO", self.source)
        for forbidden in (
            '"savevm-end"', '"qm", "delsnapshot"', '"lvremove"',
            '"lvchange"', '"dmsetup", "remove"', '"pvesm", "free"',
        ):
            self.assertNotIn(forbidden, self.source)

    def test_inventory_uses_json_pve_api_not_version_sensitive_pvesm_cli(self):
        self.assertIn('"pvesh", "get"', self.source)
        self.assertIn('"--output-format", "json"', self.source)
        self.assertNotIn('["pvesm", "list"', self.source)

    def parse_fixture(self, body):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "qemu-server"
            config.mkdir()
            (config / "999.conf").write_text(body, encoding="utf-8")
            real_path = self.module.Path

            def mapped_path(value):
                if str(value) == "/etc/pve/qemu-server/999.conf":
                    return config / "999.conf"
                return real_path(value)

            with mock.patch.object(self.module, "Path", side_effect=mapped_path):
                return self.module.read_snapshot_config("999", "snap")

    def test_snapshot_parser_proves_exact_complete_virtio_mtu_set(self):
        parsed = self.parse_fixture(
            "[snap]\nnet0: virtio=aa,bridge=vmbr0\n"
            "net1: virtio=bb,link_down=1\nnet2: e1000=cc\n"
            "vmstate: store:vm-999-state-snap\n"
            "running-nets-host-mtu: net0=1500,net1=0\n"
        )
        self.assertEqual(parsed[5], ["net0", "net1"])
        self.assertEqual(parsed[6], ["net0", "net1"])
        self.assertFalse(parsed[7])
        self.assertEqual(parsed[8], 1)

    def test_snapshot_parser_exposes_partial_duplicate_and_empty_mtu(self):
        partial = self.parse_fixture(
            "[snap]\nnet0: virtio=aa\nnet1: virtio=bb\n"
            "vmstate: store:state\nrunning-nets-host-mtu: net0=1500\n"
        )
        self.assertEqual(partial[5], ["net0", "net1"])
        self.assertEqual(partial[6], ["net0"])
        duplicate = self.parse_fixture(
            "[snap]\nnet0: virtio=aa\nvmstate: store:state\n"
            "running-nets-host-mtu: net0=1500\n"
            "running-nets-host-mtu: net0=1500\n"
        )
        self.assertTrue(duplicate[7])
        self.assertEqual(duplicate[8], 2)
        empty = self.parse_fixture(
            "[snap]\nvmstate: store:state\nrunning-nets-host-mtu:\n"
        )
        self.assertEqual(empty[3], [3])
        self.assertEqual(empty[5], [])
        self.assertEqual(empty[6], [])


if __name__ == "__main__":
    unittest.main()
