"""Receipt-exact synthetic preflight; it never grants package authority."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

import test_closed_predecessor_inspection as fixture

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "usr/libexec/pve-sharedlvmthin"
with mock.patch.object(sys, "path", [str(RUNTIME), str(ROOT / "experiments/thick-generations"), *sys.path]):
    spec = importlib.util.spec_from_file_location("closed_preflight_model",
        ROOT / "experiments/thick-generations/closed-successor-preflight-model.py")
    model = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(model)


class ClosedSuccessorPreflightModelTests(unittest.TestCase):
    setUp = fixture.ClosedPredecessorInspectionTests.setUp
    old_boot = fixture.ClosedPredecessorInspectionTests.old_boot
    new_boot = fixture.ClosedPredecessorInspectionTests.new_boot

    def candidate(self):
        return {"source_package": "pve-sharedlvmthin", "source_version": "1",
                "target_package": "pve-sharedlvmthin", "target_version": "2",
                "deb_sha256": "a" * 64, "artifact_sha256": "b" * 64,
                "manifest_sha256": "c" * 64, "tuple_status": "UNLISTED"}

    def observed(self):
        return {"boot_id": self.new_boot, "active_transition": "ABSENT",
                "freeze_and_package_intact": True, "dpkg_exclusive": True,
                "executor_fence_and_inventory": True, "no_competing_dispatch": True,
                "storage_and_workers_safe": True, "maintscript_effects_deferred": True}

    def evaluate(self, candidate=None, observed=None):
        return model.evaluate(self.pending, self.release, self.completed, self.freeze,
                              self.candidate() if candidate is None else candidate,
                              self.observed() if observed is None else observed,
                              hostname="node", boot_id=self.new_boot)

    def test_current_undeferred_maintscript_is_explicit_blocker(self):
        state = copy.deepcopy((self.pending, self.release, self.completed, self.freeze))
        observed = self.observed()
        observed["maintscript_effects_deferred"] = False
        result = self.evaluate(observed=observed)
        self.assertEqual(result["result"], "MODEL_BLOCKED")
        self.assertEqual(result["missing"], ["maintscript_effects_deferred"])
        self.assertEqual(result["authority"], "NONE")
        self.assertFalse(result["dispatch_allowed"])
        self.assertFalse(result["runtime_open"])
        self.assertEqual((self.pending, self.release, self.completed, self.freeze), state)

    def test_all_synthetic_observations_still_never_authorize(self):
        result = self.evaluate()
        self.assertEqual(result["result"], "MODEL_CONSISTENT_ONLY")
        self.assertEqual(result["authority"], "NONE")
        self.assertFalse(result["dispatch_allowed"])
        self.assertFalse(result["runtime_open"])

    def test_changed_predecessor_candidate_boot_and_foreign_successor_refuse(self):
        candidate = self.candidate()
        for key, value in (("tuple_status", "RETEST_REQUIRED"), ("deb_sha256", "bad"),
                           ("source_version", "foreign")):
            changed = dict(candidate, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.evaluate(candidate=changed)
        for key, value in (("boot_id", self.old_boot), ("active_transition", "PREPARED"),
                           ("storage_and_workers_safe", "UNKNOWN")):
            changed = dict(self.observed(), **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.evaluate(observed=changed)
        self.freeze["generation"] = "0" * 32
        with self.assertRaises(ValueError):
            self.evaluate()

    def test_each_missing_observation_blocks_without_authority(self):
        for key, value in self.observed().items():
            if type(value) is not bool:
                continue
            with self.subTest(key=key):
                observed = dict(self.observed(), **{key: False})
                result = self.evaluate(observed=observed)
                self.assertEqual(result["result"], "MODEL_BLOCKED")
                self.assertIn(key, result["missing"])
                self.assertFalse(result["dispatch_allowed"])


if __name__ == "__main__":
    unittest.main()
