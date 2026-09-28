"""Pure injected backend tests: no subprocess, device, sysfs or /proc access."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/lazy-zero-loop-lab.py"
SPEC = importlib.util.spec_from_file_location("lazy_zero_kernel_model", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)
BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 900, "start_ticks": 1000}
ARGV = ["/sbin/dmsetup", "create", "slt-lazy-model-only"]
REQUEST = "1" * 32


class MemoryJournal:
    def __init__(self):
        self.events = []
        self.fail_kind = None

    def append(self, event):
        if event["kind"] == self.fail_kind:
            raise OSError("mock persistence failure")
        self.events.append(copy.deepcopy(event))


class ScriptedBackend:
    model_only = True

    def __init__(self, journal, pidfd=True):
        self.journal = journal
        self.pidfd = pidfd
        self.calls = []
        self.transforms = {}
        self.observations = 0

    def transform(self, key, result):
        if key in self.transforms:
            return self.transforms[key](copy.deepcopy(result))
        return copy.deepcopy(result)

    def capabilities(self):
        self.calls.append("capabilities")
        return self.transform("capabilities", {"pidfd": self.pidfd, "owned_child_wnowait": True})

    def spawn_model(self, request, owner):
        self.calls.append("spawn")
        if self.journal.events[-1]["kind"] != "EXECUTOR_INTENT":
            raise AssertionError("spawn without persisted intent")
        self.child = {"request_id": request["request_id"], "boot_id": request["boot_id"],
                      "pid": 901, "start_ticks": 1001, "argv": request["argv"],
                      "owner_pid": owner["pid"], "owner_start_ticks": owner["start_ticks"]}
        return self.transform("spawn", self.child)

    def observe_spawn(self, child):
        self.calls.append("observe")
        self.observations += 1
        return self.transform("observe" + str(self.observations), self.child)

    def open_pidfd_model(self, child):
        self.calls.append("pidfd")
        return self.transform("pidfd", {"identity": self.child, "token": 10})

    def wait_exact_model(self, child, token, timeout_ms):
        self.calls.append("wait")
        capture = {"eof": True, "truncated": False, "bytes": 0,
                   "sha256": hashlib.sha256(b"").hexdigest()}
        return self.transform("wait", {
            "identity": self.child, "wait_method": "PIDFD_WNOWAIT" if self.pidfd else "OWNED_CHILD_WNOWAIT",
            "pidfd_token": token, "elapsed_ms": 3, "timed_out": False,
            "process_state": "Z", "unreaped": True, "returncode": 0,
            "descendants": [], "descendants_complete": True, "stdout": capture, "stderr": capture})

    def reap_exact_model(self, child, token, timeout_ms):
        self.calls.append("reap")
        self.reap_budget = timeout_ms
        return self.transform("reap", {
            "identity": self.child, "wait_method": "PIDFD_WNOWAIT" if self.pidfd else "OWNED_CHILD_WNOWAIT",
            "pidfd_token": token, "reaped": True, "returncode": 0, "elapsed_ms": 2})


def set_field(key, value):
    return lambda result: {**result, key: value}


class KernelExecutorModelTests(unittest.TestCase):
    def make(self, pidfd=True):
        journal = MemoryJournal()
        backend = ScriptedBackend(journal, pidfd)
        return LAB.KernelExecutorModel(journal, backend, OWNER, BOOT), journal, backend

    def assert_unknown(self, executor, backend, reap=False):
        with self.assertRaises((LAB.Refusal, OSError)):
            executor.run(REQUEST, ARGV)
        self.assertEqual(executor.state, "UNKNOWN")
        self.assertEqual(backend.calls.count("spawn"), 1)
        self.assertEqual("reap" in backend.calls, reap)
        calls = list(backend.calls)
        with self.assertRaisesRegex(LAB.Refusal, "redispatch"):
            executor.run(REQUEST, ARGV)
        self.assertEqual(backend.calls, calls)

    def test_exact_pidfd_and_owned_wnowait_paths_are_model_only(self):
        for pidfd in (True, False):
            executor, journal, backend = self.make(pidfd)
            with mock.patch.object(LAB.os, "open", side_effect=AssertionError("no filesystem backend")):
                result = executor.run(REQUEST, ARGV, timeout_ms=100)
            self.assertEqual(executor.state, "TERMINAL")
            self.assertIs(result["runtime_authorized"], False)
            self.assertIs(result["postcondition_verified"], False)
            self.assertEqual(backend.reap_budget, 97)
            self.assertEqual("pidfd" in backend.calls, pidfd)
            self.assertEqual([event["kind"] for event in journal.events],
                             ["EXECUTOR_INTENT", "EXECUTOR_BOUND", "EXECUTOR_TERMINAL"])
            with self.assertRaises(LAB.Refusal):
                executor.run("2" * 32, ARGV)

    def test_failed_intent_persist_never_spawns(self):
        executor, journal, backend = self.make()
        journal.fail_kind = "EXECUTOR_INTENT"
        with self.assertRaises(OSError):
            executor.run(REQUEST, ARGV)
        self.assertNotIn("spawn", backend.calls)
        self.assertEqual(executor.state, "UNKNOWN")

    def test_wait_capability_false_or_nonboolean_refuses_before_dispatch(self):
        for key, value in (("pidfd", 1), ("owned_child_wnowait", False)):
            executor, journal, backend = self.make()
            backend.transforms["capabilities"] = set_field(key, value)
            with self.assertRaises(LAB.Refusal):
                executor.run(REQUEST, ARGV)
            self.assertNotIn("spawn", backend.calls)
            self.assertEqual(executor.state, "UNKNOWN")

    def test_ambiguous_spawn_exception_no_retry_or_cleanup(self):
        executor, journal, backend = self.make()
        def ambiguous(unused):
            raise OSError("child may already exist")
        backend.transforms["spawn"] = ambiguous
        self.assert_unknown(executor, backend)
        self.assertIs(journal.events[-1]["retry_dispatched"], False)
        self.assertIs(journal.events[-1]["cleanup_dispatched"], False)

    def test_child_argv_pid_start_owner_boot_request_and_extra_fields_refuse(self):
        changes = (("argv", ["/bin/false"]), ("pid", True), ("start_ticks", False),
                   ("owner_pid", 800), ("owner_start_ticks", 1002),
                   ("boot_id", "22345678-1234-1234-1234-123456789abc"),
                   ("request_id", "2" * 32), ("unexpected", True))
        for key, value in changes:
            executor, journal, backend = self.make()
            backend.transforms["spawn"] = set_field(key, value)
            self.assert_unknown(executor, backend)

    def test_identity_change_during_proc_observe_or_pidfd_pin_refuses(self):
        for phase in ("observe1", "observe2"):
            executor, journal, backend = self.make()
            backend.transforms[phase] = set_field("start_ticks", 1002)
            self.assert_unknown(executor, backend)
            self.assertNotIn("wait", backend.calls)

    def test_pidfd_open_failure_never_silently_falls_back(self):
        executor, journal, backend = self.make()
        def failure(unused):
            raise OSError("pidfd pin ambiguous")
        backend.transforms["pidfd"] = failure
        self.assert_unknown(executor, backend)
        self.assertNotIn("wait", backend.calls)

    def test_wait_timeout_dstate_running_and_already_reaped_are_unknown(self):
        for key, value in (("timed_out", True), ("elapsed_ms", 30000),
                           ("process_state", "D"), ("process_state", "R"),
                           ("unreaped", False), ("returncode", True),
                           ("descendants_complete", False), ("descendants", [{"pid": 99}])):
            executor, journal, backend = self.make()
            backend.transforms["wait"] = set_field(key, value)
            self.assert_unknown(executor, backend)

    def test_held_pipe_truncation_and_output_bound_are_unknown(self):
        for key, value in (("eof", False), ("truncated", True),
                           ("bytes", 1048577), ("bytes", True), ("sha256", "bad")):
            executor, journal, backend = self.make()
            def change(result):
                result["stdout"] = {**result["stdout"], key: value}
                return result
            backend.transforms["wait"] = change
            self.assert_unknown(executor, backend)

    def test_wait_pidfd_and_nested_child_must_be_exact_typed(self):
        for phase in ("pidfd", "wait", "reap"):
            executor, journal, backend = self.make()
            def change(result):
                result["identity"]["start_ticks"] = True
                return result
            backend.transforms[phase] = change
            self.assert_unknown(executor, backend, reap=phase == "reap")
        executor, journal, backend = self.make()
        backend.transforms["wait"] = set_field("pidfd_token", 11)
        self.assert_unknown(executor, backend)

    def test_reap_mismatch_or_timeout_stays_unknown(self):
        for key, value in (("reaped", False), ("returncode", 1),
                           ("elapsed_ms", 29998), ("pidfd_token", 11)):
            executor, journal, backend = self.make()
            backend.transforms["reap"] = set_field(key, value)
            self.assert_unknown(executor, backend, reap=True)

    def test_nonzero_exit_is_reaped_but_not_safe_to_repeat(self):
        executor, journal, backend = self.make()
        backend.transforms["wait"] = set_field("returncode", 1)
        backend.transforms["reap"] = set_field("returncode", 1)
        self.assert_unknown(executor, backend, reap=True)

    def test_bound_and_terminal_persistence_failures_do_not_retry(self):
        for event in ("EXECUTOR_BOUND", "EXECUTOR_TERMINAL"):
            executor, journal, backend = self.make()
            journal.fail_kind = event
            self.assert_unknown(executor, backend, reap=event == "EXECUTOR_TERMINAL")

    def test_validated_wait_receipt_is_not_mutable_by_later_backend_callback(self):
        executor, journal, backend = self.make()
        saved = {}
        original_wait = backend.wait_exact_model
        original_reap = backend.reap_exact_model
        def wait(*args):
            saved["wait"] = original_wait(*args)
            return saved["wait"]
        def reap(*args):
            saved["wait"]["stdout"]["eof"] = False
            return original_reap(*args)
        backend.wait_exact_model = wait
        backend.reap_exact_model = reap
        result = executor.run(REQUEST, ARGV)
        self.assertTrue(result["wait"]["stdout"]["eof"])
        self.assertTrue(journal.events[-1]["result"]["wait"]["stdout"]["eof"])


def dm_identity():
    return {"kind": "dm", "role": "clone", "devno": "253:2", "diskseq": 9,
            "size_bytes": 134217728, "readonly": False, "holders": [],
            "name": "slt-lazy-model", "uuid": "SLT-LAZY-MODEL", "table":
            "0 262144 clone 7:2 7:1 253:1 2048 2 no_hydration no_discard_passdown",
            "suspended": False, "open_count": 0, "dependencies": ["7:1", "7:2", "253:1"]}


class CollectorBackend:
    model_only = True

    def __init__(self, identity):
        self.identity = copy.deepcopy(identity)
        self.selectors = []
        self.change = None

    def device_snapshot(self, selector):
        self.selectors.append(copy.deepcopy(selector))
        result = {"boot_id": BOOT, "kernel": copy.deepcopy(self.identity),
                  "node_devno": self.identity["devno"], "node_diskseq": self.identity["diskseq"]}
        if self.change:
            return self.change(result, len(self.selectors))
        return result


class IdentityCollectorModelTests(unittest.TestCase):
    def test_exact_dm_snapshots_query_incarnation_never_name_only(self):
        identity = dm_identity()
        backend = CollectorBackend(identity)
        result = LAB.collect_exact_identity_model(identity, backend, BOOT)
        self.assertIs(result["runtime_authorized"], False)
        self.assertIs(result["continuous_identity_proven"], False)
        self.assertEqual(backend.selectors, [{"kind": "dm", "devno": "253:2", "diskseq": 9}] * 2)

    def test_every_dm_identity_field_mismatch_blocks(self):
        changes = (("uuid", "foreign"), ("devno", "253:3"), ("diskseq", 10),
                   ("table", "0 262144 zero"), ("dependencies", []),
                   ("readonly", True), ("suspended", True), ("open_count", 1),
                   ("holders", ["253:10"]), ("size_bytes", 4096))
        for key, value in changes:
            identity = dm_identity()
            backend = CollectorBackend(identity)
            def change(result, unused):
                result["kernel"][key] = value
                return result
            backend.change = change
            with self.assertRaises(LAB.Refusal):
                LAB.collect_exact_identity_model(identity, backend, BOOT)

    def test_loop_backing_identity_offset_limit_and_holders_are_exact(self):
        identity = {"kind": "loop", "role": "data", "devno": "7:1", "diskseq": 3,
                    "size_bytes": 134217728, "readonly": False, "holders": ["253:2"],
                    "backing_dev": 40, "backing_inode": 123, "offset": 0, "sizelimit": 0}
        LAB.collect_exact_identity_model(identity, CollectorBackend(identity), BOOT)
        for key, value in (("backing_dev", 41), ("backing_inode", 124), ("offset", 4096),
                           ("sizelimit", 4096), ("holders", [])):
            backend = CollectorBackend(identity)
            def change(result, unused):
                result["kernel"][key] = value
                return result
            backend.change = change
            with self.assertRaises(LAB.Refusal):
                LAB.collect_exact_identity_model(identity, backend, BOOT)

    def test_stale_node_foreign_boot_extra_schema_and_second_snapshot_change_refuse(self):
        for key, value in (("node_devno", "253:9"), ("node_diskseq", True),
                           ("boot_id", "22345678-1234-1234-1234-123456789abc"),
                           ("extra", "ignored?")):
            identity = dm_identity()
            backend = CollectorBackend(identity)
            backend.change = lambda result, count: {**result, key: value} if count == 2 else result
            with self.assertRaises(LAB.Refusal):
                LAB.collect_exact_identity_model(identity, backend, BOOT)


if __name__ == "__main__":
    unittest.main()
