import copy
import importlib.util
import os
from pathlib import Path
import sys
import time
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("exact_baseline", ROOT / "experiments/thick-generations/layout-migration-exact-baseline-collector.py")
C = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {} if os.name == "posix" else {
        "fcntl": types.ModuleType("fcntl"), "resource": types.ModuleType("resource")}):
    SPEC.loader.exec_module(C)
    import test_layout_migration_barrier_backend as original
from test_layout_migration_exact_restore_model import fixture


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.base = original.Tests(); self.base.setUp()
        self.addCleanup(self.base.doCleanups)
        self.plan = fixture()
        self.plan["storage_cfg_sha256"] = self.base.plan["storage_cfg_sha256"]
        self.plan["workload_sha256"] = self.base.plan["workload_snapshot_sha256"]
        self.plan["issued_at"], self.plan["expires_at"] = 900, 1100

    def test_real_scoped_validator_derives_zero_consumers_and_control_only_record(self):
        for node in ("pve01", "pve04"):
            result = C.project(self.plan, self.base.raw(node), "a" * 64)
            self.assertEqual(result["node"], node)
            self.assertEqual(result["consumers"], [])
            self.assertEqual(result["package_sha256"], "a" * 64)

    def test_unregistered_running_guest_mapper_or_incomplete_inventory_refuses(self):
        for mutate in (lambda r: r.update(inventory_complete=False),
                       lambda r: r.update(managed_mappers=["unknown"]),
                       lambda r: r["guests"][0].update(status="running"),
                       lambda r: r["guests"][0].update(config="scsi0: lab:vm-999-disk-0\n"),
                       lambda r: r["observation"].update(quorate=False),
                       lambda r: r["observation"].update(pending_jobs=["queued"])):
            raw = self.base.raw("pve01"); mutate(raw)
            with self.assertRaises((C.Refusal, C.C.B.Refusal)):
                C.project(self.plan, raw, "a" * 64)

    def test_package_boot_or_unknown_service_state_refuses(self):
        with self.assertRaises(C.Refusal): C.project(self.plan, self.base.raw("pve01"), "b" * 64)
        raw = self.base.raw("pve01"); raw["services"][0]["active_state"] = "activating"
        with self.assertRaises(C.C.B.Refusal): C.project(self.plan, raw, "a" * 64)
        raw = self.base.raw("pve01"); raw["observation"]["boot_id"] = "changed"
        with self.assertRaises(C.C.B.Refusal): C.project(self.plan, raw, "a" * 64)

    def test_capture_is_exact_local_evidence_not_cluster_authority(self):
        value = {"observation": {"node": "pve04"}, "authority": "NONE"}
        journal = mock.Mock()
        journal.create_once.return_value = {"durable": True, "sha256": C.M.digest(value)}
        with mock.patch.object(C, "collect_local", return_value=value):
            result = C.capture_once(journal, self.plan, {})
        journal.create_once.assert_called_once_with(self.plan["tx"], "node-baseline:pve04", value)
        self.assertFalse(result["cluster_held"])
        self.assertFalse(result["release_authorized"])
        self.assertEqual(result["authority"], "NONE")


@unittest.skipUnless(os.name == "posix" and hasattr(os, "fork"), "POSIX bounded child tests")
class ChildTests(unittest.TestCase):
    def setUp(self):
        if os.getuid() != 0: self.skipTest("root-only local collector")
        self.plan = fixture(); self.plan["issued_at"] = int(time.time()) - 1
        self.plan["expires_at"] = self.plan["issued_at"] + 120

    def test_readonly_child_success_and_failure(self):
        with mock.patch.object(C, "_collect", return_value={"authority": "NONE"}):
            self.assertEqual(C.collect_local(self.plan, {}, timeout=2), {"authority": "NONE"})
        with mock.patch.object(C, "_collect", side_effect=RuntimeError("unknown")):
            with self.assertRaises(C.Refusal): C.collect_local(self.plan, {}, timeout=2)

    def test_timeout_and_oversized_child_never_return_success(self):
        started = time.monotonic()
        with mock.patch.object(C, "_collect", side_effect=lambda *_: time.sleep(10)):
            with self.assertRaises(C.Refusal): C.collect_local(self.plan, {}, timeout=1)
        self.assertLess(time.monotonic() - started, 5)
        with mock.patch.object(C, "_collect", return_value={"x": "a" * C.MAX_OUTPUT}):
            with self.assertRaises(C.Refusal): C.collect_local(self.plan, {}, timeout=2)


if __name__ == "__main__": unittest.main()
