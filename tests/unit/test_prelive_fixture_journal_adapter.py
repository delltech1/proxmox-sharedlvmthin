import copy
import importlib
import json
from pathlib import Path
import sys
import threading
import unittest


EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
SUP = importlib.import_module("prelive_supervisor_model")
LAB = importlib.import_module("prelive_fixture_journal_adapter")

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


class Backend:
    source_only = True
    def __init__(self):
        self.calls = []
        self.hook = None
        self.ack = None
    def persist_exact_file(self, name, raw):
        self.calls.append((name, bytes(raw)))
        if self.hook: self.hook(name, raw)
        return self.ack if self.ack is not None else {
            "bytes_written": len(raw), "file_synced": True,
            "dir_synced": True, "closed": True, "name": name,
            "sha256": LAB.hashlib.sha256(raw).hexdigest(),
            "record_identity": {"dev": 1, "inode": len(self.calls), "uid": 0,
                                "mode": 0o600, "nlink": 1}}


def canonical_event(sequence, kind, previous, payload):
    return SUP.canonical({"schema": 1, "request_id": "c" * 32,
                          "sequence": sequence, "kind": kind,
                          "previous_digest": previous, "payload": payload})


def setup_session():
    backend = Backend(); controller = object()
    session = LAB.create_source_session("c" * 32, OWNER, backend)
    session.claim(controller)
    return session, backend, controller


def append_intent(session, controller):
    req = request()
    raw = canonical_event(1, "INTENT", "0" * 64, {
        "request": req, "request_sha256": SUP.digest(req), "owner": OWNER,
        "unarmed_deadline_ns": 5000000000, "pre_fork_ticket_bound": True})
    return session.append_exact_event(raw, controller)


def append_child(session, controller):
    child = {"request_id": "c" * 32, "boot_id": BOOT, "pid": 300,
             "starttime": 400, "owner_pid": 100, "owner_starttime": 200,
             "launcher_sha256": "b" * 64, "armed": False}
    raw = canonical_event(2, "CHILD_BOUND", session.last_digest, {
        "intent_digest": session.last_digest, "child": child,
        "lifecycle_token": "d" * 32, "launcher": LAUNCHER,
        "channel_identity": {"token": "e" * 32, "writer_open": True,
                             "reader_owned": True}})
    return session.append_exact_event(raw, controller)


def append_exec(session, controller):
    raw = canonical_event(3, "EXEC_ISSUED", session.last_digest, {
        "intent_digest": session.receipts[0].digest,
        "child_bound_digest": session.last_digest, "attempt_id": "f" * 32,
        "grant_protocol": "ONE_BYTE_G_THEN_WRITER_EOF"})
    return session.append_exact_event(raw, controller)


class ExactJournalAdapterTests(unittest.TestCase):
    def test_exact_chain_and_seal(self):
        session, backend, controller = setup_session()
        append_intent(session, controller); append_child(session, controller)
        append_exec(session, controller); seal = session.seal_pregrant(controller)
        self.assertEqual([call[0] for call in backend.calls], [
            "exact-event-000001.json", "exact-event-000002.json",
            "exact-event-000003.json"])
        self.assertEqual(seal.final_digest, session.last_digest)
        self.assertEqual(session.snapshot()["classification"],
                         "SOURCE_EXACT_BYTES_AND_ORDER_SEALED")
        self.assertFalse(session.snapshot()["grant_authorized"])

    def test_ack_ambiguity_is_unknown_without_receipt(self):
        for field, value in (("bytes_written", 0), ("bytes_written", True),
                             ("file_synced", False), ("dir_synced", False),
                             ("closed", False), ("name", "wrong"),
                             ("sha256", "0" * 64)):
            session, backend, controller = setup_session()
            raw = canonical_event(1, "INTENT", "0" * 64, {
                "request": request(), "request_sha256": SUP.digest(request()),
                "owner": OWNER, "unarmed_deadline_ns": 5000000000,
                "pre_fork_ticket_bound": True})
            good = {"bytes_written": len(raw), "file_synced": True,
                    "dir_synced": True, "closed": True,
                    "name": "exact-event-000001.json",
                    "sha256": LAB.hashlib.sha256(raw).hexdigest(),
                    "record_identity": {"dev": 1, "inode": 1, "uid": 0,
                                        "mode": 0o600, "nlink": 1}}
            good[field] = value; backend.ack = good
            with self.assertRaises(LAB.Refusal): append_intent(session, controller)
            self.assertEqual(session.state, "UNKNOWN")
            self.assertEqual(session.receipts, [])

    def test_backend_side_effect_then_error_is_unknown_no_retry(self):
        session, backend, controller = setup_session()
        backend.hook = lambda *_: (_ for _ in ()).throw(OSError("after write"))
        with self.assertRaises(OSError): append_intent(session, controller)
        with self.assertRaises(LAB.Refusal): append_intent(session, controller)
        self.assertEqual(len(backend.calls), 1)

    def test_noncanonical_duplicate_and_oversize_refuse_before_backend(self):
        bad = [b'{"schema":1,"schema":1}',
               b'{ "schema": 1 }', b"x" * (LAB.MAX_EVENT_BYTES + 1)]
        for raw in bad:
            session, backend, controller = setup_session()
            with self.assertRaises(LAB.Refusal):
                session.append_exact_event(raw, controller)
            self.assertEqual(backend.calls, [])

    def test_wrong_order_chain_and_types_refuse(self):
        session, backend, controller = setup_session()
        raw = canonical_event(True, "INTENT", "0" * 64, {})
        with self.assertRaises(LAB.Refusal): session.append_exact_event(raw, controller)
        self.assertEqual(backend.calls, [])

    def test_reentrant_callback_poisons_without_second_backend_call(self):
        session, backend, controller = setup_session()
        def nested(_name, _raw): append_intent(session, controller)
        backend.hook = nested
        with self.assertRaises(LAB.Refusal): append_intent(session, controller)
        self.assertEqual(len(backend.calls), 1)
        self.assertEqual(session.state, "UNKNOWN")

    def test_foreign_thread_poisons(self):
        session, backend, controller = setup_session(); errors = []
        def foreign():
            try: append_intent(session, controller)
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=foreign); worker.start(); worker.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(session.state, "UNKNOWN")
        self.assertEqual(backend.calls, [])

    def test_copy_session_receipt_and_seal_refuse(self):
        session, backend, controller = setup_session(); receipt = append_intent(session, controller)
        for value in (session, receipt):
            with self.assertRaises(LAB.Refusal): copy.copy(value)
            with self.assertRaises(LAB.Refusal): copy.deepcopy(value)
        append_child(session, controller); append_exec(session, controller)
        seal = session.seal_pregrant(controller)
        with self.assertRaises(LAB.Refusal): copy.copy(seal)

    def test_append_after_seal_has_zero_backend_calls(self):
        session, backend, controller = setup_session()
        append_intent(session, controller); append_child(session, controller)
        append_exec(session, controller); session.seal_pregrant(controller)
        before = len(backend.calls)
        with self.assertRaises(LAB.Refusal): append_exec(session, controller)
        self.assertEqual(len(backend.calls), before)
        self.assertEqual(session.state, "UNKNOWN")

    def test_mutated_receipt_cannot_seal(self):
        session, backend, controller = setup_session()
        first = append_intent(session, controller)
        with self.assertRaises(LAB.Refusal): first.digest = "0" * 64
        append_child(session, controller); append_exec(session, controller)
        self.assertEqual(session.seal_pregrant(controller).sequence, 3)

    def test_unclaimed_chain_refuses_before_first_backend_call(self):
        backend = Backend()
        session = LAB.create_source_session("c" * 32, OWNER, backend)
        with self.assertRaises(LAB.Refusal): append_intent(session, None)
        self.assertEqual(backend.calls, [])
        self.assertEqual(session.state, "UNKNOWN")

    def test_nested_and_foreign_claim_monotonically_poison(self):
        session, backend, controller = setup_session()
        def nested(_name, _raw):
            try: session.claim(object())
            except LAB.Refusal: pass
        backend.hook = nested
        with self.assertRaises(LAB.Refusal): append_intent(session, controller)
        self.assertEqual(session.state, "UNKNOWN")
        session, backend, controller = setup_session(); errors = []
        def foreign():
            try: session.claim(object())
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=foreign); worker.start(); worker.join()
        self.assertEqual(len(errors), 1)
        self.assertEqual(session.state, "UNKNOWN")

    def test_foreign_controller_and_early_seal_poison(self):
        session, backend, controller = setup_session()
        with self.assertRaises(LAB.Refusal): session.seal_pregrant(controller)
        self.assertEqual(session.state, "UNKNOWN")
        session, backend, controller = setup_session()
        with self.assertRaises(LAB.Refusal): append_intent(session, object())
        self.assertEqual(backend.calls, [])


if __name__ == "__main__":
    unittest.main()
