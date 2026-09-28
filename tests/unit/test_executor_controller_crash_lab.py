import importlib.util
import hashlib
import json
import os
import pathlib
import subprocess
import tempfile
import types
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/executor-controller-crash-lab.py"
SPEC = importlib.util.spec_from_file_location("executor_controller_crash_lab", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class ControllerCrashLabTests(unittest.TestCase):
    def test_matrix_has_exact_post_persistence_revisions(self):
        self.assertEqual(
            {name: value["revision"] for name, value in LAB.SCENARIOS.items()},
            {"C1": 2, "C2": 3, "C3": 3, "C4": 4, "C5": 5, "C6": 5, "C7": 5},
        )

    def test_supervisor_uses_pidfd_and_new_ledger(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("os.pidfd_open(child.pid", source)
        self.assertIn("signal.pidfd_send_signal(pidfd, signal.SIGKILL", source)
        self.assertIn("ledger_module.Ledger(args.ledger_root, model)", source)
        self.assertIn("before != after", source)
        self.assertIn('contender["transaction"] != record["transaction"]', source)
        self.assertIn('"transaction_relationship": "SAME_TRANSACTION_NEW_ATTEMPT"', source)

    def test_checkpoint_and_terminal_proofs_are_exact(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('ready.get("boundary") != expectation["boundary"]', source)
        self.assertIn('actual != ready.get("artifacts")', source)
        self.assertIn('grant != stored["grant"]', source)
        self.assertIn("marker != marker_expected", source)
        self.assertIn('terminal.get("ExecMainStatus") != "0"', source)
        self.assertIn('terminal.get("MainPID") not in ("", "0")', source)
        self.assertIn('events.get("populated") != "0"', source)

    def test_failure_path_reaps_owned_controller(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("if child.poll() is None:", source)
        self.assertIn("bounded_wait_child(child, 5)", source)
        self.assertIn('controller.exclusive_json(control_root, "supervisor-failure.json"', source)
        self.assertIn('owner_path = control_root / "launch-owner.json"', source)
        self.assertNotIn('pathlib.Path(args.runner_root) / "launch-owner.json"', source)

    def test_finish_mode_is_c6_only_and_reports_final_ledger(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('if args.persist_finish and args.scenario != "C6"', source)
        self.assertIn('"INTEGRATED_INERT_CONTROLLER_CRASH_FINISH_PASS"', source)
        self.assertIn('"pre_finish": {', source)
        self.assertIn(
            '"record_state": finish["crash"]["record_state"] if args.finish_crash',
            source,
        )
        self.assertIn('else ("TERMINAL" if finish is not None', source)
        self.assertIn('"ledger_revision": final_revision', source)
        self.assertNotIn("lvcreate", source)

    def test_finish_crash_boundaries_hold_lock_and_never_close(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"PRE_FINISH_STORE" if args.finish_child == "F1"', source)
        self.assertIn('stored = store(data)', source)
        self.assertIn('"POST_FINISH_STORE_PRE_ACK"', source)
        self.assertIn('signal.pidfd_send_signal(pidfd, signal.SIGKILL', source)
        self.assertIn('finish crash qualification requires exact C6', source)
        self.assertNotIn("close_exact", source)
        self.assertNotIn('del updated["slots"]', source)

    def test_finish_store_boundary_call_counts_and_store_failure(self):
        events = []

        def store(data):
            events.append(("store", data))
            return {"stored": data}

        def publish(stored):
            events.append(("publish", stored))

        def stop():
            events.append(("stop", None))
            raise RuntimeError("injected stop")

        with self.assertRaisesRegex(RuntimeError, "injected stop"):
            LAB.execute_finish_store_boundary("F1", {"revision": 5}, store, publish, stop)
        self.assertEqual(events, [("publish", None), ("stop", None)])

        events.clear()
        with self.assertRaisesRegex(RuntimeError, "injected stop"):
            LAB.execute_finish_store_boundary("F2", {"revision": 5}, store, publish, stop)
        self.assertEqual(events, [
            ("store", {"revision": 5}),
            ("publish", {"stored": {"revision": 5}}),
            ("stop", None),
        ])

        events.clear()

        def failed_store(data):
            events.append(("store", data))
            raise OSError("durable store failed")

        with self.assertRaisesRegex(OSError, "durable store failed"):
            LAB.execute_finish_store_boundary(
                "F2", {"revision": 5}, failed_store, publish, stop
            )
        self.assertEqual(events, [("store", {"revision": 5})])

    def test_missing_current_run_owner_cannot_trigger_unit_access(self):
        class NoUnitAccess:
            def __getattr__(self, name):
                raise AssertionError(f"unexpected unit access: {name}")

        with tempfile.TemporaryDirectory() as directory:
            result = LAB.cleanup_exact_unit(
                NoUnitAccess(), "old.service", pathlib.Path(directory),
                pathlib.Path(directory) / "fresh-control-owner.json", {},
            )
        self.assertEqual(result["reason"], "NO_DURABLE_LAUNCH_OWNER")

    def test_expected_marker_uses_persisted_grant_schema(self):
        digest = "a" * 64
        marker = LAB.expected_marker(
            {"attempt": "b" * 32, "transaction": "c" * 32},
            {"invocation_id": "d" * 32, "inert_operation_digest": digest},
            {"grant_sha256": "e" * 64,
             "grant": {"inert_operation_digest": digest}},
            {"operation.json": {"sha256": digest}},
        )
        self.assertEqual(marker["inert_operation_digest"], digest)
        self.assertEqual(marker["grant_sha256"], "e" * 64)

    def test_loader_rejects_changed_bytes_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = pathlib.Path(directory) / "side-effect"
            module = pathlib.Path(directory) / "candidate.py"
            module.write_text(
                f"import pathlib\npathlib.Path({str(marker)!r}).write_text('executed')\n",
                encoding="utf-8",
            )
            wrong = hashlib.sha256(b"different bytes").hexdigest()
            with self.assertRaisesRegex(RuntimeError, "before execution"):
                LAB.load_exact(module, "must_not_execute", wrong)
            self.assertFalse(marker.exists())

    def test_finish_manifest_rejects_adapter_and_manifest_repinning(self):
        names = {
            "adapter": "admission-adapter.pl",
            "model": "SharedLvmAdmission.pm",
            "ledger": "executor-integrated-admission-lab.py",
            "runner": "executor-integrated-systemd-lab.py",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            for key, name in names.items():
                (root / name).write_bytes((key + "-v1").encode())
            manifest = {
                key: hashlib.sha256((root / name).read_bytes()).hexdigest()
                for key, name in names.items()
            }
            pinned_digest = hashlib.sha256(LAB.canonical(manifest)).hexdigest()
            self.assertEqual(
                LAB.verify_code_manifest(root, manifest, pinned_digest)["adapter"],
                root / names["adapter"],
            )

            (root / names["adapter"]).write_bytes(b"adapter-v2-side-effect")
            with self.assertRaisesRegex(RuntimeError, "snapshot differs"):
                LAB.verify_code_manifest(root, manifest, pinned_digest)

            changed = dict(manifest)
            changed["adapter"] = hashlib.sha256(
                (root / names["adapter"]).read_bytes()
            ).hexdigest()
            with self.assertRaisesRegex(RuntimeError, "manifest digest mismatch"):
                LAB.verify_code_manifest(root, changed, pinned_digest)

    def test_exact_ledger_reader_rejects_semantically_equal_noncanonical_bytes(self):
        class FakeLedger:
            pinned_authority = None

            def __init__(self, root_fd):
                self.root_fd = root_fd

            @staticmethod
            def _validate(value):
                return value

        def duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise RuntimeError("duplicate")
                result[key] = value
            return result

        module = types.SimpleNamespace(canonical=LAB.canonical, duplicate_keys=duplicate_keys)
        value = {
            "authority_id": "a", "authority_node": "n",
            "authority_boot_id": "b", "enrollment_epoch": "e",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            ledger_path = root / "ledger.json"
            ledger_path.write_text(json.dumps(value, indent=2), encoding="utf-8")
            os.chmod(ledger_path, 0o600)
            root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaisesRegex(RuntimeError, "not canonical"):
                    LAB.exact_ledger_load(FakeLedger(root_fd), module)
            finally:
                os.close(root_fd)

    def test_pipe_drain_timeout_is_evidence_not_control_flow_failure(self):
        class TimedOutChild:
            def communicate(self, timeout):
                raise subprocess.TimeoutExpired(
                    ["controller"], timeout, output=b"partial-out", stderr=b"partial-err"
                )

        stdout, stderr, error = LAB.capture_child_pipes(TimedOutChild())
        self.assertEqual(stdout, b"partial-out")
        self.assertEqual(stderr, b"partial-err")
        self.assertEqual(error, "PIPE_DRAIN_TIMEOUT_AFTER_CONTROLLER_REAP")
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"controller_capture_error": child_capture_error', source)
        self.assertIn("failure_cleanup = None", source)

    def test_no_takeover_replay_or_storage_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        for forbidden in ("lvcreate", "lvremove", "lvchange", "dmsetup", "pvesm", "/etc/pve"):
            self.assertNotIn(forbidden, source)
        self.assertNotIn('exclusive_json(runner_root, "grant.json"', source)
        self.assertIn("no takeover or replay", source)


if __name__ == "__main__":
    unittest.main()
