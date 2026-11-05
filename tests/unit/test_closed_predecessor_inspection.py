"""Pure consistency checks only; these tests do not authorize recovery."""
import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest import mock

RUNTIME = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"
with mock.patch.object(sys, "path", [str(RUNTIME), *sys.path]):
    spec = importlib.util.spec_from_file_location(
        "closed_predecessor", Path(__file__).resolve().parents[2] /
        "experiments/thick-generations/closed_predecessor_prototype.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)


class ClosedPredecessorInspectionTests(unittest.TestCase):
    old_boot = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    new_boot = "11111111-2222-3333-4444-555555555555"

    def setUp(self):
        prepared = {"schema": 2, "state": "PREPARED", "txid": "a" * 32,
                    "hostname": "node", "boot_id": self.old_boot,
                    "parent_ledger_sha256": "b" * 64,
                    "target_package": "pve-sharedlvmthin", "target_version": "1",
                    "candidate_sha256": "c" * 64, "candidate_artifact_sha256": "d" * 64,
                    "candidate_manifest_sha256": "e" * 64}
        self.freeze = {"phase": "ACTIVE", "generation": "f" * 32}
        self.completed = dict(prepared, state="COMPLETE", successor_generation="f" * 32,
                              successor_ledger_sha256=helper.digest(self.freeze))
        self.pending = {"schema": 1, "state": "PACKAGE_SETTLED_RUNTIME_CLOSED", "authority": "NONE",
                        "reason": "UNLISTED_RUNTIME_TUPLE", "prepared_transition": prepared,
                        "transition_sha256": helper.digest(prepared), "txid": "a" * 32,
                        "boot_id": self.old_boot, "kernel_release": "kernel",
                        "plugin_package": "pve-sharedlvmthin", "plugin_version": "1",
                        "runtime_build_id": "1" * 64, "runtime_identity_sha256": "2" * 64,
                        "plan_digest": "c" * 64, "artifact_sha256": "d" * 64,
                        "runtime_manifest_sha256": "e" * 64}
        keys = ("schema", "runtime_build_id", "artifact_sha256", "plan_digest", "boot_id",
                "kernel_release", "plugin_package", "plugin_version", "runtime_manifest_sha256",
                "runtime_identity_sha256", "transition_sha256")
        self.release = {k: self.pending[k] for k in keys}
        self.release.update(qualified="UNQUALIFIED", qualification_txid="a" * 32,
                            verdict="PACKAGE_ONLY_SETTLEMENT", reason="UNLISTED_RUNTIME_TUPLE")

    def inspect(self, host="node", boot=None):
        return helper.inspect_predecessor(self.pending, self.release, self.completed, self.freeze,
                                          hostname=host, boot_id=boot or self.new_boot)

    def test_consistency_is_non_authorizing_and_inputs_are_unchanged(self):
        before = copy.deepcopy((self.pending, self.release, self.completed, self.freeze))
        result = self.inspect()
        self.assertEqual(result["result"], "CONSISTENT_ONLY")
        self.assertEqual(result["authority"], "NONE")
        self.assertEqual(result["freeze_ledger_sha256"], helper.digest(self.freeze))
        self.assertEqual((self.pending, self.release, self.completed, self.freeze), before)

    def test_node_and_boot_mismatches_refuse(self):
        for host, boot in (("other", self.new_boot), ("node", self.old_boot), ("node", "invalid")):
            with self.subTest(host=host, boot=boot), self.assertRaises(helper.PredecessorMismatch):
                self.inspect(host, boot)
        self.pending["boot_id"] = self.new_boot
        with self.assertRaises(helper.PredecessorMismatch):
            self.inspect()

    def test_each_record_mismatch_refuses(self):
        for name, key, value in (("pending", "artifact_sha256", "9" * 64),
                                 ("pending", "transition_sha256", "9" * 64),
                                 ("release", "qualified", "QUALIFIED"),
                                 ("completed", "txid", "9" * 32),
                                 ("completed", "successor_ledger_sha256", "9" * 64),
                                 ("freeze", "generation", "9" * 32)):
            with self.subTest(record=name, key=key):
                self.setUp()
                getattr(self, name)[key] = value
                with self.assertRaises(helper.PredecessorMismatch):
                    self.inspect()

    def test_missing_or_malformed_inputs_refuse(self):
        for record in (None, {}, [], "closed"):
            with self.subTest(record=record), self.assertRaises(helper.PredecessorMismatch):
                helper.inspect_predecessor(record, self.release, self.completed, self.freeze,
                                           hostname="node", boot_id=self.new_boot)


if __name__ == "__main__":
    unittest.main()
