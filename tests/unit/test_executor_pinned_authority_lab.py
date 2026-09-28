import importlib.util
import hashlib
import json
import os
import pathlib
import types
import unittest
import tempfile
import subprocess
import time
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/executor-pinned-authority-lab.py"
SPEC = importlib.util.spec_from_file_location("executor_pinned_authority_lab", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class PinnedAuthorityLabTests(unittest.TestCase):
    @staticmethod
    def descriptor(ledger_root, code_root):
        manifest = {
            "authority.py": "a" * 64, "ledger.py": "b" * 64,
            "adapter.pl": "c" * 64, "model.pm": "d" * 64,
        }
        return {
            "schema": 1, "kind": "PINNED_AUTHORITY_TWO_CLIENT_LAB",
            "authority_id": "e" * 32, "authority_node": "pve-lab",
            "authority_boot_id": "12345678-1234-1234-1234-123456789abc",
            "enrollment_epoch": "f" * 64,
            "ledger_root": str(ledger_root), "code_root": str(code_root),
            "manifest": manifest, "code_digest": LAB.digest(manifest),
            "scope": {"vg_uuid": LAB.VG_UUID, "volume": LAB.VOLUME,
                      "object_key": "1" * 24},
        }

    def test_descriptor_schema_is_closed_and_exactly_typed(self):
        with tempfile.TemporaryDirectory(
                prefix=LAB.AUTHORITY_PREFIX, dir="/var/tmp") as ledger_dir, \
                tempfile.TemporaryDirectory(
                    prefix=LAB.CODE_PREFIX, dir="/var/tmp") as code_dir:
            descriptor = self.descriptor(ledger_dir, code_dir)
            self.assertEqual(LAB.validate_descriptor(descriptor), descriptor)
            with self.assertRaisesRegex(RuntimeError, "schema"):
                LAB.validate_descriptor({**descriptor, "fallback": True})
            with self.assertRaisesRegex(RuntimeError, "schema"):
                LAB.validate_descriptor({**descriptor, "schema": True})
            changed = dict(descriptor)
            changed["manifest"] = {**descriptor["manifest"], "authority.py": "0" * 64}
            with self.assertRaisesRegex(RuntimeError, "manifest"):
                LAB.validate_descriptor(changed)

    def test_request_rejects_alias_and_foreign_code_digest(self):
        descriptor = self.descriptor(
            "/var/tmp/" + LAB.AUTHORITY_PREFIX + "request-unit",
            "/var/tmp/" + LAB.CODE_PREFIX + "request-unit",
        )

        class LedgerModule:
            @staticmethod
            def volume_key(vg_uuid, volume):
                return "1" * 24

        request = {
            "schema": 1, "kind": "PINNED_AUTHORITY_REQUEST",
            "request_id": "2" * 32, "descriptor_sha256": "3" * 64,
            "operation": "RESERVE", **descriptor["scope"],
            "transaction": "4" * 32, "attempt": "5" * 32,
            "unit": "slt-thick-lab-exec-" + "5" * 32 + ".service",
            "code_digest": descriptor["code_digest"], "policy_digest": "6" * 64,
        }
        self.assertEqual(LAB.validate_request(request, descriptor, LedgerModule), request)
        with self.assertRaisesRegex(RuntimeError, "scope"):
            LAB.validate_request({**request, "volume": "alias"}, descriptor, LedgerModule)
        with self.assertRaisesRegex(RuntimeError, "code digest"):
            LAB.validate_request({**request, "code_digest": "7" * 64},
                                 descriptor, LedgerModule)

    def test_client_has_no_retry_fallback_or_storage_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("no retry", source)
        self.assertIn("start_new_session=True", source)
        self.assertIn("os.killpg", source)
        self.assertIn("wait_all_ready(entries)", source)
        self.assertNotIn("shell=True", source)
        self.assertNotIn("lvcreate", source)
        self.assertNotIn("lvremove", source)
        self.assertNotIn("systemctl", source)
        self.assertNotIn("ssh ", source)
        self.assertNotIn("close_exact", source)
        compile(LAB.PINNED_BOOTSTRAP, "<pinned-bootstrap>", "exec")

    def test_bootstrap_rejects_wrong_digest_before_side_effect(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            marker = pathlib.Path(directory) / "side-effect"
            candidate = pathlib.Path(directory) / "candidate.py"
            candidate.write_text(
                f"import pathlib\npathlib.Path({str(marker)!r}).write_text('ran')\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/usr/bin/python3", "-I", "-B", "-c", LAB.PINNED_BOOTSTRAP,
                 str(candidate), "0" * 64],
                capture_output=True, timeout=5, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_changed_request_bytes_are_rejected_by_argv_digest(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            request = pathlib.Path(directory) / "request.json"
            request.write_bytes(LAB.canonical({"schema": 1, "value": "original"}))
            pinned = hashlib.sha256(request.read_bytes()).hexdigest()
            request.write_bytes(LAB.canonical({"schema": 1, "value": "changed"}))
            with self.assertRaisesRegex(RuntimeError, "argv-pinned"):
                LAB.read_pinned_canonical(request, pinned, "request")

    def test_cleanup_isolates_entries_and_reports_unknown(self):
        class Child:
            def __init__(self, pid):
                self.pid = pid

        entries = [
            {"child": Child(101), "reaped": False, "ready_read": -1, "start_write": -1},
            {"child": Child(202), "reaped": False, "ready_read": -1, "start_write": -1},
        ]
        calls = []

        def finalizer(entry, timeout, force):
            calls.append(entry["child"].pid)
            if entry["child"].pid == 101:
                raise RuntimeError("first cleanup failed")
            return {"pid": 202, "group_terminal": 1, "result": "GROUP_TERMINAL"}

        outcomes = LAB.cleanup_entries(entries, finalizer=finalizer)
        self.assertEqual(calls, [101, 202])
        self.assertEqual(outcomes[0]["result"], "UNKNOWN_CLEANUP_EXCEPTION")
        self.assertEqual(outcomes[1]["group_terminal"], 1)

        tick = {"value": 0.0}

        def monotonic():
            tick["value"] += 2.0
            return tick["value"]

        fake = {"child": Child(303), "reaped": False}
        with mock.patch.object(LAB, "observe_leader_exit", return_value=None), \
                mock.patch.object(LAB, "process_group_members", return_value=[404]), \
                mock.patch.object(LAB.os, "killpg"), \
                mock.patch.object(LAB.time, "sleep"), \
                mock.patch.object(LAB.time, "monotonic", side_effect=monotonic):
            unknown = LAB.finalize_process_group(fake, timeout=1, force=True)
        self.assertEqual(unknown["result"], "UNKNOWN_LEADER_UNOBSERVED")
        self.assertEqual(unknown["group_terminal"], 0)

    def test_exited_leader_with_live_descendant_is_group_cleaned_before_reap(self):
        code = "import subprocess; subprocess.Popen(['/bin/sleep','60'])"
        child = subprocess.Popen(
            ["/usr/bin/python3", "-c", code], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        entry = {"child": child, "reaped": False}
        outcome, _, _ = LAB.finalize_process_group(entry, timeout=5)
        self.assertEqual(outcome["group_terminal"], 1)
        self.assertEqual(outcome["leader_reaped"], 1)
        self.assertEqual(outcome["signal_sent"], "SIGKILL")

    def test_ready_exit_is_not_reaped_and_reaped_leader_is_never_signaled(self):
        ready_read, ready_write = os.pipe()
        start_read, start_write = os.pipe()
        command = ["/usr/bin/python3", "-c",
                   "import os,sys; os.write(int(sys.argv[1]),b'R'); "
                   "assert os.read(int(sys.argv[2]),1)==b'G'",
                   str(ready_write), str(start_read)]
        child = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(ready_write, start_read), start_new_session=True,
        )
        os.close(ready_write)
        os.close(start_read)
        entry = {"child": child, "command": command, "ready_read": ready_read,
                 "start_write": start_write, "released": False, "reaped": False}
        try:
            LAB.wait_all_ready([entry], timeout=5)
            os.write(start_write, b"G")
            os.close(start_write)
            entry["start_write"] = -1
            deadline = time.monotonic() + 5
            observed = None
            while observed is None and time.monotonic() < deadline:
                observed = LAB.observe_leader_exit(child.pid)
                if observed is None:
                    time.sleep(0.01)
            self.assertIsNotNone(observed)
            self.assertIsNone(child.returncode)
            outcome = LAB.finalize_process_group(entry, timeout=5)
            self.assertIsInstance(outcome, tuple)
            self.assertEqual(outcome[0]["leader_reaped"], 1)
        finally:
            if entry.get("start_write", -1) >= 0:
                os.close(entry["start_write"])
            if not entry["reaped"]:
                LAB.finalize_process_group(entry, timeout=1, force=True)

        already = subprocess.Popen(
            ["/usr/bin/python3", "-c", "pass"], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
        )
        already.wait(timeout=5)
        already.stdout.close()
        already.stderr.close()
        with mock.patch.object(LAB.os, "killpg") as killpg:
            unknown = LAB.finalize_process_group(
                {"child": already, "reaped": False}, timeout=0.01, force=True
            )
        self.assertEqual(unknown["result"], "UNKNOWN_LEADER_ALREADY_REAPED")
        killpg.assert_not_called()

    def test_pipe_drain_is_bounded_when_external_writer_holds_pipe(self):
        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        os.close(stderr_write)
        stdout_stream = os.fdopen(stdout_read, "rb", buffering=0)
        stderr_stream = os.fdopen(stderr_read, "rb", buffering=0)
        started = time.monotonic()
        try:
            stdout, stderr, error = LAB.bounded_pipe_drain(
                stdout_stream, stderr_stream, timeout=0.05, limit=1024
            )
        finally:
            os.close(stdout_write)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual((stdout, stderr), (b"", b""))
        self.assertEqual(error, "PIPE_DRAIN_TIMEOUT")

    def test_response_rejects_boolean_allowed_and_foreign_record(self):
        descriptor = self.descriptor(
            "/var/tmp/" + LAB.AUTHORITY_PREFIX + "response-unit",
            "/var/tmp/" + LAB.CODE_PREFIX + "response-unit",
        )

        class LedgerModule:
            @staticmethod
            def volume_key(vg_uuid, volume):
                return "1" * 24

        request = {
            "schema": 1, "kind": "PINNED_AUTHORITY_REQUEST",
            "request_id": "2" * 32, "descriptor_sha256": "3" * 64,
            "operation": "RESERVE", **descriptor["scope"],
            "transaction": "4" * 32, "attempt": "5" * 32,
            "unit": "slt-thick-lab-exec-" + "5" * 32 + ".service",
            "code_digest": descriptor["code_digest"], "policy_digest": "6" * 64,
        }
        response = {
            "schema": 1, "kind": "PINNED_AUTHORITY_RESPONSE",
            "descriptor_sha256": request["descriptor_sha256"],
            "request_id": request["request_id"], "operation": "RESERVE",
            "result": "RESERVE_ATOMIC", "allowed": 1,
            "transaction_before_sha256": "7" * 64,
            "ledger_revision": 2, "ledger_sha256": "8" * 64,
            "record": LAB.record_from_request(request, descriptor),
        }
        self.assertEqual(
            LAB.validate_response(response, request, descriptor, LedgerModule), response
        )
        with self.assertRaisesRegex(RuntimeError, "schema"):
            LAB.validate_response({**response, "allowed": True}, request,
                                  descriptor, LedgerModule)
        foreign = dict(response["record"])
        foreign["attempt"] = "9" * 32
        with self.assertRaisesRegex(RuntimeError, "request-bound"):
            LAB.validate_response({**response, "record": foreign}, request,
                                  descriptor, LedgerModule)

    def test_l1_fault_plan_is_closed_and_request_bound(self):
        with tempfile.TemporaryDirectory(
                prefix=LAB.CONTROL_PREFIX, dir="/var/tmp") as control_dir:
            descriptor_sha = "1" * 64
            request_sha = "2" * 64
            plan = {
                "schema": 1, "kind": "PINNED_AUTHORITY_FAULT_PLAN",
                "scenario": "L1",
                "boundary": "POST_DURABLE_RESERVE_PRE_RESPONSE",
                "run_id": "3" * 32,
                "descriptor_sha256": descriptor_sha,
                "request_sha256": request_sha,
                "code_digest": "4" * 64,
                "control_root": control_dir,
                "checkpoint_name": "l1-checkpoint.json",
            }
            self.assertEqual(
                LAB.validate_l1_fault_plan(plan, descriptor_sha, request_sha), plan
            )
            for changed in (
                {**plan, "fallback": True},
                {**plan, "schema": True},
                {**plan, "request_sha256": "5" * 64},
                {**plan, "boundary": "PRE_DURABLE_RESERVE"},
                {**plan, "checkpoint_name": "other.json"},
            ):
                with self.assertRaisesRegex(RuntimeError, "schema or identity"):
                    LAB.validate_l1_fault_plan(changed, descriptor_sha, request_sha)

    def test_l1_checkpoint_requires_one_exact_durable_reserve(self):
        descriptor = self.descriptor(
            "/var/tmp/" + LAB.AUTHORITY_PREFIX + "l1-boundary-unit",
            "/var/tmp/" + LAB.CODE_PREFIX + "l1-boundary-unit",
        )
        request = {
            "schema": 1, "kind": "PINNED_AUTHORITY_REQUEST",
            "request_id": "2" * 32, "descriptor_sha256": "3" * 64,
            "operation": "RESERVE", **descriptor["scope"],
            "transaction": "4" * 32, "attempt": "5" * 32,
            "unit": "slt-thick-lab-exec-" + "5" * 32 + ".service",
            "code_digest": descriptor["code_digest"], "policy_digest": "6" * 64,
        }
        response = {
            "schema": 1, "kind": "PINNED_AUTHORITY_RESPONSE",
            "descriptor_sha256": request["descriptor_sha256"],
            "request_id": request["request_id"], "operation": "RESERVE",
            "result": "REFUSED", "allowed": 0,
            "transaction_before_sha256": "7" * 64,
            "ledger_revision": 1, "ledger_sha256": "7" * 64,
            "record": None,
        }
        ledger = types.SimpleNamespace(store_calls=0)
        with self.assertRaisesRegex(RuntimeError, "one exact successful"):
            LAB.publish_l1_checkpoint(
                {}, descriptor, request, response, ledger, types.SimpleNamespace()
            )

    def test_l1_fault_hook_precedes_all_response_output(self):
        source = SCRIPT.read_text(encoding="utf-8")
        authority_start = source.index("def authority_request")
        authority_end = source.index("\ndef helper_command", authority_start)
        authority_source = source[authority_start:authority_end]
        fault_read = authority_source.index("read_pinned_canonical(args.fault_plan")
        reserve = authority_source.index(".reserve(record)")
        fault_call = authority_source.index("publish_l1_checkpoint(")
        stdout_write = authority_source.index(
            "sys.stdout.buffer.write(canonical(response))"
        )
        self.assertLess(fault_read, reserve)
        self.assertLess(fault_call, stdout_write)
        self.assertIn('os.kill(pid, signal.SIGSTOP)', source)
        self.assertIn('stdout_a != b""', source)

    def test_l1_checkpoint_wait_rejects_exit_and_requires_stopped_identity(self):
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            missing = pathlib.Path(directory) / "missing.json"
            with mock.patch.object(LAB, "observe_leader_exit", return_value={"code": 1}):
                with self.assertRaisesRegex(RuntimeError, "exited before"):
                    LAB.wait_l1_checkpoint(missing, 123, timeout=0.05)

            checkpoint = pathlib.Path(directory) / "checkpoint.json"
            checkpoint.write_bytes(LAB.canonical({"schema": 1}))
            with mock.patch.object(LAB, "proc_state", return_value="S"), \
                    mock.patch.object(LAB, "observe_leader_exit", return_value=None), \
                    mock.patch.object(LAB.time, "sleep"):
                with self.assertRaisesRegex(RuntimeError, "deadline exceeded"):
                    LAB.wait_l1_checkpoint(checkpoint, 123, timeout=0.001)

    def test_l1_checkpoint_rejects_float_identity_and_wrong_response_digest(self):
        command = ["/usr/bin/python3", "authority.py"]
        expected = {
            "boundary": "POST_DURABLE_RESERVE_PRE_RESPONSE",
            "run_id": "1" * 32, "descriptor_sha256": "2" * 64,
            "request_sha256": "3" * 64, "code_digest": "4" * 64,
            "object_key": "5" * 24, "transaction": "6" * 32,
            "attempt": "7" * 32,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "pre_revision": 1, "post_revision": 2,
            "ledger_sha256": "8" * 64, "response_sha256": "9" * 64,
        }
        checkpoint = {
            "schema": 1, "scenario": "L1", **expected,
            "pid": 123, "start_ticks": 456, "argv": command, "pgid": 123,
        }
        with mock.patch.object(LAB, "proc_start_ticks", return_value=456), \
                mock.patch.object(LAB, "proc_argv", return_value=command), \
                mock.patch.object(LAB.os, "getpgid", return_value=123):
            self.assertEqual(
                LAB.validate_l1_checkpoint(checkpoint, expected, 123, command),
                checkpoint,
            )
            with self.assertRaisesRegex(RuntimeError, "identity is not exact"):
                LAB.validate_l1_checkpoint(
                    {**checkpoint, "pid": 123.0}, expected, 123, command
                )
            wrong = {**checkpoint, "response_sha256": "a" * 64}
            with self.assertRaisesRegex(RuntimeError, "identity is not exact"):
                LAB.validate_l1_checkpoint(wrong, expected, 123, command)

    def test_inert_reserved_slot_requires_entire_empty_envelope(self):
        record = {"state": "RESERVED"}
        slot = {"record": record, "launch_issued": None,
                "startup_observed": None, "dispatch_issued": None,
                "recovery_hold": None}
        self.assertTrue(LAB.inert_reserved_slot(slot, record, 1))
        self.assertFalse(LAB.inert_reserved_slot(
            {**slot, "recovery_hold": {"reason": "unknown"}}, record, 1
        ))
        self.assertFalse(LAB.inert_reserved_slot(slot, record, 2))
        self.assertTrue(LAB.inert_reserved_slot(
            {**slot, "finish_observed": None}, record, 2
        ))

    def test_l1_cleanup_exception_preserves_original_failure(self):
        original = RuntimeError("primary failure")

        def failing_finalizer(entry, timeout, force):
            raise OSError("cleanup failure")

        self.assertIsNone(LAB.cleanup_l1_entry(
            {"reaped": False}, original, finalizer=failing_finalizer
        ))
        self.assertTrue(any("cleanup raised" in note for note in original.__notes__))
        with self.assertRaisesRegex(RuntimeError, "cleanup raised"):
            LAB.cleanup_l1_entry(
                {"reaped": False}, None, finalizer=failing_finalizer
            )

    def test_enrolled_load_refuses_identity_change_before_store(self):
        boot = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        descriptor = {
            "authority_id": "a" * 32, "authority_node": os.uname().nodename,
            "authority_boot_id": boot, "enrollment_epoch": "b" * 64,
        }

        class BaseLedger:
            def __init__(self, root):
                self.root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
                self.pinned_authority = None

            @staticmethod
            def _validate(value):
                return value

            def _store(self, data):
                raise AssertionError("store must not run")

        module = types.SimpleNamespace(
            Ledger=BaseLedger, canonical=LAB.canonical,
            duplicate_keys=LAB.duplicate_keys,
        )
        changed = {**descriptor, "authority_id": "c" * 32}
        with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
            path = pathlib.Path(directory) / "ledger.json"
            path.write_bytes(LAB.canonical(changed))
            os.chmod(path, 0o600)
            ledger = LAB.enrolled_ledger_class(module, descriptor)(directory)
            try:
                with self.assertRaisesRegex(RuntimeError, "differs from enrollment"):
                    ledger._load()
                self.assertEqual(ledger.store_calls, 0)
            finally:
                os.close(ledger.root_fd)

    def test_qualifier_requires_exactly_one_winner(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("allowed != [0, 1]", source)
        self.assertIn('final["revision"] != initial["revision"] + 1', source)
        self.assertIn('len(final["slots"]) != 1', source)
        self.assertIn('"same-host clients only"', source)


if __name__ == "__main__":
    unittest.main()
