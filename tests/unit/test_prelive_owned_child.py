import copy
import importlib.util
from pathlib import Path
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/prelive_owned_child.py"
SPEC = importlib.util.spec_from_file_location("prelive_owned_child", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)

BOOT = "12345678-1234-1234-1234-123456789abc"
LAUNCHER = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD", "exe_dev": 20,
            "exe_inode": 30, "cmdline_sha256": "a" * 64}


def lifecycle():
    return LAB._record_owned_fork(
        LAB._FORK_KEY, 300,
        {"pid": 100, "starttime": 200, "boot_id": BOOT}, "b" * 32)


class Syscalls:
    def __init__(self):
        self.starts = [400, 400]
        self.launchers = [copy.deepcopy(LAUNCHER), copy.deepcopy(LAUNCHER)]
        self.waits = []
        self.opened = []
        self.closed = []
        self.wait_options = []
    def current_identity(self):
        return {"pid": 100, "starttime": 200, "boot_id": BOOT}
    def starttime(self, pid):
        self.assert_pid(pid); return self.starts.pop(0)
    def launcher(self, pid):
        self.assert_pid(pid); return self.launchers.pop(0)
    def pidfd_open(self, pid):
        self.assert_pid(pid); self.opened.append(pid); return 50
    def probe_waitable(self, pidfd):
        if pidfd != 50: raise AssertionError("foreign pidfd")
        return None
    def waitid(self, pidfd, options):
        if pidfd != 50: raise AssertionError("foreign pidfd")
        self.wait_options.append(options)
        return self.waits.pop(0)
    def close(self, fd):
        self.closed.append(fd)
    @staticmethod
    def assert_pid(pid):
        if pid != 300: raise AssertionError("foreign PID")


def wait(code, status, pid=300):
    return types.SimpleNamespace(si_pid=pid, si_code=code, si_status=status)


class OwnedChildTests(unittest.TestCase):
    def test_bind_observe_exit0_and_exact_reap(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        calls.waits = [wait(LAB.os.CLD_EXITED, 0), wait(LAB.os.CLD_EXITED, 0)]
        receipt = LAB.observe_exit_nonblocking(handle, calls)
        result = LAB.reap_observed_exit(handle, receipt, calls)
        self.assertEqual(result["classification"], "OWNED_LEADER_REAPED_ONLY")
        self.assertEqual(result["exit"], "EXITED_ZERO")
        self.assertTrue(all(value is False for key, value in result.items()
                            if key not in ("classification", "exit", "status")))
        closed = LAB.close_reaped_pidfd(handle, calls)
        self.assertEqual(closed["classification"],
                         "OWNED_LEADER_REAPED_PIDFD_CLOSED")
        self.assertEqual(calls.closed, [50])
        self.assertEqual(calls.wait_options,
                         [LAB.os.WEXITED | LAB.os.WNOHANG | LAB.os.WNOWAIT,
                          LAB.os.WEXITED | LAB.os.WNOHANG])

    def test_nonblocking_none_is_not_terminal_or_reaped(self):
        calls = Syscalls(); handle = LAB.bind_owned_pidfd(lifecycle(), LAUNCHER, calls)
        calls.waits = [None]
        self.assertIsNone(LAB.observe_exit_nonblocking(handle, calls))
        self.assertEqual(handle.state, "BOUND")

    def test_distinct_nonzero_and_signal_receipts(self):
        for result, classification in ((wait(LAB.os.CLD_EXITED, 7), "EXITED_NONZERO"),
                                       (wait(LAB.os.CLD_KILLED, 9), "SIGNALED")):
            calls = Syscalls(); handle = LAB.bind_owned_pidfd(lifecycle(), LAUNCHER, calls)
            calls.waits = [result]
            self.assertEqual(LAB.observe_exit_nonblocking(handle, calls).classification,
                             classification)
            self.assertEqual(handle.state, "TERMINAL_OBSERVED_NOT_REAPED")

    def test_identity_change_during_bind_is_unknown_and_closes_pidfd(self):
        calls = Syscalls(); calls.starts = [400, 401]; life = lifecycle()
        with self.assertRaises(LAB.Refusal):
            LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        self.assertEqual(life["state"], "UNKNOWN_CHILD_MAY_EXIST")
        self.assertEqual(calls.closed, [50])

    def test_foreign_nonchild_waitability_probe_is_unknown(self):
        calls = Syscalls(); life = lifecycle()
        calls.probe_waitable = lambda pidfd: (_ for _ in ()).throw(
            ChildProcessError(10, "ECHILD"))
        with self.assertRaises(ChildProcessError):
            LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        self.assertEqual(life["state"], "UNKNOWN_CHILD_MAY_EXIST")
        self.assertEqual(calls.closed, [50])

    def test_pidfd_open_failure_has_no_retry_or_fallback(self):
        calls = Syscalls(); life = lifecycle(); count = []
        def fail(pid): count.append(pid); raise OSError("pidfd unavailable")
        calls.pidfd_open = fail
        with self.assertRaises(OSError):
            LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        self.assertEqual(count, [300])
        self.assertEqual(life["owned_child"]["pid"], 300)
        self.assertEqual(life["state"], "UNKNOWN_CHILD_MAY_EXIST")

    def test_invalid_pidfd_return_never_closes_fd_zero(self):
        for value in (False, -1, 1.0):
            calls = Syscalls(); life = lifecycle(); calls.pidfd_open = lambda pid, v=value: v
            with self.assertRaises(LAB.Refusal):
                LAB.bind_owned_pidfd(life, LAUNCHER, calls)
            self.assertEqual(calls.closed, [])

    def test_foreign_supervisor_and_bad_types_refuse_before_pidfd(self):
        values = []
        bad = lifecycle(); bad["supervisor"]["boot_id"] = "87654321-1234-1234-1234-123456789abc"; values.append(bad)
        bad = lifecycle(); bad["owned_child"]["pid"] = True; values.append(bad)
        bad = lifecycle(); bad["supervisor"]["starttime"] = 200.0; values.append(bad)
        bad = lifecycle(); bad["token"] = "foreign"; values.append(bad)
        for value in values:
            calls = Syscalls()
            with self.assertRaises(LAB.Refusal):
                LAB.bind_owned_pidfd(value, LAUNCHER, calls)
            self.assertEqual(calls.opened, [])

    def test_copied_or_self_lifecycle_cannot_bind(self):
        original = lifecycle(); copied = copy.copy(original)
        calls = Syscalls(); LAB.bind_owned_pidfd(original, LAUNCHER, calls)
        with self.assertRaises(LAB.Refusal):
            LAB.bind_owned_pidfd(copied, LAUNCHER, Syscalls())
        self.assertEqual(copied["origin"].claimed, True)
        self_life = LAB._record_owned_fork(
            LAB._FORK_KEY, 100,
            {"pid": 100, "starttime": 200, "boot_id": BOOT}, "c" * 32)
        with self.assertRaisesRegex(LAB.Refusal, "cannot be supervisor"):
            LAB.bind_owned_pidfd(self_life, LAUNCHER, Syscalls())

    def test_deepcopied_lifecycle_shares_single_origin_claim(self):
        original = lifecycle(); copied = copy.deepcopy(original)
        LAB.bind_owned_pidfd(original, LAUNCHER, Syscalls())
        with self.assertRaisesRegex(LAB.Refusal, "already being bound"):
            LAB.bind_owned_pidfd(copied, LAUNCHER, Syscalls())

    def test_handle_is_noncopyable_to_prevent_fd_alias(self):
        handle = LAB.bind_owned_pidfd(lifecycle(), LAUNCHER, Syscalls())
        with self.assertRaisesRegex(LAB.Refusal, "noncopyable"):
            copy.copy(handle)
        with self.assertRaisesRegex(LAB.Refusal, "noncopyable"):
            copy.deepcopy(handle)

    def test_reap_requires_same_object_receipt_and_single_use(self):
        calls = Syscalls(); handle = LAB.bind_owned_pidfd(lifecycle(), LAUNCHER, calls)
        calls.waits = [wait(LAB.os.CLD_EXITED, 0)]
        receipt = LAB.observe_exit_nonblocking(handle, calls)
        forged = LAB.ExitReceipt(receipt._key, receipt.lifecycle_token, receipt.child_pid,
                                 receipt.child_starttime, receipt.pidfd, receipt.code,
                                 receipt.status, receipt.classification)
        with self.assertRaises(LAB.Refusal):
            LAB.reap_observed_exit(handle, forged, calls)
        calls.waits = [wait(LAB.os.CLD_EXITED, 0)]
        LAB.reap_observed_exit(handle, receipt, calls)
        with self.assertRaises(LAB.Refusal):
            LAB.reap_observed_exit(handle, receipt, calls)

    def test_changed_reap_result_and_echild_become_unknown(self):
        for second in (wait(LAB.os.CLD_EXITED, 1), ChildProcessError(errno := 10, "ECHILD")):
            calls = Syscalls(); life = lifecycle()
            handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
            calls.waits = [wait(LAB.os.CLD_EXITED, 0), second]
            receipt = LAB.observe_exit_nonblocking(handle, calls)
            if isinstance(second, BaseException):
                original = calls.waitid
                def raise_second(pidfd, options):
                    value = calls.waits.pop(0)
                    if isinstance(value, BaseException): raise value
                    return value
                calls.waitid = raise_second
            with self.assertRaises(BaseException):
                LAB.reap_observed_exit(handle, receipt, calls)
            self.assertEqual(life["state"], "UNKNOWN_REAP_OUTCOME")

    def test_close_live_pidfd_is_explicitly_unsettled(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        result = LAB.close_pidfd_without_settlement(handle, calls)
        self.assertEqual(result["classification"], "CLOSED_UNSETTLED_UNKNOWN")
        self.assertFalse(result["runtime_authorized"])
        self.assertEqual(life["state"], "CLOSED_UNSETTLED_UNKNOWN")
        with self.assertRaises(LAB.Refusal):
            LAB.close_pidfd_without_settlement(handle, calls)
        self.assertEqual(calls.closed, [50])

    def test_close_failure_is_single_use_and_never_settled(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        close_calls = []
        def fail(fd): close_calls.append(fd); raise OSError("close unknown")
        calls.close = fail
        with self.assertRaises(OSError):
            LAB.close_pidfd_without_settlement(handle, calls)
        self.assertEqual(close_calls, [50])
        self.assertEqual(life["state"], "PIDFD_CLOSE_UNKNOWN_UNSETTLED")
        with self.assertRaises(LAB.Refusal):
            LAB.close_pidfd_without_settlement(handle, calls)
        self.assertEqual(close_calls, [50])

    def test_observe_error_or_bad_receipt_is_terminal_unknown(self):
        for result in (ChildProcessError(10, "ECHILD"),
                       wait(LAB.os.CLD_STOPPED, 19),
                       wait(LAB.os.CLD_EXITED, 0, pid=999)):
            calls = Syscalls(); life = lifecycle()
            handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
            if isinstance(result, BaseException):
                calls.waitid = lambda pidfd, options, exc=result: (_ for _ in ()).throw(exc)
            else:
                calls.waits = [result]
            with self.assertRaises(BaseException):
                LAB.observe_exit_nonblocking(handle, calls)
            self.assertEqual(life["state"], "UNKNOWN_OBSERVE_OUTCOME")
            with self.assertRaises(LAB.Refusal):
                LAB.observe_exit_nonblocking(handle, calls)

    def test_owner_identity_exception_poison_is_not_retryable(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        count = []
        original = calls.current_identity
        def fail_once():
            count.append(True)
            if len(count) == 1: raise OSError("identity unavailable")
            return original()
        calls.current_identity = fail_once
        with self.assertRaises(OSError):
            LAB.observe_exit_nonblocking(handle, calls)
        self.assertEqual(life["state"], "UNKNOWN_OWNER_CHANGED")
        with self.assertRaises(LAB.Refusal):
            LAB.observe_exit_nonblocking(handle, calls)
        self.assertEqual(len(count), 1)

    def test_reentrant_unsettled_close_cannot_overwrite_unknown(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        close_calls = []
        def reentrant_close(fd):
            close_calls.append(fd)
            with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                LAB.close_pidfd_without_settlement(handle, calls)
        calls.close = reentrant_close
        with self.assertRaisesRegex(LAB.Refusal, "state changed"):
            LAB.close_pidfd_without_settlement(handle, calls)
        self.assertEqual(close_calls, [50])
        self.assertEqual(life["state"], "UNKNOWN_REENTRANT_OPERATION")

    def test_reentrant_terminal_close_cannot_overwrite_unknown(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        calls.waits = [wait(LAB.os.CLD_EXITED, 0), wait(LAB.os.CLD_EXITED, 0)]
        receipt = LAB.observe_exit_nonblocking(handle, calls)
        LAB.reap_observed_exit(handle, receipt, calls)
        close_calls = []
        def reentrant_close(fd):
            close_calls.append(fd)
            with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                LAB.close_reaped_pidfd(handle, calls)
        calls.close = reentrant_close
        with self.assertRaisesRegex(LAB.Refusal, "state changed"):
            LAB.close_reaped_pidfd(handle, calls)
        self.assertEqual(close_calls, [50])
        self.assertEqual(life["state"], "UNKNOWN_REENTRANT_OPERATION")

    def test_second_snapshot_requires_exact_types(self):
        for starts, launchers in (([400, 400.0], None),
                                  (None, [copy.deepcopy(LAUNCHER),
                                          {**LAUNCHER, "exe_inode": 30.0}])):
            calls = Syscalls(); life = lifecycle()
            if starts is not None: calls.starts = starts
            if launchers is not None: calls.launchers = launchers
            with self.assertRaises(LAB.Refusal):
                LAB.bind_owned_pidfd(life, LAUNCHER, calls)
            self.assertEqual(life["state"], "UNKNOWN_CHILD_MAY_EXIST")
            self.assertEqual(calls.closed, [50])

    def test_reentrant_observe_poison_has_one_waitid_call(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        count = []
        def reentrant(pidfd, options):
            count.append((pidfd, options))
            with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                LAB.observe_exit_nonblocking(handle, calls)
            return wait(LAB.os.CLD_EXITED, 0)
        calls.waitid = reentrant
        with self.assertRaises(LAB.Refusal):
            LAB.observe_exit_nonblocking(handle, calls)
        self.assertEqual(len(count), 1)
        self.assertEqual(life["state"], "UNKNOWN_REENTRANT_OPERATION")

    def test_reentrant_bind_consumes_origin_without_second_pidfd(self):
        calls = Syscalls(); life = lifecycle(); original = calls.starttime
        fired = []
        def reentrant_start(pid):
            if not fired:
                fired.append(True)
                with self.assertRaisesRegex(LAB.Refusal, "already being bound"):
                    LAB.bind_owned_pidfd(life, LAUNCHER, calls)
            return original(pid)
        calls.starttime = reentrant_start
        with self.assertRaises(LAB.Refusal):
            LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        self.assertEqual(calls.opened, [300])
        self.assertEqual(life["state"], "UNKNOWN_CHILD_MAY_EXIST")

    def test_reentrant_reap_cannot_be_overwritten_by_outer_success(self):
        calls = Syscalls(); life = lifecycle()
        handle = LAB.bind_owned_pidfd(life, LAUNCHER, calls)
        calls.waits = [wait(LAB.os.CLD_EXITED, 0)]
        receipt = LAB.observe_exit_nonblocking(handle, calls)
        count = []
        def reentrant_wait(pidfd, options):
            count.append((pidfd, options))
            with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                LAB.reap_observed_exit(handle, receipt, calls)
            return wait(LAB.os.CLD_EXITED, 0)
        calls.waitid = reentrant_wait
        with self.assertRaises(LAB.Refusal):
            LAB.reap_observed_exit(handle, receipt, calls)
        self.assertEqual(len(count), 1)
        self.assertEqual(life["state"], "UNKNOWN_REAP_OUTCOME")


if __name__ == "__main__":
    unittest.main()
