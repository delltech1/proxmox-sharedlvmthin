import importlib.machinery
import importlib.util
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check"


def load_checker():
    loader = importlib.machinery.SourceFileLoader("recovery_check", str(CHECKER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class RecoveryCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.checker = load_checker()

    def test_exact_storage_config_is_selected(self):
        text = """sharedlvmthin: one
        slt-vgname vg-one

sharedlvmthin: two
        slt-vgname vg-two
        slt-expected-wwid 3600abcd
"""
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(text)
            path = handle.name
        cfg = self.checker.storage_config("two", path)
        self.assertEqual(cfg["slt-vgname"], "vg-two")
        self.assertEqual(cfg["slt-expected-wwid"], "3600abcd")
        Path(path).unlink()

    def test_mutating_probe_is_impossible(self):
        with self.assertRaisesRegex(RuntimeError, "mutating command"):
            self.checker.bounded_probe(["/sbin/lvremove", "vg/lv"])

    def test_no_dstate_is_positive_evidence(self):
        with mock.patch.object(self.checker.os, "listdir", return_value=["1", "self"]), \
             mock.patch.object(self.checker, "process_state", return_value="S"):
            status, relevant, unknown = self.checker.dstate_evidence(["testvg"])
        self.assertEqual((status, relevant, unknown), ("PASS", [], []))

    def test_unscoped_dstate_is_unknown_not_pass(self):
        with mock.patch.object(self.checker.os, "listdir", return_value=["42"]), \
             mock.patch.object(self.checker, "process_state", return_value="D"), \
             mock.patch("builtins.open", side_effect=OSError("hidden")):
            status, relevant, unknown = self.checker.dstate_evidence(["testvg"])
        self.assertEqual(status, "UNKNOWN")
        self.assertEqual(relevant, [])
        self.assertEqual(len(unknown), 1)

    def test_exact_storage_token_scopes_dstate(self):
        class FakeFile:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"lvs testvg/sltp-100"

        with mock.patch.object(self.checker.os, "listdir", return_value=["42"]), \
             mock.patch.object(self.checker, "process_state", return_value="D"), \
             mock.patch("builtins.open", return_value=FakeFile()):
            status, relevant, unknown = self.checker.dstate_evidence(["testvg"])
        self.assertEqual(status, "FAIL")
        self.assertEqual(len(relevant), 1)
        self.assertEqual(unknown, [])

    def run_main(self, *, actual_wwid="3600abcd", dstate="PASS"):
        cfg = {
            "slt-vgname": "testvg",
            "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid",
            "slt-expected-wwid": "3600abcd",
            "slt-expected-min-paths": "2",
        }

        def probe(command):
            joined = " ".join(command)
            out = ""
            if command[0].endswith("vgs"):
                out = "vg-uuid"
            elif command[0].endswith("pvs"):
                out = f"pv-uuid|/dev/mapper/{actual_wwid}"
            elif command[0].endswith("lvs"):
                out = "sltp-100|twi-aotz--|||pve-slt-sid-test"
            elif command[0].endswith("pvesm"):
                out = "test sharedlvmthin active 1 1 0 0%"
            elif command[0].endswith("multipath"):
                out = "|- active ready running\n`- active ready running"
            elif command[0].endswith("pvecm"):
                out = "Quorate: Yes"
            return {"status": "PASS", "rc": 0, "out": out, "err": "", "pid": 1, "state": None}

        with mock.patch.object(self.checker, "storage_config", return_value=cfg), \
             mock.patch.object(self.checker, "bounded_probe", side_effect=probe), \
             mock.patch.object(self.checker, "dstate_evidence", return_value=(dstate, [], ["x"] if dstate == "UNKNOWN" else [])), \
             mock.patch.object(self.checker.os.path, "exists", return_value=True), \
             redirect_stdout(StringIO()) as output:
            rc = self.checker.main(["test"])
        return rc, output.getvalue()

    def test_all_positive_evidence_allows_mutation(self):
        rc, output = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("STATE=HEALTHY", output)
        self.assertIn("SAFE_FOR_MUTATION=YES", output)

    def test_identity_mismatch_fails_closed(self):
        rc, output = self.run_main(actual_wwid="3600ffff")
        self.assertEqual(rc, 2)
        self.assertIn("WWID_MATCH=FAIL", output)
        self.assertIn("SAFE_FOR_MUTATION=NO", output)

    def test_ambiguous_dstate_fails_recovery_check_closed(self):
        rc, output = self.run_main(dstate="UNKNOWN")
        self.assertEqual(rc, 2)
        self.assertIn("NO_RELEVANT_DSTATE=UNKNOWN", output)
        self.assertIn("STATE=RECOVERY_REQUIRED", output)


if __name__ == "__main__":
    unittest.main()
