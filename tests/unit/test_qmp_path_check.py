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

    def test_active_path_never_accepts_expected_backing_image(self):
        device = {"inserted": {
            "file": "/dev/mapper/foreign",
            "image": {"filename": "/dev/mapper/foreign",
                      "backing-image": {"filename": "/dev/mapper/expected"}},
        }}
        self.assertEqual(self.helper.active_device_path(device),
                         os.path.realpath("/dev/mapper/foreign"))

    def test_active_path_accepts_legacy_image_filename_only(self):
        device = {"inserted": {"image": {"filename": "/dev/mapper/expected"}}}
        self.assertEqual(self.helper.active_device_path(device),
                         os.path.realpath("/dev/mapper/expected"))

    def test_active_path_rejects_conflicting_or_invalid_explicit_file(self):
        self.assertIsNone(self.helper.active_device_path({"inserted": {
            "file": "/dev/mapper/expected",
            "image": {"filename": "/dev/mapper/foreign"},
        }}))
        self.assertIsNone(self.helper.active_device_path({"inserted": {
            "file": "/tmp/not-a-device",
            "image": {"filename": "/dev/mapper/expected"},
        }}))

    def test_match_rejects_expected_path_only_as_backing(self):
        expected = {"scsi0": os.path.realpath("/dev/mapper/expected")}
        devices = [{"qdev": "scsi0", "inserted": {
            "file": "/dev/mapper/foreign",
            "image": {"filename": "/dev/mapper/foreign",
                      "backing-image": {"filename": "/dev/mapper/expected"}},
        }}]
        with self.assertRaisesRegex(ValueError, "divergence"):
            self.helper.match_active_devices(devices, expected)

    def test_match_rejects_duplicate_slot_records(self):
        expected = {"scsi0": os.path.realpath("/dev/mapper/expected")}
        record = {"qdev": "scsi0", "inserted": {"file": "/dev/mapper/expected"}}
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.helper.match_active_devices([record, record], expected)

    def test_block_job_proof_requires_empty_lists_on_both_sides(self):
        self.assertTrue(self.helper.block_jobs_settled([], []))
        for before, after in (([{"device": "drive-scsi0"}], []),
                              ([], [{"device": "drive-scsi0"}]),
                              ({}, []), ([], None)):
            self.assertFalse(self.helper.block_jobs_settled(before, after))

    def test_slot_grammar_excludes_shell_and_path_syntax(self):
        self.assertIsNotNone(self.helper.SLOT_RE.fullmatch("scsi12"))
        for value in ("scsi0;id", "../scsi0", "unused0", "scsi"):
            self.assertIsNone(self.helper.SLOT_RE.fullmatch(value))

    @patch("builtins.open", new_callable=mock_open, read_data="LVM-vg-lv\n")
    @patch("os.stat")
    @unittest.skipUnless(os.name == "posix", "requires POSIX device numbers")
    def test_device_identity_uses_block_devno_and_dm_uuid(self, stat_mock, _open_mock):
        stat_mock.return_value.st_mode = 0o060000
        stat_mock.return_value.st_rdev = os.makedev(252, 17)
        self.assertEqual(
            self.helper.device_identity("/dev/dm-17"),
            ("252:17", "LVM-vg-lv"),
        )

    @patch("os.stat")
    @unittest.skipUnless(os.name == "posix", "requires POSIX device numbers")
    def test_device_identity_rejects_non_block_path(self, stat_mock):
        stat_mock.return_value.st_mode = 0o100000
        stat_mock.return_value.st_rdev = os.makedev(252, 17)
        self.assertIsNone(self.helper.device_identity("/dev/dm-17"))

    @patch("builtins.open", new_callable=mock_open, read_data="\n")
    @patch("os.stat")
    @unittest.skipUnless(os.name == "posix", "requires POSIX device numbers")
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
