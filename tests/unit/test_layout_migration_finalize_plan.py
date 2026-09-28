import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/layout-migration-finalize-plan.py"
SPEC = importlib.util.spec_from_file_location("layout_migration_finalize_plan", PATH)
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class LayoutMigrationFinalizePlanTests(unittest.TestCase):
    now = 2_000_000_000

    def manifest(self):
        nodes = [
            {"name": "pve01", "boot_id": "11111111-1111-4111-8111-111111111111"},
            {"name": "pve02", "boot_id": "22222222-2222-4222-8222-222222222222"},
            {"name": "pve03", "boot_id": "33333333-3333-4333-8333-333333333333"},
        ]
        return {
            "schema": "slt-package-maintenance/v1", "tx": "a" * 32,
            "phase": "CONFIG_COMMITTED", "generation": 7,
            "issued_at": self.now - 120, "expires_at": self.now + 120,
            "cluster_name": "oke-dev", "corosync_conf_sha256": "c" * 64,
            "nodes": nodes,
            "candidate": {"package": "pve-sharedlvmthin",
                          "version": "0.9.0~rc5.13~tg34", "flavor": "dual",
                          "artifact_sha256": "d" * 64, "deb_sha256": "e" * 64},
            "baseline_storage_cfg_sha256": "b" * 64,
            "target_storage_cfg_sha256": "9" * 64,
            "allowed_effects": ["package-unpack", "package-configure-deferred"],
            "plan_sha256": "f" * 64,
            "node_evidence": [
                {"node": node["name"], "challenge": str(index) * 32,
                 "evidence_sha256": "8" * 64, "barrier": True,
                 "workers_clear": True, "guard_idle_disarmed": True,
                 "old_consumers_absent": True}
                for index, node in enumerate(nodes, 1)
            ],
        }

    def evidence(self, manifest, node, index, manifest_sha):
        boot = next(row["boot_id"] for row in manifest["nodes"]
                    if row["name"] == node)
        candidate = manifest["candidate"]
        return {
            "schema": "slt-package-finalize-node/v1",
            "challenge": str(index) * 32, "observed_at": self.now - index,
            "cluster_name": manifest["cluster_name"], "node": node,
            "boot_id": boot,
            "cluster_nodes": [row["name"] for row in manifest["nodes"]],
            "quorate": True,
            "corosync_conf_sha256": manifest["corosync_conf_sha256"],
            "storage_cfg_sha256": manifest["target_storage_cfg_sha256"],
            "active_manifest_sha256": manifest_sha,
            "receipt": {
                "schema": "slt-package-maintenance-receipt/v1",
                "tx": manifest["tx"], "generation": manifest["generation"],
                "phase": "PACKAGE_CONFIGURED_DEFERRED", "node": node,
                "boot_id": boot, "package": candidate["package"],
                "version": candidate["version"], "flavor": "dual",
                "artifact_sha256": candidate["artifact_sha256"],
                "manifest_sha256": manifest_sha,
                "storage_cfg_sha256": manifest["target_storage_cfg_sha256"],
                "recorded_at": self.now - 30,
            },
            "installed": {"package": candidate["package"],
                          "version": candidate["version"], "flavor": "dual",
                          "artifact_sha256": candidate["artifact_sha256"],
                          "dpkg_state": "installed"},
            "payload": {"dpkg_verify_complete": True,
                        "dpkg_verify_clean": True,
                        "package_file_list_sha256": "7" * 64},
            "workers": {"inventory_complete": True, "storage_processes": [],
                        "transient_units": [], "pve_tasks": []},
            "thinguard": {"service_active": True, "state": "IDLE",
                          "watchdog": "DISARMED", "pid": 100 + index,
                          "starttime": 1000 + index, "socket_inode": 2000 + index,
                          "sample_sha256": "6" * 64},
            "old_consumers_absent": True,
        }

    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        manifest = self.manifest()
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        manifest_sha = PLAN.digest(manifest_path.read_bytes())
        evidence_paths = []
        for index, node in enumerate(("pve01", "pve02", "pve03"), 1):
            path = root / f"{node}.json"
            path.write_text(json.dumps(self.evidence(
                manifest, node, index, manifest_sha), sort_keys=True) + "\n")
            evidence_paths.append(str(path))
        args = argparse.Namespace(manifest=str(manifest_path),
                                  node_evidence=evidence_paths,
                                  max_age_sec=300, max_skew_sec=60,
                                  now=self.now)
        return temp, args, manifest, evidence_paths

    def test_exact_all_configured_barrier_is_read_only_plan(self):
        temp, args, _, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        result = PLAN.evaluate(args)
        self.assertEqual(result["verdict"], "READY_FOR_REFRESH_PLAN")
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["mutation_performed"])
        self.assertEqual(result["nodes"], ["pve01", "pve02", "pve03"])

    def test_missing_node_or_duplicate_challenge_refuses(self):
        temp, args, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        args.node_evidence = paths[:2]
        with self.assertRaisesRegex(PLAN.Refusal, "exactly one"):
            PLAN.evaluate(args)
        args.node_evidence = paths
        second = json.loads(Path(paths[1]).read_text())
        first = json.loads(Path(paths[0]).read_text())
        second["challenge"] = first["challenge"]
        Path(paths[1]).write_text(json.dumps(second) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "challenges"):
            PLAN.evaluate(args)

    def test_reboot_or_wrong_receipt_refuses(self):
        temp, args, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        evidence = json.loads(Path(paths[1]).read_text())
        evidence["boot_id"] = "99999999-9999-4999-8999-999999999999"
        Path(paths[1]).write_text(json.dumps(evidence) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "rebooted"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        evidence = json.loads(Path(paths[1]).read_text())
        evidence["receipt"]["phase"] = "PREINST_ACCEPTED"
        Path(paths[1]).write_text(json.dumps(evidence) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "receipt transaction"):
            PLAN.evaluate(args)

    def test_payload_worker_guard_and_old_consumer_fail_closed(self):
        mutations = [
            (lambda value: value["payload"].update(dpkg_verify_clean=False),
             "payload attestation"),
            (lambda value: value["workers"]["pve_tasks"].append({"upid": "x"}),
             "worker or task"),
            (lambda value: value["thinguard"].update(watchdog="ARMED"),
             "ThinGuard"),
            (lambda value: value.update(old_consumers_absent=False),
             "old PVE consumer"),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                temp, args, _, paths = self.fixture()
                try:
                    evidence = json.loads(Path(paths[0]).read_text())
                    mutate(evidence)
                    Path(paths[0]).write_text(json.dumps(evidence) + "\n")
                    with self.assertRaisesRegex(PLAN.Refusal, message):
                        PLAN.evaluate(args)
                finally:
                    temp.cleanup()

    def test_manifest_effect_expansion_cannot_self_authorize_refresh(self):
        temp, args, manifest, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        manifest["allowed_effects"].append("service-refresh")
        Path(args.manifest).write_text(json.dumps(manifest) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "effect allowlist"):
            PLAN.evaluate(args)

    def test_stale_or_skewed_evidence_refuses(self):
        temp, args, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        evidence = json.loads(Path(paths[0]).read_text())
        evidence["observed_at"] = self.now - 301
        Path(paths[0]).write_text(json.dumps(evidence) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "stale"):
            PLAN.evaluate(args)

    def test_cross_node_payload_identity_must_match(self):
        temp, args, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        evidence = json.loads(Path(paths[2]).read_text())
        evidence["payload"]["package_file_list_sha256"] = "5" * 64
        Path(paths[2]).write_text(json.dumps(evidence) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "different installed payload"):
            PLAN.evaluate(args)


if __name__ == "__main__":
    unittest.main()
