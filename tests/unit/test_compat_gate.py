import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-gate"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_compat_gate", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CompatibilityGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_gate_can_never_authorize_from_local_steps(self):
        verdict, blocking = self.module.classify([
            {"name": "one", "passed": True},
            {"name": "two", "passed": True},
        ])
        self.assertEqual(verdict, "RETEST_REQUIRED")
        self.assertEqual(blocking, [])

    def test_incomplete_step_blocks(self):
        verdict, blocking = self.module.classify([
            {"name": "inventory", "passed": False},
        ])
        self.assertEqual(verdict, "BLOCKED")
        self.assertEqual(blocking, ["inventory"])

    def test_qmdestroy_semantics_are_a_required_pre_inventory_gate(self):
        source = SCRIPT.read_text(encoding="utf-8")
        qmdestroy = source.index('"qmdestroy-contract"')
        inventory = source.index('"upstream-inventory"', qmdestroy)
        self.assertLess(qmdestroy, inventory)
        self.assertIn("sharedlvmthin-qmdestroy-contract-check", source[qmdestroy:inventory])

    def test_lifecycle_adapter_precedes_all_mutation_semantic_checks(self):
        source = SCRIPT.read_text(encoding="utf-8")
        adapter = source.index('"lifecycle-adapter"')
        qmdestroy = source.index('"qmdestroy-contract"')
        inventory = source.index('"upstream-inventory"', qmdestroy)
        self.assertLess(adapter, qmdestroy)
        self.assertLess(qmdestroy, inventory)
        self.assertIn("sharedlvmthin-lifecycle-adapter-check", source[adapter:qmdestroy])
        self.assertIn("accepted=(3,)", source[adapter:qmdestroy])

    @unittest.skipUnless(os.name == "posix", "process-group gate test requires POSIX")
    def test_step_records_bounded_success_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.module.run_step(
                "probe", ["/bin/sh", "-c", "printf output; printf error >&2"],
                Path(directory), timeout=5,
            )
            self.assertTrue(result["passed"])
            self.assertFalse(result["stdout"]["truncated"])
            self.assertEqual((Path(directory) / "probe.stdout").read_text(), "output")
            self.assertEqual((Path(directory) / "probe.stderr").read_text(), "error")

    @unittest.skipUnless(os.name == "posix", "process-group gate test requires POSIX")
    def test_timeout_is_blocking_and_records_it(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.module.run_step(
                "probe", ["/bin/sh", "-c", "sleep 10"],
                Path(directory), timeout=0.05,
            )
            self.assertTrue(result["timed_out"])
            self.assertFalse(result["passed"])


if __name__ == "__main__":
    unittest.main()
