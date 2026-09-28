import copy
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("hold_archive_adapter", ROOT / "experiments/thick-generations/layout-migration-live-hold-archive.py")
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Tests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name) / "maintenance"
        root.mkdir(mode=0o700)
        for name in ("archive", "release-attempts", "release-certificates"):
            (root / name).mkdir(mode=0o700)
        now = int(time.time())
        names = ["pve01", "pve02", "pve03", "pve04"]
        boots = {n: f"{i:08d}-1111-4111-8111-111111111111" for i, n in enumerate(names, 1)}
        manifest = M.canonical({"schema": "slt-package-maintenance/v1", "phase": "CONFIG_COMMITTED", "tx": "a" * 32, "generation": 1})
        certificate = M.canonical({"schema": "slt-layout-release-commit/v2", "phase": "RELEASE_COMMITTED",
                                  "tx": "a" * 32, "generation": 1, "context_sha256": "b" * 64,
                                  "commit_id": "c" * 32, "release_not_after": now + 300}) + b"\n"
        active = root / "active.json"; active.write_bytes(manifest); active.chmod(0o600)
        cert_path = root / "release-certificates" / ("a" * 32 + "-1.json")
        cert_path.write_bytes(certificate); cert_path.chmod(0o600)
        hold = {"manifest_sha256": M.digest(manifest), "source_identity": M.FILES._identity(active.stat(), manifest),
                "certificate_identity": M.FILES._identity(cert_path.stat(), certificate)}
        plan = {"schema": "slt-layout-all-certified-plan/v2", "phase": "ALL_CERTIFIED",
                "verdict": "READY_FOR_SAN_HOLD_RELEASE_AUTHORIZATION", "authorization": "NONE",
                "hold_released": False, "mutation_performed": False, "tx": "a" * 32, "generation": 1,
                "context_sha256": "b" * 64, "commit_id": "c" * 32, "certificate_sha256": M.digest(certificate),
                "candidate": {}, "nodes": names, "node_roles": {n: "SAN_PARTICIPANT" if n != "pve04" else "CONTROL_ONLY" for n in names},
                "participant_boots": boots, "release_nodes": names[:3], "verify_only_nodes": names[3:],
                "node_ack_sha256": {n: str(i) * 64 for i, n in enumerate(names, 1)},
                "latest_verification_finished_at": now - 2, "release_not_after": now + 300,
                "hold_by_node": {n: copy.deepcopy(hold) for n in names[:3]}}
        plan["plan_sha256"] = M.digest(M.canonical(plan))
        auth = {"schema": "slt-live-hold-archive-authorization/v2", "tx": plan["tx"], "generation": 1,
                "context_sha256": plan["context_sha256"], "commit_id": plan["commit_id"], "node": "pve01", "boot_id": boots["pve01"],
                "all_certified_plan_sha256": plan["plan_sha256"], "certificate_sha256": plan["certificate_sha256"],
                "manifest_sha256": hold["manifest_sha256"], "source_identity_sha256": M.digest(M.canonical(hold["source_identity"])),
                "certificate_identity_sha256": M.digest(M.canonical(hold["certificate_identity"])), "operation_id": "d" * 32,
                "issued_at": now - 1, "expires_at": now + 120, "effect": "archive-exact-active-once"}
        kwargs = {"certified_inputs": {"now": 1}, "authorization": auth, "manifest_bytes": manifest,
                  "certificate_bytes": certificate, "node": "pve01", "boot_id": boots["pve01"]}
        return root, plan, kwargs

    def run_adapter(self, root, plan, kwargs):
        with patch.object(M, "_evaluate_certified", return_value=plan) as evaluate, patch.object(M, "_local_identity", return_value=(kwargs["node"], kwargs["boot_id"])):
            adapter = M.ArchiveAdapter(root, expected_uid=os.geteuid())
            result = adapter.archive_once(**kwargs)
            self.assertGreater(evaluate.call_args.args[0]["now"], 1)
            return adapter, result

    def test_exact_archive_uses_fresh_evaluator_and_never_claims_release(self):
        root, plan, kwargs = self.fixture()
        adapter, result = self.run_adapter(root, plan, kwargs)
        self.assertEqual(result["classification"], "LOCAL_HOLD_ARCHIVED")
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["cluster_released"])
        self.assertFalse((root / "active.json").exists())
        with self.assertRaises(M.Refusal):
            adapter.archive_once(**kwargs)

    def test_control_only_refuses_before_primitive_or_local_fact_read(self):
        root, plan, kwargs = self.fixture()
        kwargs["node"] = "pve04"; kwargs["boot_id"] = plan["participant_boots"]["pve04"]
        absent = root / "must-not-exist"
        with patch.object(M, "_evaluate_certified", return_value=plan), patch.object(M, "_local_identity") as local, patch.object(M.FILES, "LocalHoldReleaseLab") as primitive:
            with self.assertRaises(M.Refusal):
                M.ArchiveAdapter(absent).archive_once(**kwargs)
            local.assert_not_called(); primitive.assert_not_called()
        self.assertFalse(absent.exists())

    def test_forged_authorization_and_expiry_refuse_without_rename(self):
        for field, value in (("generation", True), ("expires_at", 1), ("source_identity_sha256", "0" * 64),
                             ("all_certified_plan_sha256", "0" * 64), ("effect", "release-cluster")):
            with self.subTest(field=field):
                root, plan, kwargs = self.fixture(); kwargs["authorization"][field] = value
                with self.assertRaises(M.Refusal):
                    self.run_adapter(root, plan, kwargs)
                self.assertTrue((root / "active.json").exists())

    def test_fresh_plan_failure_prevents_primitive(self):
        root, plan, kwargs = self.fixture()
        with patch.object(M, "_evaluate_certified", side_effect=M.Refusal("stale")), patch.object(M.FILES, "LocalHoldReleaseLab") as primitive:
            with self.assertRaises(M.Refusal):
                M.ArchiveAdapter(root).archive_once(**kwargs)
            primitive.assert_not_called()

    def test_active_and_certificate_inode_replacement_refuse(self):
        for target in ("active.json", "release-certificates/" + "a" * 32 + "-1.json"):
            with self.subTest(target=target):
                root, plan, kwargs = self.fixture(); path = root / target
                # Keep the original inode allocated, so the replacement cannot
                # coincidentally receive its inode number.
                path.rename(path.with_name(path.name + ".old"))
                path.write_bytes(kwargs["manifest_bytes"] if target == "active.json" else kwargs["certificate_bytes"]); path.chmod(0o600)
                with self.assertRaises(M.Refusal):
                    self.run_adapter(root, plan, kwargs)
                self.assertTrue((root / "active.json").exists())

    def test_live_boot_mismatch_has_no_effect(self):
        root, plan, kwargs = self.fixture()
        with patch.object(M, "_evaluate_certified", return_value=plan), patch.object(M, "_local_identity", return_value=("pve01", "other")), patch.object(M.FILES, "LocalHoldReleaseLab") as primitive:
            with self.assertRaises(M.Refusal):
                M.ArchiveAdapter(root).archive_once(**kwargs)
            primitive.assert_not_called()

    def test_malformed_plan_and_bytes_refuse(self):
        for mutate in (lambda p, k: p.update(release_nodes=p["nodes"]),
                       lambda p, k: k.update(certificate_bytes=k["certificate_bytes"] + b" "),
                       lambda p, k: p["hold_by_node"]["pve01"]["source_identity"].update(ino=1.0)):
            root, plan, kwargs = self.fixture(); mutate(plan, kwargs)
            plan.pop("plan_sha256"); plan["plan_sha256"] = M.digest(M.canonical(plan))
            kwargs["authorization"]["all_certified_plan_sha256"] = plan["plan_sha256"]
            with self.assertRaises(M.Refusal):
                self.run_adapter(root, plan, kwargs)
            self.assertTrue((root / "active.json").exists())

    def test_effect_then_error_is_retained_without_automatic_retry(self):
        root, plan, kwargs = self.fixture()
        original = M.FILES.LocalHoldReleaseLab.release
        calls = []
        def after_effect(store, *args, **kw):
            calls.append(1); original(store, *args, **kw)
            raise OSError("after effect")
        adapter = M.ArchiveAdapter(root, expected_uid=os.geteuid())
        with patch.object(M, "_evaluate_certified", return_value=plan), patch.object(M, "_local_identity", return_value=(kwargs["node"], kwargs["boot_id"])), patch.object(M.FILES.LocalHoldReleaseLab, "release", after_effect):
            with self.assertRaises(OSError): adapter.archive_once(**kwargs)
            with self.assertRaises(M.Refusal): adapter.archive_once(**kwargs)
        self.assertEqual(calls, [1]); self.assertFalse((root / "active.json").exists())


if __name__ == "__main__":
    unittest.main()
