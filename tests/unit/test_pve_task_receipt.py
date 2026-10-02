import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "experiments" / "thick-generations" / "pve-task-receipt-verify.pl"


def row(upid, *, node="pve1", vmid="994500", task_type="resize", status="OK"):
    return {"upid": upid, "node": node, "id": vmid, "type": task_type, "status": status}


class PveTaskReceiptTests(unittest.TestCase):
    def verify(self, before, after):
        with tempfile.TemporaryDirectory() as directory:
            before_path = Path(directory) / "before.json"
            after_path = Path(directory) / "after.json"
            before_path.write_text(json.dumps(before), encoding="utf-8")
            after_path.write_text(json.dumps(after), encoding="utf-8")
            return subprocess.run(
                ["perl", str(VERIFY), str(before_path), str(after_path),
                 "pve1", "994500", "resize"],
                capture_output=True, text=True,
            )

    def test_accepts_exact_sole_new_successful_task(self):
        old = row("UPID:pve1:00000001:00000001:00000001:resize:994500:root@pam:")
        new = row("UPID:pve1:00000002:00000002:00000002:resize:994500:root@pam:")
        result = self.verify([old], [new, old])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), new["upid"])

    def test_rejects_error_or_nonterminal_task(self):
        for status in ("ERROR", "running", ""):
            with self.subTest(status=status):
                result = self.verify([], [row(
                    "UPID:pve1:00000002:00000002:00000002:resize:994500:root@pam:",
                    status=status,
                )])
                self.assertNotEqual(result.returncode, 0)

    def test_rejects_wrong_identity_and_wrong_type(self):
        cases = [
            row("UPID:pve2:00000002:00000002:00000002:resize:994500:root@pam:", node="pve2"),
            row("UPID:pve1:00000002:00000002:00000002:resize:994501:root@pam:", vmid="994501"),
            row("UPID:pve1:00000002:00000002:00000002:qmstart:994500:root@pam:", task_type="qmstart"),
        ]
        for candidate in cases:
            with self.subTest(candidate=candidate):
                self.assertNotEqual(self.verify([], [candidate]).returncode, 0)

    def test_rejects_zero_or_two_new_matching_tasks(self):
        one = row("UPID:pve1:00000002:00000002:00000002:resize:994500:root@pam:")
        two = row("UPID:pve1:00000003:00000003:00000003:resize:994500:root@pam:")
        self.assertNotEqual(self.verify([], []).returncode, 0)
        self.assertNotEqual(self.verify([], [one, two]).returncode, 0)

    def test_rejects_duplicate_upid_and_malformed_evidence(self):
        duplicate = row("UPID:pve1:00000002:00000002:00000002:resize:994500:root@pam:")
        self.assertNotEqual(self.verify([], [duplicate, duplicate]).returncode, 0)
        self.assertNotEqual(self.verify({}, [duplicate]).returncode, 0)
        self.assertNotEqual(self.verify([], [{"upid": "not-a-upid"}]).returncode, 0)

    def test_old_task_is_never_reaccepted(self):
        old = row("UPID:pve1:00000001:00000001:00000001:resize:994500:root@pam:")
        self.assertNotEqual(self.verify([old], [old]).returncode, 0)


if __name__ == "__main__":
    unittest.main()
