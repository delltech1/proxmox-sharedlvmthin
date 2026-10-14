import json
import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class ReleaseContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        match = re.search(r"^Version:\s*(\S+)\s*$", control, re.MULTILINE)
        if not match:
            raise AssertionError("DEBIAN/control has no exact Version")
        cls.version = match.group(1)
        cls.catalogue = json.loads((
            ROOT / "usr/share/pve-sharedlvmthin/pve-qualified-tuples.json"
        ).read_text(encoding="utf-8"))

    def test_current_api14_scope_is_not_overclaimed(self):
        api14 = [item for item in self.catalogue["tuples"] if item.get("api") == 14]
        exact = [item for item in api14 if self.version in item.get("plugin_versions", [])
                 and item.get("status") == "EXACT_LAB_TESTED"]
        compatibility = (ROOT / "docs/compatibility.md").read_text(encoding="utf-8")
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        notes = (ROOT / "docs/RELEASE-NOTES-RC5.79-TG53.md").read_text(encoding="utf-8")
        if exact:
            self.assertIn("API 14", compatibility)
        else:
            current = re.search(r"~rc(\d+\.\d+)~tg", self.version)
            self.assertIsNotNone(current)
            current_label = f"RC{current.group(1)}"
            for document in (compatibility, readme):
                self.assertIn(current_label, document)
                self.assertIn("API 14", document)
                self.assertIn("RETEST_REQUIRED", document)
            # Historical release notes remain immutable and must describe their
            # own artifact rather than being silently rewritten for a newer RC.
            self.assertIn("RC5.79", notes)
            self.assertIn("API 14", notes)
            self.assertIn("RETEST_REQUIRED", notes)

    def test_no_retest_tuple_is_automatic_exact_qualification(self):
        for item in self.catalogue["tuples"]:
            if item.get("status") == "RETEST_REQUIRED":
                self.assertTrue(item.get("required_tests"), item.get("id"))
                self.assertNotEqual(item.get("status"), "EXACT_LAB_TESTED")

    def test_api16_rolling_targets_preserve_distinct_source_combinations(self):
        rows = {item["id"]: item for item in self.catalogue["tuples"]}
        pairs = [
            ("original", "9.2.18", "7.0.14-16-pve"),
            ("k17", "9.2.20", "7.0.14-17-pve"),
        ]
        for name, manager, kernel in pairs:
            target = rows[f"rc594-api16-{name}-target"]
            source = rows[f"rc594-api15-{name}-source"]
            self.assertEqual(target["status"], "RETEST_REQUIRED")
            self.assertEqual(target["packages"]["pve-manager"], manager)
            self.assertEqual(target["running_kernel"], kernel)
            self.assertEqual(target["packages"]["pve-qemu-kvm"], "11.0.3-3")
            self.assertEqual(target["packages"]["qemu-server"], "9.2.10")
            self.assertEqual(target["packages"]["libpve-storage-perl"], "9.1.12")
            self.assertEqual(target["packages"]["libpve-common-perl"], "9.2.3")
            self.assertEqual(source["api"], 15)
            self.assertEqual(target["api"], 16)
            edge = next(item for item in self.catalogue["rolling_edges"]
                        if item.get("from") == source["id"]
                        and item.get("to") == target["id"])
            self.assertEqual(edge["status"], "RETEST_REQUIRED")
            self.assertIn("post-reboot-san", edge["required_tests"])
        self.assertIn("rc593-api16-bridge-target", rows)

    def test_current_artifact_is_only_listed_as_retest_required(self):
        current = [
            item for item in self.catalogue["tuples"]
            if self.version in item.get("plugin_versions", [])
        ]
        self.assertTrue(current, self.version)
        self.assertEqual(
            {item.get("status") for item in current},
            {"RETEST_REQUIRED"},
            [(item.get("id"), item.get("status")) for item in current],
        )
        for item in current:
            self.assertTrue(item.get("required_tests"), item.get("id"))

    def test_current_api14_candidate_is_exact_and_san_excluded(self):
        current = [
            item for item in self.catalogue["tuples"]
            if item.get("api") == 14
            and self.version in item.get("plugin_versions", [])
        ]
        self.assertEqual(len(current), 1, [item.get("id") for item in current])
        candidate = current[0]
        self.assertEqual(candidate.get("profiles"), ["dual", "thick-only"])
        self.assertEqual(candidate.get("status"), "RETEST_REQUIRED")
        self.assertIn("san-dataplane", candidate.get("excluded_scopes", []))
        self.assertIn("api14-clean-install", candidate.get("required_tests", []))
        self.assertIn("api14-in-place-upgrade", candidate.get("required_tests", []))
        self.assertIn(
            "dual-thick-dual-profile-cycle", candidate.get("required_tests", [])
        )

    def test_rc597_target_and_directed_update_are_exact_unqualified_candidates(self):
        rows = {item["id"]: item for item in self.catalogue["tuples"]}
        source = rows["rc597-api16-update-source"]
        target = rows["rc597-api16-k23-target"]
        for row in (source, target):
            self.assertEqual(row["plugin_versions"], ["0.9.0~rc5.97~tg53"])
            self.assertEqual(row["api"], 16)
            self.assertEqual(row["status"], "RETEST_REQUIRED")
            self.assertEqual(row["profiles"], ["dual", "thick-only"])
        self.assertEqual(target["packages"], {
            "pve-manager": "9.2.21", "libpve-storage-perl": "9.1.13",
            "qemu-server": "9.2.11", "pve-qemu-kvm": "11.0.3-4",
            "libpve-common-perl": "9.2.3",
        })
        self.assertEqual(target["running_kernel"], "7.0.14-23-pve")
        self.assertEqual(source["packages"], rows["rc594-api16-bridge-target"]["packages"])
        self.assertEqual(source["running_kernel"], "7.0.14-20-pve")
        edges = [edge for edge in self.catalogue["rolling_edges"]
                 if edge.get("id", "").startswith("rc597-")]
        self.assertEqual(len(edges), 4)
        self.assertEqual({(edge["from"], edge["to"]) for edge in edges}, {
            (source["id"], target["id"]),
            ("rc597-api15-bridge-source", target["id"]),
            (source["id"], "rc597-api16-k20-postupdate"),
            ("rc597-api16-k20-postupdate", target["id"]),
        })
        for edge in edges:
            self.assertEqual(edge["directions"], [])
            self.assertEqual(edge["status"], "RETEST_REQUIRED")
            if edge["to"] == target["id"]:
                self.assertIn("post-reboot-san", edge["required_tests"])
            self.assertIn("exact-build-evidence-binding", edge["required_tests"])
        bridge = next(edge for edge in edges if edge["from"] == "rc597-api15-bridge-source")
        self.assertEqual(bridge["api_transition"], {
            "from": 15, "to": 16, "package": "libpve-storage-perl",
            "old_version": "9.1.11", "new_version": "9.1.13",
        })
        self.assertIn("plugin-first-api15-api16-live-io-upgrade", bridge["required_tests"])
        self.assertIn("mixed-api15-api16-migration", bridge["required_tests"])
        for row in self.catalogue["tuples"]:
            if not row["id"].startswith("rc597-"):
                self.assertNotIn("0.9.0~rc5.97~tg53", row.get("plugin_versions", []))

    def test_rc597_metadata_and_notes_preserve_scoped_experimental_release(self):
        thick = (ROOT / "packaging/thick-only/control").read_text(encoding="utf-8")
        self.assertIn("Version: " + self.version + "\n", thick)
        notes = (ROOT / "docs/RELEASE-NOTES-RC5.97-TG53.md").read_text(encoding="utf-8")
        self.assertIn("experimental API14–16 bridge", notes)
        self.assertIn("RETEST_REQUIRED", notes)
        self.assertIn("historical tests are not silently", notes)
        self.assertIn("relabeled as fresh tests of another artifact", notes)
        self.assertIn("2,594 passed and 53", notes)
        self.assertIn("1,691 Perl tests", notes)

    def test_rc597_running_kernel_intermediate_and_source_families(self):
        spec = importlib.util.spec_from_file_location("rc597_policy", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py")
        policy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(policy)
        rows = {item["id"]: item for item in self.catalogue["tuples"]}
        for name in ("original", "k17"):
            new, old = rows[f"rc597-api16-{name}-source"], rows[f"rc594-api16-{name}-target"]
            self.assertEqual(new["packages"], old["packages"])
            self.assertEqual(new["running_kernel"], old["running_kernel"])
            self.assertEqual(new["status"], "RETEST_REQUIRED")
            self.assertEqual(new["plugin_versions"], ["0.9.0~rc5.97~tg53"])
        versions = rows["rc597-api16-k23-target"]["packages"]
        for profile in ("dual", "thick-only"):
            for kernel, expected in (("7.0.14-20-pve", "rc597-api16-k20-postupdate"),
                                     ("7.0.14-23-pve", "rc597-api16-k23-target"),
                                     ("7.0.14-21-pve", None)):
                args = dict(profile=profile, running_kernel=kernel, plugin_version=self.version)
                observed = policy.observed_tuple(self.catalogue, versions, api=16, **args)
                self.assertEqual(observed["id"] if observed else None, expected)
                self.assertIsNone(policy.planned_tuple(self.catalogue, versions, scope="san-dataplane", **args))


if __name__ == "__main__":
    unittest.main()
