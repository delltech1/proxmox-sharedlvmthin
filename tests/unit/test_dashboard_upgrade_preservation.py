import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(os.name == "posix", "requires a POSIX shell")
class DashboardUpgradePreservationTests(unittest.TestCase):
    def test_existing_configuration_is_preserved_and_maintenance_defers_service(self):
        source = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        fragment = source.split("# Existing web configuration is persistent and must never be overwritten.\n", 1)[1]
        fragment = fragment.split('\nelse\n    echo\n    echo "SharedLvmThin Web Dashboard is not configured."', 1)[0] + "\nfi\n"
        for maintenance in (0, 1):
            with self.subTest(maintenance=maintenance), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                conf = root / "web.conf"
                conf.write_text("[web]\naddress=127.0.0.1\nport=19443\n[auth]\naccess_mode=administrator\n")
                before = hashlib.sha256(conf.read_bytes()).hexdigest()
                command = root / "systemctl"
                command.write_text("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$CALL_LOG\"\nexit 0\n")
                command.chmod(0o755)
                log = root / "calls"
                env = {**os.environ, "CONF": str(conf), "SERVICE": "candidate-web.service",
                       "MAINTENANCE_CONFIGURE": str(maintenance), "CALL_LOG": str(log),
                       "PATH": tmp + os.pathsep + os.environ["PATH"]}
                result = subprocess.run(["/bin/sh", "-c", "set -eu\n" + fragment],
                                        env=env, text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(hashlib.sha256(conf.read_bytes()).hexdigest(), before)
                calls = log.read_text().splitlines() if log.exists() else []
                self.assertEqual(calls, [] if maintenance else ["enable candidate-web.service", "restart candidate-web.service"])

