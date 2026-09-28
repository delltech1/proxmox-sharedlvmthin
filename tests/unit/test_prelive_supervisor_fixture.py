import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/prelive-supervisor-fixture.py"
SPEC = importlib.util.spec_from_file_location("prelive_supervisor_fixture", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class PreliveSupervisorFixtureTests(unittest.TestCase):
    def test_plan_and_every_execute_token_have_zero_runtime_syscalls(self):
        for token, expected_rc in ((None, 0), ("wrong", 2), (LAB.ACK, 2)):
            argv = [] if token is None else ["--execute-token", token]
            with mock.patch.object(LAB.os, "open", side_effect=AssertionError("open")), \
                 mock.patch.object(LAB.os, "mkdir", side_effect=AssertionError("mkdir")), \
                 mock.patch.object(LAB.os, "fsync", side_effect=AssertionError("fsync")), \
                 mock.patch.object(LAB.os, "pipe2", side_effect=AssertionError("pipe"), create=True), \
                 mock.patch.object(LAB.os, "fork", side_effect=AssertionError("fork"), create=True), \
                 mock.patch.object(LAB, "prctl_subreaper", side_effect=AssertionError("prctl")), \
                 contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(LAB.main(argv), expected_rc)
            result = json.loads(stream.getvalue())
            self.assertFalse(result["runtime_authorized"])
            self.assertFalse(result["storage_authorized"])
            self.assertFalse(result["postcondition_verified"])

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_journal_is_fresh_append_only_and_identity_bound(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            request_id = "a" * 32
            journal = LAB.FixtureJournal(request_id)
            digest = journal.append("INTENT")
            root = Path(parent) / (LAB.JOURNAL_PREFIX + request_id)
            owner = json.loads((root / "journal-owner.json").read_text())
            event = json.loads((root / "event-000001.json").read_text())
            self.assertEqual(len(digest), 64)
            self.assertEqual(event["sequence"], 1)
            self.assertEqual(owner["request_id"], request_id)
            self.assertEqual(owner["root"], event["root"])
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / "event-000001.json").stat().st_mode & 0o777, 0o600)
            journal.close()
            with self.assertRaises(FileExistsError):
                LAB.FixtureJournal(request_id)
            self.assertTrue((root / "event-000001.json").exists())

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_journal_failure_poison_is_permanent_and_sequence_not_reused(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("b" * 32)
            with mock.patch.object(LAB.os, "fsync", side_effect=OSError("fault")):
                with self.assertRaises(OSError):
                    journal.append("INTENT")
            with self.assertRaisesRegex(LAB.Refusal, "poisoned"):
                journal.append("CHILD_BOUND")
            self.assertEqual(journal._sequence, 1)
            self.assertEqual(journal._state, "POISONED")

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_reentrant_poison_cannot_be_overwritten_by_successful_fsync(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("c" * 32)
            real_fsync = LAB.os.fsync
            fired = []
            def reentrant_fsync(fd):
                if not fired:
                    fired.append(True)
                    with self.assertRaisesRegex(LAB.Refusal, "ambiguous persistence"):
                        journal.close()
                return real_fsync(fd)
            with mock.patch.object(LAB.os, "fsync", side_effect=reentrant_fsync):
                with self.assertRaisesRegex(LAB.Refusal, "poisoned"):
                    journal.append("INTENT")
            self.assertTrue(journal._poisoned)
            self.assertEqual(journal._state, "POISONED")

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_root_namespace_replacement_poison_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("d" * 32)
            root = Path(parent) / (LAB.JOURNAL_PREFIX + "d" * 32)
            moved = Path(parent) / "moved-evidence"
            root.rename(moved)
            root.mkdir(mode=0o700)
            with self.assertRaisesRegex(LAB.Refusal, "namespace identity"):
                journal.append("INTENT")
            self.assertTrue(journal._poisoned)
            self.assertFalse((root / "event-000001.json").exists())

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_root_mode_drift_poison_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("2" * 32)
            root = Path(parent) / (LAB.JOURNAL_PREFIX + "2" * 32)
            root.chmod(0o777)
            with self.assertRaisesRegex(LAB.Refusal, "namespace identity"):
                journal.append("INTENT")
            self.assertTrue(journal._poisoned)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_record_replacement_during_file_fsync_never_returns_success(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("3" * 32)
            root = Path(parent) / (LAB.JOURNAL_PREFIX + "3" * 32)
            event = root / "event-000001.json"
            displaced = root / "displaced-record"
            real_fsync = LAB.os.fsync
            fired = []
            def replace_record(fd):
                if not fired:
                    fired.append(True)
                    event.rename(displaced)
                    event.write_bytes(b"{}\n")
                    event.chmod(0o600)
                return real_fsync(fd)
            with mock.patch.object(LAB.os, "fsync", side_effect=replace_record):
                with self.assertRaises(LAB.Refusal):
                    journal.append("INTENT")
            self.assertTrue(journal._poisoned)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_parent_mode_drift_poison_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("4" * 32)
            old_mode = Path(parent).stat().st_mode & 0o777
            Path(parent).chmod(0o755 if old_mode != 0o755 else 0o700)
            with self.assertRaisesRegex(LAB.Refusal, "namespace identity"):
                journal.append("INTENT")
            self.assertTrue(journal._poisoned)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_short_writes_are_completed_before_success(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("e" * 32)
            real_write = LAB.os.write
            calls = []
            def short_write(fd, data):
                calls.append(len(data))
                return real_write(fd, data[:3])
            with mock.patch.object(LAB.os, "write", side_effect=short_write):
                digest = journal.append("INTENT")
            self.assertGreater(len(calls), 1)
            self.assertEqual(len(digest), 64)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_root_fsync_fault_poison_is_distinct_from_file_fsync(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("f" * 32)
            real_fsync = LAB.os.fsync
            calls = []
            def fail_second(fd):
                calls.append(fd)
                if len(calls) == 2:
                    raise OSError("root fsync fault")
                return real_fsync(fd)
            with mock.patch.object(LAB.os, "fsync", side_effect=fail_second):
                with self.assertRaisesRegex(OSError, "root fsync"):
                    journal.append("INTENT")
            self.assertEqual(len(calls), 2)
            self.assertTrue(journal._poisoned)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_foreign_thread_attempt_poison_is_monotonic(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("1" * 32)
            real_fsync = LAB.os.fsync
            failures = []
            fired = []
            def foreign_attempt():
                try:
                    journal.append("CHILD_BOUND")
                except BaseException as exc:
                    failures.append(exc)
            def launch_during_fsync(fd):
                if not fired:
                    fired.append(True)
                    worker = threading.Thread(target=foreign_attempt)
                    worker.start()
                    worker.join()
                return real_fsync(fd)
            with mock.patch.object(LAB.os, "fsync", side_effect=launch_during_fsync):
                with self.assertRaisesRegex(LAB.Refusal, "poisoned"):
                    journal.append("INTENT")
            self.assertEqual(len(failures), 1)
            self.assertIsInstance(failures[0], LAB.Refusal)
            self.assertTrue(journal._poisoned)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_record_close_fault_poison_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("5" * 32)
            real_close = LAB.os.close
            fired = []
            def close_then_fault(fd):
                real_close(fd)
                if not fired:
                    fired.append(True)
                    raise OSError("record close fault")
            with mock.patch.object(LAB.os, "close", side_effect=close_then_fault):
                with self.assertRaisesRegex(OSError, "record close"):
                    journal.append("INTENT")
            self.assertTrue(journal._poisoned)

    @unittest.skipUnless(os.name == "posix", "journal fixture requires Linux dirfd semantics")
    def test_descriptor_close_fault_cannot_claim_closed_safe(self):
        with tempfile.TemporaryDirectory() as parent, \
             mock.patch.object(LAB, "JOURNAL_PARENT", parent):
            journal = LAB.FixtureJournal("6" * 32)
            real_close = LAB.os.close
            fired = []
            def close_then_fault(fd):
                real_close(fd)
                if not fired:
                    fired.append(True)
                    raise OSError("directory close fault")
            with mock.patch.object(LAB.os, "close", side_effect=close_then_fault):
                with self.assertRaisesRegex(LAB.Refusal, "close outcome"):
                    journal.close()
            self.assertTrue(journal._poisoned)
            self.assertEqual(journal._state, "POISONED")

    def test_journal_schema_refuses_bad_identity_before_filesystem(self):
        for value in ("A" * 32, "a" * 31, "g" * 32, 7):
            with mock.patch.object(LAB.os, "open", side_effect=AssertionError("open")):
                with self.assertRaises(LAB.Refusal):
                    LAB.FixtureJournal(value)

    def test_scope_is_fixed_and_has_no_shell_or_storage_surface(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(LAB.plan()["argv"], ["/usr/bin/true"])
        for forbidden in ("subprocess", "shell=True", "os.system", "killpg",
                          "dmsetup", "losetup", "vgs", "lvs", "pvesm"):
            self.assertNotIn(forbidden, source)
        self.assertIn("os.execve(executable_fd, [TRUE_PATH], dict(CLEAN_ENV))", source)
        self.assertNotIn("os.execv(", source)

    def test_parent_records_pid_immediately_after_fork_branch(self):
        source = SCRIPT.read_text(encoding="utf-8")
        branch = source.index("if pid == 0:")
        record = source.index('lifecycle["owned_child"] = {"pid": pid', branch)
        returned = source.index("return lifecycle", record)
        between = source[branch:record]
        self.assertNotIn("os.close", between)
        self.assertNotIn("pidfd_open", between)
        self.assertLess(branch, record)
        self.assertLess(record, returned)

    def test_parent_failure_after_fork_retains_owned_pid_in_caller_record(self):
        bundle = {"grant_r": 10, "grant_w": 11, "status_r": 12,
                  "status_w": 13, "stdout_r": 14, "stdout_w": 15,
                  "stderr_r": 16, "stderr_w": 17, "null_fd": 18}
        lifecycle = {}
        with mock.patch.object(LAB, "snapshot_open_fds", return_value=list(range(10, 20))), \
             mock.patch.object(LAB.os, "fork", return_value=222, create=True), \
             mock.patch.object(LAB.os, "close", side_effect=OSError("close failed")):
            with self.assertRaises(OSError):
                LAB.spawn_unarmed(bundle, 19, lifecycle)
        self.assertEqual(lifecycle["owned_child"]["pid"], 222)
        self.assertFalse(lifecycle["owned_child"]["settled"])

    def test_child_fd_snapshot_is_not_limited_to_known_bundle(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('os.scandir("/proc/self/fd")', source)
        self.assertIn("inherited = snapshot_open_fds()", source)

    def test_fd_bundle_cleans_every_partial_allocation_failure(self):
        for fail_step in range(5):
            next_fd = iter(range(10, 30))
            allocated = []
            calls = {"step": 0}
            def pipe2(_flags):
                if calls["step"] == fail_step:
                    raise OSError("allocation failed")
                calls["step"] += 1
                pair = (next(next_fd), next(next_fd))
                allocated.extend(pair)
                return pair
            def open_devnull(*_args):
                if fail_step == 4:
                    raise OSError("allocation failed")
                fd = next(next_fd)
                allocated.append(fd)
                return fd
            closed = []
            with mock.patch.object(LAB, "require_standard_fds"), \
                 mock.patch.object(LAB.os, "O_CLOEXEC", 0, create=True), \
                 mock.patch.object(LAB.os, "O_NOFOLLOW", 0, create=True), \
                 mock.patch.object(LAB.os, "pipe2", side_effect=pipe2, create=True), \
                 mock.patch.object(LAB.os, "open", side_effect=open_devnull), \
                 mock.patch.object(LAB.os, "close", side_effect=lambda fd: closed.append(fd)):
                with self.assertRaises(OSError):
                    LAB.create_fd_bundle()
            self.assertEqual(sorted(closed), sorted(allocated))

    def test_fd_bundle_rejects_internal_fd_zero_and_closes_it(self):
        closed = []
        with mock.patch.object(LAB, "require_standard_fds"), \
             mock.patch.object(LAB.os, "O_CLOEXEC", 0, create=True), \
             mock.patch.object(LAB.os, "pipe2", return_value=(0, 4), create=True), \
             mock.patch.object(LAB.os, "close", side_effect=lambda fd: closed.append(fd)):
            with self.assertRaises(LAB.Refusal):
                LAB.create_fd_bundle()
        self.assertEqual(sorted(closed), [0, 4])

    def test_standard_fds_must_already_be_open(self):
        def fstat(fd):
            if fd == 1:
                raise OSError("closed")
            return object()
        with mock.patch.object(LAB.os, "fstat", side_effect=fstat):
            with self.assertRaisesRegex(LAB.Refusal, "standard FD 1"):
                LAB.require_standard_fds()

    def test_prctl_pointer_is_explicitly_pointer_width(self):
        class Function:
            def __init__(self):
                self.args = None
                self.argtypes = None
                self.restype = None
            def __call__(self, *args):
                self.args = args
                return 0
        class Libc:
            def __init__(self):
                self.prctl = Function()
        libc = Libc()
        high = 2 ** 40 + 123
        cast_result = mock.Mock(value=high)
        with mock.patch.object(LAB.ctypes, "CDLL", return_value=libc), \
             mock.patch.object(LAB.ctypes, "cast", return_value=cast_result):
            LAB.prctl_subreaper(LAB.PR_GET_CHILD_SUBREAPER)
        self.assertEqual(libc.prctl.args[1].value, high)
        self.assertIs(libc.prctl.argtypes[1], LAB.ctypes.c_void_p)

    def test_executable_fd_zero_is_closed_and_refused_before_fork(self):
        closed = []
        with mock.patch.object(LAB, "require_standard_fds"), \
             mock.patch.object(LAB.os, "O_CLOEXEC", 0, create=True), \
             mock.patch.object(LAB.os, "O_NOFOLLOW", 0, create=True), \
             mock.patch.object(LAB.os, "open", return_value=0), \
             mock.patch.object(LAB.os, "close", side_effect=lambda fd: closed.append(fd)), \
             mock.patch.object(LAB.os, "fork", side_effect=AssertionError("fork"), create=True):
            with self.assertRaisesRegex(LAB.Refusal, "unsafe FD"):
                LAB.open_pinned_executable()
        self.assertEqual(closed, [0])

    def test_spawn_refuses_executable_alias_before_fork(self):
        bundle = {"grant_r": 10, "grant_w": 11, "status_r": 12,
                  "status_w": 13, "stdout_r": 14, "stdout_w": 15,
                  "stderr_r": 16, "stderr_w": 17, "null_fd": 18}
        for executable_fd in (0, True, 10):
            with mock.patch.object(LAB.os, "fork", side_effect=AssertionError("fork"), create=True):
                with self.assertRaisesRegex(LAB.Refusal, "executable FD"):
                    LAB.spawn_unarmed(bundle, executable_fd, {})

    def test_executable_is_pinned_no_follow_and_permission_checked(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("os.O_NOFOLLOW", source)
        self.assertIn("stat.S_ISREG", source)
        self.assertIn("info.st_uid != 0", source)
        self.assertIn("info.st_mode & 0o022", source)
        self.assertIn("os.fstat(fd)", source)
        self.assertIn("_sha256_fd(fd)", source)

    def test_child_requires_exact_grant_then_eof_and_error_fd_is_cloexec(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("grant != b\"G\" or not saw_eof", source)
        self.assertIn("os.pipe2(os.O_CLOEXEC)", source)
        self.assertIn('os.write(bundle["status_w"], b"R")', source)
        self.assertIn('os.write(status_fd, ("E:" + message)', source)
        self.assertIn("never a standalone proof of exec", source)


if __name__ == "__main__":
    unittest.main()
