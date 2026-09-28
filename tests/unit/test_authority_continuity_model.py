import importlib.util
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/authority-continuity-model.py"
SPEC = importlib.util.spec_from_file_location("authority_continuity_model", SCRIPT)
MODEL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODEL)


class AuthorityContinuityModelTests(unittest.TestCase):
    def enrollment(self, **changes):
        value = {
            "schema": 1, "kind": "PINNED_ADMISSION_AUTHORITY",
            "cluster_id": "pve-lab", "storage_id": "thick-a",
            "vg_uuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
            "storage_set": "1" * 64, "authority_id": "2" * 32,
            "authority_node": "pve01",
            "authority_boot_id": "12345678-1234-1234-1234-123456789abc",
            "enrollment_epoch": "3" * 64,
            "backend": "LOCAL_PERSISTENT_NO_FAILOVER",
            "code_digest": "7" * 64, "policy_digest": "4" * 64,
        }
        value.update(changes)
        return value

    def observation(self, **changes):
        value = {
            "schema": 1, "kind": "AUTHORITY_HEAD_OBSERVATION", "status": "AVAILABLE",
            "authority_id": "2" * 32, "authority_node": "pve01",
            "authority_boot_id": "12345678-1234-1234-1234-123456789abc",
            "enrollment_epoch": "3" * 64,
            "enrollment_sha256": MODEL.enrollment_digest(self.enrollment()),
            "ledger_revision": 8,
            "ledger_sha256": "5" * 64,
        }
        value.update(changes)
        return value

    def witness(self, **changes):
        value = {
            "schema": 1, "kind": "INDEPENDENT_AUTHORITY_HEAD_V1",
            "trust_domain": "ROLLBACK_INDEPENDENT", "authority_id": "2" * 32,
            "authority_node": "pve01",
            "authority_boot_id": "12345678-1234-1234-1234-123456789abc",
            "enrollment_epoch": "3" * 64,
            "enrollment_sha256": MODEL.enrollment_digest(self.enrollment()),
            "ledger_revision": 8,
            "ledger_sha256": "5" * 64, "proof_digest": "6" * 64,
        }
        value.update(changes)
        return value

    def test_exact_head_only_matches_unqualified_model_assumptions(self):
        result = MODEL.evaluate_continuity(
            self.enrollment(), self.observation(), self.witness()
        )
        self.assertEqual(result["allowed"], 0)
        self.assertEqual(result["reserve_authorized"], 0)
        self.assertEqual(result["assumptions_matched"], 1)
        self.assertEqual(result["action"], "MODEL_CONTINUITY_ASSUMPTIONS_MATCH_ONLY")
        self.assertEqual(result["dispatch_allowed"], 0)

    def test_missing_witness_and_all_unavailable_states_refuse(self):
        self.assertEqual(MODEL.evaluate_continuity(
            self.enrollment(), self.observation())["allowed"], 0)
        for status in ("UNAVAILABLE", "MISSING", "CORRUPT"):
            observation = self.observation(
                status=status, authority_id=None, authority_node=None,
                authority_boot_id=None, enrollment_epoch=None,
                enrollment_sha256=None, ledger_revision=None, ledger_sha256=None,
            )
            self.assertEqual(MODEL.evaluate_continuity(
                self.enrollment(), observation, self.witness())["allowed"], 0)

    def test_older_rollback_and_unwitnessed_newer_head_refuse(self):
        older = MODEL.evaluate_continuity(
            self.enrollment(), self.observation(ledger_revision=7,
                                                ledger_sha256="7" * 64),
            self.witness(),
        )
        self.assertEqual(older["allowed"], 0)
        self.assertIn("older", older["reason"])
        newer = MODEL.evaluate_continuity(
            self.enrollment(), self.observation(ledger_revision=9,
                                                ledger_sha256="9" * 64),
            self.witness(),
        )
        self.assertEqual(newer["allowed"], 0)
        self.assertIn("differs", newer["reason"])
        divergent = MODEL.evaluate_continuity(
            self.enrollment(), self.observation(ledger_sha256="a" * 64),
            self.witness(),
        )
        self.assertEqual(divergent["allowed"], 0)
        self.assertIn("differs", divergent["reason"])

    def test_matching_replayed_head_never_authorizes_reserve(self):
        replayed_observation = self.observation(ledger_revision=7,
                                                ledger_sha256="a" * 64)
        replayed_witness = self.witness(ledger_revision=7,
                                        ledger_sha256="a" * 64)
        result = MODEL.evaluate_continuity(
            self.enrollment(), replayed_observation, replayed_witness
        )
        self.assertEqual(result["allowed"], 0)
        self.assertEqual(result["reserve_authorized"], 0)
        self.assertEqual(result["assumptions_matched"], 1)
        self.assertIn("unqualified", result["reason"])

    def test_restored_or_alternate_authority_identity_refuses(self):
        for changed in (
            {"enrollment_epoch": "a" * 64},
            {"authority_id": "b" * 32},
            {"authority_node": "pve02"},
            {"authority_boot_id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"},
        ):
            self.assertEqual(MODEL.evaluate_continuity(
                self.enrollment(), self.observation(**changed), self.witness()
            )["allowed"], 0)

    def test_witness_from_same_rollback_domain_is_rejected(self):
        result = MODEL.evaluate_continuity(
            self.enrollment(), self.observation(),
            self.witness(trust_domain="SAME_PMxcfs_OR_LOCAL_BACKUP"),
        )
        self.assertEqual(result["allowed"], 0)
        self.assertEqual(result["classification"],
                         "BLOCKED_MALFORMED_AUTHORITY_EVIDENCE")

    def test_same_vg_aliases_require_one_exact_binding(self):
        result = MODEL.evaluate_alias_set([
            self.enrollment(), self.enrollment(storage_id="thick-b")
        ])
        self.assertEqual(result["allowed"], 0)
        self.assertEqual(result["consistent"], 1)
        self.assertEqual(result["reserve_authorized"], 0)
        duplicate = MODEL.evaluate_alias_set(
            [self.enrollment(), self.enrollment()]
        )
        self.assertEqual(duplicate["action"], "REFUSE")
        self.assertNotIn("consistent", duplicate)
        for changed in (
            {"authority_id": "a" * 32}, {"storage_set": "b" * 64},
            {"policy_digest": "c" * 64}, {"cluster_id": "other-cluster"},
        ):
            refused = MODEL.evaluate_alias_set(
                [self.enrollment(),
                 self.enrollment(storage_id="thick-b", **changed)])
            self.assertEqual(refused["action"], "REFUSE")
            self.assertNotIn("consistent", refused)

    def test_complete_enrollment_digest_binds_scope_code_and_policy(self):
        for changed in (
            {"cluster_id": "other-cluster"}, {"storage_id": "thick-b"},
            {"vg_uuid": "FEDCBA-4321-8765-9abc-def0-4321-FEDCBA"},
            {"storage_set": "a" * 64}, {"code_digest": "b" * 64},
            {"policy_digest": "c" * 64},
        ):
            enrollment = self.enrollment(**changed)
            result = MODEL.evaluate_continuity(
                enrollment, self.observation(), self.witness()
            )
            self.assertEqual(result["allowed"], 0)
            self.assertIn("complete enrollment", result["reason"])

    def test_reenrollment_requires_every_exact_retirement_proof(self):
        evidence = {
            "schema": 1, "kind": "MANUAL_REENROLLMENT_EVIDENCE",
            "cluster_id": "pve-lab",
            "vg_uuid": "ABCDEF-1234-5678-9abc-def0-1234-ABCDEF",
            "old_enrollment_sha256": MODEL.enrollment_digest(self.enrollment()),
            "recovery_transaction": "8" * 32,
            "participant_set_sha256": "9" * 64,
            "participant_set_complete": 1, "all_potential_executors_fenced": 1,
            "storage_reconciled": 1, "old_authority_disabled": 1,
            "old_history_retired": 1,
        }
        result = MODEL.evaluate_reenrollment(evidence)
        self.assertEqual(result["allowed"], 0)
        self.assertEqual(result["reenrollment_authorized"], 0)
        self.assertEqual(result["assumptions_matched"], 1)
        self.assertEqual(result["dispatch_allowed"], 0)
        for field in {"participant_set_complete", "all_potential_executors_fenced",
                      "storage_reconciled", "old_authority_disabled",
                      "old_history_retired"}:
            false_result = MODEL.evaluate_reenrollment({**evidence, field: 0})
            malformed = MODEL.evaluate_reenrollment({**evidence, field: True})
            self.assertEqual(false_result["action"], "REFUSE")
            self.assertNotIn("assumptions_matched", false_result)
            self.assertEqual(malformed["action"], "REFUSE")
            self.assertNotIn("assumptions_matched", malformed)

    def test_schemas_are_closed_and_exact_typed(self):
        self.assertEqual(MODEL.evaluate_continuity(
            {**self.enrollment(), "fallback": True}, self.observation(), self.witness()
        )["allowed"], 0)
        self.assertEqual(MODEL.evaluate_continuity(
            {**self.enrollment(), "schema": True}, self.observation(), self.witness()
        )["allowed"], 0)


if __name__ == "__main__":
    unittest.main()
