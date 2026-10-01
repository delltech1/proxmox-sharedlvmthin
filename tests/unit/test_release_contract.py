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
        notes = (ROOT / "docs/RELEASE-NOTES-RC5.77-TG53.md").read_text(encoding="utf-8")
        if exact:
            self.assertIn("API 14", compatibility)
        else:
            for document in (compatibility, readme, notes):
                self.assertIn("RC5.77", document)
                self.assertIn("API 14", document)
                self.assertIn("RETEST_REQUIRED", document)

    def test_no_retest_tuple_is_automatic_exact_qualification(self):
        for item in self.catalogue["tuples"]:
            if item.get("status") == "RETEST_REQUIRED":
                self.assertTrue(item.get("required_tests"), item.get("id"))
                self.assertNotEqual(item.get("status"), "EXACT_LAB_TESTED")


if __name__ == "__main__":
    unittest.main()
