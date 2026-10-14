import importlib.machinery
import importlib.util
import contextlib
import io
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
        return {"api": 15, "apiage": 6, "packages": packages}

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

    def test_api16_exact_family_resolves_but_never_claims_qualification(self):
        runtime = self.runtime(**{"libpve-storage-perl": "9.1.12", "libpve-common-perl": "9.2.3"})
        runtime.update({"api": 16, "apiage": 7})
        result = self.module.resolve(self.registry, runtime)
        self.assertEqual(result["adapter"], "pve9-api16-qemu9210-v1")
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        self.assertNotIn("QUALIFIED", json.dumps(result))
        runtime["packages"]["libpve-storage-perl"] = "9.1.13"
        self.assertEqual(self.module.resolve(self.registry, runtime)["verdict"], "BLOCKED")

    def test_changed_apiage_fails_closed(self):
        runtime = self.runtime()
        runtime["apiage"] = 7
        result = self.module.resolve(self.registry, runtime)
        self.assertEqual(result["verdict"], "BLOCKED")
        self.assertIsNone(result["adapter"])

    def candidate_runtime(self):
        runtime = self.runtime(**{
            "libpve-storage-perl": "9.1.13", "qemu-server": "9.2.11",
            "libpve-common-perl": "9.2.3",
        })
        runtime.update({"api": 16, "apiage": 7})
        return runtime

    def test_api16_candidate_exact_family_preserves_operation_evidence(self):
        result = self.module.resolve(self.registry, self.candidate_runtime())
        self.assertEqual(result["adapter"], "pve9-api16-qemu9211-v1")
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        previous = next(row for row in self.registry["adapters"]
                        if row["id"] == "pve9-api16-qemu9210-v1")
        self.assertEqual(result["operations"], previous["operations"])

    def test_api16_candidate_rejects_unreviewed_package_cross_products(self):
        for package, version in (
            ("libpve-storage-perl", "9.1.12"),
            ("qemu-server", "9.2.10"),
            ("pve-manager", "9.2.20"),
            ("pve-qemu-kvm", "11.0.3-3"),
            ("libpve-common-perl", "9.2.2"),
        ):
            with self.subTest(package=package, version=version):
                runtime = self.candidate_runtime()
                runtime["packages"][package] = version
                result = self.module.resolve(self.registry, runtime)
                self.assertEqual(result["verdict"], "BLOCKED")
                self.assertIsNone(result["adapter"])
                self.assertEqual(result["operations"], {})

    def test_api16_candidate_cannot_be_mapped_to_api15_or_wrong_apiage(self):
        for api, apiage in ((15, 6), (15, 7), (16, 6)):
            with self.subTest(api=api, apiage=apiage):
                runtime = self.candidate_runtime()
                runtime.update({"api": api, "apiage": apiage})
                self.assertEqual(self.module.resolve(self.registry, runtime)["verdict"],
                                 "BLOCKED")

    def test_legacy_api14_api15_and_api16_families_remain_resolvable(self):
        expected = {
            "pve9-api14-qemu915-v1", "pve9-api15-qemu927-v1",
            "pve9-api15-qemu9210-v1", "pve9-api16-qemu9210-v1",
        }
        rows = [row for row in self.registry["adapters"] if row["id"] in expected]
        self.assertEqual({row["id"] for row in rows}, expected)
        for row in rows:
            with self.subTest(adapter=row["id"]):
                runtime = {"api": row["api"], "apiage": row["apiage"],
                           "packages": {key: values[0] for key, values
                                        in row["package_versions"].items()}}
                result = self.module.resolve(self.registry, runtime)
                self.assertEqual(result["adapter"], row["id"])
                self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_candidate_cli_never_authorizes_upgrade_or_claims_qualification(self):
        # Matching packages alone cannot prove SAN behavior or delegated-user
        # CloudInit permissions, regardless of the caller's privileges.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps(self.candidate_runtime()), encoding="utf-8")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = self.module.main([
                    "--registry", str(REGISTRY), "--catalogue", str(CATALOGUE),
                    "--runtime-json", str(path),
                ])
        result = json.loads(output.getvalue())
        self.assertEqual(code, 3)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        self.assertFalse(result["upgrade_authorized"])
        self.assertNotIn("QUALIFIED", output.getvalue())

    def test_api16_rolling_intermediates_resolve_exactly_with_full_evidence(self):
        previous = next(row for row in self.registry["adapters"]
                        if row["id"] == "pve9-api16-qemu9210-v1")
        for manager in ("9.2.18", "9.2.20"):
            with self.subTest(manager=manager):
                runtime = self.candidate_runtime()
                runtime["packages"].update({
                    "pve-manager": manager, "libpve-storage-perl": "9.1.12",
                    "qemu-server": "9.2.10", "pve-qemu-kvm": "11.0.3-3",
                })
                result = self.module.resolve(self.registry, runtime)
                self.assertEqual(result["adapter"],
                                 f"pve9-api16-qemu9210-manager{manager.replace('.', '')}-v1")
                self.assertEqual(result["verdict"], "RETEST_REQUIRED")
                self.assertEqual(result["operations"], previous["operations"])
                for package, version in (
                    ("pve-qemu-kvm", "11.0.3-4"),
                    ("libpve-storage-perl", "9.1.13"),
                    ("qemu-server", "9.2.11"),
                    ("libpve-common-perl", "9.2.2"),
                    ("pve-manager", "9.2.21"),
                    ("pve-manager", "9.2.19"),
                ):
                    with self.subTest(package=package, version=version):
                        hybrid = json.loads(json.dumps(runtime))
                        hybrid["packages"][package] = version
                        blocked = self.module.resolve(self.registry, hybrid)
                        self.assertEqual(blocked["verdict"], "BLOCKED")
                        self.assertIsNone(blocked["adapter"])
                        self.assertEqual(blocked["operations"], {})

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
        duplicate = json.loads(json.dumps(next(
            row for row in document["adapters"] if row["id"] == "pve9-api15-qemu9210-v1")))
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
