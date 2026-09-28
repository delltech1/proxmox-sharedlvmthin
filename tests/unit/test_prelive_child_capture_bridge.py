"""Mocked integration of exact owned-child receipt through capture settlement."""

import copy
import importlib.util
import os
from pathlib import Path
import select
import stat
import sys
import threading
import types
import unittest

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))


def load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "experiments/thick-generations" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


import prelive_owned_child as OWNED
import prelive_capture_settlement as CAPTURE

BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 100, "starttime": 200, "boot_id": BOOT}
LAUNCHER = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD", "exe_dev": 20,
            "exe_inode": 30, "cmdline_sha256": "a" * 64}


def wait(status=0):
    return types.SimpleNamespace(
        si_pid=300, si_code=os.CLD_EXITED, si_status=status)


class OwnedSyscalls:
    def __init__(self):
        self.starts = [400, 400]
        self.launchers = [copy.deepcopy(LAUNCHER), copy.deepcopy(LAUNCHER)]
        self.waits = []
        self.wait_options = []
        self.closed = []
    def current_identity(self): return copy.deepcopy(OWNER)
    def starttime(self, pid):
        if pid != 300: raise AssertionError("foreign child")
        return self.starts.pop(0)
    def launcher(self, pid):
        if pid != 300: raise AssertionError("foreign child")
        return self.launchers.pop(0)
    def pidfd_open(self, pid):
        if pid != 300: raise AssertionError("foreign child")
        return 50
    def probe_waitable(self, pidfd): return None
    def waitid(self, pidfd, options):
        if pidfd != 50: raise AssertionError("foreign pidfd")
        self.wait_options.append(options)
        value = self.waits.pop(0)
        if isinstance(value, BaseException): raise value
        return value
    def close(self, fd): self.closed.append(fd)


def pipe_identity(fd, inode):
    return {"fd": fd, "dev": 20, "inode": inode,
            "mode": stat.S_IFIFO | 0o600,
            "flags": os.O_RDONLY | os.O_NONBLOCK}


class CaptureSyscalls:
    def __init__(self):
        self.identities = {10: pipe_identity(10, 30), 11: pipe_identity(11, 31)}
        self.reads = {10: [], 11: []}
        self.times = []
        self.polls = []
        self.poller = object()
        self.closed = []
    def owner_identity(self): return copy.deepcopy(OWNER)
    def fd_identity(self, fd): return copy.deepcopy(self.identities[fd])
    def epoll_create(self): return self.poller
    def epoll_register(self, poller, fd, mask): pass
    def epoll_unregister(self, poller, fd): pass
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


def integrated():
    owned_calls = OwnedSyscalls()
    lifecycle = OWNED._record_owned_fork(
        OWNED._FORK_KEY, 300, OWNER, "b" * 32)
    owned_handle = OWNED.bind_owned_pidfd(lifecycle, LAUNCHER, owned_calls)
    adapter = OWNED.bind_capture_child_adapter(owned_handle, owned_calls)
    capture_calls = CaptureSyscalls()
    origin = CAPTURE._record_capture_pipes(
        CAPTURE._PIPE_KEY, OWNER, adapter.identity(),
        pipe_identity(10, 30), pipe_identity(11, 31))
    capture_handle = CAPTURE.bind_capture_pipes(origin, 1024, capture_calls)
    CAPTURE.attach_child_adapter(capture_handle, adapter, capture_calls)
    return owned_handle, adapter, owned_calls, capture_handle, capture_calls


class ExactReceiptBridgeTests(unittest.TestCase):
    def _completed(self, status=0):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait(status), wait(status)]
        calls.times = [0, 0, 1, 2, 3]
        calls.polls = [[(10, select.EPOLLHUP), (11, select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        completion = CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(completion["classification"],
                         "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED")
        return owned, adapter, child, capture, calls, completion

    def test_normal_completion_reaps_exact_cached_receipt_once(self):
        owned, adapter, child, capture, calls, completion = self._completed()
        exact = adapter.cached
        calls.times = [4, 5, 6]
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "LEADER_REAPED_STREAMS_EOF")
        self.assertEqual(settled.receipt["settlement_kind"],
                         "NORMAL_COMPLETION")
        self.assertIsNone(settled.receipt["primary_failure"])
        self.assertEqual(settled.receipt["primary_completion"],
                         capture.normal_completion)
        self.assertIs(adapter.cached, exact)
        self.assertEqual(child.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])
        self.assertEqual(owned.state, "REAPED")
        self.assertTrue(settled.leader_reaped)
        self.assertIsNotNone(settled.descendant_capability)

    def test_unreaped_completion_validator_is_pure_and_repeatable(self):
        owned, adapter, child, capture, calls, completion = self._completed(7)
        before = (len(child.wait_options), len(calls.times), len(calls.polls),
                  len(calls.closed))
        cap = CAPTURE.validate_attached_completion_unreaped(capture)
        again = CAPTURE.validate_attached_completion_unreaped(capture)
        self.assertIs(cap, capture.normal_completion_capability)
        self.assertIs(again, cap)
        self.assertFalse(cap.used)
        self.assertEqual(before, (len(child.wait_options), len(calls.times),
                                  len(calls.polls), len(calls.closed)))
        self.assertEqual(completion["leader_observation"]["status"], 7)

    def test_unreaped_validator_rejects_replaced_backlinks_and_stream_drift(self):
        for mutate in (
                lambda owned, adapter, capture:
                setattr(adapter, "normal_completion_capability", object()),
                lambda owned, adapter, capture:
                setattr(capture.normal_completion_capability, "used", True),
                lambda owned, adapter, capture:
                capture.streams["stdout"].stored.extend(b"drift")):
            with self.subTest(mutate=mutate):
                owned, adapter, child, capture, calls, unused = self._completed()
                mutate(owned, adapter, capture)
                with self.assertRaises(CAPTURE.Refusal):
                    CAPTURE.validate_attached_completion_unreaped(capture)

    def test_unreaped_validator_binds_original_exit_and_full_stream(self):
        owned, adapter, child, capture, calls, unused = self._completed(7)
        cap = capture.normal_completion_capability
        for value in (cap.completion, capture.normal_completion):
            value["leader_observation"]["status"] = 73
        cap.completion_digest = CAPTURE._completion_digest(cap.completion)
        adapter.normal_completion_digest = cap.completion_digest
        with self.assertRaises(CAPTURE.Refusal):
            CAPTURE.validate_attached_completion_unreaped(capture)

        owned, adapter, child, capture, calls, unused = self._completed()
        cap = capture.normal_completion_capability
        capture.streams["stdout"].observed_bytes += 1
        for value in (cap.completion, capture.normal_completion):
            value["streams"]["stdout"]["observed_bytes"] += 1
        cap.completion_digest = CAPTURE._completion_digest(cap.completion)
        adapter.normal_completion_digest = cap.completion_digest
        with self.assertRaises(CAPTURE.Refusal):
            CAPTURE.validate_attached_completion_unreaped(capture)

    def test_unreaped_validator_requires_original_claimed_origin(self):
        owned, adapter, child, capture, calls, unused = self._completed()
        capture.origin.claimed = False
        with self.assertRaises(CAPTURE.Refusal):
            CAPTURE.validate_attached_completion_unreaped(capture)

    def test_unreaped_validator_rejects_custom_values_without_callbacks(self):
        class Evil:
            calls = []
            def __init__(self, collision="state"):
                self.collision = collision
            def __hash__(self):
                return hash(self.collision)
            def __eq__(self, other):
                type(self).calls.append(other)
                return False
            def __str__(self):
                type(self).calls.append("str")
                return "evil"
        mutations = (
            lambda owned, adapter, capture:
            owned.lifecycle.__setitem__(Evil(), "PIDFD_BOUND"),
            lambda owned, adapter, capture:
            capture.normal_completion_capability.owner.__setitem__(
                "boot_id", Evil()),
            lambda owned, adapter, capture:
            owned.lifecycle.__setitem__("token", Evil()),
            lambda owned, adapter, capture:
            owned.child.__setitem__("pid", Evil()),
            lambda owned, adapter, capture:
            setattr(owned, "pidfd", Evil()),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                owned, adapter, child, capture, calls, unused = self._completed()
                mutate(owned, adapter, capture)
                Evil.calls.clear()
                with self.assertRaises(CAPTURE.Refusal):
                    CAPTURE.validate_attached_completion_unreaped(capture)
                self.assertEqual(Evil.calls, [])

    def test_mutated_return_dict_cannot_change_normal_authority(self):
        owned, adapter, child, capture, calls, completion = self._completed(7)
        completion["leader_observation"]["status"] = 0
        completion["streams"] = {}
        calls.times = [4, 5, 6]
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertEqual(settled.receipt["primary_completion"]
                         ["leader_observation"]["status"], 7)
        self.assertEqual(set(settled.receipt["primary_completion"]["streams"]),
                         {"stdout", "stderr"})

    def test_normal_completion_deadline_before_reap_never_reaps(self):
        owned, adapter, child, capture, calls, unused = self._completed()
        calls.times = [200]
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "UNKNOWN_NORMAL_COMPLETION")
        self.assertFalse(settled.leader_reaped)
        self.assertEqual(len(child.wait_options), 1)
        self.assertIsNone(settled.descendant_capability)

    def test_post_reap_deadline_preserves_reap_without_descendant_capability(self):
        owned, adapter, child, capture, calls, unused = self._completed()
        calls.times = [4, 5, 200]
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertTrue(settled.leader_reaped)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "UNKNOWN_NORMAL_COMPLETION")
        self.assertIsNone(settled.descendant_capability)
        self.assertEqual(owned.state, "REAPED")
        with self.assertRaises(CAPTURE.Refusal):
            CAPTURE.reap_attached_completion_once(capture, 300, calls)

    def test_clock_regression_before_and_after_reap_fails_closed(self):
        owned, adapter, child, capture, calls, unused = self._completed()
        calls.times = [4, 3]
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertFalse(settled.leader_reaped)
        self.assertEqual(len(child.wait_options), 1)
        self.assertIsNone(settled.descendant_capability)
        owned, adapter, child, capture, calls, unused = self._completed()
        calls.times = [4, 5, 4]
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertTrue(settled.leader_reaped)
        self.assertIsNone(settled.descendant_capability)

    def test_last_clock_underlying_poison_preserves_reap_without_capability(self):
        owned, adapter, child, capture, calls, unused = self._completed()
        ticks = iter((4, 5, 6)); count = []
        def clock():
            value = next(ticks); count.append(value)
            if len(count) == 3: owned.state = "UNKNOWN_TEST_POISON"
            return value
        calls.monotonic_ns = clock
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertTrue(settled.leader_reaped)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "UNKNOWN_NORMAL_COMPLETION")
        self.assertIsNone(settled.descendant_capability)

    def test_last_clock_identity_drift_blocks_capability_after_exact_reap(self):
        for attack in ("lifecycle", "backlink", "child", "float_pid", "pidfd",
                       "boot_id", "supervisor"):
            owned, adapter, child, capture, calls, unused = self._completed()
            ticks = iter((4, 5, 6)); count = []
            def clock(selected=attack):
                value = next(ticks); count.append(value)
                if len(count) == 3:
                    if selected == "lifecycle":
                        owned.lifecycle["state"] = "UNKNOWN_TEST_POISON"
                    elif selected == "backlink":
                        owned.capture_adapter = object()
                    elif selected == "float_pid":
                        owned.child["pid"] = float(owned.child["pid"])
                    elif selected == "pidfd":
                        owned.pidfd += 1
                    elif selected == "boot_id":
                        owned.child["boot_id"] = (
                            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
                    elif selected == "supervisor":
                        owned.lifecycle["supervisor"]["pid"] += 1
                    else:
                        owned.child["pid"] += 1
                return value
            calls.monotonic_ns = clock
            settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
            self.assertTrue(settled.leader_reaped)
            self.assertIsNone(settled.descendant_capability)

    def test_fake_named_adapter_refuses_at_attach(self):
        owned, adapter, child, capture, calls = integrated()
        other_calls = CaptureSyscalls()
        origin = CAPTURE._record_capture_pipes(
            CAPTURE._PIPE_KEY, OWNER, capture.origin.child,
            pipe_identity(10, 30), pipe_identity(11, 31))
        other = CAPTURE.bind_capture_pipes(origin, 1024, other_calls)
        Fake = type("_CaptureChildAdapter", (), {})
        with self.assertRaisesRegex(CAPTURE.Refusal, "exact owned"):
            CAPTURE.attach_child_adapter(other, Fake(), other_calls)

    def test_direct_fake_completion_capability_cannot_enter_settlement(self):
        owned, adapter, child, capture, calls = integrated()
        capture.state = "SETTLING"
        capture.settlement_used = True
        capture.normal_handoff_active = True
        fake = types.SimpleNamespace(used=True, capture_handle=capture,
                                     child_adapter=adapter,
                                     cached_receipt=adapter.cached,
                                     completion_digest="0" * 64)
        capture.normal_completion_capability = fake
        with self.assertRaises(OWNED.Refusal):
            adapter.enter_completed_settlement(
                CAPTURE._CHILD_ADAPTER_KEY, capture, fake)

    def test_fake_completion_capability_is_rejected_at_bind_without_reap(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait()]
        calls.times = list(range(10))
        calls.polls = [[(10, select.EPOLLHUP), (11, select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        original_bind = adapter.bind_normal_completion
        attempted = []
        def forged_bind(authority, consumer, capability, digest):
            fake = types.SimpleNamespace(
                used=False, capture_handle=consumer, child_adapter=adapter,
                cached_receipt=adapter.cached, completion=capability.completion,
                completion_digest=digest)
            attempted.append(True)
            return original_bind(authority, consumer, fake, digest)
        adapter.bind_normal_completion = forged_bind
        with self.assertRaises(BaseException) as caught:
            CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(attempted, [True], repr(caught.exception))
        self.assertEqual(len(child.wait_options), 1)
        self.assertTrue(adapter.state.startswith("UNKNOWN"))
        self.assertNotEqual(owned.state, "REAPED")

    def test_swallowed_nested_normal_handoff_poison_stops_before_reap(self):
        owned, adapter, child, capture, calls, unused = self._completed()
        fired = []
        def clock():
            if not fired:
                fired.append(True)
                with self.assertRaises(CAPTURE.Refusal):
                    CAPTURE.reap_attached_completion_once(capture, 200, calls)
            return 4
        calls.monotonic_ns = clock
        settled = CAPTURE.reap_attached_completion_once(capture, 200, calls)
        self.assertFalse(settled.leader_reaped)
        self.assertEqual(len(child.wait_options), 1)
        self.assertIsNone(settled.descendant_capability)

    def test_primary_terminal_timeout_then_settlement_reuses_one_exact_receipt(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait(), wait()]
        calls.times = [0, 0, 100]
        calls.polls = [[]]
        primary = CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        exact = owned.observation
        self.assertEqual(primary["classification"], "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertIs(adapter.cached, exact)
        calls.times = [101, 102, 200]
        calls.polls = [[]]
        settled = CAPTURE.settle_attached_passively(capture, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "CLEANUP_DEADLINE_UNSETTLED")
        self.assertTrue(settled.leader_reaped)
        self.assertIs(adapter.cached, exact)
        self.assertEqual(child.wait_options,
                         [os.WEXITED | os.WNOHANG | os.WNOWAIT,
                          os.WEXITED | os.WNOHANG])
        self.assertEqual(child.closed, [])

    def test_late_terminal_in_settlement_observes_once_and_reaps_once(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [None, wait(), wait()]
        calls.times = [0, 0, 100]
        calls.polls = [[]]
        CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        calls.times = [101, 102, 103, 104, 105]
        calls.polls = [[(10, select.EPOLLHUP), (11, select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settled = CAPTURE.settle_attached_passively(capture, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "LEADER_REAPED_STREAMS_EOF")
        self.assertEqual(len(child.wait_options), 3)
        self.assertEqual(owned.state, "REAPED")

    def test_expired_cleanup_deadline_never_reaps_cached_terminal(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait()]
        calls.times = [0, 0, 100]
        calls.polls = [[]]
        CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        calls.times = [200]
        settled = CAPTURE.settle_attached_passively(capture, 200, calls)
        self.assertFalse(settled.leader_reaped)
        self.assertEqual(owned.state, "TERMINAL_OBSERVED_NOT_REAPED")
        self.assertEqual(len(child.wait_options), 1)

    def test_claim_blocks_second_adapter_and_direct_lifecycle_calls(self):
        owned, adapter, child, capture, calls = integrated()
        before = len(child.wait_options)
        with self.assertRaises(OWNED.Refusal):
            OWNED.observe_exit_nonblocking(owned, child)
        self.assertEqual(len(child.wait_options), before)
        self.assertEqual(owned.state, "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION")
        owned, adapter, child, capture, calls = integrated()
        with self.assertRaises(OWNED.Refusal):
            OWNED.bind_capture_child_adapter(owned, child)
        self.assertEqual(owned.state, "UNKNOWN_CAPTURE_AUTHORITY_VIOLATION")

    def test_adapter_noncopyable_and_cannot_attach_to_second_capture(self):
        owned, adapter, child, capture, calls = integrated()
        with self.assertRaises(OWNED.Refusal): copy.copy(adapter)
        with self.assertRaises(OWNED.Refusal): copy.deepcopy(adapter)
        second_calls = CaptureSyscalls()
        origin = CAPTURE._record_capture_pipes(
            CAPTURE._PIPE_KEY, OWNER, copy.deepcopy(capture.origin.child),
            pipe_identity(10, 30), pipe_identity(11, 31))
        second = CAPTURE.bind_capture_pipes(origin, 1024, second_calls)
        with self.assertRaises(OWNED.Refusal):
            CAPTURE.attach_child_adapter(second, adapter, second_calls)
        with self.assertRaises(CAPTURE.Refusal):
            CAPTURE.attach_child_adapter(capture, adapter, calls)

    def test_legacy_capture_and_direct_handoff_cannot_bypass_consumer_claim(self):
        owned, adapter, child, capture, calls = integrated()
        second_calls = CaptureSyscalls(); second_calls.times = [0]
        origin = CAPTURE._record_capture_pipes(
            CAPTURE._PIPE_KEY, OWNER, copy.deepcopy(capture.origin.child),
            pipe_identity(10, 30), pipe_identity(11, 31))
        second = CAPTURE.bind_capture_pipes(origin, 1024, second_calls)
        with self.assertRaises(OWNED.Refusal):
            CAPTURE.capture_until_deadline(
                second, 100,
                lambda: adapter.observe_or_cached(object(), second), second_calls)
        self.assertEqual(child.wait_options, [])
        self.assertTrue(adapter.state.startswith("UNKNOWN"))
        owned, adapter, child, capture, calls = integrated()
        with self.assertRaises(OWNED.Refusal):
            adapter.enter_settlement(object(), capture)
        with self.assertRaises(OWNED.Refusal):
            adapter.reap_cached_once(object(), capture)
        self.assertEqual(child.wait_options, [])

    def test_nested_observe_callback_poisons_outer_even_when_refusal_is_swallowed(self):
        owned, adapter, child, capture, calls = integrated(); invocations = []
        def reentrant(pidfd, options):
            invocations.append(options)
            with self.assertRaisesRegex(OWNED.Refusal, "reentrant"):
                adapter.observe_or_cached(
                    CAPTURE._CHILD_ADAPTER_KEY, capture)
            return wait()
        child.waitid = reentrant
        calls.times = [0, 0]
        with self.assertRaises(BaseException):
            CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(len(invocations), 1)
        self.assertTrue(adapter.state.startswith("UNKNOWN"))
        self.assertTrue(owned.state.startswith("UNKNOWN"))

    def test_nested_reap_callback_cannot_restore_success(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait()]
        calls.times = [0, 0, 100]; calls.polls = [[]]
        CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        invocations = []
        def reentrant(pidfd, options):
            invocations.append(options)
            with self.assertRaisesRegex(OWNED.Refusal, "reentrant"):
                adapter.reap_cached_once(
                    CAPTURE._CHILD_ADAPTER_KEY, capture)
            return wait()
        child.waitid = reentrant
        calls.times = [101, 102]
        settled = CAPTURE.settle_attached_passively(capture, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"], "UNKNOWN_CHILD_API")
        self.assertEqual(len(invocations), 1)
        self.assertFalse(settled.leader_reaped)

    def test_foreign_thread_attempt_permanently_poisons_adapter(self):
        owned, adapter, child, capture, calls = integrated(); errors = []
        def foreign():
            try:
                adapter.identity(CAPTURE._CHILD_ADAPTER_KEY, capture)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=foreign)
        thread.start(); thread.join()
        self.assertEqual(len(errors), 1)
        self.assertTrue(adapter.state.startswith("UNKNOWN"))
        with self.assertRaises(OWNED.Refusal):
            adapter.identity(CAPTURE._CHILD_ADAPTER_KEY, capture)

    def test_deadline_crossing_after_observe_or_reap_dispatches_no_poll(self):
        # Late nonterminal observation consumes the remaining cleanup budget.
        owned, adapter, child, capture, calls = integrated()
        child.waits = [None, None]
        calls.times = [0, 0, 100]; calls.polls = [[]]
        CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        calls.times = [101, 200]; calls.polls = []
        settled = CAPTURE.settle_attached_passively(capture, 200, calls)
        self.assertEqual(settled.receipt["cleanup_outcome"],
                         "CLEANUP_DEADLINE_UNSETTLED")
        self.assertEqual(calls.polls, [])
        # Exact reap may complete, but a late return still cannot dispatch poll.
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait(), wait()]
        calls.times = [0, 0, 100]; calls.polls = [[]]
        CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        calls.times = [101, 102, 200]; calls.polls = []
        settled = CAPTURE.settle_attached_passively(capture, 200, calls)
        self.assertTrue(settled.leader_reaped)
        self.assertEqual(calls.polls, [])

    def test_empty_claim_and_pre_attach_operations_poison_without_wait(self):
        child = OwnedSyscalls()
        lifecycle = OWNED._record_owned_fork(
            OWNED._FORK_KEY, 300, OWNER, "b" * 32)
        owned = OWNED.bind_owned_pidfd(lifecycle, LAUNCHER, child)
        adapter = OWNED.bind_capture_child_adapter(owned, child)
        with self.assertRaises(OWNED.Refusal):
            adapter.observe_or_cached(None, None)
        self.assertEqual(child.wait_options, [])
        self.assertTrue(adapter.state.startswith("UNKNOWN"))
        child = OwnedSyscalls()
        owned = OWNED.bind_owned_pidfd(
            OWNED._record_owned_fork(OWNED._FORK_KEY, 300, OWNER, "c" * 32),
            LAUNCHER, child)
        adapter = OWNED.bind_capture_child_adapter(owned, child)
        with self.assertRaises(OWNED.Refusal):
            adapter.claim_capture(None, None)
        self.assertTrue(adapter.state.startswith("UNKNOWN"))

    def test_valid_consumer_cannot_handoff_before_settlement_phase(self):
        owned, adapter, child, capture, calls = integrated()
        with self.assertRaisesRegex(OWNED.Refusal, "not settling"):
            adapter.enter_settlement(CAPTURE._CHILD_ADAPTER_KEY, capture)
        self.assertEqual(child.wait_options, [])
        self.assertTrue(adapter.state.startswith("UNKNOWN"))

    def test_caught_direct_lifecycle_poison_after_cached_terminal_blocks_complete(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait()]
        calls.times = [0, 0, 1, 2]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        def poisoning_poll(poller, timeout_ms):
            with self.assertRaises(OWNED.Refusal):
                OWNED.observe_exit_nonblocking(owned, child)
            return [(10, select.EPOLLHUP), (11, select.EPOLLHUP)]
        calls.epoll_poll = poisoning_poll
        with self.assertRaises(BaseException):
            CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(len(child.wait_options), 1)
        self.assertTrue(capture.state.startswith("UNKNOWN"))
        self.assertNotEqual(capture.state,
                            "LEADER_OBSERVED_CAPTURE_COMPLETE_NOT_REAPED")

    def test_clock_poison_after_first_terminal_observe_is_caught_internally(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait()]; ticks = []; injected = []
        def clock():
            ticks.append(len(ticks))
            if len(ticks) == 3:
                with self.assertRaises(OWNED.Refusal):
                    OWNED.observe_exit_nonblocking(owned, child)
                injected.append(True)
            return len(ticks) - 1
        calls.monotonic_ns = clock
        with self.assertRaises(BaseException):
            CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(injected, [True])
        self.assertEqual(len(child.wait_options), 1)
        self.assertTrue(capture.state.startswith("UNKNOWN"))

    def test_clock_poison_after_cached_validation_cannot_return_complete(self):
        owned, adapter, child, capture, calls = integrated()
        child.waits = [wait()]; calls.polls = [[]]
        ticks = []; injected = []
        def clock():
            ticks.append(len(ticks))
            if len(ticks) == 5:
                with self.assertRaises(OWNED.Refusal):
                    OWNED.observe_exit_nonblocking(owned, child)
                injected.append(True)
            return len(ticks) - 1
        calls.monotonic_ns = clock
        with self.assertRaises(BaseException):
            CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(injected, [True])
        self.assertEqual(len(child.wait_options), 1)
        self.assertTrue(capture.state.startswith("UNKNOWN"))


if __name__ == "__main__":
    unittest.main()
