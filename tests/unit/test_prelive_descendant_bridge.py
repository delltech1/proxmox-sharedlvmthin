"""Mocked opaque leader-reap handoff into descendant accounting."""

import copy
import errno
import os
from pathlib import Path
import select
import stat
import sys
import types
import unittest


EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
import prelive_owned_child as OWNED
import prelive_capture_settlement as CAPTURE
import prelive_descendant_accounting as DESC
import prelive_descendant_bridge as BRIDGE


BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 100, "starttime": 200, "boot_id": BOOT}
LAUNCHER = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD", "exe_dev": 20,
            "exe_inode": 30, "cmdline_sha256": "a" * 64}


def wait(pid=300, status=0):
    return types.SimpleNamespace(
        si_pid=pid, si_code=os.CLD_EXITED, si_status=status)


class OwnedCalls:
    def __init__(self):
        self.starts = [400, 400]
        self.launchers = [copy.deepcopy(LAUNCHER), copy.deepcopy(LAUNCHER)]
        self.waits = []
        self.wait_options = []
    def current_identity(self): return copy.deepcopy(OWNER)
    def starttime(self, _pid): return self.starts.pop(0)
    def launcher(self, _pid): return self.launchers.pop(0)
    def pidfd_open(self, _pid): return 50
    def probe_waitable(self, _pidfd): return None
    def waitid(self, _pidfd, options):
        self.wait_options.append(options)
        value = self.waits.pop(0)
        if isinstance(value, BaseException): raise value
        return value
    def close(self, _fd): pass


def pipe_identity(fd, inode):
    return {"fd": fd, "dev": 20, "inode": inode,
            "mode": stat.S_IFIFO | 0o600,
            "flags": os.O_RDONLY | os.O_NONBLOCK}


class CaptureCalls:
    def __init__(self):
        self.identities = {10: pipe_identity(10, 30), 11: pipe_identity(11, 31)}
        self.reads = {10: [], 11: []}
        self.times = []
        self.polls = []
        self.poller = object()
    def owner_identity(self): return copy.deepcopy(OWNER)
    def fd_identity(self, fd): return copy.deepcopy(self.identities[fd])
    def epoll_create(self): return self.poller
    def epoll_register(self, _poller, _fd, _mask): pass
    def epoll_unregister(self, _poller, _fd): pass
    def epoll_close(self, _poller): pass
    def read(self, fd, _maximum):
        value = self.reads[fd].pop(0)
        if isinstance(value, BaseException): raise value
        return value
    def monotonic_ns(self): return self.times.pop(0)
    def epoll_poll(self, _poller, _timeout_ms): return self.polls.pop(0)


class DescCalls:
    def __init__(self):
        self.subreaper = 0
        self.baselines = [ChildProcessError(errno.ECHILD, "none"),
                          ChildProcessError(errno.ECHILD, "none")]
        self.discovery = []
        self.times = [0]
        self.identities = {}
        self.pidfd_results = []
        self.closed = []
    def owner_identity(self): return copy.deepcopy(OWNER)
    def kernel_thread_ids(self): return [100]
    def sigchld_policy(self):
        return {"handler_default": True, "ignored": False, "no_cldwait": False}
    def get_subreaper(self): return self.subreaper
    def set_subreaper(self, value): self.subreaper = value
    def monotonic_ns(self): return self.times.pop(0)
    def wait_all_wall_wnowait(self, _options):
        value = (self.baselines.pop(0) if self.baselines else self.discovery.pop(0))
        if isinstance(value, BaseException): raise value
        return value
    def child_identity(self, pid): return copy.deepcopy(self.identities[pid])
    def pidfd_open(self, pid): return pid + 1000
    def wait_pidfd(self, _pidfd, _options): return self.pidfd_results.pop(0)
    def close_pidfd(self, fd): self.closed.append(fd)


def attached(deadline=200, capture_limit=1024):
    desc = DescCalls()
    domain = DESC.prepare_child_domain(
        OWNER, "req", "d" * 32, deadline, desc)
    enrollment = BRIDGE.prepare_domain_enrollment(domain)
    owned_calls = OwnedCalls()
    lifecycle = OWNED._record_owned_fork(
        OWNED._FORK_KEY, 300, OWNER, "b" * 32,
        enrollment.pre_fork_enrollment)
    owned = OWNED.bind_owned_pidfd(lifecycle, LAUNCHER, owned_calls)
    adapter = OWNED.bind_capture_child_adapter(owned, owned_calls)
    capture_calls = CaptureCalls()
    origin = CAPTURE._record_capture_pipes(
        CAPTURE._PIPE_KEY, OWNER, adapter.identity(),
        pipe_identity(10, 30), pipe_identity(11, 31))
    capture = CAPTURE.bind_capture_pipes(origin, capture_limit, capture_calls)
    CAPTURE.attach_child_adapter(capture, adapter, capture_calls)
    bridge = BRIDGE.attach_owned_domain(
        enrollment, owned, adapter, capture)
    return domain, desc, owned, adapter, owned_calls, capture, capture_calls, bridge


def settled_graph(deadline=200):
    values = attached(deadline)
    domain, desc, owned, adapter, owned_calls, capture, calls, bridge = values
    owned_calls.waits = [wait(), wait()]
    calls.times = [0, 0, 100]
    calls.polls = [[]]
    primary = CAPTURE.capture_attached_until_deadline(capture, 100, calls)
    calls.times = [101, 102, 103, 104, 105]
    calls.polls = [[(10, select.EPOLLHUP), (11, select.EPOLLHUP)]]
    calls.reads[10] = [b""]; calls.reads[11] = [b""]
    settlement = CAPTURE.settle_attached_passively(capture, deadline, calls)
    return (*values, primary, settlement)


def normal_settled_graph(deadline=200, status=0):
    values = attached(deadline)
    domain, desc, owned, adapter, owned_calls, capture, calls, bridge = values
    owned_calls.waits = [wait(status=status), wait(status=status)]
    calls.times = [0, 0, 1, 2, 3]
    calls.polls = [[(10, select.EPOLLHUP), (11, select.EPOLLHUP)]]
    calls.reads[10] = [b""]; calls.reads[11] = [b""]
    completion = CAPTURE.capture_attached_until_deadline(capture, 100, calls)
    calls.times = [4, 5, 6]
    settlement = CAPTURE.reap_attached_completion_once(capture, deadline, calls)
    return (*values, completion, settlement)


class DescendantBridgeTests(unittest.TestCase):
    def test_normal_completion_exact_reap_handoff_then_echild(self):
        values = normal_settled_graph(status=7)
        desc, bridge, completion, settlement = values[1], values[7], values[8], values[9]
        desc.times = [110, 111, 112]
        desc.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"],
                         "MODEL_LEADER_REAP_AND_CHILD_DOMAIN_DRAINED")
        self.assertIsNone(result["primary_failure"])
        self.assertEqual(result["primary_completion"], completion)
        self.assertEqual(result["primary_completion"]
                         ["leader_observation"]["status"], 7)

    def test_mutated_normal_settlement_receipt_blocks_bridge(self):
        values = normal_settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        settlement.receipt["primary_completion"] = {"forged": True}
        desc.times = [110]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertFalse(result["leader_reap_confirmed"])

    def test_together_mutated_normal_copies_cannot_forge_bridge_status(self):
        values = normal_settled_graph(status=7)
        desc, bridge, settlement = values[1], values[7], values[9]
        settlement.receipt["primary_completion"]["leader_observation"]["status"] = 0
        settlement.descendant_capability.primary_completion[
            "leader_observation"]["status"] = 0
        desc.times = [110]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertFalse(result["leader_reap_confirmed"])
    def test_exact_reap_handoff_then_echild(self):
        domain, desc, owned, adapter, owned_calls, capture, calls, bridge, primary, settlement = settled_graph()
        desc.times = [110, 111, 112]
        desc.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"],
                         "MODEL_LEADER_REAP_AND_CHILD_DOMAIN_DRAINED")
        self.assertEqual(result["primary_failure"], primary)
        self.assertTrue(result["leader_reap_confirmed"])
        self.assertEqual(len(owned_calls.wait_options), 2)
        self.assertFalse(result["runtime_authorized"])

    def test_live_descendant_is_pending_not_complete(self):
        values = settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        desc.times = [110, 111, 112]
        desc.discovery = [None]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "CHILDREN_PENDING")

    def test_adopted_child_reap_then_second_call_echild(self):
        values = settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        desc.discovery = [wait(500), ChildProcessError(errno.ECHILD, "none")]
        desc.identities[500] = {"pid": 500, "starttime": 600,
                                "boot_id": BOOT, "ppid": 100}
        desc.pidfd_results = [wait(500), wait(500)]
        desc.times = list(range(110, 140))
        first = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(first["classification"], "CHILDREN_PENDING")
        second = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(second["classification"],
                         "MODEL_LEADER_REAP_AND_CHILD_DOMAIN_DRAINED")
        self.assertEqual(desc.closed, [1500])

    def test_expired_deadline_does_not_transfer_capability(self):
        values = settled_graph()
        desc, adapter, bridge, settlement = values[1], values[3], values[7], values[9]
        desc.times = [200]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["primary_failure"]["classification"],
                         "EXECUTION_TIMEOUT_UNSETTLED")
        self.assertEqual(adapter.state, "REAPED")
        self.assertFalse(adapter.reap_capability.used)

    def test_fork_before_enrollment_cannot_attach(self):
        owned_calls = OwnedCalls()
        lifecycle = OWNED._record_owned_fork(
            OWNED._FORK_KEY, 300, OWNER, "c" * 32)
        owned = OWNED.bind_owned_pidfd(lifecycle, LAUNCHER, owned_calls)
        adapter = OWNED.bind_capture_child_adapter(owned, owned_calls)
        calls = CaptureCalls()
        origin = CAPTURE._record_capture_pipes(
            CAPTURE._PIPE_KEY, OWNER, adapter.identity(),
            pipe_identity(10, 30), pipe_identity(11, 31))
        capture = CAPTURE.bind_capture_pipes(origin, 1024, calls)
        CAPTURE.attach_child_adapter(capture, adapter, calls)
        desc = DescCalls()
        domain = DESC.prepare_child_domain(
            OWNER, "late", "f" * 32, 200, desc)
        enrollment = BRIDGE.prepare_domain_enrollment(domain)
        with self.assertRaises(Exception):
            BRIDGE.attach_owned_domain(enrollment, owned, adapter, capture)
        self.assertEqual(domain.state, "UNKNOWN")

    def test_prefork_supervisor_identity_must_match_exactly(self):
        other = {"pid": 101, "starttime": 201, "boot_id": BOOT}
        ticket = OWNED.prepare_descendant_fork_enrollment(
            OWNED._PREFORK_KEY, object(), other)
        with self.assertRaises(OWNED.Refusal):
            OWNED._record_owned_fork(
                OWNED._FORK_KEY, 300, OWNER, "9" * 32, ticket)
        self.assertFalse(ticket.used)

    def test_probe_use_or_different_settlement_refuses(self):
        values = settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        settlement.probe_used = True
        desc.times = [110]
        self.assertEqual(BRIDGE.advance_descendant_domain(
            bridge, settlement, desc)["classification"], "UNKNOWN")

    def test_late_enrollment_after_monitoring_refuses(self):
        domain, desc, owned, adapter, owned_calls, capture, calls, bridge = attached()
        owned_calls.waits = [None]
        calls.times = [0, 0, 100]; calls.polls = [[]]
        CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        second_desc = DescCalls()
        second = DESC.prepare_child_domain(
            OWNER, "late", "e" * 32, 200, second_desc)
        enrollment = BRIDGE.prepare_domain_enrollment(second)
        with self.assertRaises(Exception):
            BRIDGE.attach_owned_domain(enrollment, owned, adapter, capture)
        self.assertEqual(second.state, "UNKNOWN")

    def test_copy_replay_and_same_ids_do_not_create_authority(self):
        values = settled_graph()
        bridge = values[7]
        with self.assertRaises(BRIDGE.Refusal): copy.copy(bridge)
        with self.assertRaises(BRIDGE.Refusal): copy.copy(bridge.enrollment)

    def test_primary_truncation_cannot_be_erased_by_domain_drain(self):
        values = attached(capture_limit=3)
        domain, desc, owned, adapter, owned_calls, capture, calls, bridge = values
        owned_calls.waits = [wait(), wait()]
        calls.times = [0, 0, 1, 2, 3]
        calls.polls = [[(10, select.EPOLLIN)]]
        calls.reads[10] = [b"xxxx", BlockingIOError(errno.EAGAIN, "again")]
        primary = CAPTURE.capture_attached_until_deadline(capture, 100, calls)
        self.assertEqual(primary["classification"], "CAPTURE_TRUNCATED_UNSETTLED")
        calls.times = [101, 102, 103, 104, 105]
        calls.polls = [[(10, select.EPOLLHUP), (11, select.EPOLLHUP)]]
        calls.reads[10] = [b""]; calls.reads[11] = [b""]
        settlement = CAPTURE.settle_attached_passively(capture, 200, calls)
        desc.times = [110, 111, 112]
        desc.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["primary_failure"]["classification"],
                         "CAPTURE_TRUNCATED_UNSETTLED")
        self.assertEqual(result["classification"],
                         "MODEL_LEADER_REAP_AND_CHILD_DOMAIN_DRAINED")

    def test_handoff_clock_watermark_blocks_domain_regression(self):
        values = settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        desc.times = [190, 10]
        desc.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(len(desc.discovery), 1)

    def test_underlying_poison_between_steps_blocks_later_echild(self):
        values = settled_graph()
        desc, owned, owned_calls, bridge, settlement = (
            values[1], values[2], values[4], values[7], values[9])
        desc.times = [110, 111, 112]
        desc.discovery = [None]
        self.assertEqual(BRIDGE.advance_descendant_domain(
            bridge, settlement, desc)["classification"], "CHILDREN_PENDING")
        with self.assertRaises(OWNED.Refusal):
            OWNED.observe_exit_nonblocking(owned, owned_calls)
        desc.times = [120, 121]
        desc.discovery = [ChildProcessError(errno.ECHILD, "none")]
        self.assertEqual(BRIDGE.advance_descendant_domain(
            bridge, settlement, desc)["classification"], "UNKNOWN")

    def test_bridge_claim_blocks_old_probe_and_receipt_mutation(self):
        values = settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        original_owner = desc.owner_identity
        def mutate_receipt():
            settlement.receipt["primary_failure"] = {"classification": "FORGED"}
            return original_owner()
        desc.owner_identity = mutate_receipt
        desc.times = [110, 111, 112]
        desc.discovery = [ChildProcessError(errno.ECHILD, "none")]
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["primary_failure"]["classification"],
                         "EXECUTION_TIMEOUT_UNSETTLED")
        with self.assertRaises(CAPTURE.Refusal):
            CAPTURE.probe_children_after_reap(settlement, desc)

    def test_post_adopted_reap_poison_preserves_current_progress(self):
        values = settled_graph()
        desc, owned, owned_calls, bridge, settlement = (
            values[1], values[2], values[4], values[7], values[9])
        desc.discovery = [wait(500)]
        desc.identities[500] = {"pid": 500, "starttime": 600,
                                "boot_id": BOOT, "ppid": 100}
        desc.pidfd_results = [wait(500), wait(500)]
        desc.times = list(range(110, 140))
        def close_then_poison(fd):
            desc.closed.append(fd)
            try: OWNED.observe_exit_nonblocking(owned, owned_calls)
            except OWNED.Refusal: pass
        desc.close_pidfd = close_then_poison
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(result["descendant_progress"]["outcome"],
                         "ADOPTED_CHILD_REAPED_CONTINUE")
        self.assertTrue(result["descendant_progress"]
                        ["adopted_records"][0]["exact_pidfd_reap"])

    def test_nested_bridge_during_adopted_close_preserves_unknown_progress(self):
        values = settled_graph()
        desc, bridge, settlement = values[1], values[7], values[9]
        desc.discovery = [wait(500)]
        desc.identities[500] = {"pid": 500, "starttime": 600,
                                "boot_id": BOOT, "ppid": 100}
        desc.pidfd_results = [wait(500), wait(500)]
        desc.times = list(range(110, 140))
        def close_then_reenter(fd):
            desc.closed.append(fd)
            BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        desc.close_pidfd = close_then_reenter
        result = BRIDGE.advance_descendant_domain(bridge, settlement, desc)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertIsNotNone(result["descendant_progress"])
        self.assertEqual(result["descendant_progress"]["outcome"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
