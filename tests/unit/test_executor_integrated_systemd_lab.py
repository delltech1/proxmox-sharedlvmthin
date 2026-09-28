import importlib.util
import hashlib
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/executor-integrated-systemd-lab.py"
SPEC = importlib.util.spec_from_file_location("executor_integrated_systemd", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class IntegratedSystemdLabTests(unittest.TestCase):
    def test_proc_stat_parser_handles_spaces_and_parentheses_in_comm(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('close = raw.rfind(")")', source)
        self.assertIn("fields_from_three[19]", source)

    def test_typed_exec_descriptor_excludes_runtime_fields(self):
        descriptor = LAB.exec_descriptor("/usr/bin/python3", ["/usr/bin/python3", "-I"], False)
        self.assertEqual(set(descriptor), {"schema", "path", "argv", "ignore_failure"})
        self.assertNotIn("pid", descriptor)

    def test_runner_cannot_write_ledger_and_dispatch_is_persisted_first(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('f"--property=ReadWritePaths={runner_root}"', source)
        self.assertNotIn('f"--property=ReadWritePaths={args.ledger_root}"', source)
        self.assertLess(source.index("protocol.issue_dispatch(bound, grant)"),
                        source.index('exclusive_json(runner_root, "grant.json", grant)'))
        self.assertIn('read_canonical_json(root / "operation.json")', source)
        self.assertIn('exclusive_json(runner_root, "operation.json", operation)', source)
        self.assertGreaterEqual(source.count('not (runner_root / "dispatch-marker.json").exists()'), 2)
        self.assertIn("grant_file_sha256 != digest(expected)", source)
        self.assertLess(source.index('submission_outcome = "UNKNOWN"'),
                        source.index("subprocess.run(systemd_command"))
        self.assertIn('"submission_outcome": submission_outcome', source)

    def test_verified_loader_executes_captured_bytes_without_bytecode(self):
        captured = b"VALUE = 'captured'\n"
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "helper.py"
            path.write_text("VALUE = 'changed'\n", encoding="utf-8")
            with mock.patch.object(LAB, "read_no_follow", return_value=captured):
                module = LAB.load_verified_module(
                    path, hashlib.sha256(captured).hexdigest(), "slt_test_captured_helper"
                )
            self.assertEqual(module.VALUE, "captured")
            self.assertEqual(module.__file__, str(path.resolve()))
            self.assertTrue(sys.dont_write_bytecode)
            self.assertFalse((path.parent / "__pycache__").exists())

    def test_no_storage_command_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("lvcreate", "lvremove", "lvchange", "dmsetup", "pvesm", "/etc/pve"):
            self.assertNotIn(forbidden, source)

    def test_sigstop_boundaries_require_unchanged_independent_refusal(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for scenario, phase in (
            ("S1", "STARTUP_REPORTED_PRE_BIND"),
            ("S2", "BOUND_PRE_DISPATCH"),
            ("S3", "GRANT_VALIDATED_PRE_MARKER"),
            ("S4", "MARKER_PERSISTED_PRE_EXIT"),
        ):
            self.assertIn(f'args.scenario == "{scenario}"', source)
            self.assertIn(phase, source)
        self.assertIn("integrated.Ledger(args.ledger_root, model)", source)
        self.assertIn("before_sha != after_sha", source)
        self.assertIn("observed[\"revision\"] != before[\"revision\"]", source)
        self.assertIn("signal_exact(startup, signal.SIGCONT)", source)
        self.assertIn("signal.pidfd_send_signal", source)
        self.assertIn("numeric PID fallback is forbidden", source)
        self.assertNotIn('os.kill(expected_startup["pid"]', source)
        self.assertIn('"transaction": record["transaction"]', source)
        self.assertIn("SAME_TRANSACTION_NEW_ATTEMPT", source)
        self.assertIn("marker_absent_before_refusal", source)
        self.assertIn("marker_absent_after_refusal", source)
        runner_s3 = source.index('if args.scenario == "S3":')
        phase = source.index("phase_stop(root, args.scenario", runner_s3)
        marker = source.index('exclusive_json(root, "dispatch-marker.json"', runner_s3)
        self.assertLess(phase, marker)


if __name__ == "__main__":
    unittest.main()
