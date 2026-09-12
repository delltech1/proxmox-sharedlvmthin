import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check"


class UpgradeCheckTests(unittest.TestCase):
    def run_check(self, config, checker):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = root / "storage.cfg"
            recovery = root / "recovery-check"
            calls = root / "calls"
            storage.write_text(textwrap.dedent(config), encoding="utf-8")
            recovery.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' \"$1\" >>'{calls}'\n"
                + textwrap.dedent(checker),
                encoding="utf-8",
            )
            recovery.chmod(0o755)
            test_script = root / "upgrade-check"
            source = SCRIPT.read_text(encoding="utf-8")
            source = source.replace(
                "STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}"
            ).replace(
                "RECOVERY_CHECK=/usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check",
                f"RECOVERY_CHECK={recovery}",
            ).replace("PROBE_TIMEOUT=150", "PROBE_TIMEOUT=5")
            test_script.write_text(source, encoding="utf-8")
            test_script.chmod(0o755)
            result = subprocess.run(
                ["bash", str(test_script)],
                text=True,
                capture_output=True,
                check=False,
            )
            invoked = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
            return result, invoked

    def test_checks_enabled_thin_and_thick_and_skips_disabled_storage(self):
        result, invoked = self.run_check(
            """
            sharedlvmthin: thin-store
                    vgname shared-vg

            sharedlvmthin: thick-store
                    vgname shared-vg
                    slt-allocation-mode thick-generations

            sharedlvmthin: disabled-store
                    disable
                    vgname other-vg
                    slt-allocation-mode future-disabled-mode
            """,
            """
            echo THICK_ANCHORS_HEALTHY=PASS
            echo STATE=HEALTHY
            echo SAFE_FOR_MUTATION=YES
            exit 0
            """,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, ["thin-store", "thick-store"])
        self.assertIn("RESULT=SKIP_DISABLED", result.stdout)
        self.assertIn("UPGRADE_STORAGES_CHECKED=2", result.stdout)
        self.assertIn("UPGRADE_STORAGES_SKIPPED_DISABLED=1", result.stdout)
        self.assertIn(
            "UPGRADE_STORAGES_SKIPPED_EXPLICITLY_DISABLED=1", result.stdout
        )
        self.assertIn("UPGRADE_STORAGES_SKIPPED_NODE_SCOPE=0", result.stdout)
        self.assertIn("UPGRADE_SAFE=YES", result.stdout)

    def test_fails_closed_on_recovery_required_storage(self):
        result, invoked = self.run_check(
            """
            sharedlvmthin: thick-store
                    vgname shared-vg
                    slt-allocation-mode thick-generations
            """,
            """
            echo THICK_ANCHORS_HEALTHY=FAIL
            echo STATE=RECOVERY_REQUIRED
            echo SAFE_FOR_MUTATION=NO
            exit 2
            """,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(invoked, ["thick-store"])
        self.assertIn("UPGRADE_STORAGE_RESULT=FAIL", result.stdout)
        self.assertIn("UPGRADE_SAFE=NO", result.stdout)

    def test_skips_storage_outside_local_node_scope(self):
        result, invoked = self.run_check(
            """
            sharedlvmthin: remote-only
                    vgname shared-vg
                    nodes definitely-not-this-test-host
                    slt-allocation-mode thick-generations
            """,
            """
            echo THICK_ANCHORS_HEALTHY=PASS
            echo STATE=HEALTHY
            echo SAFE_FOR_MUTATION=YES
            exit 0
            """,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, [])
        self.assertIn("RESULT=SKIP_NODE_SCOPE", result.stdout)
        self.assertIn("UPGRADE_SAFE=YES", result.stdout)
        self.assertIn("UPGRADE_STORAGES_SKIPPED_NODE_SCOPE=1", result.stdout)

    def test_rejects_duplicate_storage_identifier(self):
        result, invoked = self.run_check(
            """
            sharedlvmthin: duplicate
                    vgname one
            sharedlvmthin: duplicate
                    vgname two
            """,
            """
            echo THICK_ANCHORS_HEALTHY=PASS
            echo STATE=HEALTHY
            echo SAFE_FOR_MUTATION=YES
            exit 0
            """,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(invoked, ["duplicate"])
        self.assertIn("DETAIL=duplicate storage identifier", result.stdout)
        self.assertIn("UPGRADE_SAFE=NO", result.stdout)

    def test_requires_exactly_one_positive_state_record(self):
        result, _ = self.run_check(
            """
            sharedlvmthin: thin-store
                    vgname shared-vg
            """,
            """
            echo THICK_ANCHORS_HEALTHY=PASS
            echo STATE=HEALTHY
            echo STATE=HEALTHY
            echo SAFE_FOR_MUTATION=YES
            exit 0
            """,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("UPGRADE_SAFE=NO", result.stdout)


if __name__ == "__main__":
    unittest.main()
