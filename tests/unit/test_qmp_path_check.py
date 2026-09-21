import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import mock_open, patch


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check"


def load_helper():
    loader = importlib.machinery.SourceFileLoader("qmp_path_check", str(HELPER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class QmpPathCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def test_collects_only_canonical_device_filenames(self):
        result = set()
        self.helper.collect_device_paths({
            "filename": "/dev/mapper/example",
            "backing-image": {"filename": "json:not-a-device"},
            "children": [{"filename": "/tmp/not-a-device"}],
        }, result)
        self.assertEqual(result, {os.path.realpath("/dev/mapper/example")})

    def test_slot_grammar_excludes_shell_and_path_syntax(self):
        self.assertIsNotNone(self.helper.SLOT_RE.fullmatch("scsi12"))
        for value in ("scsi0;id", "../scsi0", "unused0", "scsi"):
            self.assertIsNone(self.helper.SLOT_RE.fullmatch(value))

    @patch("builtins.open", new_callable=mock_open, read_data="LVM-vg-lv\n")
    @patch("os.stat")
    def test_device_identity_uses_block_devno_and_dm_uuid(self, stat_mock, _open_mock):
        stat_mock.return_value.st_mode = 0o060000
        stat_mock.return_value.st_rdev = os.makedev(252, 17)
        self.assertEqual(
            self.helper.device_identity("/dev/dm-17"),
            ("252:17", "LVM-vg-lv"),
        )

    @patch("os.stat")
    def test_device_identity_rejects_non_block_path(self, stat_mock):
        stat_mock.return_value.st_mode = 0o100000
        stat_mock.return_value.st_rdev = os.makedev(252, 17)
        self.assertIsNone(self.helper.device_identity("/dev/dm-17"))

    @patch("builtins.open", new_callable=mock_open, read_data="\n")
    @patch("os.stat")
    def test_device_identity_rejects_missing_dm_uuid(self, stat_mock, _open_mock):
        stat_mock.return_value.st_mode = 0o060000
        stat_mock.return_value.st_rdev = os.makedev(252, 17)
        self.assertIsNone(self.helper.device_identity("/dev/dm-17"))

    def test_stable_identity_rejects_kernel_object_replacement(self):
        with patch.object(
            self.helper, "device_identity", return_value=("252:18", "LVM-other")
        ):
            identity, error = self.helper.verify_stable_identity(
                "/dev/dm-17", ("252:17", "LVM-vg-lv")
            )
        self.assertIsNone(identity)
        self.assertEqual(error, "changed")

    def test_stable_identity_accepts_exact_second_observation(self):
        expected = ("252:17", "LVM-vg-lv")
        with patch.object(self.helper, "device_identity", return_value=expected):
            identity, error = self.helper.verify_stable_identity("/dev/dm-17", expected)
        self.assertEqual(identity, expected)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
