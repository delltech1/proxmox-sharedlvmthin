import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/layout-migration-node-evidence-v2.py"
SPEC = importlib.util.spec_from_file_location("node_v2", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class Tests(unittest.TestCase):
    def args(self):
        return SimpleNamespace(topology_evidence="topology.json",
                               candidate_deb="candidate.deb",
                               challenge="a" * 32)

    def topology(self, role):
        return {"cluster_name": "oke-dev", "storage_cfg_sha256": "s" * 64,
                "topology_sha256": "t" * 64,
                "nodes": [{"name": "pve01", "boot_id": "boot", "san_role": role}]}

    def common(self, role, installed=None):
        installed = installed or {"version": "old", "artifact_sha256": "1" * 64}
        patches = [
            patch.object(M.V1, "regular_bytes", side_effect=[b"{}", b"storage", b"corosync", b"storage"]),
            patch.object(M.TOPO, "validate_topology_evidence", return_value=self.topology(role)),
            patch.object(M.TOPO, "validate", return_value=self.topology(role)),
            patch.object(M.hashlib, "sha256", return_value=SimpleNamespace(hexdigest=lambda: "s" * 64)),
            patch.object(M.V1, "membership", side_effect=[("pve01", 1, True, ["pve01"]), ("pve01", 1, True, ["pve01"])]),
            patch.object(M.V1, "pseudo_bytes", return_value=b"boot"),
            patch.object(M, "candidate_identity", return_value={"package": "pve-sharedlvmthin", "version": "new", "architecture": "all", "flavor": "dual", "deb_sha256": "3" * 64, "artifact_sha256": "2" * 64}),
            patch.object(M.V1, "installed_identity", return_value=installed),
            patch.object(M, "inactive_guard_evidence", return_value={"service_active_state": "inactive", "main_pid": 0, "jobs": [], "daemon_processes": [], "watchdog_registrations": []}),
            patch.object(M.V1, "worker_evidence", return_value={"storage_processes": [], "blocking_transient_units": [], "active_pve_tasks": []}),
            patch.object(M.TOPO, "bind_node_evidence"),
        ]
        return patches

    def run_with(self, patches):
        for item in patches:
            item.start()
        try:
            return M.collect(self.args())
        finally:
            for item in reversed(patches):
                item.stop()

    def test_san_nodes_are_unpack_configure_and_keep_vg_proof(self):
        patches = self.common("SAN_PARTICIPANT") + [
            patch.object(M.V1, "expected_mixed_vgs", return_value=[{"vg_name": "vg"}]),
            patch.object(M.V1, "actual_lvm_identities", return_value=[{"vg_name": "vg"}]),
        ]
        result = self.run_with(patches)
        self.assertEqual(result["action"], "UNPACK_CONFIGURE")
        self.assertEqual(result["role_evidence"]["vg_identities"], [{"vg_name": "vg"}])
        self.assertFalse(result["authorizes_mutation"])

    def test_control_node_is_verify_only_with_no_san_inventory(self):
        patches = self.common("CONTROL_ONLY", {"version": "new", "artifact_sha256": "2" * 64})
        result = self.run_with(patches)
        self.assertEqual(result["action"], "VERIFY_CURRENT_ONLY")
        self.assertEqual(result["role_evidence"]["vg_identities"], [])

    def test_control_node_refuses_nonmatching_payload(self):
        with self.assertRaisesRegex(M.Refusal, "exact candidate"):
            self.run_with(self.common("CONTROL_ONLY"))

    def test_any_activity_refuses(self):
        patches = self.common("SAN_PARTICIPANT") + [
            patch.object(M.V1, "expected_mixed_vgs", return_value=[{"vg_name": "vg"}]),
            patch.object(M.V1, "actual_lvm_identities", return_value=[{"vg_name": "vg"}]),
        ]
        patches[9] = patch.object(M.V1, "worker_evidence", return_value={
            "storage_processes": [{"pid": 7}], "blocking_transient_units": [],
            "active_pve_tasks": []})
        with self.assertRaisesRegex(M.Refusal, "activity"):
            self.run_with(patches)

    def test_inactive_guard_ignores_only_wrapped_proc_disappearance(self):
        def gone(*args, **kwargs):
            try:
                raise FileNotFoundError("process exited")
            except FileNotFoundError as error:
                raise M.Refusal("process command line is unavailable") from error
        with patch.object(M.V1, "systemd_property", side_effect=["inactive", "0"]), \
             patch.object(M.V1, "command", return_value=b""), \
             patch.object(M.Path, "iterdir", return_value=[Path("/proc/123")]), \
             patch.object(M.V1, "pseudo_bytes", side_effect=gone):
            result = M.inactive_guard_evidence()
        self.assertEqual(result["daemon_processes"], [])

    def test_inactive_guard_keeps_other_proc_failures_closed(self):
        with patch.object(M.V1, "systemd_property", side_effect=["inactive", "0"]), \
             patch.object(M.V1, "command", return_value=b""), \
             patch.object(M.Path, "iterdir", return_value=[Path("/proc/123")]), \
             patch.object(M.V1, "pseudo_bytes", side_effect=M.Refusal("permission denied")):
            with self.assertRaisesRegex(M.Refusal, "permission denied"):
                M.inactive_guard_evidence()

    def test_source_is_read_only_and_marks_non_authorization(self):
        source = PATH.read_text(encoding="utf-8")
        for forbidden in ('command(["dpkg",', 'command(["systemctl", "start"',
                          'command(["pvesm", "set"', 'command(["lvchange"'):
            self.assertNotIn(forbidden, source)
        self.assertIn('"authorizes_mutation": False', source)
        self.assertIn('"mutation_performed": False', source)


if __name__ == "__main__":
    unittest.main()
