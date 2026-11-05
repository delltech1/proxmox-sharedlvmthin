"""Candidate offline audit refuses an active Lazy graph without fence proof."""
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import unittest
from unittest import mock

from test_package_preservation_scope import checker, inventory


class CandidateMissingFenceTests(unittest.TestCase):
    def test_preinstall_audit_rejects_active_but_accepts_closed_dormant(self):
        sid, volume, vg_uuid = "lazy", "vm-101-disk-0", "a" * 32
        cfg = {"id": sid, "type": "sharedlvmthin", "disable": "1",
               "slt-allocation-mode": "thick-generations-lazy", "slt-vgname": "vg-a",
               "slt-expected-vg-uuid": vg_uuid, "slt-expected-pv-uuid": "pv-uuid",
               "slt-expected-wwid": "1111"}
        for phase, expected_rc in (("LAZY_ACTIVE", 2), ("LAZY_DORMANT", 0)):
            with self.subTest(phase=phase), ExitStack() as stack:
                graph = inventory(sid, vg_uuid, volume, phase)
                def probe(command):
                    program = command[0]
                    if program == "/sbin/vgs":
                        output = "vg-a|" if "vg_name,vg_tags" in command else vg_uuid + "|4194304"
                    elif program == "/sbin/pvs":
                        output = "pv-uuid|/dev/mapper/1111"
                    elif program == "/sbin/lvs":
                        output = graph
                    elif program == "/sbin/multipath":
                        output = "active ready running"
                    else:
                        self.fail("unexpected probe: " + repr(command))
                    return {"status": "PASS", "out": output}
                replacements = {
                    "storage_configs": lambda: [cfg],
                    "bounded_probe": probe,
                    "thick_alias_group": lambda *args: {sid},
                    "pve_reference_inventory": lambda *args: ({volume: ["ref"]},
                        {volume: [("ref", 1048576, "attached")]}),
                    "pve_config_lock_health": lambda *args: ("PASS", []),
                    "settled_dstate_evidence": lambda *args: ("PASS", [], [], 0),
                    "active_storage_worker_evidence": lambda *args: ("PASS", [], []),
                }
                for name, replacement in replacements.items():
                    stack.enter_context(mock.patch.object(checker, name, side_effect=replacement))
                stack.enter_context(mock.patch.dict(checker.os.environ, {}, clear=True))
                stack.enter_context(mock.patch.object(checker.os.path, "exists", return_value=False))
                out = stack.enter_context(redirect_stdout(StringIO()))
                self.assertEqual(checker.main(["--preinstall", sid]), expected_rc, out.getvalue())
                self.assertIn("THICK_ANCHORS_HEALTHY=" + ("FAIL" if expected_rc else "PASS"), out.getvalue())
                self.assertIn("STATE=" + ("RECOVERY_REQUIRED" if expected_rc else "HEALTHY"), out.getvalue())


if __name__ == "__main__":
    unittest.main()
