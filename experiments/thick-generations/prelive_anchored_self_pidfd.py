#!/usr/bin/python3
"""Disposable anchored self-pidfd close gate; never authorizes plugin work."""

import ctypes
import fcntl
import gc
import os
import platform
import posix
import signal
import sys
import threading
import time
import types
from types import MappingProxyType


KCMP_FILE = 0
INT_MAX = (1 << 31) - 1
_SYSCALLS = MappingProxyType({
    "x86_64": MappingProxyType(
        {"close": 3, "fcntl": 72, "kcmp": 312, "pidfd_open": 434}),
    "aarch64": MappingProxyType(
        {"close": 57, "fcntl": 25, "kcmp": 272, "pidfd_open": 434}),
})
F_DUPFD_CLOEXEC = 1030
F_GETFD = 1
FD_CLOEXEC = 1


class Refusal(RuntimeError):
    pass


def _require(value, message):
    if not value:
        raise Refusal(message)


class AnchoredCloseEvidence:
    """Opaque one-owner evidence retaining the live anchor after primary close."""

    __slots__ = ("_anchor_fd", "_owner_pid", "_owner_thread",
                 "_receipt", "_sealed")

    def __init__(self, anchor_fd, owner_pid, owner_thread, receipt):
        object.__setattr__(self, "_anchor_fd", anchor_fd)
        object.__setattr__(self, "_owner_pid", owner_pid)
        object.__setattr__(self, "_owner_thread", owner_thread)
        object.__setattr__(self, "_receipt", receipt)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("anchored close evidence is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("anchored close evidence cannot be deleted")

    def receipt(self):
        _require(type(self) is AnchoredCloseEvidence
                 and os.getpid() == self._owner_pid
                 and threading.current_thread() is self._owner_thread,
                 "foreign anchored evidence owner")
        return dict(self._receipt)

    def probe_current_anchor_signal0(self):
        """Diagnostic only; does not prove continuity or original identity."""
        _require(type(self) is AnchoredCloseEvidence
                 and os.getpid() == self._owner_pid
                 and threading.current_thread() is self._owner_thread,
                 "foreign anchored evidence owner")
        signal.pidfd_send_signal(self._anchor_fd, 0)
        return True


class AnchoredSelfPIDFDCloseGate:
    """Create, immediately anchor, compare and close one fresh self pidfd once."""

    __slots__ = ("_owner_pid", "_owner_thread", "_machine", "_syscall_nr",
                 "_libc", "_syscall", "_clock", "_sigmask", "_attempted", "_outcome",
                 "_gc_collect", "_gc_disable", "_gc_enable", "_gc_isenabled",
                 "_sealed")

    def __init__(self):
        _require(sys.platform.startswith("linux")
                 and platform.system() == "Linux", "Linux required")
        machine = platform.machine()
        _require(type(machine) is str and machine in _SYSCALLS,
                 "unsupported KCMP architecture")
        _require((ctypes.sizeof(ctypes.c_void_p), ctypes.sizeof(ctypes.c_long),
                  ctypes.sizeof(ctypes.c_int)) == (8, 8, 4),
                 "Linux LP64 ABI required")
        _require(hasattr(os, "pidfd_open")
                 and hasattr(signal, "pidfd_send_signal")
                 and hasattr(signal, "pthread_sigmask"),
                 "pidfd and signal-mask APIs required")
        libc = ctypes.CDLL(None, use_errno=True)
        syscall = libc.syscall
        syscall.restype = ctypes.c_long
        syscall.argtypes = None
        object.__setattr__(self, "_owner_pid", os.getpid())
        object.__setattr__(self, "_owner_thread", threading.current_thread())
        object.__setattr__(self, "_machine", machine)
        object.__setattr__(self, "_syscall_nr", _SYSCALLS[machine])
        object.__setattr__(self, "_libc", libc)
        object.__setattr__(self, "_syscall", syscall)
        _require(os.close is posix.close and os.pidfd_open is posix.pidfd_open
                 and type(posix.close) is types.BuiltinFunctionType
                 and posix.close.__module__ == "posix"
                 and posix.close.__name__ == "close"
                 and type(posix.pidfd_open) is types.BuiltinFunctionType
                 and posix.pidfd_open.__module__ == "posix"
                 and posix.pidfd_open.__name__ == "pidfd_open"
                 and type(fcntl.fcntl) is types.BuiltinFunctionType
                 and type(time.monotonic_ns) is types.BuiltinFunctionType
                 and all(type(fn) is types.BuiltinFunctionType
                         and fn.__module__ == "gc"
                         for fn in (gc.collect, gc.disable, gc.enable,
                                    gc.isenabled))
                 and gc.collect.__name__ == "collect"
                 and gc.disable.__name__ == "disable"
                 and gc.enable.__name__ == "enable"
                 and gc.isenabled.__name__ == "isenabled"
                 and type(signal.pthread_sigmask) is types.FunctionType
                 and signal.pthread_sigmask.__module__ == "signal"
                 and signal.pthread_sigmask.__name__ == "pthread_sigmask",
                 "original POSIX descriptor functions required")
        object.__setattr__(self, "_clock", time.monotonic_ns)
        object.__setattr__(self, "_sigmask", signal.pthread_sigmask)
        object.__setattr__(self, "_gc_collect", gc.collect)
        object.__setattr__(self, "_gc_disable", gc.disable)
        object.__setattr__(self, "_gc_enable", gc.enable)
        object.__setattr__(self, "_gc_isenabled", gc.isenabled)
        object.__setattr__(self, "_attempted", False)
        object.__setattr__(self, "_outcome", None)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name, value):
        if getattr(self, "_sealed", False):
            raise Refusal("anchored close gate is immutable")
        object.__setattr__(self, name, value)

    def __delattr__(self, name):
        raise Refusal("anchored close gate cannot be deleted")

    def _environment_ok(self):
        # Perform the final audit-generating operation first. State inspected
        # below must be current after an audit hook has returned.
        tasks = os.listdir("/proc/self/task")
        monitoring = getattr(sys, "monitoring", None)
        monitoring_quiet = True
        if monitoring is not None:
            monitoring_quiet = all(
                monitoring.get_tool(tool_id) is None
                and monitoring.get_events(tool_id) == 0
                for tool_id in range(6))
        return (type(self) is AnchoredSelfPIDFDCloseGate
                and os.getpid() == self._owner_pid
                and threading.current_thread() is self._owner_thread
                and threading.active_count() == 1
                and type(tasks) is list and len(tasks) == 1
                and all(type(task) is str and task.isdigit() for task in tasks)
                and sys.gettrace() is None and sys.getprofile() is None
                and monitoring_quiet
                and self._syscall is self._libc.syscall
                and self._syscall.restype is ctypes.c_long
                and self._syscall.argtypes is None
                and self._syscall.errcheck is None
                and os.pidfd_open is posix.pidfd_open
                and self._syscall_nr == _SYSCALLS[self._machine]
                and os.close is posix.close
                and self._clock is time.monotonic_ns
                and self._sigmask is signal.pthread_sigmask
                and self._gc_collect is gc.collect
                and self._gc_disable is gc.disable
                and self._gc_enable is gc.enable
                and self._gc_isenabled is gc.isenabled)

    def outcome(self):
        _require(type(self) is AnchoredSelfPIDFDCloseGate
                 and os.getpid() == self._owner_pid
                 and threading.current_thread() is self._owner_thread,
                 "foreign gate owner")
        return None if self._outcome is None else dict(self._outcome)

    def run_once(self, deadline_ns):
        _require(type(deadline_ns) is int and deadline_ns > 0,
                 "strict positive absolute deadline")
        _require(not self._attempted, "anchored close gate is one-shot")
        object.__setattr__(self, "_attempted", True)
        _require(self._environment_ok(), "uncontrolled execution environment")
        blockable = set(signal.valid_signals()) - {signal.SIGKILL, signal.SIGSTOP}
        gc_was_enabled = self._gc_isenabled()
        # Run arbitrary cyclic finalizers before any descriptor exists, then
        # suppress automatic cyclic-GC callbacks through acquisition/close.
        self._gc_collect()
        self._gc_disable()
        try:
            old_mask = self._sigmask(signal.SIG_BLOCK, blockable)
        except BaseException:
            if gc_was_enabled:
                self._gc_enable()
            raise
        primary = None
        anchor = None
        close_attempts = 0
        primary_close_confirmed = False
        same_raw = None
        same_errno = None
        finished_ns = None
        primary_acquired_fd = None
        primary_close_target = None
        failure = None
        mask_restore_error = None
        gc_restore_error = None
        try:
            _require(self._environment_ok(), "environment changed after masking")
            _require(self._gc_isenabled() is False,
                     "cyclic GC re-enabled before acquisition")
            effective_mask = self._sigmask(signal.SIG_BLOCK, set())
            _require(type(effective_mask) is set
                     and blockable.issubset(effective_mask),
                     "signals unmasked before acquisition")
            _require(self._clock() < deadline_ns, "deadline before acquisition")
            primary_raw = self._syscall(
                ctypes.c_long(self._syscall_nr["pidfd_open"]),
                ctypes.c_int(self._owner_pid), ctypes.c_uint(0))
            _require(type(primary_raw) is int
                     and 0 <= primary_raw <= INT_MAX,
                     "invalid primary pidfd")
            primary = primary_raw
            primary_acquired_fd = primary
            anchor_raw = self._syscall(
                ctypes.c_long(self._syscall_nr["fcntl"]),
                ctypes.c_int(primary), ctypes.c_int(F_DUPFD_CLOEXEC),
                ctypes.c_int(3))
            _require(type(anchor_raw) is int and 0 <= anchor_raw <= INT_MAX
                     and anchor_raw != primary, "invalid anchor pidfd")
            anchor = anchor_raw
            anchor_flags = self._syscall(
                ctypes.c_long(self._syscall_nr["fcntl"]),
                ctypes.c_int(anchor), ctypes.c_int(F_GETFD), ctypes.c_int(0))
            _require(type(anchor_flags) is int and anchor_flags >= 0
                     and anchor_flags & FD_CLOEXEC,
                     "anchor lacks CLOEXEC")
            # From acquisition through close, use only fixed raw syscalls and
            # fixed clocks: no Python audit-generating descriptor operation.
            _require(self._clock() < deadline_ns, "deadline before compare")

            # Fixed critical sequence: no callback, logging or caller-controlled
            # operation is reachable between KCMP and the one close attempt.
            same_raw = self._syscall(
                ctypes.c_long(self._syscall_nr["kcmp"]),
                ctypes.c_int(self._owner_pid),
                ctypes.c_int(self._owner_pid), ctypes.c_int(KCMP_FILE),
                ctypes.c_ulong(primary), ctypes.c_ulong(anchor))
            if same_raw != 0:
                same_errno = ctypes.get_errno() if same_raw == -1 else 0
                raise Refusal("primary/anchor same-OFD proof failed")
            same_errno = 0
            # This fixed clock is intentionally inside the no-caller-callback
            # critical section. Expiry here performs zero primary close.
            if self._clock() >= deadline_ns:
                raise Refusal("deadline after compare before close")
            close_attempts = 1
            close_target = primary
            primary_close_target = close_target
            primary = None  # revoke ownership before the syscall; never retry
            close_raw = self._syscall(
                ctypes.c_long(self._syscall_nr["close"]),
                ctypes.c_int(close_target))
            if close_raw != 0:
                raise OSError(ctypes.get_errno(), "ambiguous raw close result")
            primary_close_confirmed = True
            finished_ns = self._clock()
        except BaseException as error:
            failure = error
        finally:
            # Do not close descriptor numbers after acquisition. Any refusal
            # may mean identity/authority loss; retain them as quarantine
            # evidence until this disposable process exits. Never retry close.
            outcome = {
                "schema": 1,
                "classification": "DISPOSABLE_ANCHORED_CLOSE_ATTEMPT",
                "owner_pid": self._owner_pid,
                "anchor_fd": anchor,
                "primary_fd": primary,
                "primary_owned": primary is not None,
                "primary_acquired_fd": primary_acquired_fd,
                "primary_close_target": primary_close_target,
                "same_raw": None if same_raw is None else int(same_raw),
                "same_errno": same_errno,
                "primary_close_attempts": close_attempts,
                "primary_close_confirmed": primary_close_confirmed,
                "anchor_owned": anchor is not None,
                "finished_ns": finished_ns,
                "late": finished_ns is not None and finished_ns >= deadline_ns,
                "failure_type": None if failure is None else type(failure).__name__,
                "failure_text": None if failure is None else str(failure),
                "mask_restore_error": None,
                "gc_restore_error": None,
                "concurrency_scope":
                    "ONE_NATIVE_TASK_BLOCKABLE_SIGNALS_MASKED_NOT_ATOMIC",
                "runtime_authorized": False,
                "storage_authorized": False,
                "exec_proven": False,
            }
            object.__setattr__(self, "_outcome", outcome)
            try:
                self._sigmask(signal.SIG_SETMASK, old_mask)
            except BaseException as error:
                mask_restore_error = error
                outcome = dict(outcome)
                outcome["mask_restore_error"] = (
                    f"{type(error).__name__}: {error}")
                object.__setattr__(self, "_outcome", outcome)
            if gc_was_enabled:
                try:
                    self._gc_enable()
                except BaseException as error:
                    gc_restore_error = error
                    outcome = dict(self._outcome)
                    outcome["gc_restore_error"] = (
                        f"{type(error).__name__}: {error}")
                    object.__setattr__(self, "_outcome", outcome)
        if failure is not None:
            raise failure
        if mask_restore_error is not None:
            raise Refusal("signal mask restoration failed") from mask_restore_error
        if gc_restore_error is not None:
            raise Refusal("cyclic GC restoration failed") from gc_restore_error
        receipt = dict(self._outcome)
        receipt["classification"] = (
            "DISPOSABLE_ANCHORED_PRIMARY_CLOSE_CONFIRMED")
        evidence = AnchoredCloseEvidence(
            anchor, self._owner_pid, self._owner_thread, receipt)
        return evidence


def run_disposable(deadline_ns):
    """Convenience entry point for a fresh, controlled subprocess only."""
    gate = AnchoredSelfPIDFDCloseGate()
    evidence = gate.run_once(deadline_ns)
    receipt = evidence.receipt()
    receipt["current_anchor_signal0_usable"] = (
        evidence.probe_current_anchor_signal0())
    return receipt
