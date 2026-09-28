import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-contract-check"
CATALOGUE = ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"
REGISTRY = ROOT / "usr/share/pve-sharedlvmthin/pve-lab-scenarios.json"


def load_checker():
    loader = importlib.machinery.SourceFileLoader("scenario_contract_check", str(CHECKER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LabScenarioRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_checker()
        cls.catalogue = cls.module.strict_load(CATALOGUE)
        cls.registry = cls.module.strict_load(REGISTRY)

    def validate(self, registry=None, root=ROOT):
        return self.module.validate_scenario_registry(
            self.registry if registry is None else registry,
            self.catalogue, root,
        )

    def test_registry_exactly_covers_every_catalogue_lab_test(self):
        errors, blocked = self.validate()
        self.assertEqual(errors, [])
        expected = {
            test for contract in self.catalogue["contracts"]
            for test in contract["lab_tests"]
        }
        self.assertEqual(set(self.registry["scenarios"]), expected)
        ready = {"web-ui-registration", "node-scope", "quorum-read-only"}
        self.assertEqual(set(blocked), expected - ready)
        self.assertEqual(
            {name for name, row in self.registry["scenarios"].items()
             if row["status"] == "READY"},
            ready,
        )

    def test_missing_and_extra_scenarios_fail_closed(self):
        document = json.loads(json.dumps(self.registry))
        del document["scenarios"]["live-migration"]
        document["scenarios"]["invented-pass"] = document["scenarios"]["resize"]
        errors, _ = self.validate(document)
        self.assertIn(
            "lab scenario registry does not exactly cover catalogue lab tests", errors,
        )

    def test_blocked_scenario_cannot_claim_runner(self):
        document = json.loads(json.dumps(self.registry))
        document["scenarios"]["live-migration"]["runner"] = {
            "entrypoint": "tests/unit/test_lab_scenarios.py",
            "subcommand": "live-migration", "revision": 1,
            "code_files": ["tests/unit/test_lab_scenarios.py"],
        }
        errors, _ = self.validate(document)
        self.assertIn("invalid blocked lab scenario: live-migration", errors)

    def test_ready_scenario_requires_exact_runner_and_safe_files(self):
        document = json.loads(json.dumps(self.registry))
        scenario = document["scenarios"]["live-migration"]
        scenario["status"] = "READY"
        scenario["blocked_reason"] = None
        scenario["runner"] = {
            "entrypoint": "../escape", "subcommand": "wrong", "revision": 1,
            "code_files": ["../escape"],
        }
        errors, _ = self.validate(document)
        self.assertIn("invalid ready lab runner: live-migration", errors)

    def test_profile_and_transition_binding_are_derived_not_self_asserted(self):
        document = json.loads(json.dumps(self.registry))
        document["scenarios"]["thin-live-migration"]["profiles"] = ["thick-only"]
        document["scenarios"]["mixed-version-migration"]["binding"] = "class"
        errors, _ = self.validate(document)
        self.assertIn("invalid lab scenario contract: thin-live-migration", errors)
        self.assertIn("invalid lab scenario contract: mixed-version-migration", errors)

    def test_fault_requirement_cannot_be_downgraded(self):
        document = json.loads(json.dumps(self.registry))
        document["scenarios"]["path-loss"]["cases"][0]["fault_required"] = False
        errors, _ = self.validate(document)
        self.assertIn("invalid lab scenario case: path-loss", errors)

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                self.module.strict_load(path)


if __name__ == "__main__":
    unittest.main()
