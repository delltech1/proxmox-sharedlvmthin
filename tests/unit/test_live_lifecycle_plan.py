import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "experiments/thick-generations/tg53-live-lifecycle-plan.py"
spec = importlib.util.spec_from_file_location("tg53_live_lifecycle_plan", SOURCE)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class LiveLifecycleDryRunPlanTests(unittest.TestCase):
    def plan(self):
        return planner.build(SimpleNamespace(
            nodes="PVE01,PVE02,PVE03", thin="slt-thin", eager="slt-eager",
            lazy="slt-lazy", backup="backup", template_vmid=9000, vmid_base=995100,
        ))

    def test_is_inert_and_complete(self):
        value = self.plan()
        self.assertEqual(value["authority"], "NONE")
        self.assertEqual(value["execution"], "FORBIDDEN_THIS_OUTPUT_IS_A_PLAN")
        self.assertEqual(len(value["vms"]), 10)
        self.assertEqual(len(value["phases"]["six_storage_moves"]), 6)
        self.assertEqual({vm["mode"] for vm in value["vms"]}, {"thin", "eager", "lazy"})
        for mode in ("thin", "eager", "lazy"):
            self.assertEqual({vm["managed_data_disks"] for vm in value["vms"] if vm["mode"] == mode},
                             {1, 2, 4})
        self.assertEqual({vm["nic"] for vm in value["vms"]},
                         {"none", "e1000", "virtio", "mixed"})
        self.assertEqual({vm["firmware"] for vm in value["vms"]}, {"bios", "efi"})
        self.assertTrue(any(vm["tpm"] for vm in value["vms"]))
        self.assertTrue(any(vm["cloud_init"] for vm in value["vms"]))
        commands = "\n".join(step["command"] for phase in value["phases"].values() for step in phase)
        for token in ("snapshot", "rollback", "delsnapshot", "resize", "vzdump", "qmrestore",
                      "move_disk", "qm migrate", "bulk-offline-evacuation.sh"):
            self.assertIn(token, commands)
        restore = next(step for step in value["phases"]["lifecycle"]
                       if step["id"] == "restore-new-vmid")
        self.assertIn("--unique 1", restore["command"])
        self.assertIn("--name SLT-RESTORED-995120", restore["command"])
        evidence = "\n".join(restore["expected_evidence"])
        for required in ("disk count, bus slots and sizes", "snapshot or vmstate",
                         "boots independently", "source VM configuration",
                         "canary SHA256"):
            self.assertIn(required, evidence)
        self.assertNotIn("subprocess", SOURCE.read_text(encoding="utf-8"))

    def test_failure_actions_require_distinct_manual_gate(self):
        value = self.plan()
        failures = value["phases"]["manual_failure_injection"]
        self.assertTrue(failures)
        for item in failures:
            self.assertTrue(item["destructive_gate"])
            self.assertTrue(item["failure_injection_gate"])
            self.assertTrue(item["command"].startswith("MANUAL_APPROVAL_REQUIRED:"))

    def test_unsafe_identifiers_are_refused(self):
        args = SimpleNamespace(nodes="PVE01,PVE02;rm,PVE03", thin="thin", eager="eager",
                               lazy="lazy", backup="backup", template_vmid=9000, vmid_base=995100)
        with self.assertRaises(SystemExit):
            planner.build(args)


if __name__ == "__main__":
    unittest.main()
