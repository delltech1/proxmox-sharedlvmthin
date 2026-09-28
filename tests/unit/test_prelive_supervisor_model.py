import copy
import hashlib
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/prelive_supervisor_model.py"
SPEC = importlib.util.spec_from_file_location("prelive_supervisor_model", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


BOOT = "11111111-2222-3333-4444-555555555555"
EMPTY = hashlib.sha256(b"").hexdigest()


def request():
    return {
        "schema": 1, "request_id": "a" * 32,
        "purpose": "RECOVERY_READONLY_PROBE", "boot_id": BOOT,
        "argv": ["/usr/bin/true"], "environment": dict(LAB.ENVIRONMENT),
        "executable": {"path": "/usr/bin/true", "sha256": "b" * 64,
                       "dev": 8, "inode": 42},
        "launcher": {"path": "/usr/libexec/slt-launcher", "sha256": "c" * 64,
                     "dev": 8, "inode": 43},
        "timeout_ms": 1000, "cleanup_ms": 100, "capture_limit": 4096,
        "signal_policy": "NONE", "storage_authorized": False,
        "postcondition_verified": False,
    }


class Journal:
    def __init__(self):
        self.events = []
        self.fail_kind = None

    def append(self, event):
        if event["kind"] == self.fail_kind:
            raise OSError("journal failure")
        self.events.append(copy.deepcopy(event))


class Backend:
    model_only = True

    def __init__(self, journal):
        self.journal = journal
        self.transform = {}
        self.calls = []

    def _value(self, name, value):
        self.calls.append(name)
        transform = self.transform.get(name)
        return transform(copy.deepcopy(value)) if transform else value

    def capabilities_model(self):
        return self._value("capabilities", {"pidfd": True, "pidfd_waitid": True,
            "pidfd_signal": True, "subreaper": True, "nonblocking_capture": True})

    def enable_subreaper_model(self, _owner):
        return self._value("subreaper", {"before": False, "after": True,
                                          "process_local": True})

    def prepare_unarmed_model(self, req, owner):
        self.assert_last("INTENT")
        return self._value("prepare", {"request_id": req["request_id"],
            "boot_id": req["boot_id"], "pid": 200, "starttime": 300,
            "owner_pid": owner["pid"], "owner_starttime": owner["starttime"],
            "launcher_sha256": "c" * 64, "armed": False})

    def observe_launcher_model(self, child):
        return self._value("observe", child)

    def bind_pidfd_model(self, child):
        return self._value("bind", {"child": child, "token": 10})

    def grant_exec_model(self, child, _token, req):
        self.assert_last("EXEC_ISSUED")
        return self._value("grant", {"request_id": req["request_id"],
                                      "bytes_written": 1, "grant_eof": True})

    def observe_exec_model(self, child, _token, req):
        value = {"pid": child["pid"], "starttime": child["starttime"],
            "boot_id": child["boot_id"], "request_id": req["request_id"],
            "argv": req["argv"], "environment_sha256": LAB.digest(req["environment"]),
            "executable": req["executable"], "armed": True}
        return self._value("exec", value)

    def monitor_model(self, command, token, req):
        # EXEC_ISSUED remains the last durable event throughout monitoring.
        self.assert_last("EXEC_ISSUED")
        capture = {"eof": True, "truncated": False, "bytes": 0, "sha256": EMPTY}
        value = {"command": command, "pidfd_token": token, "terminal": "EXITED",
            "unreaped": True, "returncode": 0, "deadline_exceeded": False,
            "survivor": False, "descendants": [], "descendants_complete": True,
            "stdout": capture, "stderr": capture,
            "elapsed_ms": req["timeout_ms"] - 1}
        return self._value("monitor", value)

    def reap_exact_model(self, command, token, remaining_ms):
        value = {"command": command, "pidfd_token": token, "reaped": True,
                 "returncode": 0, "echild_after_reap": True,
                 "elapsed_ms": remaining_ms}
        return self._value("reap", value)

    def settle_unknown_model(self, child, token, req, cleanup_ms):
        value = {"child": child, "pidfd_token": token,
                 "cleanup_elapsed_ms": cleanup_ms,
                 "signal_policy": req["signal_policy"], "signal_sent": None,
                 "terminal": False, "reaped": False,
                 "echild_after_reap": False, "survivor": True}
        return self._value("settle", value)

    def assert_last(self, kind):
        if not self.journal.events or self.journal.events[-1]["kind"] != kind:
            raise AssertionError(f"expected last journal event {kind}")


class SupervisorModelTests(unittest.TestCase):
    def make(self):
        journal = Journal()
        backend = Backend(journal)
        owner = {"pid": 100, "starttime": 101, "boot_id": BOOT}
        return LAB.SupervisorModel(journal, backend, owner), journal, backend

    def test_clean_terminal_result_never_authorizes_storage(self):
        model, journal, backend = self.make()
        result = model.run(request())
        self.assertEqual(result["classification"], "PROCESS_TERMINAL_EXIT0")
        self.assertFalse(result["storage_authorized"])
        self.assertFalse(result["postcondition_verified"])
        self.assertFalse(result["runtime_authorized"])
        self.assertEqual([e["kind"] for e in journal.events],
                         ["INTENT", "CHILD_BOUND", "EXEC_ISSUED", "PROCESS_TERMINAL"])
        self.assertEqual(model.state, "TERMINAL")

    def test_intent_failure_prevents_prepare_and_grant(self):
        model, journal, backend = self.make()
        journal.fail_kind = "INTENT"
        with self.assertRaises(OSError):
            model.run(request())
        self.assertNotIn("prepare", backend.calls)
        self.assertNotIn("grant", backend.calls)
        self.assertEqual(model.state, "UNKNOWN")

    def test_binding_or_grant_ambiguity_is_single_use_unknown(self):
        for phase, transform in (
            ("bind", lambda value: {**value, "token": 0}),
            ("grant", lambda value: {**value, "bytes_written": 0}),
        ):
            model, journal, backend = self.make()
            backend.transform[phase] = transform
            with self.assertRaises(LAB.Refusal):
                model.run(request())
            self.assertEqual(model.state, "UNKNOWN")
            with self.assertRaises(LAB.Refusal):
                model.run(request())

    def test_no_journal_write_occurs_inside_monitor(self):
        model, journal, backend = self.make()
        def inspect(value):
            self.assertEqual(journal.events[-1]["kind"], "EXEC_ISSUED")
            return value
        backend.transform["monitor"] = inspect
        model.run(request())

    def test_terminal_requires_echild_complete_capture_and_exact_reap(self):
        cases = (
            ("monitor", lambda value: {**value, "descendants": [201]}),
            ("monitor", lambda value: {**value, "survivor": True}),
            ("monitor", lambda value: {**value,
                "stdout": {**value["stdout"], "eof": False}}),
            ("monitor", lambda value: {**value, "deadline_exceeded": True}),
            ("reap", lambda value: {**value, "reaped": False}),
            ("reap", lambda value: {**value, "echild_after_reap": False}),
            ("reap", lambda value: {**value, "elapsed_ms": 1001}),
        )
        for phase, transform in cases:
            model, _, backend = self.make()
            backend.transform[phase] = transform
            with self.assertRaises(LAB.Refusal):
                model.run(request())
            self.assertEqual(model.state, "UNKNOWN")

    def test_callback_mutation_and_wrong_exec_identity_refuse(self):
        supplied = request()
        model, _, backend = self.make()
        backend.transform["prepare"] = lambda value: (
            supplied["argv"].append("mutated") or value
        )
        result = model.run(supplied)
        self.assertEqual(result["classification"], "PROCESS_TERMINAL_EXIT0")
        self.assertEqual(result["command"]["argv"], ["/usr/bin/true"])

        model, _, backend = self.make()
        backend.transform["exec"] = lambda value: {**value, "argv": ["/bin/false"]}
        with self.assertRaises(LAB.Refusal):
            model.run(request())

    def test_backend_cannot_mutate_frozen_request_owner_or_child(self):
        model, journal, backend = self.make()
        original_prepare = backend.prepare_unarmed_model
        def malicious_prepare(req, owner):
            value = original_prepare(req, owner)
            req["argv"].append("foreign")
            owner["pid"] = 999
            value["owner_pid"] = 100
            return value
        backend.prepare_unarmed_model = malicious_prepare
        original_grant = backend.grant_exec_model
        def malicious_grant(child, token, req):
            value = original_grant(child, token, req)
            child["pid"] = 999
            req["environment"]["PATH"] = "/tmp"
            return value
        backend.grant_exec_model = malicious_grant
        result = model.run(request())
        self.assertEqual(result["command"]["argv"], ["/usr/bin/true"])
        intent = journal.events[0]
        self.assertEqual(intent["request"]["argv"], ["/usr/bin/true"])
        self.assertEqual(intent["owner"]["pid"], 100)

    def test_request_schema_types_and_capabilities_are_closed(self):
        invalid = (
            {"storage_authorized": True}, {"timeout_ms": True},
            {"argv": ["/bin/other"]}, {"foreign": True},
            {"environment": {"PATH": "/tmp", "LD_PRELOAD": "/tmp/x"}},
        )
        for change in invalid:
            value = request()
            value.update(change)
            with self.assertRaises(LAB.Refusal):
                LAB.validate_request(value)
        model, _, backend = self.make()
        backend.transform["capabilities"] = lambda value: {**value, "pidfd": False}
        with self.assertRaises(LAB.Refusal):
            model.run(request())

    def test_strict_types_launcher_pin_and_settlement_before_unknown_journal(self):
        strict = (
            ("capabilities", lambda value: {**value, "pidfd": 1}),
            ("subreaper", lambda value: {**value, "after": 1}),
            ("grant", lambda value: {**value, "bytes_written": True}),
            ("reap", lambda value: {**value, "returncode": False}),
            ("reap", lambda value: {**value, "reaped": 1}),
            ("reap", lambda value: {**value, "echild_after_reap": 1}),
        )
        for phase, transform in strict:
            model, journal, backend = self.make()
            backend.transform[phase] = transform
            with self.assertRaises(LAB.Refusal):
                model.run(request())
            if "grant" in backend.calls:
                self.assertIn("settle", backend.calls)
                self.assertEqual(journal.events[-1]["kind"], "UNKNOWN_PRESERVE")

        value = request()
        value["schema"] = True
        with self.assertRaises(LAB.Refusal):
            LAB.validate_request(value)
        model, _, backend = self.make()
        backend.transform["prepare"] = lambda value: {
            **value, "launcher_sha256": "d" * 64}
        with self.assertRaises(LAB.Refusal):
            model.run(request())

    def test_post_grant_error_settles_before_any_failure_journal(self):
        model, journal, backend = self.make()
        backend.transform["exec"] = lambda value: {**value, "argv": ["/bin/false"]}
        def settlement(value):
            self.assertEqual(journal.events[-1]["kind"], "EXEC_ISSUED")
            return value
        backend.transform["settle"] = settlement
        with self.assertRaises(LAB.Refusal):
            model.run(request())
        self.assertLess(backend.calls.index("settle"), len(backend.calls))
        self.assertEqual(journal.events[-1]["kind"], "UNKNOWN_PRESERVE")

    def test_child_exists_then_journal_failure_settles_before_unknown_record(self):
        for failed_kind in ("CHILD_BOUND", "EXEC_ISSUED"):
            model, journal, backend = self.make()
            journal.fail_kind = failed_kind
            original_settle = backend.settle_unknown_model
            def settle(child, token, req, budget):
                expected = "INTENT" if failed_kind == "CHILD_BOUND" else "CHILD_BOUND"
                self.assertEqual(journal.events[-1]["kind"], expected)
                journal.fail_kind = None
                return original_settle(child, token, req, budget)
            backend.settle_unknown_model = settle
            with self.assertRaises(OSError):
                model.run(request())
            self.assertIn("settle", backend.calls)
            self.assertEqual(journal.events[-1]["kind"], "UNKNOWN_PRESERVE")

    def test_nested_identity_numeric_type_smuggling_refuses(self):
        cases = (
            ("prepare", lambda value: {**value, "owner_pid": 100.0}),
            ("exec", lambda value: {**value, "pid": 200.0}),
            ("exec", lambda value: {**value, "starttime": 300.0}),
            ("exec", lambda value: {**value, "armed": 1}),
            ("exec", lambda value: {**value,
                "executable": {**value["executable"], "dev": 8.0}}),
            ("monitor", lambda value: {**value, "pidfd_token": 10.0}),
            ("reap", lambda value: {**value,
                "command": {**value["command"], "pid": 200.0}}),
        )
        for phase, transform in cases:
            model, _, backend = self.make()
            backend.transform[phase] = transform
            with self.assertRaises(LAB.Refusal):
                model.run(request())
            self.assertEqual(model.state, "UNKNOWN")

        model, journal, backend = self.make()
        backend.transform["exec"] = lambda value: {**value, "armed": False}
        backend.transform["settle"] = lambda value: {
            **value, "pidfd_token": 10.0}
        with self.assertRaises(LAB.Refusal):
            model.run(request())
        settlement = journal.events[-1]["settlement"]
        self.assertEqual(settlement["classification"], "SETTLEMENT_UNKNOWN")
        self.assertTrue(settlement["survivor"])


if __name__ == "__main__":
    unittest.main()
