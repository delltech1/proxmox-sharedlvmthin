import copy
import fcntl
import importlib
import os
from pathlib import Path
import stat
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments/thick-generations"
sys.path.insert(0, str(EXP))

LAB = importlib.import_module("prelive_no_grant_abort_model")
from tests.unit import test_prelive_launcher_identity_consumer as FIXTURE


class Backend:
    model_only_no_grant_abort_backend = True

    def __init__(self):
        self.calls = []
        self.mode = None
        self.controller = None
        self.grant_write_calls = 0
        self.payload_exec_calls = 0
        self.now_ns = FIXTURE.START
        endpoints = []
        for index, role in enumerate(LAB.FD_ROLES):
            pair = index // 2
            flags = os.O_RDONLY if role in LAB.READ_ROLES else os.O_WRONLY
            if role in LAB.NONBLOCK_ROLES:
                flags |= os.O_NONBLOCK
            endpoints.append({
                "role": role, "fd": 10 + index, "dev": 1,
                "inode": 100 + pair, "mode": stat.S_IFIFO | 0o600,
                "flags": flags, "fd_flags": fcntl.FD_CLOEXEC})
        self.graph_descriptor = {
            "schema": 1, "kind": "NO_GRANT_ALLOCATED_FD_GRAPH_V1",
            "request_id": "1" * 32, "graph_id": "2" * 32,
            "supervisor": copy.deepcopy(FIXTURE.OWNER),
            "pre_fork_ticket_id": "3" * 32,
            "phase": "PARENT_PRE_FORK_ALL_ENDPOINTS_OWNED",
            "grant_policy": "CLOSE_ONLY_NO_WRITE",
            "endpoints": endpoints, "endpoint_count": 8,
            "all_cloexec": True, "parent_nonblocking": True}
        self.clock_count = 0
        self.clock_reenter_at = None
        self.clock_mutation = None
        self.last_result = None

    def monotonic_ns_model(self):
        self.calls.append("clock")
        self.clock_count += 1
        if self.clock_count == self.clock_reenter_at:
            try: self.controller.run()
            except LAB.Refusal: pass
        if self.clock_count == 18 and self.clock_mutation:
            if self.clock_mutation == "drop_graph": self.controller.graph = None
            elif self.clock_mutation == "drop_child": self.controller.child = None
            elif self.clock_mutation == "drop_pidfd":
                self.controller.pidfd_token = None
            elif self.clock_mutation == "repair_reap":
                self.last_result["survivor"] = False
        if self.clock_count == 16 and self.clock_mutation == "repair_terminal":
            self.last_result["stdout_eof"] = True
        if self.clock_count == 4 and self.clock_mutation == "lower_watermark":
            self.controller.last_clock_ns = FIXTURE.START
            return FIXTURE.START
        if self.mode == "clock_regress": return self.now_ns - 1
        if self.mode == "clock_expire": return FIXTURE.DEADLINE
        self.now_ns += 1
        return self.now_ns

    def _call(self, name):
        self.calls.append(name)
        if self.mode == "raise_" + name: raise OSError(name)
        if self.mode == "reenter_" + name:
            try: self.controller.run()
            except LAB.Refusal: pass

    def preflight_model(self):
        self._call("preflight")
        result = {"dedicated_wait_domain": True, "unexpected_children": 0,
                  "sigchld_autoreap": False, "subreaper_supported": True,
                  "subreaper_readback": True, "pidfd": True,
                  "waitid_pidfd": True}
        if self.mode == "unexpected_child": result["unexpected_children"] = 1
        return result

    def allocate_fd_graph_model(self, owner):
        self._call("graph")
        descriptor = copy.deepcopy(self.graph_descriptor)
        return {"descriptor": descriptor,
                "fd_graph_digest": FIXTURE.SUPERVISOR.digest(descriptor)}

    def persist_intent_model(self, raw, digest, deadline):
        self._call("intent")
        if self.mode == "joint_graph_snapshot":
            self.controller.graph["descriptor"]["graph_id"] = "8" * 32
            self.controller._graph_bytes = FIXTURE.SUPERVISOR.canonical(
                self.controller.graph)
        result = {"classification": "V2_INTENT_EXACT_BYTES_VERIFIED",
                  "sha256": digest, "exact_bytes_verified": True,
                  "child_started": False, "grant_attempted": False,
                  "exec_proven": False}
        if self.mode == "intent_unknown": result["exact_bytes_verified"] = False
        return result

    def fork_abort_once_model(self, graph, deadline):
        self._call("fork")
        if self.mode == "fork_clear_fail":
            self.controller.child_may_exist = False
            self.controller.attempted_fork = False
            raise OSError("ambiguous fork")
        result = {"pid": 300, "starttime": 400,
                  "boot_id": FIXTURE.BOOT, "fork_token": "4" * 32,
                  "fork_count": 1, "has_exec_branch": False}
        if self.mode == "exec_branch": result["has_exec_branch"] = True
        return result

    def bind_pidfd_model(self, child):
        self._call("pidfd")
        result = {"child": child, "pidfd_token": 50,
                  "observations_match": True, "open_count": 1}
        if self.mode == "identity_drift": result["child"]["pid"] += 1
        return result

    def observe_ready_model(self, child, pidfd, deadline):
        self._call("ready")
        if self.mode == "pidfd_rebind": self.controller.pidfd_token = pidfd + 1
        if self.mode == "callback_watermark":
            self.controller.last_clock_ns = FIXTURE.START
        if self.mode == "joint_child_snapshot":
            self.controller.child["pid"] += 1
            self.controller._child_bytes = FIXTURE.SUPERVISOR.canonical(
                self.controller.child)
        result = {"ready_count": 1,
                  "stdout_canary": LAB.STDOUT_CANARY,
                  "stderr_canary": LAB.STDERR_CANARY,
                  "status_error": False, "payload_exec_calls": 0,
                  "grant_bytes": 0}
        if self.mode == "duplicate_ready": result["ready_count"] = 2
        if self.mode == "ready_exec":
            result["payload_exec_calls"] = 1; self.payload_exec_calls = 1
        return result

    def close_grant_writer_no_write_model(self, child, pidfd):
        self._call("close")
        result = {"bytes_written": 0, "write_calls": 0,
                  "close_attempts": 1, "close_confirmed": True,
                  "writer_copies_remaining": 0}
        if self.mode == "write":
            result["bytes_written"] = 1; result["write_calls"] = 1
            self.grant_write_calls = 1
        if self.mode == "held_writer": result["writer_copies_remaining"] = 1
        return result

    def observe_abort_exit_model(self, child, pidfd, deadline):
        self._call("terminal")
        result = {"grant_bytes_received": 0, "grant_eof": True,
                  "exit_code": LAB.EXPECTED_EXIT, "stdout_eof": True,
                  "stderr_eof": True, "status_eof": True,
                  "payload_exec_calls": 0, "unreaped": True}
        if self.mode == "wrong_exit": result["exit_code"] = 0
        if self.mode == "held_capture": result["stdout_eof"] = False
        if self.mode == "grant_data": result["grant_bytes_received"] = 1
        if self.clock_mutation == "repair_terminal": result["stdout_eof"] = False
        self.last_result = result
        return result

    def reap_exact_model(self, child, pidfd, expected_exit):
        self._call("reap")
        result = {"child": child, "pidfd_token": pidfd,
                  "wnowait_observed": True, "reaped": True,
                  "exit_code": expected_exit, "echild_after_reap": True,
                  "unexpected_children": 0, "survivor": False}
        if self.mode == "no_echild": result["echild_after_reap"] = False
        if self.mode == "survivor": result["survivor"] = True
        if self.clock_mutation == "repair_reap": result["survivor"] = True
        self.last_result = result
        return result


def fixture(descriptor_mutator=None):
    backend = Backend()
    if descriptor_mutator is not None:
        descriptor_mutator(backend.graph_descriptor)
    value = FIXTURE.request()
    value["launcher_identity"]["run_binding"]["fd_graph_digest"] = (
        FIXTURE.SUPERVISOR.digest(backend.graph_descriptor))
    binding = FIXTURE.CONSUMER.bind_v2_request(
        value, FIXTURE.OWNER, FIXTURE.START)
    journal_backend = FIXTURE.JournalBackend()
    session = FIXTURE.CONSUMER.create_v2_journal_session(journal_backend)
    journal_backend.session = session
    FIXTURE.CONSUMER.construct_and_claim_v2_controller(
        binding, session, object())
    chain = FIXTURE.CONSUMER.start_v2_event_chain(binding, session)
    journal_backend.chain = chain
    intent = chain.persist_intent().event_bytes
    controller = LAB.create_no_grant_abort_model(
        backend, value, FIXTURE.OWNER, intent, FIXTURE.START, FIXTURE.DEADLINE)
    backend.controller = controller
    return backend, controller


class NoGrantAbortModelTests(unittest.TestCase):
    def test_complete_trace_has_no_grant_or_exec(self):
        backend, controller = fixture()
        result = controller.run()
        self.assertEqual(result["classification"],
                         "MODEL_NO_GRANT_LAUNCHER_REAPED_CAPTURE_COMPLETE")
        self.assertEqual(result["grant_write_attempts"], 0)
        self.assertEqual(result["grant_bytes_received"], 0)
        self.assertEqual(result["payload_exec_calls"], 0)
        self.assertFalse(result["runtime_authorized"])
        self.assertFalse(result["storage_authorized"])
        self.assertFalse(result["exec_proven"])
        self.assertEqual(controller.state, "COMPLETE")
        self.assertFalse(controller.child_may_exist)
        self.assertEqual([item for item in backend.calls if item != "clock"],
                         ["preflight", "graph", "intent", "fork", "pidfd",
                          "ready", "close", "terminal", "reap"])

    def test_pre_fork_failures_issue_no_fork(self):
        for mode in ("unexpected_child", "intent_unknown", "raise_intent"):
            with self.subTest(mode=mode):
                backend, controller = fixture(); backend.mode = mode
                with self.assertRaises(BaseException): controller.run()
                self.assertNotIn("fork", backend.calls)
                self.assertFalse(controller.child_may_exist)
                self.assertEqual(backend.grant_write_calls, 0)
                self.assertEqual(backend.payload_exec_calls, 0)

    def test_possible_fork_failure_is_unknown_survivor_without_refork(self):
        backend, controller = fixture(); backend.mode = "raise_fork"
        with self.assertRaises(BaseException): controller.run()
        self.assertEqual(backend.calls.count("fork"), 1)
        self.assertTrue(controller.child_may_exist)
        self.assertEqual(controller.state, "UNKNOWN_SURVIVOR")
        with self.assertRaises(BaseException): controller.run()
        self.assertEqual(backend.calls.count("fork"), 1)

    def test_fork_callback_cannot_clear_survivor_latch(self):
        backend, controller = fixture()
        backend.mode = "fork_clear_fail"
        with self.assertRaises(BaseException): controller.run()
        self.assertEqual(controller.state, "UNKNOWN_SURVIVOR")
        self.assertEqual(backend.calls.count("fork"), 1)
        self.assertFalse(controller.child_may_exist)
        with self.assertRaises(BaseException): controller.run()
        self.assertEqual(backend.calls.count("fork"), 1)

    def test_post_fork_fault_matrix_never_returns_success(self):
        modes = ("exec_branch", "identity_drift", "duplicate_ready",
                 "ready_exec", "write", "held_writer", "wrong_exit",
                 "held_capture", "grant_data", "no_echild", "survivor",
                 "raise_pidfd", "raise_ready", "raise_close",
                 "raise_terminal", "raise_reap")
        for mode in modes:
            with self.subTest(mode=mode):
                backend, controller = fixture(); backend.mode = mode
                with self.assertRaises(BaseException): controller.run()
                self.assertNotEqual(controller.state, "COMPLETE")
                self.assertEqual(backend.calls.count("fork"), 1)
                self.assertEqual(backend.calls.count("close"),
                                 1 if "close" in backend.calls else 0)

    def test_reentrancy_is_terminal_and_does_not_duplicate_callback(self):
        for phase in ("preflight", "graph", "intent", "fork", "pidfd",
                      "ready", "close", "terminal", "reap"):
            with self.subTest(phase=phase):
                backend, controller = fixture()
                backend.mode = "reenter_" + phase
                with self.assertRaises(BaseException): controller.run()
                self.assertEqual(backend.calls.count(phase), 1)
                self.assertNotEqual(controller.state, "COMPLETE")

    def test_clock_reentrancy_and_deadline_are_terminal(self):
        for mode in ("clock_regress", "clock_expire"):
            backend, controller = fixture(); backend.mode = mode
            with self.assertRaises(BaseException): controller.run()
            self.assertEqual([x for x in backend.calls if x != "clock"], [])
        for trigger in (1, 18):
            backend, controller = fixture(); backend.clock_reenter_at = trigger
            with self.assertRaises(BaseException): controller.run()
            self.assertNotEqual(controller.state, "COMPLETE")

    def test_graph_ticket_or_pidfd_rebinding_refuses(self):
        backend, controller = fixture()
        backend.graph_descriptor["pre_fork_ticket_id"] = "9" * 32
        with self.assertRaises(BaseException): controller.run()
        self.assertNotIn("fork", backend.calls)

        backend, controller = fixture()
        backend.mode = "pidfd_rebind"
        with self.assertRaises(BaseException): controller.run()
        self.assertNotEqual(controller.state, "COMPLETE")

    def test_graph_endpoint_identity_and_type_drift_refuses_pre_fork(self):
        mutations = (
            lambda graph: graph["endpoints"][0].__setitem__("fd", 11),
            lambda graph: graph["endpoints"][1].__setitem__("inode", 999),
            lambda graph: graph["endpoints"][2].__setitem__("flags",
                                                             os.O_WRONLY),
            lambda graph: graph["endpoints"][3].__setitem__("fd", True),
            lambda graph: graph["endpoints"][4].__setitem__("dev", 1.0),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                backend, controller = fixture()
                mutate(backend.graph_descriptor)
                with self.assertRaises(BaseException): controller.run()
                self.assertNotIn("fork", backend.calls)

    def test_graph_request_owner_and_policy_drift_refuses_pre_fork(self):
        changes = (
            ("request_id", "9" * 32),
            ("pre_fork_ticket_id", "9" * 32),
            ("phase", "CHILD_RUNNING"),
            ("grant_policy", "WRITE_ONCE"),
        )
        for key, value in changes:
            with self.subTest(key=key):
                backend, controller = fixture()
                backend.graph_descriptor[key] = value
                with self.assertRaises(BaseException): controller.run()
                self.assertNotIn("fork", backend.calls)

    def test_semantically_invalid_graph_refuses_with_fresh_binding(self):
        mutations = (
            lambda graph: graph["endpoints"][0].__setitem__(
                "flags", graph["endpoints"][0]["flags"] | os.O_ASYNC),
            lambda graph: graph["endpoints"][0].__setitem__(
                "fd_flags", fcntl.FD_CLOEXEC | 0x40000000),
            lambda graph: graph["supervisor"].__setitem__("pid", True),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                backend, controller = fixture(mutate)
                with self.assertRaises(BaseException): controller.run()
                self.assertNotIn("fork", backend.calls)

    def test_last_clock_cannot_delete_enrolled_identities(self):
        for mutation in ("drop_graph", "drop_child", "drop_pidfd"):
            backend, controller = fixture(); backend.clock_mutation = mutation
            with self.assertRaises(BaseException): controller.run()
            self.assertNotEqual(controller.state, "COMPLETE")

    def test_later_clock_cannot_repair_negative_result(self):
        for mutation in ("repair_terminal", "repair_reap"):
            backend, controller = fixture(); backend.clock_mutation = mutation
            with self.assertRaises(BaseException): controller.run()
            self.assertNotEqual(controller.state, "COMPLETE")

    def test_clock_callback_cannot_lower_watermark(self):
        backend, controller = fixture()
        backend.clock_mutation = "lower_watermark"
        with self.assertRaises(BaseException): controller.run()
        self.assertEqual(controller.state, "UNKNOWN")

        backend, controller = fixture(); backend.mode = "callback_watermark"
        with self.assertRaises(BaseException): controller.run()
        self.assertNotEqual(controller.state, "COMPLETE")

    def test_callback_cannot_jointly_rebind_child_and_frozen_snapshot(self):
        for mode in ("joint_child_snapshot", "joint_graph_snapshot"):
            backend, controller = fixture(); backend.mode = mode
            with self.assertRaises(BaseException): controller.run()
            self.assertNotEqual(controller.state, "COMPLETE")

    def test_custom_result_refuses_without_callback(self):
        calls = []
        class EvilDict(dict):
            def items(self): calls.append("items"); return super().items()
        backend, controller = fixture()
        real = backend.preflight_model
        def evil(): return EvilDict(real())
        backend.preflight_model = evil
        with self.assertRaises(BaseException): controller.run()
        self.assertEqual(calls, [])
        self.assertEqual(controller.state, "UNKNOWN")

    def test_source_has_no_live_or_grant_write_surface(self):
        source = (EXP / "prelive_no_grant_abort_model.py").read_text()
        for forbidden in ("os.fork", "execve", "write_grant", "subprocess",
                          "dmsetup", "lvcreate", "pvesm"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
