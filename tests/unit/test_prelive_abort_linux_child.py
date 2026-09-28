import importlib
import copy
import fcntl
import os
from pathlib import Path
import select
import sys
import unittest
import time
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments" / "thick-generations"
sys.path.insert(0, str(EXP))

LAB = importlib.import_module("prelive_abort_linux_child")
SPLIT = importlib.import_module("prelive_abort_fork_split")
FDS = importlib.import_module("prelive_real_fd_adapter")
SUP = importlib.import_module("prelive_supervisor_model")


class AbortLinuxChildSourceTests(unittest.TestCase):
    def context(self):
        return {"schema": 1, "parent": {"pid": 10, "starttime": 20,
                    "boot_id": "11111111-1111-1111-1111-111111111111"},
                "started_ns": 100, "deadline_ns": 200,
                "descriptor": {"endpoints": []},
                "descriptor_digest": "0" * 64}

    def test_parent_branch_returns_exact_positive_pid(self):
        with mock.patch.object(LAB.SPLIT, "AbortChildRoutine"), \
                mock.patch.object(LAB, "_validate_prefork_context", return_value=b"x"), \
                mock.patch.object(LAB.SUP, "canonical", return_value=b"x"), \
                mock.patch.object(LAB.time, "monotonic_ns", return_value=150), \
                mock.patch.object(LAB.threading, "active_count", return_value=1), \
                mock.patch.object(LAB.os, "listdir", return_value=["1"]), \
                mock.patch.object(LAB.os, "fork", return_value=1234):
            self.assertEqual(LAB.fork_abort_child_once(
                LAB._FORK_KEY, self.context()), 1234)

    def test_invalid_or_ambiguous_fork_result_refuses(self):
        for value in (0.0, -1, True):
            with self.subTest(value=value), \
                    mock.patch.object(LAB.SPLIT, "AbortChildRoutine"), \
                    mock.patch.object(LAB, "_validate_prefork_context", return_value=b"x"), \
                    mock.patch.object(LAB.SUP, "canonical", return_value=b"x"), \
                    mock.patch.object(LAB.time, "monotonic_ns", return_value=150), \
                    mock.patch.object(LAB.threading, "active_count", return_value=1), \
                    mock.patch.object(LAB.os, "listdir", return_value=["1"]), \
                    mock.patch.object(LAB.os, "fork", return_value=value):
                with self.assertRaises(BaseException):
                    LAB.fork_abort_child_once(LAB._FORK_KEY, self.context())

    def test_public_fork_key_is_refused_before_syscall(self):
        with mock.patch.object(LAB.os, "fork") as fork:
            with self.assertRaises(LAB.Refusal):
                LAB.fork_abort_child_once(object(), self.context())
            fork.assert_not_called()

    def test_invalid_context_is_refused_before_fork(self):
        with mock.patch.object(LAB.os, "fork") as fork:
            with self.assertRaises(BaseException):
                LAB.fork_abort_child_once(LAB._FORK_KEY, self.context())
            fork.assert_not_called()

    def test_multithread_process_is_refused_before_fork(self):
        with mock.patch.object(LAB.SPLIT, "AbortChildRoutine"), \
                mock.patch.object(LAB, "_validate_prefork_context", return_value=b"x"), \
                mock.patch.object(LAB.SUP, "canonical", return_value=b"x"), \
                mock.patch.object(LAB.threading, "active_count", return_value=2), \
                mock.patch.object(LAB.os, "fork") as fork:
            with self.assertRaises(LAB.Refusal):
                LAB.fork_abort_child_once(LAB._FORK_KEY, self.context())
            fork.assert_not_called()

    def test_expired_deadline_is_refused_before_fork(self):
        with mock.patch.object(LAB, "_validate_prefork_context", return_value=b"x"), \
                mock.patch.object(LAB.SUP, "canonical", return_value=b"x"), \
                mock.patch.object(LAB.SPLIT, "AbortChildRoutine"), \
                mock.patch.object(LAB.threading, "active_count", return_value=1), \
                mock.patch.object(LAB.os, "listdir", return_value=["1"]), \
                mock.patch.object(LAB.time, "monotonic_ns", return_value=200), \
                mock.patch.object(LAB.os, "fork") as fork:
            with self.assertRaises(LAB.Refusal):
                LAB.fork_abort_child_once(LAB._FORK_KEY, self.context())
            fork.assert_not_called()

    def test_custom_context_is_rejected_without_deepcopy_callback(self):
        calls = []
        class Evil(dict):
            def __deepcopy__(self, memo):
                calls.append("deepcopy")
                return dict(self)
        with mock.patch.object(LAB.os, "fork") as fork:
            with self.assertRaises(BaseException):
                LAB.fork_abort_child_once(LAB._FORK_KEY, Evil(self.context()))
            fork.assert_not_called()
        self.assertEqual(calls, [])

    def test_inherited_fd_cleanup_closes_every_unlisted_descriptor(self):
        with mock.patch.object(LAB.os, "listdir",
                               return_value=["0", "1", "2", "7", "8"]), \
                mock.patch.object(LAB.os, "close") as close:
            LAB._close_unlisted_fds({7})
        self.assertEqual([call.args[0] for call in close.call_args_list],
                         [0, 1, 2, 8])

    def test_wrong_poll_fd_refuses_without_read(self):
        calls = object.__new__(LAB.LinuxAbortChildCalls)
        expected = {"dev": 1, "inode": 2, "mode": 0o10600,
                    "flags": os.O_RDONLY, "fd_flags": fcntl.FD_CLOEXEC}
        object.__setattr__(calls, "_fds", {"grant_child": 10})
        object.__setattr__(calls, "_expected", {"grant_child": expected})
        object.__setattr__(calls, "_deadline_ns", 1000)
        object.__setattr__(calls, "_closed", set())
        object.__setattr__(calls, "_quarantined", set())
        object.__setattr__(calls, "_sealed", True)
        poller = mock.Mock()
        poller.poll.return_value = [(11, select.POLLIN)]
        with mock.patch.object(LAB, "_identity", return_value=expected), \
                mock.patch.object(LAB.time, "monotonic_ns", return_value=100), \
                mock.patch.object(LAB.select, "poll", return_value=poller), \
                mock.patch.object(LAB.os, "read") as read:
            with self.assertRaises(LAB.Refusal):
                calls.read_grant(1)
            read.assert_not_called()

    def test_recycled_fd_during_poll_refuses_without_foreign_read(self):
        calls = object.__new__(LAB.LinuxAbortChildCalls)
        expected = {"dev": 1, "inode": 2, "mode": 0o10600,
                    "flags": os.O_RDONLY, "fd_flags": fcntl.FD_CLOEXEC}
        foreign = {**expected, "inode": 99}
        object.__setattr__(calls, "_fds", {"grant_child": 10})
        object.__setattr__(calls, "_expected", {"grant_child": expected})
        object.__setattr__(calls, "_deadline_ns", 1000)
        object.__setattr__(calls, "_closed", set())
        object.__setattr__(calls, "_quarantined", set())
        object.__setattr__(calls, "_sealed", True)
        class Poller:
            def register(self, fd, mask):
                self.fd = fd
            def poll(self, timeout):
                object.__setattr__(calls, "_fds", {"grant_child": 11})
                return [(self.fd, select.POLLIN)]
        with mock.patch.object(LAB, "_identity",
                               side_effect=lambda fd: expected if fd == 10 else foreign), \
                mock.patch.object(LAB.time, "monotonic_ns", return_value=100), \
                mock.patch.object(LAB.select, "poll", return_value=Poller()), \
                mock.patch.object(LAB.os, "read") as read:
            with self.assertRaises(LAB.Refusal):
                calls.read_grant(1)
            read.assert_not_called()

    def test_child_branch_exits_once_with_requested_abort_codes(self):
        class Stop(BaseException):
            pass
        for code in (SPLIT.EXIT_ABORT_EOF, SPLIT.EXIT_GRANT_DATA):
            routine = mock.Mock()
            routine.step.side_effect = LAB._RequestedExit(code)
            with self.subTest(code=code), \
                    mock.patch.object(LAB, "_validate_prefork_context", return_value=b"x"), \
                    mock.patch.object(LAB.SUP, "canonical", return_value=b"x"), \
                    mock.patch.object(LAB.SPLIT, "AbortChildRoutine", return_value=routine), \
                    mock.patch.object(LAB.threading, "active_count", return_value=1), \
                    mock.patch.object(LAB.os, "listdir", return_value=["1"]), \
                    mock.patch.object(LAB.time, "monotonic_ns", return_value=150), \
                    mock.patch.object(LAB.os, "fork", return_value=0), \
                    mock.patch.object(LAB, "_close_unlisted_fds"), \
                    mock.patch.object(LAB, "LinuxAbortChildCalls"), \
                    mock.patch.object(LAB.os, "_exit", side_effect=Stop) as exit_call:
                with self.assertRaises(Stop):
                    LAB.fork_abort_child_once(LAB._FORK_KEY, self.context())
                exit_call.assert_called_once_with(code)

    def test_child_branch_unexpected_error_exits_75_once(self):
        class Stop(BaseException):
            pass
        routine = mock.Mock()
        routine.step.side_effect = RuntimeError("child fault")
        with mock.patch.object(LAB, "_validate_prefork_context", return_value=b"x"), \
                mock.patch.object(LAB.SUP, "canonical", return_value=b"x"), \
                mock.patch.object(LAB.SPLIT, "AbortChildRoutine", return_value=routine), \
                mock.patch.object(LAB.threading, "active_count", return_value=1), \
                mock.patch.object(LAB.os, "listdir", return_value=["1"]), \
                mock.patch.object(LAB.time, "monotonic_ns", return_value=150), \
                mock.patch.object(LAB.os, "fork", return_value=0), \
                mock.patch.object(LAB, "_close_unlisted_fds"), \
                mock.patch.object(LAB, "LinuxAbortChildCalls"), \
                mock.patch.object(LAB.os, "_exit", side_effect=Stop) as exit_call:
            with self.assertRaises(Stop):
                LAB.fork_abort_child_once(LAB._FORK_KEY, self.context())
            exit_call.assert_called_once_with(SPLIT.EXIT_CHILD_ERROR)

    @unittest.skipUnless(os.name == "posix" and hasattr(os, "pipe2"),
                         "real Linux pipe semantics required")
    def test_real_routine_and_linux_calls_reach_ready_with_exact_canaries(self):
        handle = FDS.allocate_real_fd_graph(
            FDS.LinuxFDCalls(), FDS.CLOSE_ONLY_NO_WRITE)
        retained = {}
        try:
            descriptor = {
                "schema": 1, "kind": "NO_GRANT_ALLOCATED_FD_GRAPH_V1",
                "request_id": "1" * 32, "graph_id": "2" * 32,
                "supervisor": copy.deepcopy(handle.owner),
                "pre_fork_ticket_id": "3" * 32,
                "phase": "PARENT_PRE_FORK_ALL_ENDPOINTS_OWNED",
                "grant_policy": FDS.CLOSE_ONLY_NO_WRITE,
                "endpoints": [{"role": role, **copy.deepcopy(identity)}
                              for role, identity in handle.frozen_bundle.items()],
                "endpoint_count": 8, "all_cloexec": True,
                "parent_nonblocking": True}
            now = time.monotonic_ns()
            context = {"schema": 1, "parent": copy.deepcopy(handle.owner),
                       "started_ns": now, "deadline_ns": now + 5_000_000_000,
                       "descriptor": descriptor,
                       "descriptor_digest": SUP.digest(descriptor)}
            for role in ("status_parent", "stdout_parent", "stderr_parent"):
                retained[role] = fcntl.fcntl(
                    handle.frozen_bundle[role]["fd"], fcntl.F_DUPFD_CLOEXEC, 3)
            calls = LAB.LinuxAbortChildCalls(LAB._CALLS_KEY, context)
            routine = SPLIT.AbortChildRoutine(SPLIT._CHILD_KEY, context)
            child_identity = {"pid": handle.owner["pid"] + 1000,
                              "ppid": handle.owner["pid"], "starttime": 99,
                              "boot_id": handle.owner["boot_id"]}
            def identity(self):
                return copy.deepcopy(child_identity)
            with mock.patch.object(LAB.LinuxAbortChildCalls,
                                   "current_child_identity", new=identity):
                result = routine.step(calls)
            self.assertEqual(result["classification"],
                             "CHILD_WAITING_GRANT_EOF")
            self.assertEqual(os.read(retained["status_parent"], 1),
                             SPLIT.STATUS_READY)
            self.assertEqual(os.read(retained["stdout_parent"], 65536),
                             SPLIT.STDOUT_CANARY)
            self.assertEqual(os.read(retained["stderr_parent"], 65536),
                             SPLIT.STDERR_CANARY)
            with self.assertRaises(LAB._RequestedExit) as requested:
                routine.step(calls)
            self.assertEqual(requested.exception.code,
                             SPLIT.EXIT_ABORT_EOF)
        finally:
            for fd in list(retained.values()) + [
                    item["fd"] for item in handle.frozen_bundle.values()]:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def test_source_has_no_exec_grant_write_or_storage_surface(self):
        source = (EXP / "prelive_abort_linux_child.py").read_text()
        for forbidden in ("execve", "subprocess", "dmsetup", "lvcreate",
                          "pvesm", "grant_parent\", data"):
            self.assertNotIn(forbidden, source)
        self.assertEqual(source.count("os.fork()"), 1)
        self.assertEqual(source.count("os._exit("), 1)


if __name__ == "__main__":
    unittest.main()
