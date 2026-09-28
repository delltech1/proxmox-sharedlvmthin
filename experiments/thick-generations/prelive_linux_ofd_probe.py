#!/usr/bin/python3
"""Disposable Linux KCMP_FILE probe; not wired to plugin resource cleanup."""

import ctypes
import os
import platform
import sys
import threading
import time


KCMP_FILE = 0
INT_MAX = (1 << 31) - 1
_SYS_KCMP = {"x86_64": 312, "aarch64": 272}


class Refusal(RuntimeError):
    pass


def require(value, message):
    if not value:
        raise Refusal(message)


class LinuxOFDProbe:
    """Compare two FDs in this exact single-threaded process via KCMP_FILE."""

    def __init__(self):
        system = platform.system()
        require(sys.platform.startswith("linux")
                and type(system) is str and system == "Linux",
                "KCMP probe requires Linux")
        machine = platform.machine()
        require(type(machine) is str and machine in _SYS_KCMP,
                "unsupported KCMP syscall architecture")
        abi = (ctypes.sizeof(ctypes.c_void_p),
               ctypes.sizeof(ctypes.c_long), ctypes.sizeof(ctypes.c_int))
        require(abi == (8, 8, 4), "KCMP probe requires Linux LP64 ABI")
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        syscall.argtypes = None
        object.__setattr__(self, "machine", machine)
        object.__setattr__(self, "syscall_number", _SYS_KCMP[machine])
        object.__setattr__(self, "owner_pid", os.getpid())
        object.__setattr__(self, "owner_thread", threading.current_thread())
        object.__setattr__(self, "_abi", abi)
        object.__setattr__(self, "_libc", libc)
        object.__setattr__(self, "_syscall", syscall)
        object.__setattr__(self, "_sealed", True)

    _PROTECTED = frozenset(("machine", "syscall_number", "owner_pid",
        "owner_thread", "_abi", "_libc", "_syscall", "_sealed"))

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False) and name in type(self)._PROTECTED:
            raise Refusal("KCMP syscall binding is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        if getattr(self, "_sealed", False) and name in type(self)._PROTECTED:
            raise Refusal("KCMP syscall binding cannot be deleted")
        object.__delattr__(self, name)

    def compare(self, first_fd, second_fd):
        require(threading.current_thread() is self.owner_thread
                and os.getpid() == self.owner_pid
                and threading.active_count() == 1
                and self._abi == (8, 8, 4)
                and ctypes.sizeof(ctypes.c_void_p) == 8
                and ctypes.sizeof(ctypes.c_long) == 8
                and ctypes.sizeof(ctypes.c_int) == 4
                and self._syscall is self._libc.syscall
                and self._syscall.restype is ctypes.c_long
                and self._syscall.argtypes is None
                and self._syscall.errcheck is None,
                "KCMP probe requires original single-threaded process")
        tasks = os.listdir("/proc/self/task")
        require(type(tasks) is list
                and len(tasks) == 1
                and all(type(task) is str and task.isdigit()
                        for task in tasks),
                "KCMP probe requires one native task")
        require(type(first_fd) is int and 0 <= first_fd <= INT_MAX
                and type(second_fd) is int and 0 <= second_fd <= INT_MAX
                and first_fd != second_fd,
                "strict distinct KCMP descriptor numbers")
        started = time.monotonic_ns()
        ctypes.set_errno(0)
        result = self._syscall(
            ctypes.c_long(self.syscall_number),
            ctypes.c_int(self.owner_pid), ctypes.c_int(self.owner_pid),
            ctypes.c_int(KCMP_FILE),
            ctypes.c_ulong(first_fd), ctypes.c_ulong(second_fd))
        saved_errno = ctypes.get_errno()
        finished = time.monotonic_ns()
        require(type(started) is int and type(finished) is int
                and finished >= started,
                "KCMP clock regressed")
        if result == -1:
            raise OSError(saved_errno, os.strerror(saved_errno))
        require(type(result) is int and result in (0, 1, 2, 3)
                and saved_errno == 0,
                "strict KCMP syscall result")
        return {"schema": 1, "classification": "LINUX_KCMP_FILE_RESULT",
                "same": result == 0, "raw_result": result,
                "returned_ns": finished, "owner_pid": self.owner_pid,
                "first_fd": first_fd, "second_fd": second_fd,
                "concurrency_scope":
                "SINGLE_NATIVE_TASK_OBSERVED_NO_SIGNAL_GUARANTEE",
                "runtime_authorized": False,
                "storage_authorized": False, "exec_proven": False}
