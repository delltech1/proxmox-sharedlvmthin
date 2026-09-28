import copy
import errno
import importlib.util
import os
from pathlib import Path
import stat
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/prelive_capture_settlement.py"
SPEC = importlib.util.spec_from_file_location("prelive_capture_settlement", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)

OWNER = {"pid": 100, "starttime": 200,
         "boot_id": "12345678-1234-1234-1234-123456789abc"}
CHILD = {"lifecycle_token": "a" * 32, "pid": 300, "starttime": 400,
         "boot_id": OWNER["boot_id"]}


def identity(fd, inode):
    return {"fd": fd, "dev": 20, "inode": inode,
            "mode": stat.S_IFIFO | 0o600, "flags": os.O_RDONLY | os.O_NONBLOCK}


class Syscalls:
    def __init__(self):
        self.identities = {10: identity(10, 30), 11: identity(11, 31)}
        self.reads = {10: [], 11: []}
        self.registered = []
        self.unregistered = []
        self.closed = []
        self.poller = object()
        self.times = []
        self.polls = []
        self.wait_all = []
    def owner_identity(self): return copy.deepcopy(OWNER)
    def fd_identity(self, fd): return copy.deepcopy(self.identities[fd])
    def epoll_create(self): return self.poller
    def epoll_register(self, poller, fd, mask): self.registered.append((poller, fd, mask))
    def epoll_unregister(self, poller, fd): self.unregistered.append((poller, fd))
    def epoll_close(self, poller): self.closed.append(poller)
    def read(self, fd, maximum):
        value = self.reads[fd].pop(0)
        if isinstance(value, BaseException): raise value
        return value
    def monotonic_ns(self):
        value = self.times.pop(0)
        if isinstance(value, BaseException): raise value
        return value
    def epoll_poll(self, poller, timeout_ms):
        if poller is not self.poller: raise AssertionError("foreign poller")
        value = self.polls.pop(0)
        if isinstance(value, BaseException): raise value
        return value
    def wait_all_nohang_wnowait(self):
        value = self.wait_all.pop(0)
        if isinstance(value, BaseException): raise value
        return value


class ChildAdapter:
    def __init__(self, observations=()):
        self.observations = list(observations)
        self.observe_calls = 0
        self.normalize_calls = []
        self.reap_calls = []
        self.owned = True
    def identity(self): return copy.deepcopy(CHILD)
    def pidfd_owned(self): return self.owned
    def observe(self):
        self.observe_calls += 1
        return self.observations.pop(0) if self.observations else None
    def normalize(self, opaque):
        self.normalize_calls.append(opaque)
        return opaque["terminal"]
    def reap_once(self, opaque):
        self.reap_calls.append(opaque)
        terminal = opaque["terminal"]
        return {"classification": "OWNED_LEADER_REAPED_ONLY",
                "lifecycle_token": CHILD["lifecycle_token"],
                "child_pid": CHILD["pid"], "child_starttime": CHILD["starttime"],
                "exit": terminal["classification"], "status": terminal["status"],
                "runtime_authorized": False, "storage_authorized": False,
                "postcondition_verified": False, "descendants_qualified": False,
                "capture_qualified": False}


def terminal(status=0):
    return {"classification": "EXITED_ZERO" if status == 0 else "EXITED_NONZERO",
            "lifecycle_token": CHILD["lifecycle_token"],
            "child_pid": CHILD["pid"], "child_starttime": CHILD["starttime"],
            "code": os.CLD_EXITED, "status": status}


def timed_out():
    handle, calls, unused = bound()
    calls.times = [0, 100]
    result = LAB.capture_until_deadline(handle, 100, lambda: None, calls)
    assert result["classification"] == "EXECUTION_TIMEOUT_UNSETTLED"
    return handle, calls


def bound(limit=8, calls=None):
    calls = calls or Syscalls()
    origin = LAB._record_capture_pipes(
        LAB._PIPE_KEY, OWNER, CHILD, identity(10, 30), identity(11, 31))
    return LAB.bind_capture_pipes(origin, limit, calls), calls, origin


class CaptureCoreTests(unittest.TestCase):
    def test_bind_exact_distinct_nonblocking_read_pipes(self):
        handle, calls, unused = bound()
        self.assertEqual([item[1] for item in calls.registered], [10, 11])
        self.assertEqual(handle.state, "BOUND")

    def test_bad_identity_alias_blocking_or_write_fd_refuses(self):
        values = []
        bad = identity(11, 30); values.append((identity(10, 30), bad))
        bad = identity(11, 31); bad["flags"] = os.O_RDONLY; values.append((identity(10, 30), bad))
        bad = identity(11, 31); bad["flags"] = os.O_WRONLY | os.O_NONBLOCK; values.append((identity(10, 30), bad))
        for stdout, stderr in values:
            with self.assertRaises(LAB.Refusal):
                LAB._record_capture_pipes(LAB._PIPE_KEY, OWNER, CHILD, stdout, stderr)

    def test_partial_epoll_setup_closes_only_internal_poller(self):
        calls = Syscalls(); count = []
        def register(poller, fd, mask):
            count.append(fd)
            if len(count) == 2: raise OSError("register failed")
            calls.registered.append((poller, fd, mask))
        calls.epoll_register = register
        origin = LAB._record_capture_pipes(
            LAB._PIPE_KEY, OWNER, CHILD, identity(10, 30), identity(11, 31))
        with self.assertRaises(OSError):
            LAB.bind_capture_pipes(origin, 8, calls)
        self.assertEqual(calls.unregistered, [(calls.poller, 10)])
        self.assertEqual(calls.closed, [calls.poller])

    def test_hup_with_data_requires_later_empty_read_for_eof(self):
        handle, calls, unused = bound()
        calls.reads[10] = [b"abc", BlockingIOError(errno.EAGAIN, "again")]
        result = LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLHUP)], calls)
        self.assertFalse(result["stdout"]["eof"])
        calls.reads[10] = [b""]
        result = LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLHUP)], calls)
        self.assertTrue(result["stdout"]["eof"])

    def test_eagain_is_not_eof(self):
        handle, calls, unused = bound()
        calls.reads[10] = [BlockingIOError(errno.EAGAIN, "again")]
        result = LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)
        self.assertFalse(result["stdout"]["eof"])
        self.assertEqual(result["stdout"]["observed_bytes"], 0)

    def test_exact_limit_and_overflow_digest_scope(self):
        handle, calls, unused = bound(limit=3)
        calls.reads[10] = [b"abc", b""]
        exact = LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)["stdout"]
        self.assertFalse(exact["truncated"]); self.assertEqual(exact["digest_scope"], "FULL_CAPTURE")
        handle, calls, unused = bound(limit=3)
        calls.reads[10] = [b"abcd", b""]
        over = LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)["stdout"]
        self.assertTrue(over["truncated"]); self.assertEqual(over["stored_bytes"], 3)
        self.assertEqual(over["observed_bytes"], 4)
        self.assertEqual(over["digest_scope"], "CAPTURED_PREFIX")

    def test_per_stream_turn_is_bounded_and_second_stream_is_served(self):
        handle, calls, unused = bound(limit=1000000)
        calls.reads[10] = [b"a"] * 10
        calls.reads[11] = [b"b", BlockingIOError(errno.EAGAIN, "again")]
        result = LAB.drain_ready_turn(
            handle, [(10, LAB.select.EPOLLIN), (11, LAB.select.EPOLLIN)], calls)
        self.assertEqual(result["stdout"]["observed_bytes"], 4)
        self.assertEqual(result["stderr"]["observed_bytes"], 1)

    def test_foreign_duplicate_unknown_event_and_read_error_poison(self):
        cases = ([(12, LAB.select.EPOLLIN)],
                 [(10, LAB.select.EPOLLIN), (10, LAB.select.EPOLLHUP)],
                 [(10, 1 << 30)], [(10, LAB.select.EPOLLERR)],
                 [(10, LAB.select.EPOLLIN | LAB.select.EPOLLERR)],
                 [(10, LAB.select.EPOLLHUP | LAB.select.EPOLLERR)])
        for events in cases:
            handle, calls, unused = bound()
            with self.assertRaises(LAB.Refusal):
                LAB.drain_ready_turn(handle, events, calls)
            self.assertEqual(handle.state, "UNKNOWN_DRAIN")
        handle, calls, unused = bound(); calls.reads[10] = [OSError("read failed")]
        with self.assertRaises(OSError):
            LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)
        self.assertEqual(handle.state, "UNKNOWN_DRAIN")

    def test_origin_single_claim_handle_noncopyable_and_epoll_only_close(self):
        handle, calls, origin = bound()
        with self.assertRaises(LAB.Refusal): LAB.bind_capture_pipes(origin, 8, calls)
        with self.assertRaises(LAB.Refusal): copy.copy(handle)
        with self.assertRaises(LAB.Refusal): copy.deepcopy(handle)
        LAB.close_capture_epoll(handle, calls)
        self.assertEqual(calls.closed, [calls.poller])
        self.assertEqual(calls.unregistered, [])

    def test_owner_exact_types_refuse_before_epoll(self):
        for field, value in (("pid", True), ("starttime", 200.0),
                             ("boot_id", "x" * 36)):
            owner = copy.deepcopy(OWNER); owner[field] = value
            with self.assertRaises(LAB.Refusal):
                LAB._record_capture_pipes(
                    LAB._PIPE_KEY, owner, CHILD, identity(10, 30), identity(11, 31))

    def test_fd_recycle_before_or_during_read_poison_never_returns_receipt(self):
        handle, calls, unused = bound(); calls.identities[10]["inode"] = 999
        with self.assertRaisesRegex(LAB.Refusal, "identity changed"):
            LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)
        self.assertEqual(handle.state, "UNKNOWN_DRAIN")
        handle, calls, unused = bound(); calls.identities[10]["flags"] = os.O_RDONLY
        with self.assertRaises(LAB.Refusal):
            LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)
        self.assertEqual(handle.state, "UNKNOWN_DRAIN")
        handle, calls, unused = bound(); calls.reads[10] = [b"foreign"]
        original = calls.read
        def recycle(fd, maximum):
            value = original(fd, maximum)
            calls.identities[fd]["inode"] = 999
            return value
        calls.read = recycle
        with self.assertRaises(LAB.Refusal):
            LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)
        self.assertEqual(handle.state, "UNKNOWN_DRAIN")

    def test_reentrant_drain_cannot_be_overwritten_by_outer_success(self):
        handle, calls, unused = bound(); fired = []
        calls.reads[10] = [b"abc", b""]
        original = calls.read
        def reentrant(fd, maximum):
            if not fired:
                fired.append(True)
                with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                    LAB.drain_ready_turn(handle, [], calls)
            return original(fd, maximum)
        calls.read = reentrant
        with self.assertRaises(LAB.Refusal):
            LAB.drain_ready_turn(handle, [(10, LAB.select.EPOLLIN)], calls)
        self.assertEqual(handle.state, "UNKNOWN_DRAIN")

    def test_reentrant_close_cannot_be_overwritten_by_outer_success(self):
        handle, calls, unused = bound(); close_calls = []
        def reentrant(poller):
            close_calls.append(poller)
            with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                LAB.close_capture_epoll(handle, calls)
        calls.epoll_close = reentrant
        with self.assertRaises(LAB.Refusal):
            LAB.close_capture_epoll(handle, calls)
        self.assertEqual(close_calls, [calls.poller])
        self.assertEqual(handle.state, "UNKNOWN_REENTRANT_OPERATION")

    def test_owner_lookup_failure_consumes_origin_and_owner_error_poisons_handle(self):
        calls = Syscalls(); origin = LAB._record_capture_pipes(
            LAB._PIPE_KEY, OWNER, CHILD, identity(10, 30), identity(11, 31))
        calls.owner_identity = lambda: (_ for _ in ()).throw(OSError("owner lookup"))
        with self.assertRaises(OSError):
            LAB.bind_capture_pipes(origin, 8, calls)
        with self.assertRaises(LAB.Refusal):
            LAB.bind_capture_pipes(origin, 8, Syscalls())
        handle, calls, unused = bound(); count = []
        original = calls.owner_identity
        def fail_once():
            count.append(True)
            if len(count) == 1: raise OSError("owner lookup")
            return original()
        calls.owner_identity = fail_once
        with self.assertRaises(OSError):
            LAB.drain_ready_turn(handle, [], calls)
        with self.assertRaises(LAB.Refusal):
            LAB.drain_ready_turn(handle, [], calls)
        self.assertEqual(len(count), 1)

    def test_deadline_leader_exit_before_eof_then_success_ceiling(self):
        handle, calls, unused = bound(); calls.times = [0, 0, 1, 2]
        calls.polls = [[(10, LAB.select.EPOLLIN)], [(11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        terminal = {"classification": "EXITED_ZERO", "lifecycle_token": "a" * 32,
                    "child_pid": 300, "child_starttime": 400,
                    "code": 1, "status": 0}
        seen = []
        def observer(): seen.append(True); return terminal
        result = LAB.capture_until_deadline(handle, 100, observer, calls)
        self.assertEqual(result["classification"],
                         "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED")
        self.assertFalse(result["leader_reaped"])
        self.assertEqual(len(seen), 1)

    def test_both_eof_before_leader_exit_remain_independent(self):
        handle, calls, unused = bound(); calls.times = [0, 0, 1, 1]
        calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        answers = iter((None, {"classification": "EXITED_NONZERO",
            "lifecycle_token": "a" * 32, "child_pid": 300,
            "child_starttime": 400, "code": 1, "status": 7}))
        result = LAB.capture_until_deadline(handle, 100, lambda: next(answers), calls)
        self.assertEqual(result["leader_observation"]["classification"],
                         "EXITED_NONZERO")

    def test_held_pipe_after_leader_exit_times_out_unsettled(self):
        handle, calls, unused = bound(); calls.times = [0, 0, 100]
        calls.polls = [[]]
        terminal = {"classification": "EXITED_ZERO", "lifecycle_token": "a" * 32,
                    "child_pid": 300, "child_starttime": 400,
                    "code": 1, "status": 0}
        result = LAB.capture_until_deadline(handle, 100, lambda: terminal, calls)
        self.assertEqual(result["classification"], "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertFalse(result["streams"]["stdout"]["eof"])

    def test_flood_is_bounded_and_deadline_still_wins(self):
        handle, calls, unused = bound(limit=1000); calls.times = [0, 0, 50, 50, 100]
        calls.polls = [[(10, LAB.select.EPOLLIN)], [(10, LAB.select.EPOLLIN)]]
        calls.reads[10] = [b"a"] * 8
        result = LAB.capture_until_deadline(handle, 100, lambda: None, calls)
        self.assertEqual(result["classification"], "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertEqual(result["streams"]["stdout"]["observed_bytes"], 8)

    def test_truncation_fails_before_later_eof_can_claim_full(self):
        handle, calls, unused = bound(limit=3); calls.times = [0, 0, 1]
        calls.polls = [[(10, LAB.select.EPOLLIN)]]; calls.reads[10] = [b"abcd", b""]
        result = LAB.capture_until_deadline(handle, 100, lambda: None, calls)
        self.assertEqual(result["classification"], "CAPTURE_TRUNCATED_UNSETTLED")
        self.assertEqual(result["streams"]["stdout"]["digest_scope"], "CAPTURED_PREFIX")

    def test_clock_regression_stall_and_bad_terminal_receipt_poison(self):
        for times, observer in (([10, 9], lambda: None),
                                ([1] * 10, lambda: None),
                                ([0], lambda: {"classification": "EXITED_ZERO"})):
            handle, calls, unused = bound(); calls.times = times
            calls.polls = [[]] * 10
            with self.assertRaises(LAB.Refusal):
                LAB.capture_until_deadline(handle, 100, observer, calls)
            self.assertEqual(handle.state, "UNKNOWN_MONITOR")

    def test_eintr_consumes_absolute_budget_without_reset(self):
        handle, calls, unused = bound(); calls.times = [0, 0, 50, 50, 100]
        calls.polls = [InterruptedError(), InterruptedError()]
        result = LAB.capture_until_deadline(handle, 100, lambda: None, calls)
        self.assertEqual(result["classification"], "EXECUTION_TIMEOUT_UNSETTLED")

    def test_terminal_receipt_must_match_bound_child_and_exit_semantics(self):
        base = {"classification": "EXITED_ZERO", "lifecycle_token": "a" * 32,
                "child_pid": 300, "child_starttime": 400,
                "code": LAB.os.CLD_EXITED, "status": 0}
        mutations = ({**base, "child_pid": 301},
                     {**base, "lifecycle_token": "b" * 32},
                     {**base, "classification": "EXITED_NONZERO"},
                     {**base, "status": 1},
                     {**base, "code": LAB.os.CLD_KILLED})
        for value in mutations:
            handle, calls, unused = bound(); calls.times = [0]
            with self.assertRaises(LAB.Refusal):
                LAB.capture_until_deadline(handle, 100, lambda v=value: v, calls)
            self.assertEqual(handle.state, "UNKNOWN_MONITOR")

    def test_reentrant_observer_cannot_return_false_success(self):
        handle, calls, unused = bound(); calls.times = [0]
        handle.streams["stdout"].eof = True; handle.streams["stderr"].eof = True
        terminal = {"classification": "EXITED_ZERO", "lifecycle_token": "a" * 32,
                    "child_pid": 300, "child_starttime": 400,
                    "code": LAB.os.CLD_EXITED, "status": 0}
        def observer():
            with self.assertRaisesRegex(LAB.Refusal, "monitor owns"):
                LAB.drain_ready_turn(handle, [], calls)
            return terminal
        with self.assertRaises(LAB.Refusal):
            LAB.capture_until_deadline(handle, 100, observer, calls)
        self.assertEqual(handle.state, "UNKNOWN_REENTRANT_OPERATION")

    def test_completion_at_or_after_deadline_is_timeout(self):
        terminal = {"classification": "EXITED_ZERO", "lifecycle_token": "a" * 32,
                    "child_pid": 300, "child_starttime": 400,
                    "code": LAB.os.CLD_EXITED, "status": 0}
        handle, calls, unused = bound(); calls.times = [0, 0, 100]
        calls.polls = [[(10, LAB.select.EPOLLIN), (11, LAB.select.EPOLLIN)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        result = LAB.capture_until_deadline(handle, 100, lambda: terminal, calls)
        self.assertEqual(result["classification"], "EXECUTION_TIMEOUT_UNSETTLED")
        handle, calls, unused = bound(); calls.times = [0, 100]
        result = LAB.capture_until_deadline(handle, 100, lambda: terminal, calls)
        self.assertEqual(result["classification"], "EXECUTION_TIMEOUT_UNSETTLED")

    def test_nested_monitor_clock_reentrancy_and_close_are_poisoned(self):
        handle, calls, unused = bound(); calls.times = [0]
        def nested_observer():
            with self.assertRaisesRegex(LAB.Refusal, "reentrant capture monitor"):
                LAB.capture_until_deadline(handle, 100, lambda: None, calls)
            return None
        with self.assertRaises(LAB.Refusal):
            LAB.capture_until_deadline(handle, 100, nested_observer, calls)
        self.assertEqual(handle.state, "UNKNOWN_REENTRANT_OPERATION")
        handle, calls, unused = bound(); fired = []
        def clock_reentrant():
            if not fired:
                fired.append(True)
                with self.assertRaisesRegex(LAB.Refusal, "monitor owns"):
                    LAB.drain_ready_turn(handle, [], calls)
            return 0
        calls.monotonic_ns = clock_reentrant
        with self.assertRaises(LAB.Refusal):
            LAB.capture_until_deadline(handle, 100, lambda: None, calls)
        handle, calls, unused = bound(); calls.times = [0]
        def close_observer():
            with self.assertRaisesRegex(LAB.Refusal, "monitor owns"):
                LAB.close_capture_epoll(handle, calls)
            return None
        with self.assertRaises(LAB.Refusal):
            LAB.capture_until_deadline(handle, 100, close_observer, calls)
        self.assertEqual(calls.closed, [])


class PassiveSettlementTests(unittest.TestCase):
    def test_late_exit_and_eof_reaps_exactly_once_but_preserves_primary_failure(self):
        handle, calls = timed_out()
        opaque = {"terminal": terminal()}
        child = ChildAdapter([opaque])
        calls.times = [101, 102, 103, 104, 105]
        calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settled = LAB.settle_passively(handle, child, 1000, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "LEADER_REAPED_STREAMS_EOF")
        self.assertEqual(settled.receipt["primary_failure"]["classification"],
                         "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertEqual(child.reap_calls, [opaque])
        self.assertFalse(settled.receipt["runtime_authorized"])
        self.assertTrue(settled.receipt["remaining_resources"]["stdout_read_fd_owned"])
        self.assertTrue(settled.receipt["remaining_resources"]["stderr_read_fd_owned"])
        self.assertEqual(calls.closed, [])

    def test_reaped_leader_with_held_pipe_stays_incomplete_at_deadline(self):
        handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [101, 102, 200]
        calls.polls = [[]]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "CLEANUP_DEADLINE_UNSETTLED")
        self.assertTrue(settled.receipt["remaining_resources"]["stdout_read_fd_owned"])
        self.assertTrue(settled.leader_reaped)

    def test_live_leader_with_eof_never_reaps_and_deadline_wins(self):
        handle, calls = timed_out(); child = ChildAdapter([None])
        calls.times = [101, 200]
        calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "CLEANUP_DEADLINE_UNSETTLED")
        self.assertEqual(child.reap_calls, [])
        self.assertTrue(settled.receipt["remaining_resources"]["leader_may_survive"])

    def test_settlement_is_single_use_and_unknown_child_api_is_not_retried(self):
        handle, calls = timed_out(); child = ChildAdapter()
        child.observe = lambda: (_ for _ in ()).throw(OSError("unknown"))
        calls.times = [101]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"], "UNKNOWN_CHILD_API")
        with self.assertRaises(LAB.Refusal):
            LAB.settle_passively(handle, child, 300, calls)

    def test_malformed_reap_is_unknown_single_attempt_and_cannot_enable_probe(self):
        handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
        attempts = []
        def malformed(opaque):
            attempts.append(opaque)
            return {"classification": "AMBIGUOUS"}
        child.reap_once = malformed
        calls.times = [101, 102]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"], "UNKNOWN_CHILD_API")
        self.assertEqual(len(attempts), 1)
        self.assertFalse(settled.leader_reaped)
        with self.assertRaises(LAB.Refusal):
            LAB.probe_children_after_reap(settled, calls)

    def test_primary_failure_is_mutation_isolated(self):
        handle, calls, unused = bound(); calls.times = [0, 100]
        primary = LAB.capture_until_deadline(handle, 100, lambda: None, calls)
        primary["classification"] = "CALLER_MUTATED"
        primary["streams"]["stdout"]["eof"] = True
        self.assertEqual(handle.primary_failure["classification"],
                         "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertFalse(handle.primary_failure["streams"]["stdout"]["eof"])
        child = ChildAdapter(); calls.times = [101, 200]; calls.polls = [[]]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(settled.receipt["primary_failure"]["classification"],
                         "EXECUTION_TIMEOUT_UNSETTLED")

    def test_cleanup_deadline_before_observation_dispatches_no_observe(self):
        handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [200]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(child.observe_calls, 0)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "CLEANUP_DEADLINE_UNSETTLED")

    def test_completion_exactly_at_deadline_is_not_affirmative(self):
        handle, calls = timed_out()
        handle.streams["stdout"].eof = True
        handle.streams["stderr"].eof = True
        child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [101, 102, 200, 200]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "CLEANUP_DEADLINE_UNSETTLED")
        self.assertEqual(len(child.reap_calls), 1)

    def test_primary_terminal_observation_is_not_observed_or_reaped_again(self):
        handle, calls, unused = bound()
        calls.times = [0, 0, 100]
        calls.polls = [[]]
        primary = LAB.capture_until_deadline(handle, 100, lambda: terminal(), calls)
        self.assertIsNotNone(primary["leader_observation"])
        child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [101, 200]
        calls.polls = [[]]
        settled = LAB.settle_passively(handle, child, 200, calls)
        self.assertEqual(child.observe_calls, 0)
        self.assertEqual(child.reap_calls, [])
        self.assertTrue(settled.receipt["remaining_resources"]["leader_may_survive"])

    def test_post_reap_probe_classifies_none_echild_and_unexpected_child(self):
        class WaitReceipt:
            si_pid = 999
            si_code = os.CLD_EXITED
            si_status = 0
        expected = ((None, "CHILDREN_PRESENT_NONE_WAITABLE"),
                    (ChildProcessError(errno.ECHILD, "none"),
                     "ECHILD_OBSERVED_AFTER_EXACT_REAP"),
                    (WaitReceipt(), "UNEXPECTED_WAITABLE_CHILD"))
        for answer, classification in expected:
            handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
            calls.times = [101, 102, 103, 104, 105, 106, 107]
            calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
            calls.reads[10] = [b""]; calls.reads[11] = [b""]
            settled = LAB.settle_passively(handle, child, 1000, calls)
            calls.wait_all = [answer]
            probe = LAB.probe_children_after_reap(settled, calls)
            self.assertEqual(probe["classification"], classification)
            self.assertFalse(probe["descendants_qualified"])
            with self.assertRaises(LAB.Refusal):
                LAB.probe_children_after_reap(settled, calls)

    def test_post_reap_probe_crossing_deadline_never_returns_child_claim(self):
        handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [101, 102, 103, 104]
        calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settled = LAB.settle_passively(handle, child, 110, calls)
        calls.times = [105, 110]
        calls.wait_all = [None]
        probe = LAB.probe_children_after_reap(settled, calls)
        self.assertEqual(probe["classification"], "UNKNOWN_DEADLINE_CROSSED")

    def test_post_reap_echild_at_deadline_is_unknown(self):
        handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [101, 102, 103, 104]
        calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settled = LAB.settle_passively(handle, child, 110, calls)
        calls.times = [105, 110]
        calls.wait_all = [ChildProcessError(errno.ECHILD, "none")]
        probe = LAB.probe_children_after_reap(settled, calls)
        self.assertEqual(probe["classification"], "UNKNOWN")

    def test_echild_outside_wait_all_never_becomes_affirmative_echild_evidence(self):
        callbacks = ("owner", "initial_clock", "finishing_clock")
        for callback in callbacks:
            handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
            calls.times = [101, 102, 103, 104]
            calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
            calls.reads[10] = [b""]; calls.reads[11] = [b""]
            settled = LAB.settle_passively(handle, child, 110, calls)
            failure = ChildProcessError(errno.ECHILD, callback)
            calls.wait_all = [None]
            if callback == "owner":
                calls.owner_identity = lambda: (_ for _ in ()).throw(failure)
                calls.times = []
            elif callback == "initial_clock":
                calls.times = [failure]
            else:
                calls.times = [105, failure]
            probe = LAB.probe_children_after_reap(settled, calls)
            self.assertEqual(probe["classification"], "UNKNOWN")

    def test_post_reap_owner_mismatch_is_unknown_and_single_use(self):
        handle, calls = timed_out(); child = ChildAdapter([{"terminal": terminal()}])
        calls.times = [101, 102, 103, 104]
        calls.polls = [[(10, LAB.select.EPOLLHUP), (11, LAB.select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settled = LAB.settle_passively(handle, child, 110, calls)
        calls.owner_identity = lambda: {**OWNER, "starttime": 201}
        probe = LAB.probe_children_after_reap(settled, calls)
        self.assertEqual(probe["classification"], "UNKNOWN")
        with self.assertRaises(LAB.Refusal):
            LAB.probe_children_after_reap(settled, calls)


if __name__ == "__main__":
    unittest.main()
