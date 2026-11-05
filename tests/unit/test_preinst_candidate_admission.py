import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
class CandidatePreinstAdmissionTests(unittest.TestCase):
    def run_admission(self, rc, *, preflight=0, prepared=0, status="", missing=False):
        source = (ROOT / "DEBIAN/preinst").read_text()
        block = source.split("# Candidate-owned admission", 1) if "# Candidate-owned admission" in source else source.split("# Candidate-side admission", 1)
        self.assertEqual(len(block), 2)
        block = block[1].split("\n", 1)[1].split("# dpkg has not unpacked", 1)[0]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in (("package", "pve-sharedlvmthin\n"), ("version", "candidate\n"),
                                ("manifest", "{}\n"), ("sharedlvmthin_update_policy.py", "")):
                (root / name).write_text(value)
            helper = root / "helper.py"
            if not missing:
                helper.write_text(f"raise SystemExit({rc})\n")
            cmd = root / "sharedlvmthin"
            cmd.write_text("#!/bin/sh\nprintf '%s\\n' \"$TEST_FREEZE_STATUS\"\n")
            cmd.chmod(0o755)
            variables = {"CANDIDATE_UPDATE_POLICY": str(helper),
                         "CANDIDATE_UPDATE_POLICY_LIB": str(root / "sharedlvmthin_update_policy.py"),
                         "CANDIDATE_QUALIFIED_TUPLES": str(root / "manifest"),
                         "CANDIDATE_PACKAGE_FILE": str(root / "package"),
                         "CANDIDATE_VERSION_FILE": str(root / "version"),
                         "PREFLIGHT_ONLY": str(preflight), "FREEZE_PACKAGE_PREPARED": str(prepared),
                         "INSTALLED_FLAVOR": "",
                         "TEST_FREEZE_STATUS": status,
                         "PATH": tmp + os.pathsep + os.environ["PATH"]}
            script = "set -eu\nread_identity_line() { cat \"$1\"; }\n" + block + "\necho ADMITTED\n"
            return subprocess.run(["/bin/sh", "-c", script], env={**os.environ, **variables},
                                  text=True, capture_output=True, check=False)

    def test_admission_and_closed_freeze(self):
        self.assertEqual(self.run_admission(0).returncode, 0)
        for rc in (1, 78, 79):
            self.assertNotEqual(self.run_admission(rc).returncode, 0)
        self.assertNotEqual(self.run_admission(0, missing=True).returncode, 0)
        status = "UPDATE_POLICY=FREEZE\nFREEZE_INTEGRITY=PASS"
        self.assertEqual(self.run_admission(78, preflight=1, status=status).returncode, 0)
        self.assertNotEqual(self.run_admission(78, preflight=1, status=status + "\nUPDATE_POLICY=WARN").returncode, 0)
        self.assertNotEqual(self.run_admission(79, preflight=1, status=status).returncode, 0)
        self.assertEqual(self.run_admission(78, prepared=1).returncode, 0)

