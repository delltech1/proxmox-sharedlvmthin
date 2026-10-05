import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-lifecycle-adapter-check"
REGISTRY = ROOT / "usr/share/pve-sharedlvmthin/pve-lifecycle-adapters.json"
CATALOGUE = ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_lifecycle_adapter", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LifecycleAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.registry = cls.module.strict_load(REGISTRY)
        cls.catalogue = cls.module.strict_load(CATALOGUE)

    def runtime(self, **updates):
        packages = {
            "pve-manager": "9.2.21",
            "libpve-storage-perl": "9.1.11",
            "qemu-server": "9.2.10",
            "pve-qemu-kvm": "11.0.3-4",
            "libpve-common-perl": "9.2.2",
        }
        packages.update(updates)
        return {"api": 15, "apiage": 1, "packages": packages}

    def test_registry_is_complete_and_references_real_contracts(self):
        self.assertEqual(
            self.module.validate_registry(self.registry, self.catalogue), []
        )

    def test_current_qualified_tuple_selects_one_family(self):
        result = self.module.resolve(self.registry, self.runtime())
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        self.assertEqual(result["adapter"], "pve9-api15-qemu9210-v1")
        self.assertEqual(
            set(result["operations"]), set(self.registry["operation_groups"])
        )
        self.assertNotIn("QUALIFIED", json.dumps(result))

    def test_unknown_point_update_fails_closed_without_nearest_fallback(self):
        result = self.module.resolve(
            self.registry, self.runtime(**{"qemu-server": "9.2.11"})
        )
        self.assertEqual(result["verdict"], "BLOCKED")
        self.assertIsNone(result["adapter"])
        self.assertEqual(result["operations"], {})

    def test_operation_resolution_is_bounded(self):
        result = self.module.resolve(
            self.registry, self.runtime(), "snapshot-vmstate"
        )
        self.assertEqual(set(result["operations"]), {"snapshot-vmstate"})
        binding = result["operations"]["snapshot-vmstate"]
        self.assertEqual(binding["contract"], "thick.snapshot-generation")
        self.assertIn("snapshot-preflight", binding["required_preflights"])

    def test_missing_operation_binding_invalidates_entire_adapter(self):
        document = json.loads(json.dumps(self.registry))
        del document["adapters"][0]["operations"]["migration"]
        errors = self.module.validate_registry(document, self.catalogue)
        self.assertIn(
            "pve9-api14-qemu915-v1: operation coverage is incomplete", errors
        )

    def test_ambiguous_adapter_is_blocked(self):
        document = json.loads(json.dumps(self.registry))
        duplicate = json.loads(json.dumps(document["adapters"][-1]))
        duplicate["id"] = "pve9-api15-qemu9210-copy-v1"
        document["adapters"].append(duplicate)
        result = self.module.resolve(document, self.runtime())
        self.assertEqual(result["verdict"], "BLOCKED")
        self.assertEqual(result["reason"], "ambiguous lifecycle family")

    def test_runtime_schema_is_closed(self):
        runtime = self.runtime()
        runtime["guessed"] = True
        with self.assertRaisesRegex(ValueError, "runtime identity is malformed"):
            self.module.validate_runtime(runtime)


if __name__ == "__main__":
    unittest.main()
