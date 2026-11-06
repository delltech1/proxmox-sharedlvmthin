"""Executable maintscript boundary: CLOSED alone does not defer PVE restarts."""
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
class ClosedSuccessorPostinstEffectsTests(unittest.TestCase):
    def test_normal_configure_attempts_active_ha_restart(self):
        source = (ROOT / "DEBIAN/postinst").read_text()
        start = source.index("# Long-running PVE workers cache")
        end = source.index("# Existing web configuration", start)
        block = source[start:end]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls"
            fake = root / "systemctl"
            fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> " + shlex.quote(str(log)) + "\nexit 0\n")
            fake.chmod(0o755)
            script = "set -eu\nMAINTENANCE_CONFIGURE=0\n" + block + "\n"
            result = subprocess.run(["/bin/sh", "-c", script],
                                    env={"PATH": str(root)}, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text().splitlines()
            self.assertIn("is-active --quiet pve-ha-lrm.service", calls)
            self.assertIn("try-restart pve-ha-lrm.service", calls)
            # Fake systemctl is the only executable used; no service was touched.


if __name__ == "__main__":
    unittest.main()
