import copy
import importlib
import os
from pathlib import Path
import sys
import threading
import unittest


EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
SUP = importlib.import_module("prelive_supervisor_model")
OWNED = importlib.import_module("prelive_owned_child")
LAB = importlib.import_module("prelive_pregrant_composition")

BOOT = "12345678-1234-1234-1234-123456789abc"
OWNER = {"pid": 100, "starttime": 200, "boot_id": BOOT}
EXE = {"path": "/usr/bin/true", "sha256": "a" * 64,
       "dev": 10, "inode": 20}
LAUNCHER = {"path": "/usr/bin/true", "sha256": "b" * 64,
            "dev": 11, "inode": 21}


def request():
    return {"schema": 1, "request_id": "c" * 32,
            "purpose": "DISPOSABLE_KERNEL_LAB", "boot_id": BOOT,
            "argv": ["/usr/bin/true"], "environment": dict(SUP.ENVIRONMENT),
            "executable": copy.deepcopy(EXE), "launcher": copy.deepcopy(LAUNCHER),
            "timeout_ms": 5000, "cleanup_ms": 1000, "capture_limit": 4096,
            "signal_policy": "NONE", "storage_authorized": False,
            "postcondition_verified": False}


class JournalBackend:
    model_only = True
    def __init__(self):
        self.events = []
        self.fail_kind = None
        self.after_store = False
        self.hook = None
    def persist_model(self, raw, event):
        if self.hook: self.hook(event)
        if self.fail_kind == event["kind"] and not self.after_store:
            raise OSError("persist failed")
        self.events.append((bytes(raw), copy.deepcopy(event)))
        if self.fail_kind == event["kind"] and self.after_store:
            raise OSError("persist ambiguous")
        return {"bytes_written": len(raw), "file_synced": True,
                "dir_synced": True}


class LauncherBackend:
    model_only = True
    def __init__(self):
        self.times = iter(range(0, 10000000000, 1000000))
        self.owner = copy.deepcopy(OWNER)
        self.prepare_calls = 0
        self.grant_calls = 0
        self.prepare_hook = None
        self.grant_hook = None
        self.grant_result = None
        self.recheck_result = None
        self.recheck_hook = None
        self.serialized_child_pid = 300
        self.owned_calls = None
    def monotonic_ns_model(self): return next(self.times)
    def owner_identity_model(self): return copy.deepcopy(self.owner)
    def prepare_bind_unarmed_model(self, req, owner, ticket, lifecycle, channel):
        self.prepare_calls += 1
        if self.prepare_hook: self.prepare_hook(req, owner, ticket)
        class Calls:
            def __init__(self):
                self.starts = [400, 400]
                self.launchers = [{"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                                   "exe_dev": req["launcher"]["dev"],
                                   "exe_inode": req["launcher"]["inode"],
                                   "cmdline_sha256": req["launcher"]["sha256"]}] * 2
            def current_identity(self): return copy.deepcopy(owner)
            def starttime(self, _pid): return self.starts.pop(0)
            def launcher(self, _pid): return copy.deepcopy(self.launchers.pop(0))
            def pidfd_open(self, _pid): return 50
            def probe_waitable(self, _pidfd): return None
            def waitid(self, _pidfd, _options): return None
            def close(self, _fd): pass
        self.owned_calls = Calls()
        recorded = OWNED._record_owned_fork(
            OWNED._FORK_KEY, 300, owner, "d" * 32, ticket)
        lifecycle.update(recorded)
        expected = {"kind": "UNARMED_LAUNCHER_NOT_PAYLOAD",
                    "exe_dev": req["launcher"]["dev"],
                    "exe_inode": req["launcher"]["inode"],
                    "cmdline_sha256": req["launcher"]["sha256"]}
        owned = OWNED.bind_owned_pidfd(lifecycle, expected, self.owned_calls)
        adapter = OWNED.bind_capture_child_adapter(owned, self.owned_calls)
        child = {"request_id": req["request_id"], "boot_id": req["boot_id"],
                 "pid": self.serialized_child_pid, "starttime": 400,
                 "owner_pid": owner["pid"],
                 "owner_starttime": owner["starttime"],
                 "launcher_sha256": req["launcher"]["sha256"], "armed": False}
        return {"child": child, "launcher": copy.deepcopy(req["launcher"]),
                "owned_handle": owned, "capture_adapter": adapter,
                "fork_origin": lifecycle["origin"], "grant_channel": channel,
                "readiness": "R"}
    def recheck_unarmed_model(self, binding, channel):
        if self.recheck_hook: self.recheck_hook(binding, channel)
        return (self.recheck_result if self.recheck_result is not None else
                {"launcher_alive": binding.pidfd_handle.state == "BOUND",
                 "pidfd_bound": binding.pidfd_handle.pidfd == 50,
                 "channel_exact": channel is binding.grant_channel
                                  and channel.writer_open is True,
                 "readiness": "R"})
    def grant_once_model(self, binding, permit, channel):
        self.grant_calls += 1
        if self.grant_hook: self.grant_hook(binding, permit, channel)
        return (self.grant_result if self.grant_result is not None else
                {"bytes_written": 1, "writer_eof": True, "channel": channel})


def prepared():
    journal = JournalBackend(); launcher = LauncherBackend()
    gate = LAB.prepare_pregrant_model(request(), OWNER, journal, launcher)
    return gate, journal, launcher


def bound():
    gate, journal, launcher = prepared()
    gate.persist_intent(); gate.prepare_and_bind_unarmed()
    return gate, journal, launcher


class PreGrantCompositionTests(unittest.TestCase):
    def test_exact_three_event_chain_then_one_grant(self):
        gate, journal, launcher = bound()
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"],
                         "MODEL_GRANT_PROTOCOL_COMPLETED_EXEC_UNPROVEN")
        self.assertEqual([event[1]["kind"] for event in journal.events],
                         list(LAB.EVENT_ORDER))
        self.assertEqual(launcher.prepare_calls, 1)
        self.assertEqual(launcher.grant_calls, 1)
        self.assertTrue(gate.permit.used)
        self.assertEqual(gate.journal.state, "SEALED_ACTIVE")
        self.assertFalse(result["exec_proven"])

    def test_intent_failure_prevents_launcher_and_grant(self):
        for after in (False, True):
            gate, journal, launcher = prepared()
            journal.fail_kind = "INTENT"; journal.after_store = after
            self.assertEqual(gate.persist_intent()["classification"], "UNKNOWN")
            self.assertEqual(launcher.prepare_calls, 0)
            self.assertEqual(launcher.grant_calls, 0)

    def test_child_bound_or_exec_persist_failure_never_grants(self):
        for kind in ("CHILD_BOUND", "EXEC_ISSUED"):
            for after in (False, True):
                gate, journal, launcher = prepared()
                gate.persist_intent()
                journal.fail_kind = kind; journal.after_store = after
                if kind == "CHILD_BOUND": gate.prepare_and_bind_unarmed()
                else:
                    gate.prepare_and_bind_unarmed(); gate.issue_grant_once("f" * 32)
                self.assertEqual(gate.state, "UNKNOWN")
                self.assertEqual(launcher.grant_calls, 0)
                self.assertTrue(gate.settlement_required)

    def test_grant_side_effect_then_error_is_single_attempt_unknown(self):
        gate, journal, launcher = bound()
        def ambiguous(_binding, _permit, _channel):
            raise OSError("after write")
        launcher.grant_hook = ambiguous
        first = gate.issue_grant_once("f" * 32)
        self.assertEqual(first["classification"], "UNKNOWN")
        self.assertTrue(first["grant_attempted"])
        self.assertEqual(launcher.grant_calls, 1)
        self.assertEqual(gate.issue_grant_once("f" * 32)["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 1)

    def test_append_during_grant_is_blocked_without_backend_call(self):
        gate, journal, launcher = bound()
        before = len(journal.events)
        def attack(_binding, _permit, _channel):
            with self.assertRaises(LAB.Refusal):
                gate.journal.persist("INTENT", {}, gate)
        launcher.grant_hook = attack
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(len(journal.events), before + 1)
        self.assertEqual(launcher.grant_calls, 1)

    def test_copied_journal_cannot_bypass_active_seal(self):
        gate, journal, launcher = bound()
        with self.assertRaises(LAB.Refusal): copy.copy(gate.journal)
        with self.assertRaises(LAB.Refusal): copy.deepcopy(gate.journal)

    def test_nested_controller_operation_poisons_outer_callback(self):
        gate, journal, launcher = prepared()
        def nested(_event): gate.persist_intent()
        journal.hook = nested
        self.assertEqual(gate.persist_intent()["classification"], "UNKNOWN")
        self.assertEqual(launcher.prepare_calls, 0)

    def test_slow_persist_expires_unarmed_deadline_before_grant(self):
        gate, journal, launcher = bound()
        # EXEC_ISSUED returns, then the fresh pre-grant clock is already expired.
        launcher.times = iter([gate.unarmed_deadline_ns])
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 0)

    def test_deadline_after_persisted_exec_event_never_grants(self):
        gate, journal, launcher = bound()
        def expire_after_exec(event):
            if event["kind"] == "EXEC_ISSUED":
                launcher.times = iter([gate.unarmed_deadline_ns])
        journal.hook = expire_after_exec
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(journal.events[-1][1]["kind"], "EXEC_ISSUED")
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 0)

    def test_exec_event_persists_then_fresh_recheck_refuses(self):
        gate, journal, launcher = bound()
        launcher.recheck_result = {"launcher_alive": False,
                                   "pidfd_bound": True,
                                   "channel_exact": True, "readiness": "R"}
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(journal.events[-1][1]["kind"], "EXEC_ISSUED")
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 0)

    def test_grant_callback_reentrancy_is_one_attempt(self):
        gate, journal, launcher = bound()
        def nested(_binding, _permit, _channel):
            gate.issue_grant_once("1" * 32)
        launcher.grant_hook = nested
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 1)

    def test_mutated_callback_inputs_do_not_change_frozen_request(self):
        gate, journal, launcher = prepared()
        def mutate(event):
            event["payload"].setdefault("request", {}).setdefault("argv", []).append("bad")
        journal.hook = mutate
        gate.persist_intent()
        self.assertEqual(gate.request["argv"], ["/usr/bin/true"])
        self.assertEqual(gate.intent.kind, "INTENT")

    def test_bad_types_copied_permit_and_wrong_channel_refuse(self):
        gate, journal, launcher = bound()
        launcher.grant_result = {"bytes_written": True, "writer_eof": True,
                                 "channel": object()}
        self.assertEqual(gate.issue_grant_once("f" * 32)["classification"], "UNKNOWN")
        with self.assertRaises(LAB.Refusal): copy.copy(gate.permit)

    def test_foreign_owner_after_child_requires_settlement(self):
        gate, journal, launcher = bound()
        launcher.owner["starttime"] += 1
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertTrue(result["settlement_required"])
        self.assertEqual(launcher.grant_calls, 0)

    def test_grant_callback_cannot_mutate_binding_or_rearm_permit(self):
        for attack in ("binding", "permit", "attempt", "channel"):
            gate, journal, launcher = bound()
            def mutate(binding, permit, _channel, selected=attack):
                if selected == "binding": binding.lifecycle_token = "0" * 32
                elif selected == "permit": permit.used = False
                elif selected == "attempt": permit.attempt_id = "0" * 32
                else: binding.grant_channel = object()
            launcher.grant_hook = mutate
            result = gate.issue_grant_once("f" * 32)
            self.assertEqual(result["classification"], "UNKNOWN")
            self.assertEqual(launcher.grant_calls, 1)
            self.assertTrue(result["grant_attempted"])

    def test_serialized_child_must_match_exact_owned_handle(self):
        gate, journal, launcher = prepared(); gate.persist_intent()
        launcher.serialized_child_pid = 301
        result = gate.prepare_and_bind_unarmed()
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 0)
        self.assertTrue(result["settlement_required"])
        self.assertEqual(result["owned_child_evidence"]["pid"], 300)

    def test_poisoned_owned_handle_during_recheck_never_grants(self):
        gate, journal, launcher = bound()
        def poison(binding, _channel):
            binding.pidfd_handle.state = "UNKNOWN_OBSERVE_OUTCOME"
            binding.pidfd_handle.lifecycle["state"] = "UNKNOWN_OBSERVE_OUTCOME"
        launcher.recheck_hook = poison
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 0)
        self.assertTrue(result["settlement_required"])

    def test_poisoned_owned_handle_during_grant_is_one_attempt_unknown(self):
        gate, journal, launcher = bound()
        def poison(binding, _permit, _channel):
            binding.pidfd_handle.state = "UNKNOWN_OBSERVE_OUTCOME"
            binding.pidfd_handle.lifecycle["state"] = "UNKNOWN_OBSERVE_OUTCOME"
        launcher.grant_hook = poison
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 1)
        self.assertTrue(result["grant_attempted"])
        self.assertTrue(result["settlement_required"])

    def test_changed_owned_identity_before_grant_never_grants(self):
        gate, journal, launcher = bound()
        def alter_identity(binding, _channel):
            binding.pidfd_handle.child["pid"] = 301
        launcher.recheck_hook = alter_identity
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 0)
        self.assertTrue(result["settlement_required"])

    def test_changed_owned_identity_during_grant_is_attempted_unknown(self):
        gate, journal, launcher = bound()
        def alter_identity(binding, _permit, _channel):
            binding.pidfd_handle.pidfd = 51
        launcher.grant_hook = alter_identity
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 1)
        self.assertTrue(result["grant_attempted"])
        self.assertTrue(result["settlement_required"])

    def test_float_owned_identity_before_grant_never_grants(self):
        for field in ("pid", "pidfd"):
            gate, journal, launcher = bound()
            def alter_type(binding, _channel, selected=field):
                if selected == "pid":
                    binding.pidfd_handle.child["pid"] = 300.0
                else:
                    binding.pidfd_handle.pidfd = 50.0
            launcher.recheck_hook = alter_type
            result = gate.issue_grant_once("f" * 32)
            self.assertEqual(result["classification"], "UNKNOWN")
            self.assertEqual(launcher.grant_calls, 0)

    def test_float_pidfd_during_grant_is_attempted_unknown(self):
        gate, journal, launcher = bound()
        def alter_type(binding, _permit, _channel):
            binding.pidfd_handle.pidfd = 50.0
        launcher.grant_hook = alter_type
        result = gate.issue_grant_once("f" * 32)
        self.assertEqual(result["classification"], "UNKNOWN")
        self.assertEqual(launcher.grant_calls, 1)
        self.assertTrue(result["grant_attempted"])

    def test_wrong_order_and_reused_controller_never_progress(self):
        gate, journal, launcher = prepared()
        self.assertEqual(gate.prepare_and_bind_unarmed()["classification"], "UNKNOWN")
        self.assertEqual(gate.persist_intent()["classification"], "UNKNOWN")
        self.assertEqual(launcher.prepare_calls, 0)

    def test_launcher_side_effect_then_error_latches_possible_child(self):
        for malformed in (False, True):
            gate, journal, launcher = prepared(); gate.persist_intent()
            original = launcher.prepare_bind_unarmed_model
            def fail(*args):
                if malformed:
                    value = original(*args); value["readiness"] = False; return value
                original(*args)
                raise OSError("after launcher creation")
            launcher.prepare_bind_unarmed_model = fail
            result = gate.prepare_and_bind_unarmed()
            self.assertEqual(result["classification"], "UNKNOWN")
            self.assertTrue(result["child_may_exist"])
            self.assertTrue(result["settlement_required"])
            self.assertTrue(result["child_identity_known"])
            self.assertEqual(result["owned_child_evidence"]["pid"], 300)

    def test_event_specific_payload_refuses_even_in_correct_order(self):
        gate, journal, launcher = prepared()
        gate.state = "PERSISTING_INTENT"
        with self.assertRaises(LAB.Refusal):
            gate.journal.persist("INTENT", {}, gate)

    def test_foreign_thread_attempt_monotonically_poisons_controller(self):
        gate, journal, launcher = prepared()
        failures = []
        def foreign():
            try: gate.persist_intent()
            except BaseException as exc: failures.append(exc)
        worker = threading.Thread(target=foreign)
        worker.start(); worker.join()
        self.assertEqual(gate.state, "UNKNOWN")
        self.assertEqual(len(journal.events), 0)
        self.assertEqual(launcher.prepare_calls, 0)


if __name__ == "__main__":
    unittest.main()
