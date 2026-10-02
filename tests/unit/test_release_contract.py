import json
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


if __name__ == "__main__":
    unittest.main()
