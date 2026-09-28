import importlib.util
import json
import os
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/executor-integrated-admission-lab.py"
SPEC = importlib.util.spec_from_file_location("executor_integrated_lab", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class IntegratedAdmissionLabStaticTests(unittest.TestCase):
    def test_volume_key_binds_vg_and_volume(self):
        first = LAB.volume_key("vg-a", "vm-1-disk-0")
        self.assertRegex(first, r"^[a-f0-9]{24}$")
        self.assertNotEqual(first, LAB.volume_key("vg-b", "vm-1-disk-0"))
        self.assertNotEqual(first, LAB.volume_key("vg-a", "vm-1-disk-1"))

    def test_source_has_one_shot_launch_and_dispatch(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('slot["launch_issued"] is not None', source)
        self.assertIn('slot["dispatch_issued"] is not None', source)
        self.assertIn('"issued_revision": data["revision"] + 1', source)
        self.assertIn('"recovery_hold"', source)
        self.assertNotIn("os.waitpid(child, 0)", source)
        self.assertIn("bounded_reap_after_signal", source)

    def test_source_uses_pinned_model_adapter(self):
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn('"/usr/bin/perl"', source)
        self.assertIn('"--module-sha256", self.module_sha256', source)
        self.assertIn('self.model.evaluate("reserve"', source)
        self.assertIn('self.model.evaluate("bind"', source)
        self.assertIn('self.model.evaluate("dispatch"', source)
        self.assertIn('self.model.evaluate("finish"', source)
        self.assertIn("def finish_exact", source)
        self.assertIn('"finish_observed"', source)

    def test_finish_evidence_schema_rejects_truthy_and_unknown_fields(self):
        identity = {
            "schema": 1, "kind": "THICK_EXECUTOR_LAB", "state": "BOUND",
        }
        evidence = {
            "identity": identity, "cgroup_terminal": 1,
            "pending_jobs_absent": 1, "io_terminal": 1,
            "storage_postcondition_proven": 1, "executor_result": "SUCCESS",
        }
        self.assertIs(LAB.validate_finish_evidence(evidence), evidence)
        with self.assertRaisesRegex(RuntimeError, "exact bit"):
            LAB.validate_finish_evidence({**evidence, "io_terminal": True})
        with self.assertRaisesRegex(RuntimeError, "schema is malformed"):
            LAB.validate_finish_evidence({**evidence, "future": 1})
        with self.assertRaisesRegex(RuntimeError, "result is invalid"):
            LAB.validate_finish_evidence({**evidence, "executor_result": "MAYBE"})

    def test_finish_exact_persists_proof_once_and_keeps_slot(self):
        boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        key = "a" * 24
        record = {
            "schema": 1, "kind": "THICK_EXECUTOR_LAB",
            "enrollment_epoch": "e" * 64, "vg_uuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
            "volume": "vm-900001-disk-0", "object_key": key,
            "transaction": "1" * 32, "attempt": "2" * 32,
            "node": os.uname().nodename, "boot_id": boot_id,
            "unit": "slt-thick-lab-exec-" + "2" * 32 + ".service",
            "code_digest": "3" * 64, "policy_digest": "4" * 64,
            "state": "BOUND", "invocation_id": "5" * 32,
        }
        startup = {
            "attempt": record["attempt"], "unit": record["unit"],
            "invocation_id": record["invocation_id"], "boot_id": boot_id,
            "run_nonce": "6" * 32, "pid": 123, "start_ticks": 456,
            "runner_sha256": "7" * 64, "command_sha256": "8" * 64,
            "control_group": "/system.slice/" + record["unit"],
            "inert_operation_digest": "9" * 64,
        }
        grant = {
            "schema": 1, "authority_id": "b" * 32,
            "enrollment_epoch": record["enrollment_epoch"],
            "transaction": record["transaction"], "code_digest": record["code_digest"],
            "policy_digest": record["policy_digest"], **startup,
        }
        dispatch = {"grant": grant, "grant_sha256": LAB.digest(grant),
                    "issued_revision": 5}
        proof = LAB.model_only_terminal_proof(record, "b" * 32, dispatch, startup)
        evidence = LAB.evidence_from_terminal_proof(record, proof, "b" * 32, dispatch)
        data = {
            "schema": 2, "revision": 5, "authority_id": "b" * 32,
            "authority_node": os.uname().nodename,
            "authority_boot_id": boot_id,
            "slots": {key: {"record": record, "launch_issued": {},
                             "startup_observed": startup, "dispatch_issued": dispatch,
                             "recovery_hold": None, "finish_observed": None}},
        }

        class FakeLedger:
            def __init__(self, value):
                self.value = value

            def transaction(self, callback):
                after, result = callback(self.value)
                if after is not None:
                    after["revision"] += 1
                    self.value = after
                return result, self.value

        class FakeModel:
            def evaluate(self, action, payload):
                self.assert_action = action
                terminal = json.loads(json.dumps(payload["record"]))
                terminal.update({"state": "TERMINAL", "executor_result": "SUCCESS"})
                return {"allowed": 1, "action": "MARK_TERMINAL", "record": terminal}

        ledger = FakeLedger(data)
        synthetic_refusal, synthetic_state = LAB.Protocol(
            ledger, FakeModel()
        ).finish_exact(record, proof)
        self.assertEqual(synthetic_refusal["allowed"], 0)
        self.assertEqual(synthetic_state["revision"], 5)
        decision, after = LAB.Protocol(ledger, FakeModel()).finish_exact(
            record, proof, allow_model_fixture=True
        )
        self.assertEqual(decision["action"], "MARK_TERMINAL")
        self.assertIn(key, after["slots"])
        self.assertEqual(after["slots"][key]["record"]["state"], "TERMINAL")
        self.assertEqual(after["slots"][key]["finish_observed"]["evidence_sha256"],
                         LAB.digest(evidence))
        before = json.loads(json.dumps(after))
        refused, unchanged = LAB.Protocol(ledger, FakeModel()).finish_exact(
            record, proof, allow_model_fixture=True
        )
        self.assertEqual(refused["allowed"], 0)
        self.assertEqual(unchanged, before)

    def test_schema2_validator_enforces_dispatch_before_finish(self):
        boot_id = pathlib.Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        attempt, transaction, invocation = "2" * 32, "1" * 32, "5" * 32
        epoch = "e" * 64
        key = LAB.volume_key(
            "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF", "vm-900001-disk-0"
        )
        unit = "slt-thick-lab-exec-" + attempt + ".service"
        record = {
            "schema": 1, "kind": "THICK_EXECUTOR_LAB", "enrollment_epoch": epoch,
            "vg_uuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
            "volume": "vm-900001-disk-0", "object_key": key,
            "transaction": transaction, "attempt": attempt,
            "node": os.uname().nodename, "boot_id": boot_id, "unit": unit,
            "code_digest": "3" * 64, "policy_digest": "4" * 64,
            "state": "TERMINAL", "invocation_id": invocation,
            "executor_result": "SUCCESS",
        }
        bound = {**record, "state": "BOUND"}
        bound.pop("executor_result")
        startup = {
            "attempt": attempt, "unit": unit, "invocation_id": invocation,
            "boot_id": boot_id, "run_nonce": "6" * 32, "pid": 123,
            "start_ticks": 456, "runner_sha256": "7" * 64,
            "command_sha256": "8" * 64,
            "control_group": "/system.slice/" + unit,
            "inert_operation_digest": "9" * 64,
        }
        grant = {
            "schema": 1, "authority_id": "b" * 32, "enrollment_epoch": epoch,
            "transaction": transaction, "code_digest": "3" * 64,
            "policy_digest": "4" * 64, **startup,
        }
        evidence = {
            "identity": bound, "cgroup_terminal": 1, "pending_jobs_absent": 1,
            "io_terminal": 1, "storage_postcondition_proven": 1,
            "executor_result": "SUCCESS",
        }
        dispatch = {"grant": grant, "grant_sha256": LAB.digest(grant),
                    "issued_revision": 5}
        proof = LAB.model_only_terminal_proof(bound, "b" * 32, dispatch, startup)
        slot = {
            "record": record,
            "launch_issued": {"attempt": attempt, "unit": unit, "boot_id": boot_id,
                              "command_sha256": startup["command_sha256"]},
            "startup_observed": startup,
            "dispatch_issued": dispatch,
            "recovery_hold": None,
            "finish_observed": {"proof": proof, "proof_sha256": LAB.digest(proof),
                                "evidence": evidence,
                                "evidence_sha256": LAB.digest(evidence),
                                "observed_revision": 6},
        }
        data = {
            "schema": 2, "revision": 6, "authority_id": "b" * 32,
            "authority_node": os.uname().nodename, "authority_boot_id": boot_id,
            "enrollment_epoch": epoch, "consumed_attempts": [attempt],
            "slots": {key: slot},
        }

        class ReproducingModel:
            def evaluate(self, action, payload):
                if action == "validate":
                    return {"allowed": 1, "action": "VALID_LAB_RECORD"}
                return {"allowed": 1, "action": "MARK_TERMINAL", "record": record}

        validator = object.__new__(LAB.Ledger)
        validator.model = ReproducingModel()
        self.assertIs(validator._validate(data), data)
        for non_integer in (True, 1.0):
            changed = json.loads(json.dumps(proof))
            changed["verifier"]["version"] = non_integer
            with self.assertRaisesRegex(RuntimeError, "verifier binding"):
                LAB.validate_terminal_proof(changed)
            changed = json.loads(json.dumps(proof))
            changed["marker"]["value"]["schema"] = non_integer
            changed["marker"]["sha256"] = LAB.digest(changed["marker"]["value"])
            observations = {key: value for key, value in changed.items()
                            if key != "verifier"}
            changed["verifier"]["observations_sha256"] = LAB.digest(observations)
            with self.assertRaisesRegex(RuntimeError, "marker is malformed"):
                LAB.validate_terminal_proof(changed)
        for invalid_revision in (5, 1):
            changed = json.loads(json.dumps(data))
            changed["slots"][key]["finish_observed"]["observed_revision"] = invalid_revision
            with self.assertRaisesRegex(RuntimeError, "finish evidence envelope"):
                validator._validate(changed)

    def test_grant_schema_is_closed_and_authority_bound(self):
        grant = {
            "schema": 1, "authority_id": "a" * 32, "enrollment_epoch": "e" * 64,
            "attempt": "1" * 32, "transaction": "2" * 32,
            "unit": "slt-thick-lab-exec-" + "1" * 32 + ".service",
            "invocation_id": "3" * 32,
            "boot_id": "11111111-2222-3333-4444-555555555555",
            "run_nonce": "4" * 32, "pid": 10, "start_ticks": 20,
            "code_digest": "5" * 64, "policy_digest": "6" * 64,
            "runner_sha256": "8" * 64, "command_sha256": "9" * 64,
            "control_group": "/system.slice/slt-thick-lab-exec-" + "1" * 32 + ".service",
            "inert_operation_digest": "7" * 64,
        }
        self.assertIs(LAB.validate_grant(grant), grant)
        with self.assertRaisesRegex(RuntimeError, "schema is malformed"):
            LAB.validate_grant({**grant, "future": 1})
        with self.assertRaisesRegex(RuntimeError, "pid is invalid"):
            LAB.validate_grant({**grant, "pid": True})
        with self.assertRaisesRegex(RuntimeError, "control group is invalid"):
            LAB.validate_grant({
                **grant,
                "control_group": "/system.slice/slt-thick-lab-exec-" + "2" * 32 + ".service",
            })


if __name__ == "__main__":
    unittest.main()
