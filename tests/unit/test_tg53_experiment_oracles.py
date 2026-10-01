import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
EXP = ROOT / "experiments" / "thick-generations"
SPEC = importlib.util.spec_from_file_location(
    "tg53_move_receipt_oracle", EXP / "tg53-move-receipt-oracle.py"
)
MOVE_ORACLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOVE_ORACLE)


class Tg53ExperimentOracleTests(unittest.TestCase):
    def test_fiveway_does_not_call_mixed_terminal_receipts_pass(self):
        source = (EXP / "tg53-fiveway-mixed-wave.sh").read_text(encoding="utf-8")
        self.assertIn("TG53_FIVEWAY_RECEIPTS=COLLECTED", source)
        self.assertNotIn("TG53_FIVEWAY_RECEIPTS=PASS", source)

    def test_offline_canary_reader_refuses_running_vm(self):
        source = (EXP / "tg53-verify-canary-vm.sh").read_text(encoding="utf-8")
        self.assertIn('status="$(qm status "$VMID")"', source)
        self.assertIn("CANARY_VERIFY=REFUSED_VM_NOT_STOPPED", source)

    def move_fixture(self, *, expected="REFUSED", result="admission refused: busy",
                     after_disk="src:vm-100-disk-0", source_after=None,
                     target_after=None, refusal_regex="admission refused"):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        source_before = ["src:vm-100-disk-0"]
        target_before = []
        docs = {
            "config_before": {"scsi0": "src:vm-100-disk-0,size=1G"},
            "config_after": {"scsi0": after_disk + ",size=1G"},
            "source_before": [{"volid": item} for item in source_before],
            "source_after": [{"volid": item} for item in (
                source_before if source_after is None else source_after)],
            "target_before": [{"volid": item} for item in target_before],
            "target_after": [{"volid": item} for item in (
                target_before if target_after is None else target_after)],
            "task_status": {
                "upid": "UPID:pve1:0001:0002:0003:qmmove:100:root@pam:",
                "status": "stopped", "exitstatus": result,
            },
        }
        paths = {}
        for name, document in docs.items():
            path = root / f"{name}.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            paths[name] = str(path)
        args = SimpleNamespace(
            expected=expected, refusal_regex=refusal_regex, disk="scsi0",
            source_volid="src:vm-100-disk-0", target_storage="dst", **paths,
        )
        return temporary, args

    def test_refusal_requires_exact_reason_and_unchanged_inventories(self):
        temporary, args = self.move_fixture()
        with temporary:
            _upid, _status, verdict = MOVE_ORACLE.evaluate(args)
        self.assertEqual(verdict, "REFUSED_BEFORE_EFFECT")

    def test_partial_target_error_is_not_a_refusal(self):
        temporary, args = self.move_fixture(target_after=["dst:vm-100-disk-0"])
        with temporary, self.assertRaisesRegex(ValueError, "target inventory changed"):
            MOVE_ORACLE.evaluate(args)

    def test_unexpected_worker_error_is_not_a_refusal(self):
        temporary, args = self.move_fixture(result="copy failed: I/O error")
        with temporary, self.assertRaisesRegex(ValueError, "not the expected admission"):
            MOVE_ORACLE.evaluate(args)

    def test_success_requires_config_inventory_and_source_settlement(self):
        temporary, args = self.move_fixture(
            expected="OK", result="OK", after_disk="dst:vm-100-disk-0",
            source_after=[], target_after=["dst:vm-100-disk-0"], refusal_regex="",
        )
        with temporary:
            _upid, _status, verdict = MOVE_ORACLE.evaluate(args)
        self.assertEqual(verdict, "OK_WITH_SETTLED_INVENTORY")

    def test_runner_bounds_client_and_tracks_original_receipt(self):
        source = (EXP / "tg53-one-move-with-receipt.sh").read_text(encoding="utf-8")
        self.assertIn("TG53_DISPATCH_DEADLINE_SEC", source)
        self.assertIn("TG53_RECEIPT_DEADLINE_SEC", source)
        self.assertIn("TIMED_OUT_UNKNOWN", source)
        self.assertIn('/tasks/$upid/status', source)
        self.assertNotIn("expected refusal but task succeeded", source)

    def test_snapshot_runners_detach_dispatch_from_observer(self):
        helper = (EXP / "tg53-async-dispatch.sh").read_text(encoding="utf-8")
        self.assertIn("systemd-run --quiet --collect", helper)
        self.assertIn("RESULT=OBSERVATION_DEADLINE_UNKNOWN", helper)
        self.assertIn('status.casefold() != "running"', helper)
        self.assertIn("RESULT=AMBIGUOUS_TASK_UNKNOWN", helper)
        self.assertNotIn("--property=RuntimeMaxSec", helper)
        for name in ("tg53-cross-node-snapshot-worker.sh",
                     "tg53-mixed-three-disk-cycle.sh",
                     "tg53-concurrent-snapshot-move.sh",
                     "tg53-fiveway-mixed-wave.sh"):
            source = (EXP / name).read_text(encoding="utf-8")
            self.assertIn("tg53_dispatch_detached", source)
            self.assertIn("tg53_wait_exact_task", source)
            self.assertNotIn("timeout --foreground", source)


if __name__ == "__main__":
    unittest.main()
