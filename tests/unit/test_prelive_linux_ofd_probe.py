import errno
import fcntl
import importlib
import os
from pathlib import Path
import sys
import _thread
import threading
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
LAB = importlib.import_module("prelive_linux_ofd_probe")


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux KCMP only")
class LinuxOFDProbeTests(unittest.TestCase):
    def setUp(self):
        self.probe = LAB.LinuxOFDProbe()

    def test_duplicate_pipe_is_same_open_file_description(self):
        read_fd, write_fd = os.pipe()
        anchor = fcntl.fcntl(read_fd, fcntl.F_DUPFD_CLOEXEC, 3)
        try:
            result = self.probe.compare(read_fd, anchor)
            self.assertTrue(result["same"])
            self.assertEqual(result["raw_result"], 0)
            self.assertFalse(result["runtime_authorized"])
        finally:
            os.close(anchor)
            os.close(read_fd)
            os.close(write_fd)

    def test_distinct_open_descriptions_are_not_same(self):
        left = os.open("/dev/null", os.O_RDONLY)
        right = os.open("/dev/null", os.O_RDONLY)
        try:
            result = self.probe.compare(left, right)
            self.assertFalse(result["same"])
            self.assertGreater(result["raw_result"], 0)
        finally:
            os.close(right)
            os.close(left)

    def test_closed_descriptor_is_not_reclassified(self):
        read_fd, write_fd = os.pipe()
        anchor = os.dup(read_fd)
        os.close(read_fd)
        try:
            with self.assertRaises(OSError) as raised:
                self.probe.compare(read_fd, anchor)
            self.assertEqual(raised.exception.errno, errno.EBADF)
        finally:
            os.close(anchor)
            os.close(write_fd)

    @unittest.skipUnless(hasattr(os, "pidfd_open"), "Python pidfd_open absent")
    def test_duplicate_self_pidfd_is_same_open_file_description(self):
        pidfd = os.pidfd_open(os.getpid(), 0)
        anchor = fcntl.fcntl(pidfd, fcntl.F_DUPFD_CLOEXEC, 3)
        try:
            result = self.probe.compare(pidfd, anchor)
            self.assertTrue(result["same"])
            self.assertEqual(result["raw_result"], 0)
        finally:
            os.close(anchor)
            os.close(pidfd)

    def test_out_of_range_and_nonexact_fd_values_are_refused(self):
        read_fd, write_fd = os.pipe()
        anchor = os.dup(read_fd)
        try:
            for value in (read_fd + 2 ** 32, read_fd + 2 ** 64,
                          True, 1.0):
                with self.subTest(value=value):
                    with self.assertRaises(BaseException):
                        self.probe.compare(value, anchor)
        finally:
            os.close(anchor)
            os.close(read_fd)
            os.close(write_fd)

    def test_syscall_binding_is_immutable(self):
        for name, value in (("syscall_number", 1), ("owner_pid", 1),
                            ("_syscall", object()), ("_libc", object())):
            with self.subTest(name=name):
                with self.assertRaises(BaseException):
                    setattr(self.probe, name, value)

    def test_non_lp64_abi_is_refused_before_libc_binding(self):
        with mock.patch.object(LAB.ctypes, "sizeof", return_value=4), \
                mock.patch.object(LAB.ctypes, "CDLL") as loader:
            with self.assertRaises(BaseException):
                LAB.LinuxOFDProbe()
        self.assertEqual(loader.call_count, 0)

    def test_non_linux_platform_is_refused_before_libc_binding(self):
        with mock.patch.object(LAB.platform, "system",
                               return_value="Darwin"), \
                mock.patch.object(LAB.ctypes, "CDLL") as loader:
            with self.assertRaises(BaseException):
                LAB.LinuxOFDProbe()
        self.assertEqual(loader.call_count, 0)

    def test_mutated_cfunc_errcheck_is_refused_without_callback(self):
        calls = []
        def rewrite(result, function, arguments):
            calls.append(result)
            return 0
        self.probe._syscall.errcheck = rewrite
        read_fd, write_fd = os.pipe()
        anchor = os.dup(read_fd)
        try:
            with self.assertRaises(BaseException):
                self.probe.compare(read_fd, anchor)
            self.assertEqual(calls, [])
        finally:
            del self.probe._syscall.errcheck
            os.close(anchor)
            os.close(read_fd)
            os.close(write_fd)

    def test_additional_native_task_is_refused(self):
        ready = threading.Event()
        finish = threading.Event()
        ended = threading.Event()
        def native_worker():
            ready.set()
            finish.wait()
            ended.set()
        _thread.start_new_thread(native_worker, ())
        self.assertTrue(ready.wait(2))
        read_fd, write_fd = os.pipe()
        anchor = os.dup(read_fd)
        try:
            with self.assertRaises(BaseException):
                self.probe.compare(read_fd, anchor)
        finally:
            finish.set()
            self.assertTrue(ended.wait(2))
            os.close(anchor)
            os.close(read_fd)
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
