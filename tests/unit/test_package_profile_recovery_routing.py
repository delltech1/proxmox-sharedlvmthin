"""Execute the checked-in gate's recovery routing with no package/host effects."""
import os
from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parents[2]
GATE = ROOT / "experiments/thick-generations/package-profile-gate.sh"


class PackageProfileRecoveryRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = GATE.read_text(encoding="utf-8")
        prepare_start = source.index('if ((settle_recovery == 1)) && [[ "$prior_update_policy" == FREEZE ]]')
        dispatch_end = source.index("# dpkg-query expands", prepare_start)
        settle_start = source.index("# A candidate runtime is qualified", dispatch_end)
        settle_end = source.index('doctor_output=', settle_start)
        runtime_finalize_start = source.index(
            "# Doctor must prove the complete PVE-facing runtime", settle_end)
        runtime_finalize_end = source.index(
            '"${clean_env[@]}" timeout --foreground --kill-after=10 1800 sharedlvmthin upgrade-check',
            runtime_finalize_start)
        finalize_start = source.index('if ((profile_replacement == 1)); then', source.index("# Consume the durable replacement evidence"))
        finalize_end = source.index('echo "RESULT=EXECUTE_PASS"', finalize_start)
        cls.routing = (source[prepare_start:dispatch_end] + source[settle_start:settle_end]
                       + source[runtime_finalize_start:runtime_finalize_end]
                       + source[finalize_start:finalize_end])
        # Replace only the absolute replacement-helper endpoint. All branching,
        # argument assembly and recovery guards are the real source slices.
        cls.routing = cls.routing.replace(
            "/usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement",
            "replacement_fixture")

    def run_routing(self, replacement_id="", freeze_id="b" * 32, profile="pve-sharedlvmthin"):
        prelude = f'''set -euo pipefail
settle_recovery=1
prior_update_policy=FREEZE
select_update_policy=freeze
freeze_package_txid={freeze_id!r}
transaction_id={replacement_id!r}
installed_name={profile!r}
target_name={profile!r}
target_version=0.9.0-test
actual_hash={'a' * 64}
candidate_artifact={'d' * 64}
direct_package_txid=''
prior_update_policy_schema=2
clean_env=()
dpkg() {{ echo UNEXPECTED_DPKG >&2; return 97; }}
dpkg-deb() {{ echo UNEXPECTED_EXTRACT >&2; return 97; }}
python3() {{ echo UNEXPECTED_PREPARE >&2; return 97; }}
sharedlvmthin() {{ printf 'POLICY'; printf ' <%s>' "$@"; printf '\\n'; }}
replacement_fixture() {{ printf 'REPLACEMENT'; printf ' <%s>' "$@"; printf '\\n'; }}
'''
        return subprocess.run(["/bin/bash", "-c", prelude + self.routing],
                              text=True, capture_output=True, check=False, timeout=10)

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_same_profile_recovery_only_settles_existing_freeze_transaction(self):
        result = self.run_routing()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<settle-freeze-package>", result.stdout)
        self.assertIn("<--txid> <" + "b" * 32 + ">", result.stdout)
        self.assertIn("<qualify-runtime>", result.stdout)
        self.assertIn("<finalize-runtime>", result.stdout)
        self.assertNotIn("--replacement-txid", result.stdout)
        self.assertNotIn("REPLACEMENT", result.stdout)
        self.assertNotIn("UNEXPECTED", result.stderr)

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_both_cross_profile_directions_keep_distinct_ids_and_finalize_recovery(self):
        for target in ("pve-sharedlvmthin", "pve-sharedlvmthin-thick"):
            with self.subTest(target=target):
                result = self.run_routing(replacement_id="c" * 32, profile=target)
                self.assertEqual(result.returncode, 0, result.stderr)
                lines = result.stdout.splitlines()
                settle = next(line for line in lines if "<settle-freeze-package>" in line)
                replacement = next(line for line in lines if "REPLACEMENT <finalize>" in line)
                self.assertIn("<--txid> <" + "b" * 32 + ">", settle)
                self.assertIn("<--replacement-txid> <" + "c" * 32 + ">", settle)
                self.assertIn("<--txid> <" + "c" * 32 + ">", replacement)
                self.assertIn("<--recovery>", replacement)
                self.assertNotIn("UNEXPECTED", result.stderr)

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_missing_freeze_id_refuses_before_any_settlement(self):
        result = self.run_routing(replacement_id="c" * 32, freeze_id="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("original --freeze-transaction-id", result.stderr)
        self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
