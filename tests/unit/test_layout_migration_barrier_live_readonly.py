import copy
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = load("collector_test", ROOT / "experiments/thick-generations/layout-migration-barrier-collector.py")
L = load("latch_test", ROOT / "experiments/thick-generations/layout-migration-barrier-node-latch.py")
F = load("barrier_fixture", ROOT / "tests/unit/test_layout_migration_barrier_backend.py")


class Tests(unittest.TestCase):
    def setUp(self):
        self.fixture = F.Tests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.latch_root = Path(self.tmp.name) / "attempts"
        self.agent = self.fixture.agents["pve01"]
        self.request = {"tx": self.fixture.plan["tx"], "node": "pve01",
                        "plan_sha256": F.B.MODEL.frozen_hash(self.fixture.plan), "operation": "ENTRY_BLOCK"}

    def test_success_has_durable_request_and_exact_raw_receipt(self):
        response = L.DurableNodeAgent(self.agent, self.latch_root).request(self.request)
        inspected = L.inspect(self.request, self.latch_root)
        self.assertEqual(inspected["state"], "COMPLETED_HISTORICAL")
        self.assertEqual(inspected["records"][1]["response"], response)
        self.assertFalse(inspected["retry_authorized"])
        with self.assertRaises((FileExistsError, L.Refusal)):
            L.DurableNodeAgent(self.agent, self.latch_root).request(self.request)
        self.assertEqual(len(self.fixture.commands), 2)

    def test_effect_then_error_survives_new_agent_instance(self):
        original = self.agent.execute
        def failed(argv):
            original(argv); raise TimeoutError("unknown")
        self.agent.execute = failed
        with self.assertRaises(TimeoutError): L.DurableNodeAgent(self.agent, self.latch_root).request(self.request)
        self.assertEqual(L.inspect(self.request, self.latch_root)["state"], "RECOVERY_REQUIRED")
        fresh = F.B.NodeAgent(self.fixture.plan, "pve01", self.agent.collect, original, lambda: 1000)
        with self.assertRaises((FileExistsError, L.Refusal)):
            L.DurableNodeAgent(fresh, self.latch_root).request(self.request)
        self.assertEqual(len(self.fixture.commands), 1)

    def test_durable_write_failure_never_dispatches(self):
        with mock.patch.object(L, "_write_once", side_effect=OSError("fsync failed")):
            with self.assertRaises(OSError): L.DurableNodeAgent(self.agent, self.latch_root).request(self.request)
        self.assertEqual(self.fixture.commands, [])

    def test_result_persist_failure_refuses_replay(self):
        original = L._write_once
        def failed(backend, name, raw):
            if name.endswith("000001.json"): raise OSError("result fsync failed")
            return original(backend, name, raw)
        with mock.patch.object(L, "_write_once", failed):
            with self.assertRaises(OSError): L.DurableNodeAgent(self.agent, self.latch_root).request(self.request)
        self.assertEqual(L.inspect(self.request, self.latch_root)["state"], "RECOVERY_REQUIRED")
        self.assertEqual(len(self.fixture.commands), 2)

    def test_inspection_rejects_linked_or_foreign_records(self):
        L.DurableNodeAgent(self.agent, self.latch_root).request(self.request)
        root = self.latch_root / ("attempt-" + L.request_nonce(self.request))
        os.link(root / "exact-event-000000.json", root / "alias")
        with self.assertRaises(L.Refusal): L.inspect(self.request, self.latch_root)

    def test_safe_read_rejects_symlink_and_duplicate_json(self):
        root = Path(self.tmp.name)
        (root / "regular").write_bytes(b"abc")
        (root / "link").symlink_to(root / "regular")
        with self.assertRaises(OSError): C.read(root / "link")
        with self.assertRaises(C.Refusal): C.strict(b'{"a":1,"a":2}')
        self.assertEqual(C.read(root / "regular"), b"abc")

    def test_ha_active_idle_is_only_initial_empty_idle(self):
        rows = self.fixture.states["pve01"]["services"]
        with mock.patch.object(C, "read", side_effect=[b"", b'{"timestamp":1000,"state":"wait_for_agent_lock","mode":"active","results":{}}', b""]), mock.patch.object(C.time, "time", return_value=1001):
            self.assertEqual(C.ha_state("pve01", rows), "EMPTY_IDLE")

    def test_ha_disarm_fresh_is_only_initial_empty_idle(self):
        rows = self.fixture.states["pve01"]["services"]
        with mock.patch.object(C, "read", side_effect=[b"", b'{"timestamp":1000,"state":"wait_for_agent_lock","mode":"disarm","results":{}}', b""]), mock.patch.object(C.time, "time", return_value=1001):
            self.assertEqual(C.ha_state("pve01", rows), "EMPTY_IDLE")
        with mock.patch.object(C, "read", return_value=b"vm: 100\n"):
            with self.assertRaises(C.Refusal): C.ha_state("pve01", rows)

    def test_stale_ha_and_inactive_orphan_refuse(self):
        rows = copy.deepcopy(self.fixture.states["pve01"]["services"])
        with mock.patch.object(C, "read", side_effect=[b"", b'{"timestamp":1,"state":"wait_for_agent_lock","mode":"disarm","results":{}}']):
            with self.assertRaises(C.Refusal): C.ha_state("pve01", rows)
        row = next(r for r in rows if r["unit"] == "pve-ha-lrm.service")
        row.update(active_state="inactive", cgroup_empty=False)
        with mock.patch.object(C, "read", return_value=b""):
            with self.assertRaises(C.Refusal): C.ha_state("pve01", rows)

    def test_unknown_systemd_schema_refuses(self):
        with mock.patch.object(C, "command", return_value=b"ActiveState=active\nActiveState=inactive\n"):
            with self.assertRaises(C.Refusal): C.properties("pvedaemon.service")

    def test_only_exact_lxc_monitor_daemon_is_exempted(self):
        source = (ROOT / "experiments/thick-generations/layout-migration-barrier-collector.py").read_text()
        self.assertIn('executable == "/usr/libexec/lxc/lxc-monitord"', source)
        self.assertIn('argv == [b"/usr/libexec/lxc/lxc-monitord", b"--daemon"]', source)
        self.assertIn('raise Refusal("live LXC process requires qualified runtime inventory")', source)


if __name__ == "__main__": unittest.main()
