import importlib.machinery
import importlib.util
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-candidate-inspect"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_candidate_inspect", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CandidateInspectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.source = SCRIPT.read_text(encoding="utf-8")

    def test_member_paths_are_canonical_and_traversal_safe(self):
        self.assertTrue(self.module._safe_member_name("./usr/share/perl5/PVE/Storage.pm"))
        for unsafe in (
            "/usr/share/perl5/PVE/Storage.pm",
            "../usr/share/perl5/PVE/Storage.pm",
            "./usr/../etc/passwd",
            "./usr//share/PVE.pm",
            "usr/share/PVE.pm",
        ):
            self.assertFalse(self.module._safe_member_name(unsafe), unsafe)

    def test_duplicate_inventory_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "inventory.json"
            path.write_text('{"inventory":{},"inventory":{}}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                self.module._strict_json_load(path)

    def test_candidate_layer_never_offers_compatible_or_runtime_authority(self):
        self.assertNotIn('verdict = "COMPATIBLE"', self.source)
        self.assertIn('verdict = "BLOCKED" if errors else "RETEST_REQUIRED"', self.source)
        self.assertIn('"candidate_runtime_authorized": False', self.source)
        self.assertIn('"dependency_plan_complete": False', self.source)

    def test_candidate_code_is_streamed_as_data_not_loaded_or_extracted(self):
        self.assertIn('"dpkg-deb", "--fsys-tarfile"', self.source)
        self.assertIn("tarfile.open", self.source)
        self.assertNotIn("dpkg-deb\", \"-x", self.source)
        self.assertNotIn("PERL5LIB", self.source)
        self.assertNotIn("perl -c", self.source)
        self.assertNotIn("extractall", self.source)


if __name__ == "__main__":
    unittest.main()
