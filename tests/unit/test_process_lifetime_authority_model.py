import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/process-lifetime-authority-model.py"
SPEC = importlib.util.spec_from_file_location("process_lifetime_authority_model", SCRIPT)
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


class ProcessLifetimeAuthorityModelTests(unittest.TestCase):
    def setUp(self):
        self.cold = MODEL.cold_state("1" * 64)
        self.session = {"session_nonce": "2" * 32,
                        "boot_id": "12345678-1234-1234-1234-123456789abc",
                        "invocation_id": "3" * 32, "pid": 123, "start_ticks": 456}
        self.head = {"revision": 7, "ledger_sha256": "4" * 64}
        self.active = MODEL.active_fixture(self.cold, self.session, self.head)
        self.binding = MODEL.client_binding(self.active)
        self.request = {"request_id": "5" * 32, "transaction": "6" * 32,
                        "attempt": "7" * 32}

    def test_disk_rollback_during_active_poisoned(self):
        result = MODEL.observe_head(
            self.active, {"revision": 6, "ledger_sha256": "8" * 64}
        )
        self.assertEqual(result["action"], "REFUSE")
        self.assertEqual(result["state"]["state"], "POISONED")

    def test_process_or_reboot_loss_returns_cold_not_active(self):
        lost = MODEL.process_lost(self.active)
        self.assertEqual(lost, self.cold)
        result = MODEL.begin_mutation(lost, self.binding, self.head, self.request)
        self.assertEqual(result["action"], "REFUSE")

    def test_missing_or_stale_client_session_binding_refuses(self):
        for binding in (None, {**self.binding, "session_nonce": "a" * 32},
                        {**self.binding, "invocation_id": "b" * 32}):
            result = MODEL.begin_mutation(
                self.active, binding, self.head, self.request
            )
            self.assertEqual(result["action"], "REFUSE")
            self.assertNotIn("assumptions_matched", result)

    def test_only_one_in_flight_model_transition(self):
        first = MODEL.begin_mutation(
            self.active, self.binding, self.head, self.request
        )
        self.assertEqual(first["action"], "MODEL_BEGIN_ASSUMED")
        self.assertEqual(first["runtime_authorized"], 0)
        second = MODEL.begin_mutation(
            first["state"], self.binding, self.head,
            {"request_id": "8" * 32, "transaction": "9" * 32,
             "attempt": "a" * 32},
        )
        self.assertEqual(second["action"], "REFUSE")
        self.assertEqual(second["state"]["state"], "ACTIVE")
        self.assertEqual(second["state"]["in_flight"], first["state"]["in_flight"])

    def test_in_flight_or_stale_client_cannot_poison_session_by_head_probe(self):
        begun = MODEL.begin_mutation(
            self.active, self.binding, self.head, self.request
        )["state"]
        advanced_disk = {"revision": 8, "ledger_sha256": "8" * 64}
        busy = MODEL.begin_mutation(
            begun, self.binding, advanced_disk,
            {"request_id": "8" * 32, "transaction": "9" * 32,
             "attempt": "a" * 32},
        )
        self.assertEqual(busy["action"], "REFUSE")
        self.assertEqual(busy["state"], begun)
        standalone = MODEL.observe_head(begun, advanced_disk)
        self.assertEqual(standalone["action"], "REFUSE")
        self.assertEqual(standalone["state"], begun)
        stale = MODEL.begin_mutation(
            self.active, {**self.binding, "session_nonce": "b" * 32},
            advanced_disk, self.request,
        )
        self.assertEqual(stale["action"], "REFUSE")
        self.assertEqual(stale["state"], self.active)

    def test_ambiguous_persist_poisoned_and_process_loss_cold(self):
        begun = MODEL.begin_mutation(
            self.active, self.binding, self.head, self.request
        )["state"]
        for outcome in ("UNKNOWN", "FAILED", "EXACT_COMMITTED"):
            post = None if outcome != "EXACT_COMMITTED" else {
                "revision": 8, "ledger_sha256": self.head["ledger_sha256"]}
            result = MODEL.complete_persist(
                begun, self.binding, self.request, post, outcome
            )
            self.assertEqual(result["action"], "REFUSE")
            self.assertEqual(result["state"]["state"], "POISONED")
            self.assertEqual(MODEL.process_lost(result["state"])["state"], "COLD")

    def test_exact_persist_advances_ram_head_before_model_ack(self):
        begun = MODEL.begin_mutation(
            self.active, self.binding, self.head, self.request
        )["state"]
        post = {"revision": 8, "ledger_sha256": "8" * 64}
        result = MODEL.complete_persist(
            begun, self.binding, self.request, post, "EXACT_COMMITTED"
        )
        self.assertEqual(result["action"], "MODEL_RAM_HEAD_ADVANCED_ASSUMED")
        self.assertEqual(result["runtime_authorized"], 0)
        self.assertEqual(result["state"]["ram_revision"], 8)
        self.assertIsNone(result["state"]["in_flight"])

    def test_old_session_response_and_delayed_request_refuse(self):
        response = {"session_nonce": self.active["session_nonce"], **self.request,
                    "ledger_revision": 7, "ledger_sha256": "4" * 64}
        new_session = {**self.session, "session_nonce": "a" * 32,
                       "invocation_id": "b" * 32, "pid": 789,
                       "start_ticks": 999}
        new_active = MODEL.active_fixture(self.cold, new_session, self.head)
        result = MODEL.correlate_response(
            new_active, MODEL.client_binding(new_active), self.request, response
        )
        self.assertEqual(result["action"], "REFUSE")
        delayed = MODEL.begin_mutation(
            new_active, self.binding, self.head, self.request
        )
        self.assertEqual(delayed["action"], "REFUSE")

    def test_same_session_response_is_bound_to_exact_request(self):
        response = {"session_nonce": self.active["session_nonce"], **self.request,
                    "ledger_revision": 7, "ledger_sha256": "4" * 64}
        matched = MODEL.correlate_response(
            self.active, self.binding, self.request, response
        )
        self.assertEqual(matched["action"], "MODEL_RESPONSE_CORRELATION_ONLY")
        self.assertEqual(matched["runtime_authorized"], 0)
        self.assertEqual(matched["replay_uniqueness_proven"], 0)
        for field in ("request_id", "transaction", "attempt"):
            stale = {**response, field: "a" * 32}
            refused = MODEL.correlate_response(
                self.active, self.binding, self.request, stale
            )
            self.assertEqual(refused["action"], "REFUSE")
            self.assertNotIn("assumptions_matched", refused)
        begun = MODEL.begin_mutation(
            self.active, self.binding, self.head, self.request
        )["state"]
        in_flight = MODEL.correlate_response(
            begun, self.binding, self.request, response
        )
        self.assertEqual(in_flight["action"], "REFUSE")
        self.assertEqual(in_flight["state"], begun)

    def test_surviving_old_executor_does_not_activate_successor(self):
        successor = MODEL.process_lost(self.active)
        self.assertEqual(successor["state"], "COLD")
        self.assertIsNone(successor["session_nonce"])
        self.assertEqual(MODEL.observe_head(successor, self.head)["action"], "REFUSE")

    def test_state_schema_is_closed_and_exact_typed(self):
        with self.assertRaisesRegex(ValueError, "malformed"):
            MODEL.validate_state({**self.active, "enabled": True})
        with self.assertRaisesRegex(ValueError, "malformed"):
            MODEL.validate_state({**self.active, "pid": 123.0})
        begun = MODEL.begin_mutation(
            self.active, self.binding, self.head, self.request
        )["state"]
        forged = {**begun, "in_flight": {
            **begun["in_flight"], "pre_revision": 3,
            "pre_ledger_sha256": "a" * 64,
        }}
        with self.assertRaisesRegex(ValueError, "in-flight"):
            MODEL.validate_state(forged)
        with self.assertRaisesRegex(ValueError, "in-flight"):
            MODEL.complete_persist(
                forged, self.binding, self.request,
                {"revision": 4, "ledger_sha256": "b" * 64},
                "EXACT_COMMITTED",
            )


if __name__ == "__main__":
    unittest.main()
