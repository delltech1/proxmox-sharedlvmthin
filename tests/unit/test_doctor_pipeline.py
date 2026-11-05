"""Exercise the actual Doctor output gates using isolated delayed/failing writers."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]

@unittest.skipUnless(os.name == "posix", "requires bash and POSIX tools")
class DoctorPipelineTests(unittest.TestCase):
    def test_default_deadlines_are_consistent(self):
        for gate in ("package-profile-gate.sh", "package-post-reboot-gate.sh"):
            source = (ROOT / "experiments/thick-generations" / gate).read_text()
            self.assertIn("doctor_timeout_sec=1800", source)
        postinst = (ROOT / "DEBIAN/postinst").read_text()
        self.assertIn("DOCTOR_TIMEOUT=${SLT_INSTALL_DOCTOR_TIMEOUT_SEC:-1800}", postinst)
        wrapper = (ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-preservation").read_text()
        self.assertIn('values.get("--doctor-timeout-sec", "1800")', wrapper)

    def run_gate(self, gate, writer="slow", timed_out=False):
        source = (ROOT / "experiments/thick-generations" / gate).read_text()
        if gate == "package-profile-gate.sh":
            start = source.index('doctor_output="$temp/doctor.out"')
            end = source.index('\n# Doctor must prove', start)
        else:
            start = source.index('doctor_file=$(mktemp)', source.index('sharedlvmthin compat-check'))
            end = source.index('\ntimeout --foreground --kill-after=10 1800 sharedlvmthin upgrade-check', start)
        block = source[start:end]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); commands = root / "bin"; commands.mkdir()
            def script(name, text):
                path = commands / name; path.write_text(text); path.chmod(0o755)
            script("sharedlvmthin", '#!/bin/sh\nprintf "FAIL: 0\\nRESULT: PASS WITH WARNINGS\\n"\n')
            if writer == "slow":
                script("tee", '#!/bin/sh\nsleep 0.3\nexec /usr/bin/tee "$@"\n')
            else:
                script("tee", '#!/bin/sh\ncat >/dev/null\nexit 1\n')
            if timed_out:
                script("timeout", '#!/bin/sh\nexit 124\n')
            env = dict(os.environ, PATH=f"{commands}:/usr/bin:/bin")
            preamble = (f'temp="{root}"\ndoctor_timeout_sec=7200\n'
                        f'clean_env=(/usr/bin/env -i PATH={commands}:/usr/bin:/bin LC_ALL=C)\n')
            return subprocess.run(["/bin/bash", "-c", "set -euo pipefail\n" + preamble + block + '\necho SETTLED\n'],
                                  capture_output=True, text=True, timeout=10, env=env)

    def test_delayed_writer_is_joined_and_failure_and_timeout_refuse(self):
        for gate in ("package-profile-gate.sh", "package-post-reboot-gate.sh"):
            with self.subTest(gate=gate, scenario="delayed"):
                result = self.run_gate(gate)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("SETTLED", result.stdout)
            with self.subTest(gate=gate, scenario="writer-failure"):
                result = self.run_gate(gate, writer="fail")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("SETTLED", result.stdout)
            with self.subTest(gate=gate, scenario="deadline"):
                result = self.run_gate(gate, timed_out=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("result is UNKNOWN", result.stderr)
                self.assertNotIn("SETTLED", result.stdout)
