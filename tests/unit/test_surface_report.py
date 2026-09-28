import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/exact-surface-test-report.py"
CATALOGUE = ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"


def load_module():
    spec = importlib.util.spec_from_file_location("slt_surface_report", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SurfaceReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_report_runs_exact_catalogue_tests_and_seals_results(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            result = self.module.main([
                "--source-root", str(ROOT), "--catalogue", str(CATALOGUE),
                "--contract-fingerprint", "a" * 64,
                "--node", "node-a", "--plugin-commit", "deadbeef",
                "--output", str(output),
            ])
            self.assertEqual(result, 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            report = document["report"]
            self.assertEqual({row["status"] for row in report["results"]}, {"PASS"})
            catalogue = json.loads(CATALOGUE.read_text(encoding="utf-8"))
            coverage = catalogue["source_dependency_coverage"]
            self.assertEqual(
                len(report["results"]),
                sum(len(coverage[name]) for name in ("commands", "perl_symbols", "hooks")),
            )
            self.assertEqual(
                len({row["id"] for row in report["results"]}),
                len(report["results"]),
            )
            self.assertEqual(
                document["sha256"],
                hashlib.sha256(json.dumps(
                    report, sort_keys=True, separators=(",", ":")
                ).encode()).hexdigest(),
            )


if __name__ == "__main__":
    unittest.main()
