import copy
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("offline_composer", ROOT / "experiments/thick-generations/layout-migration-offline-composer.py")
H = load("composer_preflight_fixture", ROOT / "tests/unit/test_layout_migration_v2_v1_adapter.py")
N = load("composer_cas_fixture", ROOT / "tests/unit/test_layout_migration_native_config_cas_controller.py")


class ComposerTests(unittest.TestCase):
    def fixture(self):
        baseline, target, template, context, rows, barrier, auth, now = H.Tests().fixture()
        topology, evidence = M.CTX.topology_for(template, baseline)
        args = [{"tx": context["tx"], "generation": context["generation"]}, topology, evidence,
                rows, baseline, target, "pve01", now]
        fields = {key: auth[key] for key in ("authorization_id", "issued_at", "expires_at")}
        return args, barrier, fields

    def fill(self, now, action="CONTROLLER_MODEL_ONE_CAS_ONLY"):
        req = N.request()
        return {"attempt_id": req["attempt_id"], "baseline_native_digest": req["config"]["baseline_native_digest"],
                "serializer": copy.deepcopy(req["serializer"]), "preinst_sha256": copy.deepcopy(req["evidence"]["preinst_sha256"]),
                "payload_sha256": copy.deepcopy(req["evidence"]["payload_sha256"]), "action": action,
                "issued_wall_ns": str(now * 10**9), "expires_wall_ns": str((now + 60) * 10**9)}

    def test_default_is_deterministic_invalid_drafts_without_fabricated_barrier(self):
        args, _, _ = self.fixture()
        result = M.compose(*args)
        self.assertEqual(result, M.compose(*args))
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["mutation_performed"])
        self.assertFalse(result["execution_authorized"])
        self.assertEqual(result["verdict"], "DRAFT_INPUTS_ONLY")
        self.assertIsNone(result["prepare"])
        self.assertIsNone(result["live_barrier"])
        self.assertEqual(result["live_barrier_template"]["authorization"], "NONE")
        self.assertFalse(result["live_barrier_template"]["mutation_performed"])
        for node in result["live_barrier_template"]["nodes"]:
            for key in ("evidence_sha256", "barrier", "workers_clear", "guard_quiescent", "old_consumers_absent"):
                self.assertIsNone(node[key])
        with self.assertRaises(M.CAS.Refusal): M.CAS.validate_request(result["native_cas_request"])

    def test_explicit_current_barrier_and_auth_project_exact_existing_bytes(self):
        args, barrier, fields = self.fixture()
        result = M.compose(*args, barrier=barrier, adapter_auth_fields=fields)
        prepare = result["prepare"]
        expected = M.ADAPTER.project(result["context"], result["topology_template"], args[4], args[5], args[3],
                                     barrier, result["adapter_authorization"], args[7])
        self.assertEqual(prepare["manifest"], expected["manifest"])
        self.assertEqual(prepare["sidecar"], expected["sidecar"])
        self.assertEqual(bytes.fromhex(prepare["manifest_bytes_hex"]), M.ADAPTER.canonical(expected["manifest"]))
        self.assertEqual(prepare["manifest_bytes_sha256"], M.raw_sha(bytes.fromhex(prepare["manifest_bytes_hex"])))
        self.assertEqual(result["native_cas_request"]["evidence"]["prepare_manifest_sha256"], prepare["manifest_bytes_sha256"])
        self.assertEqual(prepare["sidecar"]["actions"][-1],
                         {"node": "pve04", "role": "CONTROL_ONLY", "action": "VERIFY_CURRENT_ONLY"})
        self.assertEqual(result["authorization"], "NONE")

    def test_complete_collector_fields_match_current_cas_schema(self):
        args, barrier, fields = self.fixture()
        result = M.compose(*args, barrier=barrier, adapter_auth_fields=fields, cas_fill=self.fill(args[7]))
        cas = result["native_cas_request"]
        self.assertEqual(M.CAS.validate_request(cas), M.CAS.canonical(cas))
        self.assertEqual(result["missing_inputs"], [])
        self.assertTrue(result["native_cas_schema_complete"])
        self.assertEqual(cas["serializer"]["perl_hash_seed"], "0")
        self.assertEqual(cas["serializer"]["perl_perturb_keys"], "0")
        self.assertEqual(set(cas["evidence"]["preinst_sha256"]), {"pve01", "pve02", "pve03"})
        self.assertEqual(set(cas["evidence"]["payload_sha256"]), {"pve01", "pve02", "pve03", "pve04"})
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["execution_authorized"])

    def test_native_action_requires_explicit_operator_selection(self):
        args, barrier, fields = self.fixture()
        filled = self.fill(args[7], "NATIVE_PVE_CONFIG_CAS_ONCE")
        result = M.compose(*args, barrier=barrier, adapter_auth_fields=fields, cas_fill=filled)
        self.assertEqual(result["native_cas_request"]["authorization"]["action"], "NATIVE_PVE_CONFIG_CAS_ONCE")
        self.assertEqual(result["authorization"], "NONE")
        filled["action"] = "force"
        with self.assertRaises(M.Refusal): M.compose(*args, barrier=barrier, adapter_auth_fields=fields, cas_fill=filled)

    def test_saved_preflight_is_not_renewed_and_cannot_project_after_expiry(self):
        args, barrier, fields = self.fixture(); args[7] += 600
        result = M.compose(*args)
        self.assertTrue(all(value is False for value in result["preflight_fresh_by_node"].values()))
        self.assertIsNone(result["prepare"])
        fields.update(issued_at=args[7], expires_at=args[7]+60)
        barrier["observed_at"] = args[7]
        with self.assertRaises(M.Refusal): M.compose(*args, barrier=barrier, adapter_auth_fields=fields)

    def test_barrier_and_auth_not_inferred_from_preflight(self):
        args, barrier, fields = self.fixture()
        with self.assertRaises(M.Refusal): M.compose(*args, barrier=barrier)
        only_auth = M.compose(*args, adapter_auth_fields=fields)
        self.assertIsNone(only_auth["prepare"])
        self.assertIn("fresh_positive_live_barrier", only_auth["missing_inputs"])
        with self.assertRaises(M.Refusal): M.compose(*args, cas_fill=self.fill(args[7]))
        template = M.compose(*args)["live_barrier_template"]
        with self.assertRaises(M.Refusal): M.compose(*args, barrier=template, adapter_auth_fields=fields)

    def test_negative_barrier_and_expired_or_unbounded_auth_refuse(self):
        args, barrier, fields = self.fixture()
        for key in ("barrier", "workers_clear", "guard_quiescent", "old_consumers_absent"):
            bad = copy.deepcopy(barrier); bad["nodes"][0][key] = False
            with self.assertRaises(M.Refusal): M.compose(*args, barrier=bad, adapter_auth_fields=fields)
        for issued, expires in ((args[7]-100, args[7]-1), (args[7]+1, args[7]+60), (args[7], args[7]+1801)):
            bad = {**fields, "issued_at": issued, "expires_at": expires}
            with self.assertRaises(M.Refusal): M.compose(*args, barrier=barrier, adapter_auth_fields=bad)

    def test_exact_rendered_target_is_imported_not_regenerated(self):
        args, _, _ = self.fixture()
        rendered = args[5].replace(b"\tslt-vgname vg-a\n\tnodes pve01,pve02,pve03\n",
                                   b"\tnodes pve01,pve02,pve03\n\tslt-vgname vg-a\n", 1)
        self.assertNotEqual(rendered, args[5])
        args[5] = rendered
        result = M.compose(*args)
        self.assertEqual(result["supplied_target"]["bytes_hex"], rendered.hex())
        self.assertEqual(result["context"]["target_storage_cfg_sha256"], M.raw_sha(rendered))
        self.assertEqual(result["native_cas_request"]["config"]["target_hex"], rendered.hex())
        self.assertIn("MUST_BE_REVERIFIED", result["supplied_target"]["provenance"])

    def test_baseline_target_topology_and_role_drift_refuse(self):
        for mutation in (lambda a: a.__setitem__(4, a[4]+b"# drift\n"),
                         lambda a: a.__setitem__(5, a[5].replace(b"vg-a", b"vg-z")),
                         lambda a: a.__setitem__(6, "pve04"),
                         lambda a: a[1]["nodes"][0].update(boot_id="0"*36),
                         lambda a: a[3][3].update(action="UNPACK_CONFIGURE"),
                         lambda a: a[3][0]["role_evidence"].update(node="pve02")):
            args, _, _ = self.fixture(); mutation(args)
            with self.assertRaises(M.Refusal): M.compose(*args)

    def test_cas_filling_wrong_maps_serializer_and_time_refuse(self):
        args, barrier, fields = self.fixture()
        for mutation in (lambda f: f["preinst_sha256"].update(pve04="0"*64),
                         lambda f: f["payload_sha256"].pop("pve04"),
                         lambda f: f["serializer"].update(perl_hash_seed="random"),
                         lambda f: f.update(expires_wall_ns=str((args[7]-1)*10**9)),
                         lambda f: f.update(attempt_id=None)):
            fill = self.fill(args[7]); mutation(fill)
            with self.assertRaises(M.Refusal): M.compose(*args, barrier=barrier, adapter_auth_fields=fields, cas_fill=fill)

    def test_custom_namespace_scalars_and_numeric_aliases_refuse_without_callbacks(self):
        calls = []
        class EvilStr(str):
            def __eq__(self, other): calls.append("eq"); return True
            __hash__ = str.__hash__
        args, _, _ = self.fixture(); args[3][0]["node"] = EvilStr("pve01")
        with self.assertRaises(M.Refusal): M.compose(*args)
        args, _, _ = self.fixture(); args[0]["generation"] = True
        with self.assertRaises(M.Refusal): M.compose(*args)
        args, _, _ = self.fixture(); args[1][EvilStr("extra")] = None
        with self.assertRaises(M.Refusal): M.compose(*args)
        self.assertEqual(calls, [])

    def test_inputs_and_drafts_have_no_shared_mutable_state(self):
        args, barrier, fields = self.fixture(); original = copy.deepcopy([args, barrier, fields])
        result = M.compose(*args, barrier=barrier, adapter_auth_fields=fields)
        result["context"]["node_roles"]["pve04"] = "SAN_PARTICIPANT"
        result["native_cas_request"]["participants"][0]["node"] = "foreign"
        self.assertEqual([args, barrier, fields], original)

    def test_saved_tg36_artifacts_validate_as_historical_inputs_when_present(self):
        directory = ROOT / "outputs/tg35-live"
        paths = [directory / f"tg36-preflight-pve0{i}.json" for i in range(1, 5)]
        if not all(path.is_file() for path in paths):
            self.skipTest("untracked TG36 lab artifacts unavailable")
        topology = M.read_json(directory / "topology-template-tg36.json")
        evidence = M.read_json(directory / "topology-evidence-tg36.json")
        records = [M.read_json(path) for path in paths]
        rows = M.preflight_inputs(records, topology, evidence, max(row["observed_at"] for row in records)+1, 300)
        self.assertTrue(rows[-1]["node"])
        self.assertEqual(rows[-1]["action"], "VERIFY_CURRENT_ONLY")
        self.assertTrue(all(row["candidate"]["version"] == "0.9.0~rc5.15~tg36" for row in rows))

    def test_cli_is_stdout_only_and_preserves_non_authorizing_draft(self):
        args, _, _ = self.fixture()
        files = {"topology": args[1], "evidence": args[2], **{f"node{i}": row for i, row in enumerate(args[3])}}
        argv = ["--topology", "topology", "--topology-evidence", "evidence", "--baseline", "baseline",
                "--target-rendered", "target", "--tx", args[0]["tx"], "--generation", "1",
                "--executor", args[6], "--now", str(args[7])]
        for i in range(4): argv.extend(["--preflight", f"node{i}"])
        output = io.StringIO()
        with patch.object(M, "read_json", side_effect=lambda path: copy.deepcopy(files[path])), \
             patch.object(M, "read_raw", side_effect=lambda path: args[4] if path == "baseline" else args[5]), \
             patch.object(M.sys, "stdout", output):
            self.assertEqual(M.main(argv), 0)
        self.assertEqual(json.loads(output.getvalue()), M.compose(*args))

    def test_json_transport_rejects_duplicate_keys_and_nonfinite_numbers(self):
        for raw in (b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}'):
            with patch.object(M, "read_raw", return_value=raw), self.assertRaises(M.Refusal):
                M.read_json("unused")


if __name__ == "__main__":
    unittest.main()
