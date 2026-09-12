import importlib.machinery
import importlib.util
import hashlib
import re
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

    def test_transient_dstate_requires_and_passes_bounded_recheck(self):
        samples = [
            ("FAIL", ["pid=42 evidence=lvs testvg"], []),
            ("PASS", [], []),
        ]
        with mock.patch.object(
            self.checker, "dstate_evidence", side_effect=samples
        ) as evidence, mock.patch.object(self.checker.time, "sleep") as sleep:
            result = self.checker.settled_dstate_evidence(["testvg"])
        self.assertEqual(result, ("PASS", [], [], 1))
        self.assertEqual(evidence.call_count, 2)
        sleep.assert_called_once_with(self.checker.DSTATE_CONFIRM_SECONDS)

    def test_persistent_dstate_remains_fail_closed(self):
        sample = ("FAIL", ["pid=42 evidence=lvs testvg"], [])
        with mock.patch.object(
            self.checker, "dstate_evidence", side_effect=[sample, sample]
        ), mock.patch.object(self.checker.time, "sleep"):
            result = self.checker.settled_dstate_evidence(["testvg"])
        self.assertEqual(result, ("FAIL", sample[1], [], 0))

    def test_pve_snapshot_reference_scan_is_section_scoped_and_deduplicated(self):
        contents = (
            "scsi0: test:vm-100-disk-0,size=1G\n"
            "[snap-one]\n"
            "scsi0: test:vm-100-disk-0,size=1G\n"
            "scsi1: test:vm-100-disk-1,snapshot=0,size=1G\n"
            "[snap.two]\n"
            "scsi0: other:vm-100-disk-0,size=1G\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: ["/etc/pve/nodes/n/qemu-server/100.conf"],
        ), mock.patch("builtins.open", fake):
            result = self.checker.pve_snapshot_references("test")
        self.assertEqual(result, {("vm-100-disk-0", "snap-one")})

    def test_pve_snapshot_section_never_leaks_into_the_next_config(self):
        contents = {
            "/a.conf": "[old-snapshot]\nscsi0: test:vm-100-disk-0,size=1G\n",
            "/b.conf": "scsi0: test:vm-200-disk-0,size=1G\n",
        }

        def fake_open(path, **kwargs):
            return StringIO(contents[path])

        with mock.patch.object(
            self.checker.glob, "glob", side_effect=lambda pattern: list(contents)
        ), mock.patch("builtins.open", side_effect=fake_open):
            result = self.checker.pve_snapshot_references("test")
        self.assertEqual(result, {("vm-100-disk-0", "old-snapshot")})

    def test_thin_snapshot_inventory_must_exactly_match_pve(self):
        complete = (
            "sltp-100|twi-aotz--|||pve-slt-sid-test|\n"
            "vm-100-disk-0|Vwi-a-tz--||||sltp-100\n"
            "snap_vm-100-disk-0_good|Vri---tz--||||sltp-100"
        )
        references = lambda sid, vol: ["config"]
        expected = lambda sid: {("vm-100-disk-0", "good")}
        healthy, _, failures = self.checker.thin_reference_health(
            complete, "test", references, expected
        )
        self.assertTrue(healthy)
        self.assertEqual(failures, [])

        missing = complete.rsplit("\n", 1)[0]
        healthy, _, failures = self.checker.thin_reference_health(
            missing, "test", references, expected
        )
        self.assertFalse(healthy)
        self.assertIn("required by PVE but missing", failures[0])

        healthy, _, failures = self.checker.thin_reference_health(
            complete, "test", references, lambda sid: set()
        )
        self.assertFalse(healthy)
        self.assertIn("no matching PVE snapshot reference", failures[0])

    def test_malformed_thin_snapshot_identity_fails_closed(self):
        output = (
            "sltp-100|twi-aotz--|||pve-slt-sid-test|\n"
            "vm-100-disk-0|Vwi-a-tz--||||sltp-100\n"
            "snap_not-a-canonical-disk_bad|Vri---tz--||||sltp-100"
        )
        healthy, _, failures = self.checker.thin_reference_health(
            output, "test", lambda sid, vol: ["config"], lambda sid: set()
        )
        self.assertFalse(healthy)
        self.assertIn("malformed snapshot identity", failures[0])

    def run_main(self, *, actual_wwid="3600abcd", dstate="PASS", transient=0,
                 allocation_mode="thin", lvs_output=None, referenced=True):
        cfg = {
            "slt-vgname": "testvg",
            "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid",
            "slt-expected-wwid": "3600abcd",
            "slt-expected-min-paths": "2",
            "slt-allocation-mode": allocation_mode,
        }

        def probe(command):
            joined = " ".join(command)
            out = ""
            if command[0].endswith("vgs"):
                out = "vg-uuid"
            elif command[0].endswith("pvs"):
                out = f"pv-uuid|/dev/mapper/{actual_wwid}"
            elif command[0].endswith("lvs"):
                out = lvs_output or (
                    "sltp-100|twi-aotz--|||pve-slt-sid-test|\n"
                    "vm-100-disk-0|Vwi-a-tz--||||sltp-100"
                )
            elif command[0].endswith("pvesm"):
                out = "test sharedlvmthin active 1 1 0 0%"
            elif command[0].endswith("multipath"):
                out = "|- active ready running\n`- active ready running"
            elif command[0].endswith("pvecm"):
                out = "Quorate: Yes"
            return {"status": "PASS", "rc": 0, "out": out, "err": "", "pid": 1, "state": None}

        with mock.patch.object(self.checker, "storage_config", return_value=cfg), \
             mock.patch.object(self.checker, "bounded_probe", side_effect=probe), \
             mock.patch.object(self.checker, "settled_dstate_evidence", return_value=(dstate, [], ["x"] if dstate == "UNKNOWN" else [], transient)), \
             mock.patch.object(self.checker, "pve_reference_files", return_value=["ref"] if referenced else []), \
             mock.patch.object(self.checker, "pve_snapshot_references", return_value=set()), \
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

    def test_unreferenced_owned_thin_disk_fails_closed(self):
        rc, output = self.run_main(referenced=False)
        self.assertEqual(rc, 2)
        self.assertIn("THIN_REFERENCES_HEALTHY=FAIL", output)
        self.assertIn("vm-100-disk-0 in sltp-100 has no PVE reference", output)
        self.assertIn("SAFE_FOR_MUTATION=NO", output)

    def test_confirmed_transient_dstate_is_visible_but_allows_healthy_result(self):
        rc, output = self.run_main(transient=1)
        self.assertEqual(rc, 0)
        self.assertIn("NO_RELEVANT_DSTATE=PASS", output)
        self.assertIn("transient_dstate_tasks=1", output)
        self.assertIn("SAFE_FOR_MUTATION=YES", output)

    def test_ambiguous_dstate_fails_recovery_check_closed(self):
        rc, output = self.run_main(dstate="UNKNOWN")
        self.assertEqual(rc, 2)
        self.assertIn("NO_RELEVANT_DSTATE=UNKNOWN", output)
        self.assertIn("STATE=RECOVERY_REQUIRED", output)

    def test_interrupted_thick_anchor_blocks_mutation(self):
        name, tags = self.anchor(
            phase="HYDRATING", op="SNAPSHOT", snapshot="snap1",
            source="old", old="old", new="new", head="new", generation="2",
        )
        output = f"{name}|-wi------k|||{tags}"
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output
        )
        self.assertEqual(rc, 2)
        self.assertIn("THICK_ANCHORS_HEALTHY=FAIL", text)
        self.assertIn("phase=HYDRATING", text)
        self.assertIn("SAFE_FOR_MUTATION=NO", text)

    def test_materialized_thick_anchor_allows_other_positive_evidence(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output
        )
        self.assertEqual(rc, 0)
        self.assertIn("THICK_ANCHORS_HEALTHY=PASS", text)
        self.assertIn("SAFE_FOR_MUTATION=YES", text)

    def test_unreferenced_materialized_thick_anchor_fails_closed(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output,
            referenced=False,
        )
        self.assertEqual(rc, 2)
        self.assertIn("THICK_ANCHORS_HEALTHY=FAIL", text)
        self.assertIn("has no PVE reference", text)
        self.assertIn("SAFE_FOR_MUTATION=NO", text)

    @staticmethod
    def anchor(**updates):
        values = {
            "v": "5", "sid": "test", "vol": "vm-100-disk-0",
            "phase": "MATERIALIZED", "tx": "0123456789abcdef0123456789abcdef",
            "op": "ALLOC", "snapshot": "none", "source": "head", "old": "head",
            "new": "head", "head": "head", "generation": "1", "region": "8",
        }
        values.update(updates)
        order = ("v", "sid", "vol", "phase", "tx", "op", "snapshot", "source",
                 "old", "new", "head", "generation", "region")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        tags = ",".join([*(f"slt_tg_{key}={values[key]}" for key in order),
                         f"slt_tg_sha256={digest}"])
        name = "sltg-a-" + hashlib.sha256(
            ("vg-uuid" + "\0" + values["vol"]).encode()
        ).hexdigest()[:24]
        return name, tags

    def test_materialized_anchor_with_tampered_digest_fails_closed(self):
        name, tags = self.anchor()
        tags = re.sub(r"slt_tg_sha256=[0-9a-f]+", "slt_tg_sha256=" + "0" * 32, tags)
        rc, text = self.run_main(
            allocation_mode="thick-generations",
            lvs_output=f"{name}|-wi------k|||{tags}",
        )
        self.assertEqual(rc, 2)
        self.assertIn("anchor digest mismatch", text)

    def test_materialized_anchor_with_wrong_identity_name_fails_closed(self):
        _name, tags = self.anchor()
        rc, text = self.run_main(
            allocation_mode="thick-generations",
            lvs_output=f"sltg-a-{'f' * 24}|-wi------k|||{tags}",
        )
        self.assertEqual(rc, 2)
        self.assertIn("name does not match", text)

    def test_materialized_anchor_with_missing_head_fails_closed(self):
        name, tags = self.anchor()
        rc, text = self.run_main(
            allocation_mode="thick-generations",
            lvs_output=f"{name}|-wi------k|||{tags}",
        )
        self.assertEqual(rc, 2)
        self.assertIn("authoritative HEAD LV is missing", text)

    def test_materialized_anchor_with_retained_metadata_fails_closed(self):
        name, tags = self.anchor()
        key = name.removeprefix("sltg-a-")
        output = "\n".join([
            f"{name}|-wi------k|||{tags}", self.head_line(),
            f"sltg-m-{key}-00000001|-wi------k|||slt_tgt_v=1",
        ])
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output,
        )
        self.assertEqual(rc, 2)
        self.assertIn("retains transition metadata", text)

    @staticmethod
    def head_line():
        values = {
            "v": "1", "sid": "test", "vol": "vm-100-disk-0",
            "role": "head", "generation": "1",
        }
        order = ("v", "sid", "vol", "role", "generation")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        tags = ",".join([*(f"slt_tgo_{key}={values[key]}" for key in order),
                         f"slt_tgo_sha256={digest}"])
        return f"head|-wi------k|||{tags}"


if __name__ == "__main__":
    unittest.main()
