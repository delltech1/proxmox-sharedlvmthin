import pathlib
import re
import subprocess
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
COLLECTOR = ROOT / "experiments/thick-generations/recovery-evidence.sh"
CLASSIFIER = ROOT / "experiments/thick-generations/classify-evidence.pl"
PLUGIN = ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
FAULT_DRIVER = ROOT / "experiments/thick-generations/fault-driver.pl"
PREPARE_RECOVERY = ROOT / "experiments/thick-generations/recover-prepare-incomplete.pl"


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
        self.assertIn("upper_name=", self.source)

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

    def test_classifier_is_read_only_and_uses_authoritative_library(self):
        source = CLASSIFIER.read_text(encoding="utf-8")
        self.assertIn("classify_recovery(", source)
        self.assertIn("decode_anchor_tags", source)
        self.assertIn("decode_generation_tags", source)
        self.assertIn("decode_vg_intent_tags", source)
        self.assertIn("frontend mapper does not contain an exact", source)
        for command in ("lvcreate", "lvremove", "dmsetup create", "dmsetup load"):
            self.assertNotIn(command, source)

    def test_classifier_syntax(self):
        result = subprocess.run(
            [
                "perl", f"-I{ROOT / 'usr/share/perl5'}", "-c", str(CLASSIFIER),
            ], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fault_boundaries_are_explicit_and_production_inert(self):
        source = PLUGIN.read_text(encoding="utf-8")
        for point in range(10):
            self.assertEqual(
                source.count(f"_thick_fault_point('C{point}'"), 1,
                f"C{point} must identify exactly one crash boundary",
            )
        body = re.search(
            r"sub _thick_fault_point \{(?P<body>.*?)\n\}", source, re.DOTALL
        )
        self.assertIsNotNone(body)
        self.assertEqual(body.group("body").strip(), "return;")
        self.assertNotIn("SLT_FAULT", source)

    def test_fault_driver_requires_disposable_ack_and_process_local_override(self):
        source = FAULT_DRIVER.read_text(encoding="utf-8")
        self.assertIn("DISPOSABLE-DATA-WILL-BE-LEFT-INCOMPLETE", source)
        self.assertIn("no warnings 'redefine'", source)
        self.assertIn("POSIX::_exit(137)", source)
        self.assertNotIn("$ENV", source)

    def test_prepare_recovery_is_exact_and_refuses_partial_objects(self):
        source = PREPARE_RECOVERY.read_text(encoding="utf-8")
        self.assertIn("CLEAR-ONLY-PROVEN-PREPARE-INCOMPLETE", source)
        self.assertIn("_with_vg_lock", source)
        self.assertIn("_thick_verify_frontend", source)
        self.assertIn("transition metadata exists", source)
        self.assertIn("a second HEAD exists", source)
        self.assertEqual(source.count("_clear_vg_intent"), 1)
        for command in ("lvremove", "dmsetup remove", "pvcreate", "vgcreate"):
            self.assertNotIn(command, source)


if __name__ == "__main__":
    unittest.main()
