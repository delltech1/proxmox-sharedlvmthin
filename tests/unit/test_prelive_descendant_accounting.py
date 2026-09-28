import copy
import errno
import importlib.util
import os
from pathlib import Path
import unittest


PATH = Path(__file__).parents[2] / "experiments/thick-generations/prelive_descendant_accounting.py"
SPEC = importlib.util.spec_from_file_location("prelive_descendant_accounting", PATH)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)
BOOT = "11111111-2222-3333-4444-555555555555"


class Wait:
    def __init__(self, pid, code=os.CLD_EXITED, status=0):
        self.si_pid, self.si_code, self.si_status = pid, code, status


class Calls:
    def __init__(self):
        self.owner = {"pid": 10, "starttime": 20, "boot_id": BOOT}
        self.tasks = [10]
        self.policy = {"handler_default": True, "ignored": False,
                       "no_cldwait": False}
        self.subreaper = 0
        self.baselines = [ChildProcessError(errno.ECHILD, "none"),
                          ChildProcessError(errno.ECHILD, "none")]
        self.discovery = []
        self.times = iter(range(10, 1000, 10))
        self.identities = {}
        self.pidfd_results = []
        self.closed = []
        self.options = []

    def owner_identity(self): return copy.deepcopy(self.owner)
    def kernel_thread_ids(self): return list(self.tasks)
    def sigchld_policy(self): return dict(self.policy)
    def get_subreaper(self): return self.subreaper
    def set_subreaper(self, value): self.subreaper = value
    def monotonic_ns(self): return next(self.times)
    def wait_all_wall_wnowait(self, options):
        self.options.append(options)
        item = (self.baselines.pop(0) if self.baselines else self.discovery.pop(0))
        if isinstance(item, BaseException): raise item
        return item
    def child_identity(self, pid): return copy.deepcopy(self.identities[pid])
    def pidfd_open(self, pid): return pid + 1000
    def wait_pidfd(self, _pidfd, options):
        self.options.append(options)
        return self.pidfd_results.pop(0)
    def close_pidfd(self, fd): self.closed.append(fd)


def prepared(calls=None):
    calls = calls or Calls()
    domain = LAB.prepare_child_domain(
        calls.owner, "req", "a" * 32, 10000, calls)
    leader = {"pid": 11, "starttime": 21, "boot_id": BOOT}
    origin = LAB._launcher_origin(LAB._LEADER_KEY, domain, leader)
    domain.claim_launcher(origin)
    claim = LAB._leader_reap_claim(LAB._LEADER_KEY, domain, origin)
    domain.accept_leader_reap(claim)
    return domain, calls, claim


class DescendantAccountingTests(unittest.TestCase):
    def test_prepare_requires_wall_and_two_exact_echild_baselines(self):
        domain, calls, _ = prepared()
        self.assertEqual(domain.state, "DRAINING")
        self.assertEqual(calls.options[:2], [LAB.WAIT_DISCOVERY_OPTIONS] * 2)
        self.assertTrue(LAB.WAIT_DISCOVERY_OPTIONS & LAB.LINUX_WAIT_WALL)

    def test_preexisting_live_or_terminal_child_refuses_without_reap(self):
        for value in (None, Wait(99)):
            calls = Calls(); calls.baselines[0] = value
            with self.assertRaises(LAB.Refusal):
                LAB.prepare_child_domain(calls.owner, "r", "b" * 32, 100, calls)
            self.assertEqual(calls.closed, [])

    def test_sigchld_threads_and_existing_subreaper_refuse(self):
        variants = [("tasks", [10, 12]),
                    ("policy", {"handler_default": False, "ignored": True,
                                "no_cldwait": False}),
                    ("subreaper", 1)]
        for attr, value in variants:
            calls = Calls(); setattr(calls, attr, value)
            with self.assertRaises(LAB.Refusal):
                LAB.prepare_child_domain(calls.owner, "r", "c" * 32, 100, calls)

    def test_pending_is_not_drained(self):
        domain, calls, _ = prepared(); calls.discovery = [None]
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "CHILDREN_PENDING")
        self.assertEqual(domain.state, "DRAINING")

    def test_exact_adopted_reap_then_wall_echild_drains(self):
        domain, calls, _ = prepared()
        calls.discovery = [Wait(90), ChildProcessError(errno.ECHILD, "none")]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        calls.pidfd_results = [Wait(90), Wait(90)]
        first = domain.step(calls)
        self.assertEqual(first["outcome"], "ADOPTED_CHILD_REAPED_CONTINUE")
        self.assertEqual(calls.closed, [1090])
        self.assertTrue(LAB.WAIT_PIDFD_OBSERVE & LAB.LINUX_WAIT_WALL)
        self.assertTrue(LAB.WAIT_PIDFD_REAP & LAB.LINUX_WAIT_WALL)
        second = domain.step(calls)
        self.assertEqual(second["outcome"], "MODEL_CHILD_DOMAIN_DRAINED")
        self.assertFalse(second["runtime_authorized"])

    def test_receipt_mismatch_poison_is_monotonic(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90)]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        calls.pidfd_results = [Wait(90, status=1)]
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")
        self.assertEqual(domain.state, "UNKNOWN")
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")

    def test_snapshot_cannot_forge_drained(self):
        calls = Calls()
        domain = LAB.prepare_child_domain(calls.owner, "r", "e" * 32, 100, calls)
        self.assertEqual(domain.snapshot()["outcome"], "PREPARED")
        self.assertIsNone(domain.snapshot()["terminal_echild"])

    def test_same_textual_domain_id_cannot_swap_capabilities(self):
        calls1 = Calls()
        first = LAB.prepare_child_domain(calls1.owner, "req", "a" * 32, 10000, calls1)
        calls2 = Calls()
        second = LAB.prepare_child_domain(calls2.owner, "req", "a" * 32, 10000, calls2)
        leader = {"pid": 12, "starttime": 22, "boot_id": BOOT}
        foreign = LAB._launcher_origin(LAB._LEADER_KEY, second, leader)
        with self.assertRaises(LAB.Refusal): first.claim_launcher(foreign)

    def test_bad_leader_identities_refuse(self):
        calls = Calls()
        domain = LAB.prepare_child_domain(calls.owner, "r", "f" * 32, 100, calls)
        for leader in ({"pid": 10, "starttime": 21, "boot_id": BOOT},
                       {"pid": 11, "starttime": 21,
                        "boot_id": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"}):
            with self.assertRaises(LAB.Refusal):
                LAB._launcher_origin(LAB._LEADER_KEY, domain, leader)

    def test_strict_types_refuse(self):
        calls = Calls(); calls.tasks = [10.0]
        with self.assertRaises(LAB.Refusal):
            LAB.prepare_child_domain(calls.owner, "r", "1" * 32, 100, calls)
        calls = Calls(); calls.subreaper = False
        with self.assertRaises(LAB.Refusal):
            LAB.prepare_child_domain(calls.owner, "r", "2" * 32, 100, calls)
        calls = Calls(); calls.policy["ignored"] = 0
        with self.assertRaises(LAB.Refusal):
            LAB.prepare_child_domain(calls.owner, "r", "3" * 32, 100, calls)

    def test_invalid_terminal_status_refuses(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90, status=256)]
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")

    def test_successful_reap_then_close_failure_preserves_pending_evidence(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90)]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        calls.pidfd_results = [Wait(90), Wait(90)]
        def fail_close(_fd): raise OSError("close ambiguous")
        calls.close_pidfd = fail_close
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertTrue(receipt["pending_adopted"]["reap_confirmed"])
        self.assertFalse(receipt["pending_adopted"]["pidfd_close_confirmed"])

    def test_nested_step_cannot_be_swallowed_into_drained(self):
        domain, calls, _ = prepared()
        def nested(_options):
            domain.step(calls)
            raise ChildProcessError(errno.ECHILD, "none")
        calls.wait_all_wall_wnowait = nested
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertEqual(domain.state, "UNKNOWN")

    def test_nested_public_mutations_cannot_be_swallowed_into_drained(self):
        for method in ("claim", "accept"):
            domain, calls, claim = prepared()
            leader = {"pid": 12, "starttime": 22, "boot_id": BOOT}
            origin = LAB._launcher_origin(LAB._LEADER_KEY, domain, leader)
            def nested(_options, selected=method):
                try:
                    if selected == "claim": domain.claim_launcher(origin)
                    else: domain.accept_leader_reap(claim)
                except LAB.Refusal:
                    pass
                raise ChildProcessError(errno.ECHILD, "none")
            calls.wait_all_wall_wnowait = nested
            self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")
            self.assertEqual(domain.state, "UNKNOWN")

    def test_nested_close_after_reap_remains_unknown_with_evidence(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90)]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        calls.pidfd_results = [Wait(90), Wait(90)]
        def nested_close(fd):
            calls.closed.append(fd)
            domain.step(calls)
        calls.close_pidfd = nested_close
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertTrue(receipt["pending_adopted"]["reap_confirmed"])

    def test_mutated_identity_alias_is_detected(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90)]
        shared = {"pid": 90, "starttime": 900, "boot_id": BOOT, "ppid": 10}
        calls.child_identity = lambda _pid: shared
        def mutate_then_open(pid):
            shared["starttime"] = 901
            return pid + 1000
        calls.pidfd_open = mutate_then_open
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertEqual(receipt["pending_adopted"]["identity"]["starttime"], 900)

    def test_nonwaitable_pidfd_is_unknown_not_pending(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90)]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        calls.pidfd_results = [None]
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")

    def test_reentrant_pidfd_open_is_closed_and_recorded(self):
        domain, calls, claim = prepared(); calls.discovery = [Wait(90)]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        def reentrant_open(pid):
            try: domain.accept_leader_reap(claim)
            except LAB.Refusal: pass
            return pid + 1000
        calls.pidfd_open = reentrant_open
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "UNKNOWN")
        self.assertTrue(receipt["pending_adopted"]["pidfd_opened"])
        self.assertEqual(calls.closed, [1090])

    def test_discovery_receipt_is_frozen_before_continuity_callbacks(self):
        domain, calls, _ = prepared()
        shared = Wait(90)
        calls.discovery = [shared]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 10}
        original_owner = calls.owner_identity
        count = [0]
        def mutate_owner_callback():
            count[0] += 1
            if count[0] >= 2: shared.si_pid = 91
            return original_owner()
        calls.owner_identity = mutate_owner_callback
        calls.pidfd_results = [Wait(90), Wait(90)]
        receipt = domain.step(calls)
        self.assertEqual(receipt["outcome"], "ADOPTED_CHILD_REAPED_CONTINUE")
        self.assertEqual(receipt["adopted_records"][0]["terminal"]["pid"], 90)

    def test_clock_regression_poison(self):
        domain, calls, _ = prepared(); calls.discovery = [None]
        calls.times = iter([0])
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")

    def test_parent_identity_drift_refuses_without_reap(self):
        domain, calls, _ = prepared(); calls.discovery = [Wait(90)]
        calls.identities[90] = {"pid": 90, "starttime": 900,
                                "boot_id": BOOT, "ppid": 77}
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")
        self.assertEqual(calls.pidfd_results, [])

    def test_leader_claim_is_single_use_noncopyable(self):
        domain, calls, claim = prepared()
        with self.assertRaises(LAB.Refusal): copy.copy(domain)
        with self.assertRaises(LAB.Refusal): copy.copy(claim)
        with self.assertRaises(LAB.Refusal): domain.accept_leader_reap(claim)

    def test_continuity_loss_and_deadline_never_drain(self):
        domain, calls, _ = prepared(); calls.tasks = [10, 12]
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")
        domain, calls, _ = prepared(); calls.times = iter([10000])
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")

    def test_record_limit_is_fail_closed(self):
        domain, calls, _ = prepared()
        domain.records = [{}] * LAB.MAX_ADOPTED_RECORDS
        calls.discovery = [Wait(90)]
        self.assertEqual(domain.step(calls)["outcome"], "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
