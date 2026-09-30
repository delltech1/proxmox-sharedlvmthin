import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments" / "thick-generations"


class Tg53ExperimentOracleTests(unittest.TestCase):
    def test_fiveway_does_not_call_mixed_terminal_receipts_pass(self):
        source = (EXP / "tg53-fiveway-mixed-wave.sh").read_text(encoding="utf-8")
        self.assertIn("TG53_FIVEWAY_RECEIPTS=COLLECTED", source)
        self.assertNotIn("TG53_FIVEWAY_RECEIPTS=PASS", source)

    def test_offline_canary_reader_refuses_running_vm(self):
        source = (EXP / "tg53-verify-canary-vm.sh").read_text(encoding="utf-8")
        self.assertIn('status="$(qm status "$VMID")"', source)
        self.assertIn("CANARY_VERIFY=REFUSED_VM_NOT_STOPPED", source)


if __name__ == "__main__":
    unittest.main()
