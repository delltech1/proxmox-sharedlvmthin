import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "experiments/thick-generations/recovery-evidence.sh"


class ThickRecoveryHarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = COLLECTOR.read_text(encoding="utf-8")

    def test_shell_syntax(self):
        result = subprocess.run(
            ["sh", "-n", str(COLLECTOR)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_requires_exact_mapper_device(self):
        self.assertIn('/dev/mapper/*', self.source)
        self.assertIn('pinned mapper device is not a block device', self.source)

    def test_lvm_inventory_is_single_scoped_and_read_only(self):
        self.assertEqual(len(re.findall(r"run_guarded vgs ", self.source)), 1)
        self.assertEqual(len(re.findall(r"run_guarded lvs ", self.source)), 1)
        self.assertEqual(self.source.count('--devices "$device"'), 2)
        self.assertGreaterEqual(self.source.count("--readonly"), 2)

    def test_dm_table_and_status_are_noflush(self):
        self.assertIn('dmsetup table --noflush "$mapper"', self.source)
        self.assertIn('dmsetup status --noflush "$mapper"', self.source)

    def test_collector_contains_no_mutating_storage_commands(self):
        forbidden = (
            "lvcreate", "lvremove", "lvchange", "lvextend", "lvreduce",
            "pvcreate", "vgcreate", "wipefs", "dmsetup create", "dmsetup load",
            "dmsetup reload", "dmsetup suspend", "dmsetup resume", "dmsetup remove",
        )
        for command in forbidden:
            self.assertNotIn(command, self.source)

    def test_existing_output_is_never_overwritten(self):
        self.assertIn('[ ! -e "$output_dir" ]', self.source)


if __name__ == "__main__":
    unittest.main()
