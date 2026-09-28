import copy
import importlib.util
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "layout_config_cas_model", ROOT / "experiments/thick-generations/layout-migration-config-cas-model.py")
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


def request():
    nodes = [{"node": "pve0" + str(i),
              "boot_id": str(i) * 8 + "-1111-4111-8111-" + str(i) * 12,
              "role": "CONTROL_ONLY" if i == 4 else "SAN_PARTICIPANT"}
             for i in range(1, 5)]
    return {"schema": "slt-config-cas-model/v1", "tx": "a" * 32,
            "generation": 1, "attempt_id": "b" * 32,
            "context_sha256": "c" * 64, "candidate_sha256": "d" * 64,
            "serializer_sha256": "e" * 64, "nodes": nodes,
            "executor": {key: nodes[0][key] for key in ("node", "boot_id")},
            "preinst_sha256": {node["node"]: "f" * 64 for node in nodes[:3]},
            "payload_sha256": {node["node"]: "1" * 64 for node in nodes},
            "baseline": b"sharedlvmthin: test\n\tslt-vgname vg-a\n",
            "target": b"sharedlvmthin: test\n\tslt-vgname vg-a\n\tslt-vg-layout mixed\n",
            "baseline_native_digest": "2" * 40,
            "authorization": {"action": "MODEL_ONE_CAS_ONLY", "issued_wall_ns": 1000,
                              "expires_wall_ns": 2000, "start_monotonic_ns": 5000}}


def trace(req, *specs):
    seal = MODEL.validate_request(req)
    return [{"sequence": i, "kind": kind, "attempt_id": req["attempt_id"],
             "request_sha256": seal, "wall_ns": 1000 + i, "monotonic_ns": 5000 + i,
             **extras} for i, (kind, extras) in enumerate(specs, 1)]


RESERVE = ("RESERVE", {})
INTENT = ("INTENT_ACK", {"durability": "DURABLE"})
DISPATCH = ("CAS_DISPATCH", {})
SUCCESS = ("CAS_RETURN", {"outcome": "SUCCESS", "detail": "returned"})
ERROR = ("CAS_RETURN", {"outcome": "ERROR", "detail": "effect may have happened"})
CRASH = ("CRASH", {})
QUIET = ("QUIESCENCE", {"executor_terminated": True, "config_lock_held": True,
                        "barrier_current": True})
DURABLE = ("RECEIPT_ACK", {"durability": "DURABLE"})


class ConfigCasModelTests(unittest.TestCase):
    def setUp(self):
        self.req = request()

    def completed(self, outcome=SUCCESS, raw=None, publication=DURABLE):
        raw = self.req["target"] if raw is None else raw
        return trace(self.req, RESERVE, INTENT, DISPATCH, outcome, QUIET,
                     ("OBSERVE", {"result": "OK", "raw": raw}), publication)

    def assert_no_authority(self, result):
        self.assertEqual(result["authorization"], "NONE")
        self.assertTrue(result["inspection_only"])
        for key in ("retry_authorized", "hold_transition_authorized", "configure_authorized",
                    "release_authorized", "mutation_performed"):
            self.assertIs(result[key], False)

    def test_success_is_only_durable_target_observation(self):
        result = MODEL.evaluate(self.req, self.completed())
        self.assertEqual(result["classification"], "TARGET_OBSERVED_NONQUALIFIED")
        self.assertEqual(result["modeled_write_dispatches"], 1)
        self.assertTrue(result["settlement_receipt_durable"])
        self.assert_no_authority(result)

    def test_full_outcome_and_observation_matrix(self):
        for outcome in ("SUCCESS", "ERROR", "UNKNOWN"):
            for observed, raw in (("TARGET", self.req["target"]),
                                  ("BASELINE", self.req["baseline"]),
                                  ("FOREIGN", b"foreign\n")):
                with self.subTest(outcome=outcome, observed=observed):
                    events = self.completed(("CAS_RETURN", {"outcome": outcome, "detail": "kept"}), raw)
                    result = MODEL.evaluate(self.req, events)
                    expected = {"TARGET": "TARGET_OBSERVED_NONQUALIFIED",
                                "BASELINE": "BASELINE_OBSERVED_NONQUALIFIED",
                                "FOREIGN": "FOREIGN_OBSERVED_RETAIN"}[observed]
                    if outcome == "SUCCESS" and observed != "TARGET":
                        expected = "UNKNOWN_RETAIN"
                    self.assertEqual(result["classification"], expected)
                    self.assertEqual(result["observed_raw"], raw)
                    self.assertEqual(result["cas_detail"], "kept")
                    self.assert_no_authority(result)

    def test_effect_then_error_is_not_rolled_back_or_retried(self):
        result = MODEL.evaluate(self.req, self.completed(ERROR))
        self.assertEqual(result["cas_outcome"], "ERROR")
        self.assertEqual(result["observed"], "TARGET")
        self.assertEqual(result["classification"], "TARGET_OBSERVED_NONQUALIFIED")
        self.assertEqual(result["modeled_write_dispatches"], 1)

    def test_baseline_observed_does_not_assert_effect_never_happened(self):
        result = MODEL.evaluate(self.req, self.completed(ERROR, self.req["baseline"]))
        self.assertEqual(result["classification"], "BASELINE_OBSERVED_NONQUALIFIED")
        self.assertEqual(result["modeled_write_dispatches"], 1)
        self.assertNotIn("never_applied", result)

    def test_each_incomplete_prefix_is_non_authorizing(self):
        full = self.completed()
        for count in range(len(full)):
            with self.subTest(count=count):
                result = MODEL.evaluate(self.req, full[:count])
                self.assertEqual(result["classification"],
                                 "NEW_NO_EFFECT" if count == 0 else "UNKNOWN_RETAIN")
                self.assert_no_authority(result)
        self.assertEqual(MODEL.evaluate(self.req, full[:-1])["observed"], "TARGET")

    def test_failed_or_ambiguous_intent_never_dispatches(self):
        for durability in ("FAILED", "UNKNOWN"):
            specs = (RESERVE, ("INTENT_ACK", {"durability": durability}))
            result = MODEL.evaluate(self.req, trace(self.req, *specs))
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
            self.assertEqual(result["modeled_write_dispatches"], 0)
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(self.req, trace(self.req, *specs, DISPATCH))

    def test_crash_at_every_pre_observation_boundary_allows_only_inspection(self):
        prefixes = ((RESERVE,), (RESERVE, INTENT), (RESERVE, INTENT, DISPATCH),
                    (RESERVE, INTENT, DISPATCH, SUCCESS),
                    (RESERVE, INTENT, DISPATCH, ERROR, QUIET))
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                specs = (*prefix, CRASH)
                result = MODEL.evaluate(self.req, trace(self.req, *specs))
                self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
                with self.assertRaises(MODEL.Refusal):
                    MODEL.evaluate(self.req, trace(self.req, *specs, DISPATCH))
                recovery = trace(self.req, *specs, QUIET,
                                 ("OBSERVE", {"result": "OK", "raw": self.req["target"]}), DURABLE)
                result = MODEL.evaluate(self.req, recovery)
                self.assertEqual(result["classification"], "TARGET_OBSERVED_NONQUALIFIED")
                self.assertTrue(result["crashed"])
                self.assert_no_authority(result)

    def test_crash_after_read_preserves_evidence_without_republication(self):
        events = self.completed()[:-1]
        crash = trace(self.req, CRASH)[0]
        crash.update(sequence=7, wall_ns=1007, monotonic_ns=5007)
        events.append(crash)
        result = MODEL.evaluate(self.req, events)
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertEqual(result["observed_raw"], self.req["target"])
        final = trace(self.req, DURABLE)[0]
        final.update(sequence=8, wall_ns=1008, monotonic_ns=5008)
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events + [final])

    def test_receipt_durability_faults_preserve_observation(self):
        for durability in ("FAILED", "UNKNOWN"):
            result = MODEL.evaluate(self.req, self.completed(
                publication=("RECEIPT_ACK", {"durability": durability})))
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
            self.assertEqual(result["observed_raw"], self.req["target"])
            self.assertFalse(result["settlement_receipt_durable"])

    def test_settlement_requires_quiescence_lock_and_barrier(self):
        for field in QUIET[1]:
            for invalid in (False, 1, None):
                with self.subTest(field=field, invalid=invalid):
                    events = self.completed()
                    events[4][field] = invalid
                    with self.assertRaises(MODEL.Refusal):
                        MODEL.evaluate(self.req, events)
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, trace(self.req, RESERVE, INTENT, DISPATCH,
                QUIET, ("OBSERVE", {"result": "OK", "raw": self.req["target"]})))

    def test_read_error_is_unknown_and_cannot_smuggle_bytes(self):
        events = self.completed()
        events[5].update(result="ERROR", raw=None)
        result = MODEL.evaluate(self.req, events)
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        events[5]["raw"] = self.req["target"]
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)

    def test_second_dispatch_and_new_attempt_envelope_refuse(self):
        for specs in ((RESERVE, INTENT, DISPATCH, DISPATCH),
                      (RESERVE, INTENT, DISPATCH, ERROR, DISPATCH),
                      (RESERVE, INTENT, CRASH, DISPATCH)):
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(self.req, trace(self.req, *specs))
        events = self.completed()
        events[2]["attempt_id"] = "9" * 32
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)

    def test_terminal_replay_and_duplicate_steps_refuse(self):
        full = self.completed()
        extra = copy.deepcopy(full[-1])
        extra.update(sequence=8, wall_ns=1008, monotonic_ns=5008)
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, full + [extra])
        for specs in ((RESERVE, RESERVE), (RESERVE, INTENT, INTENT),
                      (RESERVE, INTENT, DISPATCH, SUCCESS, SUCCESS),
                      (RESERVE, INTENT, DISPATCH, SUCCESS, QUIET, QUIET)):
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(self.req, trace(self.req, *specs))

    def test_identical_history_replay_is_only_identical_inspection(self):
        first = MODEL.evaluate(self.req, self.completed())
        second = MODEL.evaluate(self.req, self.completed())
        self.assertEqual(first, second)
        self.assert_no_authority(second)

    def test_expiry_before_dispatch_blocks_even_if_wall_clock_rolls_back(self):
        events = trace(self.req, RESERVE, INTENT, DISPATCH)
        events[1].update(wall_ns=2001, monotonic_ns=6001)
        events[2].update(wall_ns=1003, monotonic_ns=6002)
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)
        result = MODEL.evaluate(self.req, events[:2])
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertIn("EXPIRED", result["reasons"])

    def test_expiry_during_effect_retains_diagnostic_target_no_success(self):
        for field, begin in (("wall_ns", 2001), ("monotonic_ns", 6001)):
            events = self.completed()
            for offset, event in enumerate(events[3:]):
                event[field] = begin + offset
            result = MODEL.evaluate(self.req, events)
            self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
            self.assertEqual(result["observed"], "TARGET")
            self.assertIn("EXPIRED", result["reasons"])

    def test_wall_rollback_poison_is_sticky_and_mono_rollback_refuses(self):
        events = self.completed()
        events[3]["wall_ns"] = 900
        result = MODEL.evaluate(self.req, events)
        self.assertEqual(result["classification"], "UNKNOWN_RETAIN")
        self.assertIn("WALL_CLOCK_ROLLBACK", result["reasons"])
        events = self.completed()
        events[3]["monotonic_ns"] = 4999
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)

    def test_request_and_trace_drift_break_seal(self):
        events = self.completed()
        for key in ("context_sha256", "candidate_sha256", "serializer_sha256"):
            changed = copy.deepcopy(self.req)
            changed[key] = "9" * 64
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(changed, events)
        changed = copy.deepcopy(self.req)
        changed["authorization"]["expires_wall_ns"] += 1
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(changed, events)
        changed = copy.deepcopy(self.req)
        changed["target"] += b"# drift\n"
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(changed, events)

    def test_exact_topology_receipt_coverage_executor_and_schema(self):
        variants = []
        value = copy.deepcopy(self.req); value["nodes"].reverse(); variants.append(value)
        value = copy.deepcopy(self.req); value["nodes"][3] = value["nodes"][0]; variants.append(value)
        value = copy.deepcopy(self.req); value["preinst_sha256"]["pve04"] = "0" * 64; variants.append(value)
        value = copy.deepcopy(self.req); del value["payload_sha256"]["pve04"]; variants.append(value)
        value = copy.deepcopy(self.req); value["executor"]["node"] = "pve04"; variants.append(value)
        value = copy.deepcopy(self.req); value["executor"]["boot_id"] = value["nodes"][1]["boot_id"]; variants.append(value)
        value = copy.deepcopy(self.req); value["baseline"] = value["target"]; variants.append(value)
        value = copy.deepcopy(self.req); value["extra"] = True; variants.append(value)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(value, [])

    def test_numeric_aliases_and_extra_event_fields_refuse(self):
        for bad in (True, 1.0, "1", -1):
            value = copy.deepcopy(self.req); value["generation"] = bad
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(value, [])
            events = self.completed(); events[0]["sequence"] = bad
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(self.req, events)
        for key in ("wall_ns", "monotonic_ns"):
            events = self.completed(); events[0][key] = True
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(self.req, events)
        events = self.completed(); events[0]["release_authorized"] = True
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)

    def test_custom_objects_are_rejected_without_callbacks(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other):
                calls.append("eq"); return super().__eq__(other)
            __hash__ = str.__hash__
        class EvilDict(dict):
            def __iter__(self):
                calls.append("iter"); return super().__iter__()
            def items(self):
                calls.append("items"); return super().items()
        class EvilInt(int):
            def __eq__(self, other):
                calls.append("int-eq"); return True
        class EvilMeta(type):
            def __eq__(self, other):
                calls.append("meta-eq"); return True
        class EvilObject(metaclass=EvilMeta):
            pass
        variants = [EvilDict(self.req)]
        value = copy.deepcopy(self.req); value["tx"] = EvilStr(value["tx"]); variants.append(value)
        value = copy.deepcopy(self.req); value["generation"] = EvilInt(1); variants.append(value)
        value = copy.deepcopy(self.req); value[EvilStr("extra")] = None; variants.append(value)
        value = copy.deepcopy(self.req); value["extra"] = EvilObject(); variants.append(value)
        for value in variants:
            with self.assertRaises(MODEL.Refusal):
                MODEL.evaluate(value, [])
        events = self.completed(); events[0]["kind"] = EvilStr("RESERVE")
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)
        self.assertEqual(calls, [])

    def test_cyclic_or_oversized_inputs_refuse(self):
        events = []; events.append(events)
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(self.req, events)
        value = copy.deepcopy(self.req); value["target"] = b"x" * (MODEL.MAX_BYTES + 1)
        with self.assertRaises(MODEL.Refusal):
            MODEL.evaluate(value, [])

    def test_result_and_inputs_do_not_share_mutable_state(self):
        events = self.completed()
        original_request, original_events = copy.deepcopy(self.req), copy.deepcopy(events)
        first = MODEL.evaluate(self.req, events)
        first["reasons"].append("fake")
        first["release_authorized"] = True
        self.assertEqual(self.req, original_request)
        self.assertEqual(events, original_events)
        second = MODEL.evaluate(self.req, events)
        self.assertEqual(second["reasons"], [])
        self.assert_no_authority(second)


if __name__ == "__main__":
    unittest.main()
