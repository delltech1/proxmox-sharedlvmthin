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
        prepare_start = source.index('# An already active FREEZE policy is part of the package transaction')
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
        cls.routing = cls.routing.replace('/usr/bin/timeout', 'timeout_fixture')
        carrier_start = source.index('# Candidate-carried recovery is package-only')
        carrier_end = source.index('# Ordinary package workflow starts here.', carrier_start)
        cls.carrier_routing = source[carrier_start:carrier_end].replace('/usr/bin/timeout', 'timeout_fixture')

    def run_routing(self, replacement_id="", freeze_id="b" * 32, profile="pve-sharedlvmthin", qualify_rc=0,
                    carrier=False, old_cli_rc=0, direct_id=""):
        prelude = f'''set -euo pipefail
settle_recovery=1
prior_update_policy=FREEZE
select_update_policy=freeze
freeze_package_txid={freeze_id!r}
transaction_id={replacement_id!r}
installed_name={profile!r}
installed_version=0.9.0-test
target_name={profile!r}
target_version=0.9.0-test
actual_hash={'a' * 64}
candidate_artifact={'d' * 64}
direct_package_txid=''
direct_recovery_txid={direct_id!r}
prior_update_policy_schema=2
recovery_code_package={'/new-code.deb' if carrier else ''!r}
recovery_code_control=/fixture/new-code-control
recovery_code_deb=/fixture/new-code.deb
recovery_actual_hash={'f' * 64}
clean_env=()
dpkg() {{ echo UNEXPECTED_DPKG >&2; return 97; }}
dpkg-deb() {{ echo UNEXPECTED_EXTRACT >&2; return 97; }}
python3() {{ echo UNEXPECTED_PREPARE >&2; return 97; }}
sharedlvmthin() {{ if [[ {old_cli_rc} -ne 0 ]]; then echo OLD_CLI_REFUSED >&2; return {old_cli_rc}; fi;
    printf 'POLICY'; printf ' <%s>' "$@"; printf '\\n';
    if [[ "${{2:-}}" == qualify-runtime ]]; then return {qualify_rc}; fi; }}
replacement_fixture() {{ printf 'REPLACEMENT'; printf ' <%s>' "$@"; printf '\\n'; }}
timeout_fixture() {{ printf 'CARRIER'; printf ' <%s>' "$@"; printf '\\n'; }}
'''
        return subprocess.run(["/bin/bash", "-c", prelude + (self.carrier_routing if carrier else self.routing)],
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
    def test_direct_lost_ack_recovery_after_bootstrap_freeze_uses_direct_settlement(self):
        direct_id = "d" * 32
        result = self.run_routing(freeze_id="", direct_id=direct_id)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("<settle-direct-package>", result.stdout)
        self.assertIn("<--txid> <" + direct_id + ">", result.stdout)
        self.assertIn("<--bootstrap-policy> <freeze>", result.stdout)
        self.assertIn("<finalize-runtime>", result.stdout)
        self.assertNotIn("<qualify-runtime>", result.stdout)
        self.assertNotIn("<settle-freeze-package>", result.stdout)
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

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_typed_unlisted_uses_closed_settlement_without_runtime_finalize(self):
        result = self.run_routing(replacement_id="c" * 32, qualify_rc=78)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn("<settle-unqualified-freeze-package>", result.stdout)
        self.assertIn("<--replacement-txid> <" + "c" * 32 + ">", result.stdout)
        self.assertIn("RESULT=PACKAGE_SETTLED_RUNTIME_CLOSED", result.stdout)
        self.assertNotIn("<finalize-runtime>", result.stdout)
        self.assertNotIn("REPLACEMENT <finalize>", result.stdout)
        self.assertNotIn("EXECUTE_PASS", result.stdout)

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_generic_qualification_failure_never_becomes_package_settlement(self):
        result = self.run_routing(qualify_rc=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("<settle-unqualified-freeze-package>", result.stdout)
        self.assertNotIn("<settle-freeze-package>", result.stdout)
        self.assertNotIn("<finalize-runtime>", result.stdout)

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_new_carrier_executes_only_closed_settlement_of_old_target(self):
        result = self.run_routing(replacement_id='c' * 32, carrier=True)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('</usr/bin/python3> <-I>', result.stdout)
        self.assertIn('</fixture/new-code-control/sharedlvmthin-candidate-update-policy>', result.stdout)
        self.assertIn('<settle-unqualified-freeze-package>', result.stdout)
        self.assertIn('<--version> <0.9.0-test>', result.stdout)
        self.assertIn('<--sha256> <' + 'a' * 64 + '>', result.stdout)
        self.assertIn('<--txid> <' + 'b' * 32 + '>', result.stdout)
        self.assertIn('<--replacement-txid> <' + 'c' * 32 + '>', result.stdout)
        self.assertIn('<--recovery-code-sha256> <' + 'f' * 64 + '>', result.stdout)
        self.assertNotIn('<qualify-runtime>', result.stdout)
        self.assertNotIn('<finalize-runtime>', result.stdout)
        self.assertNotIn('REPLACEMENT <finalize>', result.stdout)
        self.assertNotIn('EXECUTE_PASS', result.stdout)
        self.assertNotIn('UNEXPECTED', result.stderr)

    @unittest.skipUnless(os.name == "posix", "executes Bash routing; Linux runtime required")
    def test_closed_latch_replay_does_not_require_any_old_cli_to_accept_it(self):
        result = self.run_routing(replacement_id='c' * 32, carrier=True, old_cli_rc=99)
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertIn('<settle-unqualified-freeze-package>', result.stdout)
        self.assertNotIn('POLICY', result.stdout)
        self.assertNotIn('OLD_CLI_REFUSED', result.stderr)
        self.assertNotIn('UNEXPECTED', result.stderr)

    def test_carrier_path_precedes_old_preflight_and_all_installed_cli_calls(self):
        source = GATE.read_text(encoding='utf-8')
        carrier = source.index('# Candidate-carried recovery is package-only')
        ordinary = source.index('# Ordinary package workflow starts here.')
        self.assertLess(source.index('actual_hash=$(sha256sum "$candidate_deb"'), carrier)
        self.assertLess(source.index('recovery_actual_hash=$(sha256sum "$recovery_code_deb"'), carrier)
        self.assertLess(source.index('observed_current='), carrier)
        self.assertLess(carrier, ordinary)
        self.assertLess(ordinary, source.index('"$candidate_control/preinst" preflight'))
        self.assertNotIn('sharedlvmthin update-policy', source[:ordinary])
        branch = source[carrier:ordinary]
        self.assertNotIn('candidate_manifest', branch)
        self.assertNotIn('qualify-runtime', branch)
        self.assertNotIn('finalize-runtime', branch)
        self.assertIn('--sha256 "$actual_hash"', branch)
        self.assertIn('--recovery-code-sha256 "$recovery_actual_hash"', branch)


if __name__ == "__main__":
    unittest.main()
