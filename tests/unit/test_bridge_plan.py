import importlib.machinery
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLANNER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-plan"


def load_planner():
    loader = importlib.machinery.SourceFileLoader("bridge_plan", str(PLANNER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class BridgePlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.plan = staticmethod(load_planner().classify)

    def classify(self, **updates):
        args = dict(
            phase="MATERIALIZING", authority="source", return_thin=1,
            disk_count=3, thin_count=2, thick_count=1, admission="exact",
        )
        args.update(updates)
        return self.plan(**args)

    def test_partial_source_materialization_continues(self):
        self.assertEqual(self.classify(), "CONTINUE_MATERIALIZE")

    def test_all_thick_source_migrates(self):
        self.assertEqual(self.classify(thin_count=0, thick_count=3), "MIGRATE_THICK")

    def test_all_thick_target_prepares_return(self):
        self.assertEqual(self.classify(
            phase="MIGRATED_THICK", authority="target", thin_count=0,
            thick_count=3, admission="none"), "PREPARE_RETURN_THIN")

    def test_partial_target_return_continues(self):
        self.assertEqual(self.classify(
            phase="RETURN_THIN_PREPARED", authority="target", thin_count=1,
            thick_count=2, admission="none"), "CONTINUE_RETURN_THIN")

    def test_all_thin_target_finalizes(self):
        self.assertEqual(self.classify(
            phase="RETURN_THIN_PREPARED", authority="target", thin_count=3,
            thick_count=0, admission="none"), "FINALIZE_THIN")

    def test_keep_thick_target_finalizes(self):
        self.assertEqual(self.classify(
            phase="MIGRATED_THICK", authority="target", return_thin=0,
            thin_count=0, thick_count=3, admission="none"), "FINALIZE_THICK")

    def test_foreign_admission_always_refuses(self):
        with self.assertRaisesRegex(ValueError, "another transaction"):
            self.classify(admission="foreign")

    def test_foreign_or_missing_disk_refuses(self):
        with self.assertRaisesRegex(ValueError, "foreign or duplicate"):
            self.classify(thin_count=1, thick_count=1)

    def test_target_with_unreleased_admission_refuses(self):
        with self.assertRaisesRegex(ValueError, "released VG admission"):
            self.classify(
                phase="MIGRATED_THICK", authority="target", thin_count=0,
                thick_count=3, admission="exact")

    def test_impossible_phase_authority_pair_refuses(self):
        with self.assertRaisesRegex(ValueError, "incompatible"):
            self.classify(phase="PREPARED", authority="target", admission="none")


if __name__ == "__main__":
    unittest.main()
