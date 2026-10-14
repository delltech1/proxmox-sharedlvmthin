import importlib.util
from pathlib import Path
import unittest

SOURCE = Path(__file__).resolve().parents[2] / "experiments/thick-generations/release-functional-configurations.py"
spec = importlib.util.spec_from_file_location("release_configurations", SOURCE)
matrix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(matrix)


class FunctionalConfigurationTests(unittest.TestCase):
    def test_all_factor_pairs_are_covered(self):
        self.assertEqual(matrix.missing_pairs(), [])

    def test_absent_supported_rows_are_not_silently_covered(self):
        self.assertTrue(matrix.missing_pairs(matrix.ROWS[:1]))

    def test_plan_has_no_execution_authority(self):
        result = matrix.manifest()
        self.assertEqual(result["execution"], "PLAN_ONLY_NO_LIVE_AUTHORITY")
        self.assertEqual(len(result["rows"]), 14)
        self.assertEqual(len({r["case_id"] for r in result["rows"]}), 14)
        self.assertIn("mixed-thin-eager-lazy-three-disk", result["additional_required_cases"])

    def test_release_operations_are_explicit_and_unique(self):
        operations = matrix.manifest()["required_operations"]
        self.assertEqual(len(operations), len(set(operations)))
        for operation in ("ram-snapshot-rollback-ram-and-disk-sha",
                          "multi-disk-snapshot-rollback-sha",
                          "shrink-native-refusal-preserve-size-and-data",
                          "native-backup-restore-new-id-sha",
                          "six-directed-storage-moves-sha"):
            self.assertIn(operation, operations)

    def test_fault_campaign_and_missing_evidence_are_not_normal_pass(self):
        manifest = matrix.manifest()
        self.assertEqual(set(manifest["campaigns"]), {"release-functional", "burst", "fault"})
        self.assertIn("NOT_RUN", manifest["result_policy"]["missing_adapter"])
        self.assertIn("UNKNOWN", manifest["result_policy"]["ambiguous"])
        self.assertIn("same exact native task", manifest["result_policy"]["slow"])

    def test_every_row_compiles_literal_options_without_execution(self):
        for row in matrix.manifest()["rows"]:
            result = matrix.case_options(row, vmid=999901, storage="disposable", image="/lab/boot.raw", bridge="labbridge")
            self.assertEqual(result["execution"], "OPTIONS_ONLY_NO_LIVE_AUTHORITY")
            self.assertEqual(len(result["data_keys"]), row["disks"])
            self.assertIn("--" + result["boot_key"], result["argv_options"])
            self.assertEqual("--efidisk0" in result["argv_options"], row["firmware"] == "ovmf")
            self.assertEqual(result["machine_option"], "pc" if row["machine"] == "i440fx" else "q35")

    def test_rejects_ambiguous_or_injected_case_options(self):
        row = matrix.manifest()["rows"][0]
        defaults = dict(vmid=999901, storage="disposable", image="/lab/boot.raw", bridge="labbridge")
        for replacement in (dict(vmid=True), dict(storage="store,unsafe=1"), dict(image="/lab/boot.raw,discard=on"), dict(image="C:/lab/boot.raw"), dict(image="//server/boot.raw"), dict(bridge="a\nb")):
            with self.assertRaises(ValueError):
                matrix.case_options(row, **(defaults | replacement))
        for replacement in (dict(disks=True), dict(controller="arbitrary"), dict(extra="unreviewed"), dict(case_id="config-999"), dict(case_id="config-٠١"), dict(case_id="config-02")):
            with self.assertRaises(ValueError):
                matrix.case_options(row | replacement, **defaults)


if __name__ == "__main__":
    unittest.main()
