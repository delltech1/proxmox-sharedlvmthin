import importlib.machinery
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-lab-evidence-check"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_lab_evidence", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class LabEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_obligations_follow_profile_and_directed_transition(self):
        catalogue = {"lab_execution_schema": {
            "revision": 1,
            "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"],
        }, "contracts": [
            {"id": "lvm.basic", "revision": 2, "scope": ["thick-only"],
             "lab_tests": ["allocate"]},
            {"id": "pve.migration-offline-live", "revision": 3,
             "scope": ["dual", "thick-only"], "lab_tests": ["live"],
             "transition_tests": ["live"]},
        ]}
        aggregate = {
            "classes": [{"contract_fingerprint": "a" * 64,
                         "package_flavor": "thick-only", "running_kernel": "test",
                         "plugin_payload_sha256": "1" * 64, "nodes": ["node-a"]}],
            "expected_nodes": ["node-a"], "observed_nodes": ["node-a"],
            "class_descriptors": [
                {"contract_fingerprint": value, "package_flavor": "thick-only",
                 "running_kernel": "test", "plugin_payload_sha256": "1" * 64}
                for value in ("a" * 64, "b" * 64, "c" * 64)
            ],
            "required_transitions": [{"id": "old-new", "source_class": "b" * 64,
                                      "destination_class": "a" * 64,
                                      "peer_classes": ["c" * 64],
                                      "direction": "old-to-new", "order": 1}],
        }
        obligations = self.module.derive_obligations(catalogue, aggregate)
        self.assertIn("lvm.basic@2:allocate@1:class:" + "a" * 64, obligations)
        transition = obligations[
            "pve.migration-offline-live@3:live@1:transition:old-new"]
        self.assertEqual(transition["classes"], ["a" * 64, "b" * 64, "c" * 64])

    def test_empty_transition_plan_cannot_erase_mixed_version_tests(self):
        catalogue = {
            "lab_execution_schema": {"revision": 1, "required_parameters": ["concurrency"]},
            "contracts": [{"id": "pve.migration-offline-live", "revision": 1,
                           "scope": ["dual"], "lab_tests": ["mixed-version-migration"],
                           "transition_tests": ["mixed-version-migration"]}],
        }
        aggregate = {"classes": [{"contract_fingerprint": "a" * 64,
                                  "package_flavor": "dual", "running_kernel": "test",
                                  "plugin_payload_sha256": "1" * 64,
                                  "nodes": ["node-a"]}],
                     "expected_nodes": ["node-a"], "observed_nodes": ["node-a"],
                     "class_descriptors": [{"contract_fingerprint": "a" * 64,
                                            "package_flavor": "dual",
                                            "running_kernel": "test",
                                            "plugin_payload_sha256": "1" * 64}],
                     "required_transitions": []}
        with self.assertRaisesRegex(ValueError, "directed transitions"):
            self.module.derive_obligations(catalogue, aggregate)

    def test_transitions_cannot_replace_all_observed_class_coverage(self):
        catalogue = {
            "lab_execution_schema": {"revision": 1, "required_parameters": ["concurrency"]},
            "contracts": [{"id": "pve.migration-offline-live", "revision": 1,
                           "scope": ["dual"], "lab_tests": ["offline-migration",
                                                              "mixed-version-migration"],
                           "transition_tests": ["mixed-version-migration"]}],
        }
        aggregate = {
            "classes": [], "expected_nodes": ["node-a"], "observed_nodes": ["node-a"],
            "class_descriptors": [
                {"contract_fingerprint": value, "package_flavor": "dual",
                 "running_kernel": "test", "plugin_payload_sha256": "1" * 64}
                for value in ("a" * 64, "b" * 64)
            ],
            "required_transitions": [{"id": "old-new", "source_class": "a" * 64,
                                      "destination_class": "b" * 64,
                                      "peer_classes": [], "direction": "old-to-new",
                                      "order": 1}],
        }
        with self.assertRaisesRegex(ValueError, "no observed contract classes"):
            self.module.derive_obligations(catalogue, aggregate)

    def test_main_blocks_empty_replacement_catalogue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalogue_path = root / "catalogue.json"
            catalogue_path.write_text(json.dumps({
                "schema": 1, "catalogue": "pve-compatibility-contracts-v1",
                "lab_execution_schema": {"revision": 1,
                                         "required_parameters": ["concurrency"]},
                "contracts": [],
            }), encoding="utf-8")
            catalogue_sha = hashlib.sha256(catalogue_path.read_bytes()).hexdigest()
            aggregate_path = root / "aggregate.json"
            aggregate_path.write_text(json.dumps({
                "kind": "cluster-compatibility-aggregate", "schema": 1,
                "verdict": "RETEST_REQUIRED", "cluster_upgrade_authorized": False,
                "errors": [], "catalogue_sha256": catalogue_sha,
                "scenario_registry_sha256": "7" * 64,
                "collection_plan_sha256": "1" * 64, "upgrade_plan_sha256": "2" * 64,
                "lab_runner_sha256": "3" * 64,
                "plugin_artifacts": {"dual_sha256": "4" * 64,
                                     "thick_sha256": "5" * 64},
                "classes": [], "required_transitions": [], "class_descriptors": [],
            }), encoding="utf-8")
            manifest_path = root / "manifest.json"
            manifest_path.write_text(json.dumps({
                "kind": "lab-qualification-manifest", "schema": 1, "executions": [],
            }), encoding="utf-8")
            output = root / "result.json"
            result = self.module.main([
                "--catalogue", str(catalogue_path),
                "--cluster-aggregate", str(aggregate_path),
                "--manifest", str(manifest_path), "--evidence-dir", str(root),
                "--output", str(output),
            ])
            self.assertEqual(result, 2)
            self.assertEqual(json.loads(output.read_text())["verdict"], "BLOCKED")

    def execution(self, obligation):
        return {
            "kind": "lab-test-execution", "schema": 1,
            "obligation_id": "obligation", "test_run_id": "1" * 8 + "-" + "1" * 4
            + "-" + "1" * 4 + "-" + "1" * 4 + "-" + "1" * 12,
            "attempt_id": "2" * 8 + "-" + "2" * 4 + "-" + "2" * 4 + "-"
            + "2" * 4 + "-" + "2" * 12,
            "contract_id": obligation["contract_id"],
            "contract_revision": obligation["revision"],
            "test_id": obligation["test_id"], "test_revision": 1,
            "runner_sha256": "d" * 64, "scenario_id": obligation["test_id"],
            "scenario_case_id": "default",
            "parameters": {"fixture_bytes": 1073741824, "io_sector_bytes": 4096,
                           "concurrency": 1},
            "fixture": {"disposable": True, "format": "raw", "identity_sha256": "e" * 64},
            "participants": [{"role": "source", "node_identity_sha256": "f" * 64,
                              "boot_id_start": "3" * 8 + "-" + "3" * 4 + "-" + "3" * 4
                              + "-" + "3" * 4 + "-" + "3" * 12,
                              "boot_id_finish": "3" * 8 + "-" + "3" * 4 + "-" + "3" * 4
                              + "-" + "3" * 4 + "-" + "3" * 12,
                              "running_kernel": "test", "plugin_payload_sha256": "1" * 64,
                              "contract_fingerprint": obligation["classes"][0]}],
            "contract_classes": obligation["classes"],
            "directed_transition_id": obligation["transition_id"],
            "transition_direction": obligation["transition_direction"],
            "transition_order": obligation["transition_order"],
            "started_at": "2026-09-23T08:00:00+00:00",
            "finished_at": "2026-09-23T08:00:01+00:00", "elapsed_ms": 1000,
            "precondition": {"proven": True, "evidence_sha256": "2" * 64},
            "fault": {"required": False, "boundary_id": "", "requested": False,
                      "confirmed": False, "evidence_sha256": "3" * 64},
            "oracle": {"definition_sha256": "4" * 64, "expected_sha256": "5" * 64,
                       "observed_sha256": "5" * 64, "comparison": "MATCH"},
            "invariants": {"forbidden_actions": 0, "competing_writers": 0,
                           "redispatches": 0, "ambiguous_outcome": False},
            "reconciliation": {"complete": True, "original_transaction_reconciled": True,
                               "prior_executor_alive": False,
                               "expected_state_sha256": "6" * 64,
                               "observed_state_sha256": "6" * 64,
                               "comparison": "MATCH", "fixture_disposition": "clean"},
            "terminal_outcome": "PASS",
            "evidence_artifacts": {},
        }

    def validate(self, execution, obligation, scenario=None):
        fields = {
            "precondition": (execution["precondition"], "evidence_sha256"),
            "fault": (execution["fault"], "evidence_sha256"),
            "oracle_definition": (execution["oracle"], "definition_sha256"),
            "oracle_expected": (execution["oracle"], "expected_sha256"),
            "oracle_observed": (execution["oracle"], "observed_sha256"),
            "reconciliation_expected": (execution["reconciliation"], "expected_state_sha256"),
            "reconciliation_observed": (execution["reconciliation"], "observed_state_sha256"),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for number, (label, (owner, key)) in enumerate(fields.items()):
                if label == "precondition":
                    payload = {"proven": True, "state_sha256": "a" * 64}
                elif label == "fault":
                    payload = {"requested": execution["fault"]["requested"],
                               "confirmed": execution["fault"]["confirmed"],
                               "boundary_id": execution["fault"]["boundary_id"]}
                elif label == "oracle_definition":
                    payload = {"definition_sha256": "b" * 64}
                elif label in ("oracle_expected", "oracle_observed"):
                    payload = {"data_sha256": "c" * 64}
                    if scenario is not None:
                        case = next(row for row in scenario["cases"]
                                    if row["id"] == execution["scenario_case_id"])
                        payload.update({
                            "scenario_id": execution["scenario_id"],
                            "scenario_case_id": execution["scenario_case_id"],
                            "assertions": {
                                name: True
                                for name in case["oracle"]["required_assertions"]
                            },
                        })
                else:
                    payload = {"state_sha256": "d" * 64}
                value_sha256 = hashlib.sha256(
                    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()
                document = {
                    "kind": "lab-supporting-evidence", "schema": 1,
                    "test_run_id": execution["test_run_id"],
                    "attempt_id": execution["attempt_id"],
                    "evidence_type": label, "value_sha256": value_sha256,
                    "payload": payload,
                }
                data = json.dumps(document, sort_keys=True).encode()
                name = f"{label}.evidence"
                (root / name).write_bytes(data)
                sha256 = hashlib.sha256(data).hexdigest()
                owner[key] = value_sha256
                execution["evidence_artifacts"][label] = {
                    "artifact": name, "size": len(data), "sha256": sha256,
                }
            return self.module.validate_execution(
                execution, obligation, "d" * 64, root, scenario)

    def test_oracle_mismatch_and_redispatch_are_rejected(self):
        obligation = {"contract_id": "lvm.basic", "revision": 1,
                      "test_id": "allocate", "test_revision": 1,
                      "transition_id": None, "classes": ["a" * 64],
                      "source_class": None, "destination_class": None,
                      "peer_classes": [], "fault_required": False,
                      "transition_direction": None, "transition_order": None,
                      "class_descriptors": {"a" * 64: {
                          "contract_fingerprint": "a" * 64, "package_flavor": "dual",
                          "running_kernel": "test", "plugin_payload_sha256": "1" * 64}},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        execution = self.execution(obligation)
        execution["oracle"]["comparison"] = "MISMATCH"
        with self.assertRaisesRegex(ValueError, "oracle"):
            self.validate(execution, obligation)

    def test_execution_must_match_registered_scenario_case_and_parameters(self):
        obligation = {"contract_id": "lvm.basic", "revision": 1,
                      "test_id": "allocate", "test_revision": 1,
                      "transition_id": None, "classes": ["a" * 64],
                      "source_class": None, "destination_class": None,
                      "peer_classes": [], "fault_required": False,
                      "transition_direction": None, "transition_order": None,
                      "class_descriptors": {"a" * 64: {
                          "contract_fingerprint": "a" * 64, "package_flavor": "dual",
                          "running_kernel": "test", "plugin_payload_sha256": "1" * 64}},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        execution = self.execution(obligation)
        scenario = {"status": "READY", "revision": 1,
                    "fixture_kind": "disposable-storage",
                    "parameters": {name: {"type": "integer", "minimum": 1}
                                   for name in obligation["required_parameters"]},
                    "cases": [{"id": "default", "fault_required": False,
                               "fault_boundary": None,
                               "oracle": {"required_assertions": ["allocated"]}}]}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "exact registered"):
                bad = dict(execution)
                bad["scenario_id"] = "other"
                self.module.validate_execution(
                    bad, obligation, "d" * 64, Path(directory), scenario,
                )

    def test_read_only_fixture_is_explicitly_non_disposable_and_unchanged(self):
        obligation = {"contract_id": "pve.storage-config-parser", "revision": 1,
                      "test_id": "node-scope", "test_revision": 1,
                      "transition_id": None, "classes": ["a" * 64],
                      "source_class": None, "destination_class": None,
                      "peer_classes": [], "fault_required": False,
                      "transition_direction": None, "transition_order": None,
                      "class_descriptors": {"a" * 64: {
                          "contract_fingerprint": "a" * 64, "package_flavor": "dual",
                          "running_kernel": "test", "plugin_payload_sha256": "1" * 64}},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        scenario = {"status": "READY", "revision": 1,
                    "fixture_kind": "read-only-cluster",
                    "parameters": {name: {"type": "integer", "minimum": 1}
                                   for name in obligation["required_parameters"]},
                    "cases": [{"id": "default", "fault_required": False,
                               "fault_boundary": None,
                               "oracle": {"required_assertions": ["scope-readable"]}}]}
        execution = self.execution(obligation)
        execution["fixture"] = {"disposable": False, "format": "none",
                                "identity_sha256": "e" * 64}
        execution["reconciliation"]["fixture_disposition"] = "unchanged"
        self.assertIsNone(self.validate(execution, obligation, scenario))
        execution = self.execution(obligation)
        execution["invariants"]["redispatches"] = 1
        with self.assertRaisesRegex(ValueError, "forbidden"):
            self.validate(execution, obligation)

    def test_required_fault_must_be_confirmed(self):
        obligation = {"contract_id": "dm.clone", "revision": 1,
                      "test_id": "path-loss", "test_revision": 1,
                      "transition_id": None, "classes": ["a" * 64],
                      "source_class": None, "destination_class": None,
                      "peer_classes": [], "fault_required": True,
                      "transition_direction": None, "transition_order": None,
                      "class_descriptors": {"a" * 64: {
                          "contract_fingerprint": "a" * 64, "package_flavor": "dual",
                          "running_kernel": "test", "plugin_payload_sha256": "1" * 64}},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        execution = self.execution(obligation)
        execution["fault"]["required"] = True
        execution["fault"]["requested"] = True
        execution["fault"]["boundary_id"] = "after-dispatch"
        with self.assertRaisesRegex(ValueError, "fault"):
            self.validate(execution, obligation)

    def test_transition_requires_participant_for_every_role_and_class(self):
        obligation = {"contract_id": "pve.migration-offline-live", "revision": 1,
                      "test_id": "mixed-version-migration", "test_revision": 1,
                      "transition_id": "old-new", "classes": ["a" * 64, "b" * 64],
                      "source_class": "a" * 64, "destination_class": "b" * 64,
                      "peer_classes": [], "fault_required": False,
                      "transition_direction": "old-to-new", "transition_order": 1,
                      "class_descriptors": {
                          value: {"contract_fingerprint": value, "package_flavor": "dual",
                                  "running_kernel": "test", "plugin_payload_sha256": "1" * 64}
                          for value in ("a" * 64, "b" * 64)},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        execution = self.execution(obligation)
        execution["participants"][0]["contract_fingerprint"] = "c" * 64
        with self.assertRaisesRegex(ValueError, "participant|cover every required"):
            self.validate(execution, obligation)

    def test_boolean_numeric_parameter_is_rejected(self):
        obligation = {"contract_id": "lvm.basic", "revision": 1,
                      "test_id": "allocate", "test_revision": 1,
                      "transition_id": None, "classes": ["a" * 64],
                      "source_class": None, "destination_class": None,
                      "peer_classes": [], "fault_required": False,
                      "transition_direction": None, "transition_order": None,
                      "class_descriptors": {"a" * 64: {
                          "contract_fingerprint": "a" * 64, "package_flavor": "dual",
                          "running_kernel": "test", "plugin_payload_sha256": "1" * 64}},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        execution = self.execution(obligation)
        execution["parameters"]["concurrency"] = False
        with self.assertRaisesRegex(ValueError, "parameters"):
            self.validate(execution, obligation)

    def test_supporting_payload_digest_is_derived_from_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            execution = {"test_run_id": "1" * 8 + "-" + "1" * 4 + "-" + "1" * 4
                         + "-" + "1" * 4 + "-" + "1" * 12,
                         "attempt_id": "2" * 8 + "-" + "2" * 4 + "-" + "2" * 4
                         + "-" + "2" * 4 + "-" + "2" * 12,
                         "fault": {"requested": False, "confirmed": False,
                                   "boundary_id": ""}}
            document = {"kind": "lab-supporting-evidence", "schema": 1,
                        "test_run_id": execution["test_run_id"],
                        "attempt_id": execution["attempt_id"],
                        "evidence_type": "precondition", "value_sha256": "a" * 64,
                        "payload": {"proven": False, "state_sha256": "b" * 64}}
            data = json.dumps(document, sort_keys=True).encode()
            path = root / "bad.evidence"
            path.write_bytes(data)
            reference = {"artifact": path.name, "size": len(data),
                         "sha256": hashlib.sha256(data).hexdigest()}
            with self.assertRaisesRegex(ValueError, "value digest|semantics"):
                self.module.verify_artifact(reference, root, "precondition", execution)

    def test_registered_oracle_requires_exact_positive_assertion_set(self):
        obligation = {"contract_id": "lvm.basic", "revision": 1,
                      "test_id": "allocate", "test_revision": 1,
                      "transition_id": None, "classes": ["a" * 64],
                      "source_class": None, "destination_class": None,
                      "peer_classes": [], "fault_required": False,
                      "transition_direction": None, "transition_order": None,
                      "class_descriptors": {"a" * 64: {
                          "contract_fingerprint": "a" * 64, "package_flavor": "dual",
                          "running_kernel": "test", "plugin_payload_sha256": "1" * 64}},
                      "required_parameters": ["fixture_bytes", "io_sector_bytes", "concurrency"]}
        scenario = {"status": "READY", "revision": 1,
                    "fixture_kind": "disposable-storage",
                    "parameters": {name: {"type": "integer", "minimum": 1}
                                   for name in obligation["required_parameters"]},
                    "cases": [{"id": "default", "fault_required": False,
                               "fault_boundary": None,
                               "oracle": {"required_assertions": ["allocated", "zeroed"]}}]}
        execution = self.execution(obligation)
        self.assertIsNone(self.validate(execution, obligation, scenario))
        scenario["cases"][0]["oracle"]["required_assertions"].append("allocated")
        with self.assertRaisesRegex(ValueError, "oracle_expected|semantics"):
            self.validate(self.execution(obligation), obligation, scenario)


if __name__ == "__main__":
    unittest.main()
