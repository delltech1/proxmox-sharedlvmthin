import argparse
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/layout-migration-all-ready-plan.py"
SPEC = importlib.util.spec_from_file_location("layout_migration_all_ready", PATH)
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class LayoutMigrationAllReadyPlanTests(unittest.TestCase):
    now = 2_000_000_000
    names = ["pve01", "pve02", "pve03"]
    units = [
        "pve-sharedlvmthin-thin-guard.service", "pvedaemon.service",
        "pvestatd.service", "pveproxy.service", "pve-ha-lrm.service",
    ]

    def manifest(self):
        nodes = [{"name": name,
                  "boot_id": (str(index) * 8 + "-" + str(index) * 4
                              + "-4" + str(index) * 3 + "-8"
                              + str(index) * 3 + "-" + str(index) * 12)}
                 for index, name in enumerate(self.names, 1)]
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
                {"node": name, "challenge": str(index) * 32,
                 "evidence_sha256": "8" * 64, "barrier": True,
                 "workers_clear": True, "guard_idle_disarmed": True,
                 "old_consumers_absent": True}
                for index, name in enumerate(self.names, 1)
            ],
        }

    def all_configured(self, manifest, manifest_sha):
        body = {
            "schema": "slt-layout-finalize-plan/v1", "phase": "ALL_CONFIGURED",
            "verdict": "READY_FOR_REFRESH_PLAN", "authorization": "NONE",
            "mutation_performed": False, "tx": manifest["tx"],
            "generation": manifest["generation"],
            "cluster_name": manifest["cluster_name"],
            "manifest_sha256": manifest_sha, "candidate": manifest["candidate"],
            "target_storage_cfg_sha256": manifest["target_storage_cfg_sha256"],
            "nodes": self.names,
            "node_evidence_sha256": {name: str(index + 3) * 64
                                     for index, name in enumerate(self.names)},
            "next_phase": "EXPLICIT_REFRESH_AUTHORIZATION_REQUIRED",
        }
        body["plan_sha256"] = PLAN.digest(PLAN.canonical(body))
        return body

    def authorization(self, manifest, manifest_sha, configured_sha):
        return {
            "schema": "slt-layout-refresh-authorization/v1",
            "tx": manifest["tx"], "generation": manifest["generation"],
            "manifest_sha256": manifest_sha,
            "all_configured_plan_sha256": configured_sha,
            "candidate": manifest["candidate"],
            "participant_boots": {row["name"]: row["boot_id"]
                                  for row in manifest["nodes"]},
            "target_storage_cfg_sha256": manifest["target_storage_cfg_sha256"],
            "corosync_conf_sha256": manifest["corosync_conf_sha256"],
            "authorization_id": "7" * 32,
            "issued_at": self.now - 30, "expires_at": self.now + 90,
            "node_order": self.names,
            "service_plan": [
                {"node": name, "daemon_reload": True,
                 "units": [{"unit": unit, "operation": "try-restart-active",
                            "was_active": unit != "pve-ha-lrm.service"}
                           for unit in self.units]}
                for name in self.names
            ],
            "challenges": {name: str(index + 4) * 32
                           for index, name in enumerate(self.names)},
        }

    def evidence(self, manifest, authorization, authorization_sha, node, index):
        boot = next(row["boot_id"] for row in manifest["nodes"]
                    if row["name"] == node)
        candidate = manifest["candidate"]
        units = []
        for offset, unit in enumerate(self.units, 1):
            active = unit != "pve-ha-lrm.service"
            before = {"active": active,
                      "pid": 1000 + index * 100 + offset if active else 0,
                      "starttime": 2000 + index * 100 + offset if active else 0}
            after = {"active": active,
                     "pid": 3000 + index * 100 + offset if active else 0,
                     "starttime": 4000 + index * 100 + offset if active else 0}
            units.append({"unit": unit,
                          "effect": "REFRESHED" if active else "PRESERVED_INACTIVE",
                          "terminal": True, "rc": 0,
                          "before": before, "after": after})
        started_at = self.now - 25 + (index - 1) * 5
        finished_at = started_at + 3
        journal_body = {
            "started_at": started_at, "finished_at": finished_at,
            "daemon_reload": {"attempted": True, "terminal": True, "rc": 0},
            "units": units,
        }
        guard_result = units[0]["after"]
        retired = [{"unit": item["unit"], "pid": item["before"]["pid"],
                    "starttime": item["before"]["starttime"]}
                   for item in units if item["before"]["active"]]
        return {
            "schema": "slt-package-all-ready-node/v1",
            "challenge": authorization["challenges"][node],
            "authorization_sha256": authorization_sha,
            "observed_at": self.now - index, "tx": manifest["tx"],
            "generation": manifest["generation"],
            "cluster_name": manifest["cluster_name"], "node": node,
            "boot_id": boot, "cluster_nodes": self.names, "quorate": True,
            "corosync_conf_sha256": manifest["corosync_conf_sha256"],
            "storage_cfg_sha256": manifest["target_storage_cfg_sha256"],
            "active_manifest_sha256": self.manifest_sha,
            "installed": {"package": candidate["package"],
                          "version": candidate["version"], "flavor": "dual",
                          "artifact_sha256": candidate["artifact_sha256"],
                          "dpkg_state": "installed"},
            "payload": {"dpkg_verify_complete": True,
                        "dpkg_verify_clean": True,
                        "package_file_list_sha256": "6" * 64},
            "workers": {"inventory_complete": True, "storage_processes": [],
                        "transient_units": [], "pve_tasks": [],
                        "retired_service_lifecycles_absent": retired},
            "thinguard": {"service_active": True, "state": "IDLE",
                          "watchdog": "DISARMED", "pid": guard_result["pid"],
                          "starttime": guard_result["starttime"],
                          "socket_inode": 10000 + index,
                          "sample_sha256": "5" * 64},
            "hold": {"active": True, "manifest_sha256": self.manifest_sha},
            "refresh": {"state": "COMPLETE",
                        "journal_sha256": PLAN.digest(PLAN.canonical(journal_body)),
                        **journal_body},
            "health": {"observed_at": finished_at + 1,
                       "storage_api": "PASS", "doctor": "PASS",
                       "recovery": "PASS", "unexpected": []},
        }

    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name)
        manifest = self.manifest()
        manifest_path = root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n")
        self.manifest_sha = PLAN.digest(manifest_path.read_bytes())
        configured = self.all_configured(manifest, self.manifest_sha)
        configured_path = root / "all-configured.json"
        configured_path.write_text(json.dumps(configured, sort_keys=True) + "\n")
        configured_sha = PLAN.digest(configured_path.read_bytes())
        authorization = self.authorization(manifest, self.manifest_sha,
                                           configured_sha)
        authorization_path = root / "authorization.json"
        authorization_path.write_text(json.dumps(authorization, sort_keys=True) + "\n")
        authorization_sha = PLAN.digest(authorization_path.read_bytes())
        evidence_paths = []
        for index, node in enumerate(self.names, 1):
            path = root / f"{node}.json"
            path.write_text(json.dumps(self.evidence(
                manifest, authorization, authorization_sha, node, index),
                sort_keys=True) + "\n")
            evidence_paths.append(str(path))
        args = argparse.Namespace(
            manifest=str(manifest_path), all_configured=str(configured_path),
            authorization=str(authorization_path), node_evidence=evidence_paths,
            max_age_sec=300, max_skew_sec=60, now=self.now)
        return temp, args, manifest, authorization, evidence_paths

    def mutate(self, path, callback):
        value = json.loads(Path(path).read_text())
        callback(value)
        Path(path).write_text(json.dumps(value, sort_keys=True) + "\n")

    def test_exact_all_ready_is_non_authorizing_release_plan(self):
        temp, args, _, _, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        result = PLAN.evaluate(args)
        self.assertEqual(result["verdict"], "READY_FOR_RELEASE_COMMIT")
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["mutation_performed"])
        self.assertEqual(result["ready_observed_at_max"], self.now - 1)
        self.assertEqual(result["next_phase"], "EXPLICIT_RELEASE_COMMIT_REQUIRED")

    def test_manifest_and_authorization_must_both_be_live(self):
        temp, args, _, _, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        args.now = self.now + 121
        with self.assertRaisesRegex(PLAN.Refusal, "transaction is not currently valid"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, authorization, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        authorization["expires_at"] = self.now - 1
        Path(args.authorization).write_text(json.dumps(authorization) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "authorization is stale"):
            PLAN.evaluate(args)

    def test_expected_challenge_and_exact_node_set_are_required(self):
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda value: value.update(challenge="f" * 32))
        with self.assertRaisesRegex(PLAN.Refusal, "challenge differs"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        args.node_evidence = paths[:2]
        with self.assertRaisesRegex(PLAN.Refusal, "exactly one"):
            PLAN.evaluate(args)

    def test_boot_config_artifact_and_hold_drift_refuse(self):
        mutations = [
            (lambda value: value.update(boot_id="9" * 8 + "-9999-4999-8999-" + "9" * 12),
             "cluster, boot, config or active hold"),
            (lambda value: value.update(storage_cfg_sha256="1" * 64),
             "cluster, boot, config or active hold"),
            (lambda value: value["installed"].update(artifact_sha256="1" * 64),
             "installed package"),
            (lambda value: value["hold"].update(active=False),
             "hold was released"),
        ]
        for callback, message in mutations:
            with self.subTest(message=message):
                temp, args, _, _, paths = self.fixture()
                try:
                    self.mutate(paths[0], callback)
                    with self.assertRaisesRegex(PLAN.Refusal, message):
                        PLAN.evaluate(args)
                finally:
                    temp.cleanup()

    def test_worker_guard_and_health_must_be_unambiguously_safe(self):
        mutations = [
            (lambda value: value["workers"]["pve_tasks"].append({"upid": "x"}),
             "worker or task"),
            (lambda value: value["thinguard"].update(watchdog="ARMED"),
             "ThinGuard"),
            (lambda value: value["health"].update(doctor="WARN"),
             "health contains"),
        ]
        for callback, message in mutations:
            with self.subTest(message=message):
                temp, args, _, _, paths = self.fixture()
                try:
                    self.mutate(paths[1], callback)
                    with self.assertRaisesRegex(PLAN.Refusal, message):
                        PLAN.evaluate(args)
                finally:
                    temp.cleanup()

    def test_refreshed_guard_and_retired_lifecycles_are_bound(self):
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda value: value["thinguard"].update(pid=99999))
        with self.assertRaisesRegex(PLAN.Refusal, "refreshed lifecycle"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[1], lambda value: value["workers"]
                    ["retired_service_lifecycles_absent"].pop())
        with self.assertRaisesRegex(PLAN.Refusal, "old service lifecycle"):
            PLAN.evaluate(args)

    def test_service_plan_and_lifecycle_are_exact(self):
        temp, args, _, authorization, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        authorization["service_plan"][0]["units"].pop()
        Path(args.authorization).write_text(json.dumps(authorization) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "missing or duplicate"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda value: value["refresh"]["units"][0]
                    ["after"].update(pid=value["refresh"]["units"][0]
                                        ["before"]["pid"],
                                    starttime=value["refresh"]["units"][0]
                                        ["before"]["starttime"]))
        with self.assertRaisesRegex(PLAN.Refusal, "new proven lifecycle"):
            PLAN.evaluate(args)

    def test_bool_rc_and_inactive_guard_intent_refuse(self):
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda value: value["refresh"]["daemon_reload"]
                    .update(rc=False))
        with self.assertRaisesRegex(PLAN.Refusal, "daemon-reload"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, authorization, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        authorization["service_plan"][0]["units"][0]["was_active"] = False
        Path(args.authorization).write_text(json.dumps(authorization) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "active ThinGuard"):
            PLAN.evaluate(args)

    def test_bool_and_float_schema_aliases_refuse(self):
        mutations = [
            (lambda value: value["hold"].update(active=1), "hold was released"),
            (lambda value: value["workers"].update(inventory_complete=1),
             "worker or task"),
            (lambda value: value.update(generation=7.0), "transaction.*differs"),
        ]
        for callback, message in mutations:
            with self.subTest(message=message):
                temp, args, _, _, paths = self.fixture()
                try:
                    self.mutate(paths[0], callback)
                    with self.assertRaisesRegex(PLAN.Refusal, message):
                        PLAN.evaluate(args)
                finally:
                    temp.cleanup()

    def test_refresh_time_order_and_journal_binding_refuse(self):
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda value: value["health"].update(
            observed_at=value["refresh"]["started_at"] - 1))
        with self.assertRaisesRegex(PLAN.Refusal, "out of order"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[1], lambda value: value["refresh"].update(
            journal_sha256="1" * 64))
        with self.assertRaisesRegex(PLAN.Refusal, "does not bind"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        first = json.loads(Path(paths[0]).read_text())
        def overlap(value):
            value["refresh"]["started_at"] = first["refresh"]["finished_at"] - 1
            body = {key: value["refresh"][key] for key in
                    ("started_at", "finished_at", "daemon_reload", "units")}
            value["refresh"]["journal_sha256"] = PLAN.digest(PLAN.canonical(body))
        self.mutate(paths[1], overlap)
        with self.assertRaisesRegex(PLAN.Refusal, "overlap"):
            PLAN.evaluate(args)

    def test_overlong_manifest_refuses_authorization(self):
        temp, args, manifest, authorization, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        manifest["expires_at"] = manifest["issued_at"] + 1801
        with self.assertRaisesRegex(PLAN.Refusal, "transaction is not currently valid"):
            PLAN.validate_authorization(
                authorization, manifest=manifest,
                manifest_sha=authorization["manifest_sha256"],
                all_configured_sha=authorization["all_configured_plan_sha256"],
                now=self.now)

    def test_authorization_cannot_predate_committed_manifest(self):
        temp, args, manifest, authorization, _ = self.fixture()
        self.addCleanup(temp.cleanup)
        authorization["issued_at"] = manifest["issued_at"] - 1
        Path(args.authorization).write_text(json.dumps(authorization) + "\n")
        with self.assertRaisesRegex(PLAN.Refusal, "predates CONFIG_COMMITTED"):
            PLAN.evaluate(args)

    def test_retired_lifecycle_rejects_numeric_aliases(self):
        for field, value in (("pid", 1101.0), ("starttime", True)):
            with self.subTest(field=field):
                temp, args, _, _, paths = self.fixture()
                try:
                    self.mutate(paths[0], lambda record: record["workers"]
                                ["retired_service_lifecycles_absent"][0]
                                .update({field: value}))
                    with self.assertRaisesRegex(
                            PLAN.Refusal, "lifecycle identity is invalid"):
                        PLAN.evaluate(args)
                finally:
                    temp.cleanup()

    def test_stale_duplicate_journal_and_schema_confusion_refuse(self):
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[0], lambda value: value.update(observed_at=self.now - 301))
        with self.assertRaisesRegex(PLAN.Refusal, "stale"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        first = json.loads(Path(paths[0]).read_text())
        def duplicate_refresh(value):
            value["refresh"] = first["refresh"]
            value["workers"]["retired_service_lifecycles_absent"] = first[
                "workers"]["retired_service_lifecycles_absent"]
            guard_after = first["refresh"]["units"][0]["after"]
            value["thinguard"]["pid"] = guard_after["pid"]
            value["thinguard"]["starttime"] = guard_after["starttime"]
        self.mutate(paths[1], duplicate_refresh)
        with self.assertRaisesRegex(PLAN.Refusal, "journals are duplicated"):
            PLAN.evaluate(args)
        temp.cleanup()
        temp, args, _, _, paths = self.fixture()
        self.addCleanup(temp.cleanup)
        self.mutate(paths[2], lambda value: value.update(release_committed=True))
        with self.assertRaisesRegex(PLAN.Refusal, "fields do not match"):
            PLAN.evaluate(args)


if __name__ == "__main__":
    unittest.main()
