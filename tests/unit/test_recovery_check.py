import importlib.machinery
import importlib.util
import hashlib
import re
import tempfile
import sys
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
CHECKER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check"


def load_checker():
    runtime_dir = str(CHECKER.parent)
    if runtime_dir not in sys.path:
        sys.path.insert(0, runtime_dir)
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

    def test_storage_inventory_parses_adjacent_blocks_and_rejects_duplicates(self):
        text = ("sharedlvmthin: eager\n\tslt-vgname vg\n"
                "sharedlvmthin: lazy\n\tslt-vgname vg\n")
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(text)
            path = handle.name
        try:
            self.assertEqual(
                [cfg["id"] for cfg in self.checker.storage_configs(path)],
                ["eager", "lazy"],
            )
        finally:
            Path(path).unlink()
        for bad in (
            "sharedlvmthin: x\n\tslt-vgname a\nsharedlvmthin: x\n\tslt-vgname b\n",
            "sharedlvmthin: x\n\tslt-vgname a\n\tslt-vgname b\n",
        ):
            with tempfile.NamedTemporaryFile("w", delete=False) as handle:
                handle.write(bad)
                path = handle.name
            try:
                with self.assertRaisesRegex(RuntimeError, "duplicate"):
                    self.checker.storage_configs(path)
            finally:
                Path(path).unlink()

    def test_storage_inventory_keeps_block_across_comments_and_blank_lines(self):
        text = ("sharedlvmthin:eager\n"
                "\tslt-vgname vg\n"
                "# operator comment\n\t# indented comment\n\n"
                "\tslt-vg-layout mixed\n")
        with tempfile.NamedTemporaryFile("w", delete=False) as handle:
            handle.write(text)
            path = handle.name
        try:
            configs = self.checker.storage_configs(path)
            self.assertEqual(configs[0]["slt-vg-layout"], "mixed")
        finally:
            Path(path).unlink()

    def test_pve_config_lock_gate_matches_only_managed_guests(self):
        contents = {
            "/100.conf": "lock: rollback\nscsi0: test:vm-100-disk-0,size=1G\n",
            "/101.conf": "lock: backup\nscsi0: other:vm-101-disk-0,size=1G\n",
            "/102.conf": (
                "scsi0: test:vm-102-disk-0,size=1G\n"
                "[snap]\nlock: rollback\n"
            ),
        }

        def fake_open(path, **kwargs):
            return StringIO(contents[path])

        with mock.patch.object(
            self.checker.glob, "glob", side_effect=lambda pattern: list(contents)
        ), mock.patch("builtins.open", side_effect=fake_open):
            status, failures = self.checker.pve_config_lock_health({"test"})
        self.assertEqual(status, "FAIL")
        self.assertEqual(len(failures), 1)
        self.assertIn("/100.conf", failures[0])
        self.assertIn("rollback", failures[0])

    def test_pve_config_lock_gate_rejects_malformed_or_unreadable_evidence(self):
        contents = {
            "/empty.conf": "lock:\nscsi0: test:vm-100-disk-0\n",
            "/duplicate.conf": (
                "lock: backup\nlock: migrate\nscsi0: test:vm-101-disk-0\n"
            ),
        }

        def fake_open(path, **kwargs):
            if path == "/unreadable.conf":
                raise OSError("synthetic read failure")
            return StringIO(contents[path])

        paths = [*contents, "/unreadable.conf"]
        with mock.patch.object(
            self.checker.glob, "glob", side_effect=lambda pattern: paths
        ), mock.patch("builtins.open", side_effect=fake_open):
            status, failures = self.checker.pve_config_lock_health({"test"})
        self.assertEqual(status, "FAIL")
        self.assertEqual(len(failures), 3)
        self.assertTrue(any("unreadable" in item for item in failures))

    @staticmethod
    def thick_cfg(sid, mode, **updates):
        cfg = {
            "id": sid, "type": "sharedlvmthin", "slt-vgname": "testvg",
            "shared": "1", "slt-vg-layout": "isolated",
            "slt-allocation-mode": mode,
            "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid",
            "slt-expected-wwid": "3600abcd", "nodes": "n2,n1",
        }
        cfg.update(updates)
        return cfg

    def test_exact_eager_lazy_alias_group_is_accepted(self):
        eager = self.thick_cfg("eager", "thick-generations")
        lazy = self.thick_cfg("lazy", "thick-generations-lazy", nodes="n1,n2")
        self.assertEqual(
            self.checker.thick_alias_group([eager, lazy], "lazy", lazy),
            {"eager", "lazy"},
        )

    def test_single_thick_alias_still_validates_mode_layout_and_identity(self):
        for cfg in (
            self.thick_cfg("eager", "thin"),
            self.thick_cfg("eager", "thick-generations", **{"slt-vg-layout": "mixed"}),
            self.thick_cfg("eager", "thick-generations", **{"shared": "0"}),
            self.thick_cfg("eager", "thick-generations", **{"slt-expected-wwid": ""}),
        ):
            with self.subTest(cfg=cfg):
                with self.assertRaises(RuntimeError):
                    self.checker.thick_alias_group([cfg], "eager", cfg)

    def test_thick_alias_group_rejects_unsafe_variants(self):
        eager = self.thick_cfg("eager", "thick-generations")
        variants = [
            [eager, self.thick_cfg("lazy", "thick-generations-lazy", nodes="n1")],
            [eager, self.thick_cfg("lazy", "thick-generations-lazy",
                                   **{"slt-tg-region-size-kib": "2048"})],
            [eager, self.thick_cfg("second", "thick-generations")],
            [eager, self.thick_cfg("thin", "thin")],
            [eager, self.thick_cfg("lazy", "thick-generations-lazy"),
             self.thick_cfg("third", "thick-generations-lazy")],
            [eager, {"id": "foreign", "type": "lvm", "vgname": "testvg"}],
        ]
        for configs in variants:
            with self.subTest(configs=[item["id"] for item in configs]):
                with self.assertRaises(RuntimeError):
                    self.checker.thick_alias_group(configs, "eager", eager)

    def test_sibling_owned_anchor_uses_owner_reference_namespace(self):
        name, tags = self.anchor(sid="eager")
        output = f"{name}|-wi------k|||{tags}\n{self.head_line(sid='eager')}"
        refs = {("eager", "vm-100-disk-0")}
        ok, _, failures = self.checker.thick_anchor_health(
            output, "lazy", "vg-uuid",
            lambda owner, vol: ["ref"] if (owner, vol) in refs else [],
            lambda owner, vol: [("ref", 2**30, "attached")]
            if (owner, vol) in refs else [],
            {"eager", "lazy"},
        )
        self.assertTrue(ok, failures)

    def test_materialized_vmstate_anchor_accepts_one_snapshot_reference(self):
        vol = "vm-100-state-ram-snapshot"
        name, tags = self.anchor(vol=vol)
        output = f"{name}|-wi------k|||{tags}\n{self.head_line(vol=vol)}"
        ok, _, failures = self.checker.thick_anchor_health(
            output, "test", "vg-uuid", lambda owner, name: ["ref"],
            lambda owner, name: [("ref", None, "auxiliary")], {"test"},
        )
        self.assertTrue(ok, failures)

    def test_disk_anchor_never_accepts_auxiliary_reference_role(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        ok, _, failures = self.checker.thick_anchor_health(
            output, "test", "vg-uuid", lambda owner, vol: ["ref"],
            lambda owner, vol: [("ref", None, "auxiliary")], {"test"},
        )
        self.assertFalse(ok)
        self.assertTrue(any("size is unavailable" in item for item in failures))

    def test_closed_lazy_dormant_anchor_and_owned_objects_are_healthy(self):
        sid = "lazy"
        vol = "vm-100-disk-1"
        tx = "1" * 32
        key = hashlib.sha256(("vg-uuid\0" + vol).encode()).hexdigest()[:24]
        head = f"sltg-g-{key}-00000000"
        metadata = f"sltg-m-{key}-00000000"
        values = {
            "v": "6", "sid": sid, "vol": vol, "phase": "LAZY_DORMANT",
            "tx": tx, "op": "ALLOC", "snapshot": "none", "source": head,
            "old": head, "new": head, "head": head, "generation": "0",
            "region": "2048", "policy": "lazy-zero", "bytes": str(2**30),
            "metadata": metadata, "data_uuid": "data-uuid",
            "metadata_uuid": "meta-uuid", "zero_source": "dm-zero",
            "publication": "1", "owner_node": "none", "owner_boot": "none",
            "owner_epoch": "none",
        }
        order = (
            "v", "sid", "vol", "phase", "tx", "op", "snapshot", "source",
            "old", "new", "head", "generation", "region", "policy", "bytes",
            "metadata", "data_uuid", "metadata_uuid", "zero_source",
            "publication", "owner_node", "owner_boot", "owner_epoch",
        )
        canonical = "|".join(f"{field}={values[field]}" for field in order)
        anchor_tags = ",".join([
            *(f"slt_tg_{field}={values[field]}" for field in order),
            "slt_tg_sha256=" + hashlib.sha256(canonical.encode()).hexdigest()[:32],
        ])

        def object_tags(kind, size):
            obj = {"v": "1", "sid": sid, "vol": vol, "tx": tx, "kind": kind,
                   "bytes": str(size), "region": "2048"}
            fields = ("v", "sid", "vol", "tx", "kind", "bytes", "region")
            encoded = "|".join(f"{field}={obj[field]}" for field in fields)
            return ",".join([
                *(f"slt_tgl_{field}={obj[field]}" for field in fields),
                "slt_tgl_sha256=" + hashlib.sha256(encoded.encode()).hexdigest()[:32],
            ])

        output = "\n".join((
            f"sltg-a-{key}|-wi------k|||{anchor_tags}|||8388608|anchor-uuid",
            f"{head}|-wi------k|||{object_tags('data', 2**30)}|||{2**30}|data-uuid",
            f"{metadata}|-wi------k|||{object_tags('metadata', 20 * 2**20)}|||{20 * 2**20}|meta-uuid",
        ))
        ok, _, failures = self.checker.thick_anchor_health(
            output, sid, "vg-uuid", lambda owner, name: ["ref"],
            lambda owner, name: [("ref", 2**30, "attached")], {sid},
        )
        self.assertTrue(ok, failures)

        prefix, _ = output.rsplit("meta-uuid", 1)
        tampered = prefix + "wrong-uuid"
        ok, _, failures = self.checker.thick_anchor_health(
            tampered, sid, "vg-uuid", lambda owner, name: ["ref"],
            lambda owner, name: [("ref", 2**30, "attached")], {sid},
        )
        self.assertFalse(ok)
        self.assertTrue(any("ownership does not match" in item for item in failures))

    def test_sibling_anchor_refuses_unknown_owner_head_drift_and_cross_reference(self):
        cases = []
        name, tags = self.anchor(sid="foreign")
        cases.append((f"{name}|-wi------k|||{tags}\n{self.head_line(sid='foreign')}",
                      lambda sid, vol: ["ref"], "outside the exact Thick alias group"))
        name, tags = self.anchor(sid="eager")
        cases.append((f"{name}|-wi------k|||{tags}\n{self.head_line(sid='lazy')}",
                      lambda sid, vol: ["ref"], "HEAD ownership does not match"))
        cases.append((f"{name}|-wi------k|||{tags}\n{self.head_line(sid='eager')}",
                      lambda sid, vol: ["ref"], "also referenced through sibling"))
        for output, refs, expected in cases:
            with self.subTest(expected=expected):
                ok, _, failures = self.checker.thick_anchor_health(
                    output, "lazy", "vg-uuid", refs,
                    lambda sid, vol: [("ref", 2**30, "attached")],
                    {"eager", "lazy"},
                )
                self.assertFalse(ok)
                self.assertTrue(any(expected in item for item in failures), failures)

    def test_duplicate_lvm_inventory_row_fails_closed(self):
        name, tags = self.anchor()
        anchor = f"{name}|-wi------k|||{tags}"
        output = "\n".join((anchor, anchor, self.head_line()))
        ok, _, failures = self.checker.thick_anchor_health(
            output, "test", "vg-uuid", lambda sid, vol: ["ref"],
            lambda sid, vol: [("ref", 2**30, "attached")], {"test"},
        )
        self.assertFalse(ok)
        self.assertIn("duplicate LVM inventory row", failures[0])

    def test_malformed_thick_anchor_lv_name_fails_closed(self):
        ok, _, failures = self.checker.thick_anchor_health(
            "sltg-a-not-canonical|-wi------k|||", "test", "vg-uuid",
        )
        self.assertFalse(ok)
        self.assertIn("malformed Thick anchor LV name", failures[0])

    def test_truncated_canonical_anchor_row_fails_closed(self):
        name, _ = self.anchor()
        ok, _, failures = self.checker.thick_anchor_health(
            f"{name}|-wi------k", "test", "vg-uuid",
        )
        self.assertFalse(ok)
        self.assertIn("malformed or truncated LVM inventory row", failures[0])

    def test_truncated_head_and_garbage_inventory_rows_fail_closed(self):
        name, tags = self.anchor()
        for extra in ("head|-wi", "unparseable"):
            with self.subTest(extra=extra):
                output = f"{name}|-wi------k|||{tags}\n{self.head_line()}\n{extra}"
                ok, _, failures = self.checker.thick_anchor_health(
                    output, "test", "vg-uuid", lambda sid, vol: ["ref"],
                    lambda sid, vol: [("ref", 2**30, "attached")], {"test"},
                )
                self.assertFalse(ok)
                self.assertTrue(any("malformed or truncated" in x for x in failures))

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

    def test_dstate_evidence_is_single_line_for_machine_readable_detail(self):
        class FakeFile:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return b"worker\x00arg\nblocked on io\tstack"

        with mock.patch.object(self.checker.os, "listdir", return_value=["42"]), \
             mock.patch.object(self.checker, "process_state", return_value="D"), \
             mock.patch("builtins.open", return_value=FakeFile()):
            status, relevant, unknown = self.checker.dstate_evidence(["other-vg"])
        self.assertEqual(status, "UNKNOWN")
        self.assertEqual(relevant, [])
        self.assertEqual(len(unknown), 1)
        self.assertNotRegex(unknown[0], r"[\r\n\t]")
        self.assertIn("worker arg blocked on io stack", unknown[0])

    def test_process_identity_binds_ppid_and_starttime(self):
        class FakeStat:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                fields = ["D", "77"] + ["0"] * 17 + ["123456"]
                return "42 (vgs worker) " + " ".join(fields)

        with mock.patch("builtins.open", return_value=FakeStat()):
            identity = self.checker.process_identity("42")
        self.assertEqual(
            identity, {"state": "D", "ppid": "77", "starttime": "123456"}
        )

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
            ("FAIL", ["pid=42 evidence=ppid=7 starttime=100 lvs testvg"], []),
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
        sample = ("FAIL", ["pid=42 evidence=ppid=7 starttime=100 lvs testvg"], [])
        with mock.patch.object(
            self.checker, "dstate_evidence", side_effect=[sample, sample]
        ), mock.patch.object(self.checker.time, "sleep"):
            result = self.checker.settled_dstate_evidence(["testvg"])
        self.assertEqual(result, ("FAIL", sample[1], [], 0))

    def test_rotating_transient_dstate_is_not_false_persistence(self):
        samples = [
            ("UNKNOWN", [], ["pid=42 evidence=ppid=7 starttime=100 pvs vg-a"]),
            ("UNKNOWN", [], ["pid=43 evidence=ppid=7 starttime=200 pvs vg-b"]),
        ]
        with mock.patch.object(
            self.checker, "dstate_evidence", side_effect=samples
        ), mock.patch.object(self.checker.time, "sleep"):
            result = self.checker.settled_dstate_evidence(["testvg"])
        self.assertEqual(result, ("PASS", [], [], 2))

    def test_dstate_without_stable_identity_remains_unknown(self):
        sample = ("UNKNOWN", [], ["pid=42 evidence=identity unavailable"])
        with mock.patch.object(
            self.checker, "dstate_evidence", side_effect=[sample, sample]
        ), mock.patch.object(self.checker.time, "sleep"):
            result = self.checker.settled_dstate_evidence(["testvg"])
        self.assertEqual(result, ("UNKNOWN", [], sample[2], 0))

    def test_exact_zeroout_worker_is_rejected_regardless_of_state(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root) / "42"
            proc.mkdir()
            (proc / "cmdline").write_bytes(
                b"/usr/sbin/blkdiscard\0--zeroout\0/dev/testvg/exact-head\0"
            )
            with mock.patch.object(self.checker, "process_state", return_value="S"):
                status, relevant, unknown = self.checker.active_storage_worker_evidence(
                    "testvg", root
                )
        self.assertEqual(status, "FAIL")
        self.assertIn("worker=blkdiscard", relevant[0])
        self.assertEqual(unknown, [])

    def test_exact_direct_write_worker_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            proc = Path(root) / "43"
            proc.mkdir()
            (proc / "cmdline").write_bytes(
                b"/usr/bin/dd\0if=/dev/zero\0of=/dev/testvg/exact-head\0"
            )
            status, relevant, unknown = self.checker.active_storage_worker_evidence(
                "testvg", root
            )
        self.assertEqual(status, "FAIL")
        self.assertIn("worker=dd", relevant[0])
        self.assertEqual(unknown, [])

    def test_unrelated_and_near_name_workers_are_ignored(self):
        with tempfile.TemporaryDirectory() as root:
            for pid, target in (("44", "/dev/other/exact-head"),
                                ("45", "/dev/testvg-evil/exact-head")):
                proc = Path(root) / pid
                proc.mkdir()
                (proc / "cmdline").write_bytes(
                    f"/usr/sbin/blkdiscard\0--zeroout\0{target}\0".encode()
                )
            status, relevant, unknown = self.checker.active_storage_worker_evidence(
                "testvg", root
            )
        self.assertEqual((status, relevant, unknown), ("PASS", [], []))

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

    def test_current_reference_size_ignores_snapshot_sections(self):
        contents = (
            "scsi0: test:vm-100-disk-0,size=4G\n"
            "[old-snapshot]\n"
            "scsi0: test:vm-100-disk-0,size=2G\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: ["/etc/pve/nodes/n/qemu-server/100.conf"],
        ), mock.patch("builtins.open", fake):
            result = self.checker.pve_current_reference_sizes(
                "test", "vm-100-disk-0"
            )
        self.assertEqual(
            result,
            [("/etc/pve/nodes/n/qemu-server/100.conf", 4 * 2**30, "attached")],
        )

    def test_vmstate_snapshot_section_is_authoritative_auxiliary_reference(self):
        contents = (
            "scsi0: test:vm-100-disk-0,size=4G\n"
            "[ram-snapshot]\n"
            "vmstate: test:vm-100-state-ram-snapshot\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: ["/etc/pve/nodes/n/qemu-server/100.conf"],
        ), mock.patch("builtins.open", fake):
            _references, current = self.checker.pve_reference_inventory("test")
        self.assertEqual(
            current["vm-100-state-ram-snapshot"],
            [("/etc/pve/nodes/n/qemu-server/100.conf", None, "auxiliary")],
        )

    def test_vmstate_inventory_covers_multiple_snapshots_and_suspend(self):
        contents = (
            "vmstate: test:vm-100-state-suspend\n"
            "[ram-one]\n"
            "vmstate: test:vm-100-state-ram-one\n"
            "[ram-two]\n"
            "vmstate: test:vm-100-state-ram-two\n"
        )
        fake = mock.mock_open(read_data=contents)
        path = "/etc/pve/nodes/n/qemu-server/100.conf"
        with mock.patch.object(
            self.checker.glob, "glob", side_effect=lambda pattern: [path]
        ), mock.patch("builtins.open", fake):
            _references, current = self.checker.pve_reference_inventory("test")
        self.assertEqual(
            set(current),
            {
                "vm-100-state-suspend",
                "vm-100-state-ram-one",
                "vm-100-state-ram-two",
            },
        )
        self.assertTrue(all(
            entries == [(path, None, "auxiliary")]
            for entries in current.values()
        ))

    def test_vmstate_inventory_rejects_wrong_vmid_and_non_scalar_schema(self):
        contents = (
            "vmstate: test:vm-200-state-wrong-owner\n"
            "[bad-alias]\n"
            "vmstate: file=test:vm-100-state-file-alias\n"
            "[bad-properties]\n"
            "vmstate: test:vm-100-state-extra,size=1G\n"
            "description: vmstate: test:vm-100-state-description-only\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: [
                "/etc/pve/nodes/n/qemu-server/100.conf"
            ],
        ), mock.patch("builtins.open", fake):
            _references, current = self.checker.pve_reference_inventory("test")
        self.assertEqual(
            {name: entries[0][2] for name, entries in current.items()},
            {
                "vm-200-state-wrong-owner": "invalid",
                "vm-100-state-file-alias": "invalid",
                "vm-100-state-extra": "invalid",
            },
        )
        self.assertNotIn("vm-100-state-description-only", current)

    def test_duplicate_vmstate_reference_is_not_silently_deduplicated(self):
        contents = (
            "[ram-one]\n"
            "vmstate: test:vm-100-state-shared\n"
            "[ram-two]\n"
            "vmstate: test:vm-100-state-shared\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: [
                "/etc/pve/nodes/n/qemu-server/100.conf"
            ],
        ), mock.patch("builtins.open", fake):
            _references, current = self.checker.pve_reference_inventory("test")
        self.assertEqual(len(current["vm-100-state-shared"]), 2)
        self.assertEqual(
            [entry[2] for entry in current["vm-100-state-shared"]],
            ["auxiliary", "auxiliary"],
        )

    def test_current_reference_rejects_description_and_duplicate_size(self):
        contents = (
            "description: test:vm-100-disk-0,size=4G\n"
            "scsi0: cache=none,file=test:vm-100-disk-0,size=4G,size=3G\n"
            "scsi1: test:vm-100-disk-1,file=test:vm-100-disk-2,size=4G\n"
            "scsi99999: test:vm-100-disk-3,size=4G\n"
            "unused0: test:vm-100-disk-4\n"
            "scsi2: cache=none,file=test:vm-100-disk-4,size=3G\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: ["/etc/pve/nodes/n/qemu-server/100.conf"],
        ), mock.patch("builtins.open", fake):
            result = self.checker.pve_current_reference_sizes(
                "test", "vm-100-disk-0"
            )
            references, current = self.checker.pve_reference_inventory("test")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1:], (None, "attached"))
        self.assertEqual(current["vm-100-disk-1"][0][2], "invalid")
        self.assertEqual(current["vm-100-disk-2"][0][2], "invalid")
        self.assertEqual(current["vm-100-disk-3"][0][2], "invalid")
        self.assertEqual(
            [entry[2] for entry in current["vm-100-disk-4"]],
            ["detached", "attached"],
        )

    def test_current_reference_accepts_reordered_lxc_volume_at_schema_boundary(self):
        contents = (
            "mp255: mp=/data,volume=test:vm-100-disk-0,size=1.5G\n"
            "mp256: mp=/bad,volume=test:vm-100-disk-1,size=4G\n"
        )
        fake = mock.mock_open(read_data=contents)
        with mock.patch.object(
            self.checker.glob, "glob",
            side_effect=lambda pattern: ["/etc/pve/nodes/n/lxc/100.conf"]
            if pattern == "/etc/pve/nodes/*/lxc/*.conf" else [],
        ), mock.patch("builtins.open", fake):
            _references, current = self.checker.pve_reference_inventory("test")
        self.assertEqual(
            current["vm-100-disk-0"][0][1:],
            (int(1.5 * 2**30), "attached"),
        )
        self.assertEqual(current["vm-100-disk-1"][0][2], "invalid")

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

    def test_detached_thin_metadata_recovery_artifact_fails_closed(self):
        output = (
            "sltp-100_meta0|-ri-a-----||||\n"
            "sltp-106|twi-aotz--|||pve-slt-sid-test|\n"
            "vm-106-disk-0|Vwi-a-tz--||||sltp-106"
        )
        healthy, pools, failures = self.checker.thin_reference_health(
            output, "test", lambda sid, vol: ["config"], lambda sid: set()
        )
        self.assertFalse(healthy)
        self.assertEqual(pools, ["sltp-106"])
        self.assertIn("detached thin metadata recovery artifact", failures[0])
        self.assertIn("manual identity and reference review", failures[0])

    def test_thin_owner_runtime_correlation_is_fail_closed(self):
        base = "sltp-100|twi-aotz--|||pve-slt-sid-test,pve-slt-owner-v1"
        healthy, failures = self.checker.thin_owner_runtime_health(
            base, "test", "test-vg", node="node1", path_exists=lambda path: False,
        )
        self.assertTrue(healthy)
        self.assertEqual(failures, [])

        local = base + (
            ",pve-slt-owner-node-node1,"
            "pve-slt-owner-epoch-0123456789abcdef0123456789abcdef"
        )
        expected_mapper = "/dev/mapper/test--vg-sltp--100-tpool"
        healthy, failures = self.checker.thin_owner_runtime_health(
            local, "test", "test-vg", node="node1",
            path_exists=lambda path: path == expected_mapper,
        )
        self.assertTrue(healthy)
        self.assertEqual(failures, [])

        healthy, failures = self.checker.thin_owner_runtime_health(
            local, "test", "test-vg", node="node1", path_exists=lambda path: False,
        )
        self.assertFalse(healthy)
        self.assertIn("activation outcome is ambiguous", failures[0])

        foreign = local.replace("owner-node-node1", "owner-node-node2")
        healthy, failures = self.checker.thin_owner_runtime_health(
            foreign, "test", "test-vg", node="node1", path_exists=lambda path: True,
        )
        self.assertFalse(healthy)
        self.assertIn("local tpool mapper", failures[0])

    def run_main(self, *, actual_wwid="3600abcd", dstate="PASS", transient=0,
                 allocation_mode="thin", lvs_output=None, referenced=True,
                 vg_tags="", observed_commands=None,
                 expected_wwid="3600abcd", disabled=False,
                 declared_size=2**30, reference_role="attached", argv=None,
                 config_lock_status="PASS"):
        cfg = {
            "slt-vgname": "testvg",
            "shared": "1",
            "slt-vg-layout": "isolated",
            "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid",
            "slt-expected-wwid": expected_wwid,
            "slt-expected-min-paths": "2",
            "slt-allocation-mode": allocation_mode,
        }
        if disabled:
            cfg["disable"] = "1"

        def probe(command):
            if observed_commands is not None:
                observed_commands.append(command)
            joined = " ".join(command)
            out = ""
            if command[0].endswith("vgs"):
                out = f"testvg|{vg_tags}" if "vg_name,vg_tags" in command else "vg-uuid|4194304"
            elif command[0].endswith("pvs"):
                out = f"pv-uuid|/dev/mapper/{actual_wwid}"
            elif command[0].endswith("lvs"):
                out = lvs_output or (
                    "sltp-100|twi-aotz--|||pve-slt-sid-test,pve-slt-owner-v1||20.00\n"
                    "vm-100-disk-0|Vwi-a-tz--||||sltp-100|"
                )
            elif command[0].endswith("pvesm"):
                out = "test sharedlvmthin active 1 1 0 0%"
            elif command[0].endswith("multipath"):
                out = "|- active ready running\n`- active ready running"
            elif command[0].endswith("pvecm"):
                out = "Quorate: Yes"
            return {"status": "PASS", "rc": 0, "out": out, "err": "", "pid": 1, "state": None}

        with mock.patch.object(
                 self.checker, "storage_configs",
                 return_value=[{"id": "test", "type": "sharedlvmthin", **cfg}],
             ), \
             mock.patch.object(self.checker, "bounded_probe", side_effect=probe), \
             mock.patch.object(self.checker, "settled_dstate_evidence", return_value=(dstate, [], ["x"] if dstate == "UNKNOWN" else [], transient)), \
             mock.patch.object(self.checker, "active_storage_worker_evidence", return_value=("PASS", [], [])), \
             mock.patch.object(self.checker, "pve_reference_files", return_value=["ref"] if referenced else []), \
             mock.patch.object(
                 self.checker, "pve_current_reference_sizes",
                 return_value=[("ref", declared_size, reference_role)] if referenced else [],
             ), \
             mock.patch.object(
                 self.checker, "pve_reference_inventory",
                 return_value=(
                     {"vm-100-disk-0": ["ref"]} if referenced else {},
                     {"vm-100-disk-0": [("ref", declared_size, reference_role)]}
                     if referenced else {},
                 ),
             ), \
             mock.patch.object(self.checker, "pve_snapshot_references", return_value=set()), \
             mock.patch.object(
                 self.checker, "pve_config_lock_health",
                 return_value=(config_lock_status,
                               ["managed guest is locked"]
                               if config_lock_status != "PASS" else []),
             ), \
             mock.patch.object(
                 self.checker.os.path, "exists",
                 side_effect=lambda path: not str(path).startswith("/dev/mapper/"),
             ), \
             redirect_stdout(StringIO()) as output:
            rc = self.checker.main(argv or ["test"])
        return rc, output.getvalue()

    def test_all_positive_evidence_allows_mutation(self):
        rc, output = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("STATE=HEALTHY", output)
        self.assertIn("SAFE_FOR_MUTATION=YES", output)

    def test_managed_pve_config_lock_blocks_safe_for_mutation(self):
        rc, output = self.run_main(config_lock_status="FAIL")
        self.assertEqual(rc, 2)
        self.assertIn("PVE_CONFIG_LOCKS_CLEAR=FAIL", output)
        self.assertIn("SAFE_FOR_MUTATION=NO", output)
        self.assertIn("managed guest is locked", output)

    def test_all_lvm_inventory_is_scoped_to_pinned_wwid(self):
        commands = []
        rc, _ = self.run_main(observed_commands=commands)
        self.assertEqual(rc, 0)
        lvm_commands = [
            command for command in commands
            if command[0] in {"/sbin/vgs", "/sbin/pvs", "/sbin/lvs"}
        ]
        self.assertEqual(len(lvm_commands), 4)
        for command in lvm_commands:
            position = command.index("--devices")
            self.assertEqual(command[position + 1], "/dev/mapper/3600abcd")

    def test_invalid_wwid_runs_no_unscoped_lvm_probe(self):
        commands = []
        rc, output = self.run_main(
            expected_wwid="not-a-wwid", observed_commands=commands,
        )
        self.assertEqual(rc, 2)
        self.assertEqual(commands, [])
        self.assertIn("VG_INTENT_CLEAR=FAIL", output)
        self.assertIn("THICK_ANCHORS_HEALTHY=FAIL", output)
        self.assertIn("no LVM probe was run", output)

    def test_disabled_storage_skips_only_pve_active_probe(self):
        commands = []
        name, tags = self.anchor()
        lvs_output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        rc, output = self.run_main(
            disabled=True, allocation_mode="thick-generations",
            lvs_output=lvs_output, observed_commands=commands,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(any(command[0].endswith("pvesm") for command in commands))
        self.assertEqual(
            sum(command[0] in {"/sbin/vgs", "/sbin/pvs", "/sbin/lvs"}
                for command in commands),
            4,
        )
        self.assertIn("PVE_STORAGE_HEALTH=PASS", output)
        self.assertIn("not applicable to explicitly disabled storage", output)
        self.assertIn("SAFE_FOR_MUTATION=YES", output)

    def test_preinstall_skips_only_unavailable_pve_plugin_probe(self):
        commands = []
        cfg = {
            "slt-vgname": "testvg", "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid", "slt-expected-wwid": "3600abcd",
            "slt-expected-min-paths": "2", "slt-allocation-mode": "thin",
        }

        def probe(command):
            commands.append(command)
            if command[0].endswith("vgs"):
                out = "testvg|" if "vg_name,vg_tags" in command else "vg-uuid|4194304"
            elif command[0].endswith("pvs"):
                out = "pv-uuid|/dev/mapper/3600abcd"
            elif command[0].endswith("lvs"):
                out = ""
            elif command[0].endswith("multipath"):
                out = "|- active ready running\n`- active ready running"
            elif command[0].endswith("pvecm"):
                out = "Quorate: Yes"
            else:
                out = ""
            return {"status": "PASS", "rc": 0, "out": out, "err": "", "pid": 1, "state": None}

        with mock.patch.object(
                 self.checker, "storage_configs",
                 return_value=[{"id": "test", "type": "sharedlvmthin", **cfg}],
             ), \
             mock.patch.object(self.checker, "bounded_probe", side_effect=probe), \
             mock.patch.object(self.checker, "settled_dstate_evidence", return_value=("PASS", [], [], 0)), \
             mock.patch.object(self.checker, "active_storage_worker_evidence", return_value=("PASS", [], [])), \
             mock.patch.object(self.checker, "pve_snapshot_references", return_value=set()), \
             redirect_stdout(StringIO()) as output:
            rc = self.checker.main(["--preinstall", "test"])
        self.assertEqual(rc, 0, output.getvalue())
        self.assertFalse(any(command[0].endswith("pvesm") for command in commands))
        self.assertIn("PVE_STORAGE_HEALTH=PASS", output.getvalue())
        self.assertIn("package offline audit", output.getvalue())

    def test_runtime_qualification_proves_storage_without_pve_activation(self):
        commands = []
        cfg = {
            "slt-vgname": "testvg", "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid", "slt-expected-wwid": "3600abcd",
            "slt-expected-min-paths": "2", "slt-allocation-mode": "thin",
        }

        def probe(command):
            commands.append(command)
            if command[0].endswith("vgs"):
                out = "testvg|" if "vg_name,vg_tags" in command else "vg-uuid|4194304"
            elif command[0].endswith("pvs"):
                out = "pv-uuid|/dev/mapper/3600abcd"
            elif command[0].endswith("lvs"):
                out = ""
            elif command[0].endswith("multipath"):
                out = "|- active ready running\n`- active ready running"
            elif command[0].endswith("pvecm"):
                out = "Quorate: Yes"
            else:
                out = ""
            return {"status": "PASS", "rc": 0, "out": out, "err": "",
                    "pid": 1, "state": None}

        with mock.patch.object(
                 self.checker, "storage_configs",
                 return_value=[{"id": "test", "type": "sharedlvmthin", **cfg}],
             ), \
             mock.patch.object(self.checker, "bounded_probe", side_effect=probe), \
             mock.patch.object(self.checker, "settled_dstate_evidence",
                               return_value=("PASS", [], [], 0)), \
             mock.patch.object(self.checker, "active_storage_worker_evidence",
                               return_value=("PASS", [], [])), \
             mock.patch.object(self.checker, "pve_snapshot_references", return_value=set()), \
             redirect_stdout(StringIO()) as output:
            rc = self.checker.main(["--runtime-qualification", "test"])
        text = output.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertFalse(any(command[0].endswith("pvesm") for command in commands))
        self.assertIn("PVE_STORAGE_HEALTH=NOT_PROBED", text)
        self.assertIn("SAFE_FOR_MUTATION=NO", text)
        self.assertIn("RUNTIME_QUALIFICATION_READY=YES", text)

    def test_invalid_disable_value_fails_without_probes(self):
        commands = []
        cfg = {
            "slt-vgname": "testvg", "slt-expected-vg-uuid": "vg-uuid",
            "slt-expected-pv-uuid": "pv-uuid", "slt-expected-wwid": "3600abcd",
            "disable": "ambiguous",
        }
        with mock.patch.object(
                 self.checker, "storage_configs",
                 return_value=[{"id": "test", "type": "sharedlvmthin", **cfg}],
             ), \
             mock.patch.object(self.checker, "bounded_probe", side_effect=commands.append), \
             redirect_stdout(StringIO()) as output:
            rc = self.checker.main(["test"])
        self.assertEqual(rc, 2)
        self.assertEqual(commands, [])
        self.assertIn("invalid disable value", output.getvalue())

    def test_nearly_full_thin_pool_is_scoped_capacity_warning(self):
        rc, output = self.run_main(lvs_output=(
            "sltp-100|twi-aotz--|||pve-slt-sid-test,pve-slt-owner-v1||100.00\n"
            "vm-100-disk-0|Vwi-a-tz--||||sltp-100|"
        ))
        self.assertEqual(rc, 0)
        self.assertIn("POOL_FLAGS_HEALTHY=PASS", output)
        self.assertIn("SAFE_FOR_MUTATION=YES", output)
        self.assertIn("capacity-warning: sltp-100 Data% is 100.00", output)

    def test_thin_pool_below_capacity_gate_passes(self):
        rc, output = self.run_main(lvs_output=(
            "sltp-100|twi-aotz--|||pve-slt-sid-test,pve-slt-owner-v1||94.99\n"
            "vm-100-disk-0|Vwi-a-tz--||||sltp-100|"
        ))
        self.assertEqual(rc, 0)
        self.assertIn("POOL_FLAGS_HEALTHY=PASS", output)

    def test_inactive_thin_pool_without_live_data_percent_is_not_full_evidence(self):
        rc, output = self.run_main(lvs_output=(
            "sltp-100|twi---tz--|||pve-slt-sid-test,pve-slt-owner-v1||\n"
            "vm-100-disk-0|Vwi---tz--||||sltp-100|"
        ))
        self.assertEqual(rc, 0)
        self.assertIn("POOL_FLAGS_HEALTHY=PASS", output)

    def test_identity_mismatch_fails_closed(self):
        rc, output = self.run_main(actual_wwid="3600ffff")
        self.assertEqual(rc, 2)
        self.assertIn("WWID_MATCH=FAIL", output)
        self.assertIn("SAFE_FOR_MUTATION=NO", output)

    def test_legacy_thin_owner_schema_fails_closed(self):
        lvs_output = (
            "sltp-100|twi-aotz--|||pve-slt-sid-test|\n"
            "vm-100-disk-0|Vwi-a-tz--||||sltp-100"
        )
        rc, output = self.run_main(lvs_output=lvs_output)
        self.assertEqual(rc, 2)
        self.assertIn("THIN_OWNER_STATE=FAIL", output)
        self.assertIn("lacks one exact owner schema", output)
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
        self.assertIn("DETAIL=unscoped_dstate=x", output)
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

    def test_lazy_mode_never_skips_the_thick_anchor_gate(self):
        name, tags = self.anchor(
            phase="HYDRATING", op="SNAPSHOT", snapshot="snap1",
            source="old", old="old", new="new", head="new", generation="2",
        )
        rc, text = self.run_main(
            allocation_mode="thick-generations-lazy",
            lvs_output=f"{name}|-wi------k|||{tags}",
        )
        self.assertEqual(rc, 2)
        self.assertIn("THICK_ANCHORS_HEALTHY=FAIL", text)
        self.assertIn("phase=HYDRATING", text)
        self.assertNotIn("anchor gate is not applicable", text)

    def test_materialized_thick_anchor_allows_other_positive_evidence(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output
        )
        self.assertEqual(rc, 0)
        self.assertIn("THICK_ANCHORS_HEALTHY=PASS", text)
        self.assertIn("SAFE_FOR_MUTATION=YES", text)

    def test_materialized_thick_anchor_rejects_stale_pve_size(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output,
            declared_size=3 * 2**30,
        )
        self.assertEqual(rc, 2)
        self.assertIn("THICK_ANCHORS_HEALTHY=FAIL", text)
        self.assertIn("current PVE size", text)
        self.assertIn("authoritative HEAD size", text)
        self.assertIn("SAFE_FOR_MUTATION=NO", text)

    def test_materialized_efi_accepts_exact_extent_rounded_528k_geometry(self):
        name, tags = self.anchor()
        head = self.head_line().rsplit("|", 1)[0] + "|4194304"
        rc, text = self.run_main(
            allocation_mode="thick-generations",
            lvs_output=f"{name}|-wi------k|||{tags}\n{head}",
            declared_size=528 * 1024,
            reference_role="efi",
        )
        self.assertEqual(rc, 0)
        self.assertIn("THICK_ANCHORS_HEALTHY=PASS", text)

    def test_ordinary_disk_never_gets_efi_rounding_exception(self):
        name, tags = self.anchor()
        head = self.head_line().rsplit("|", 1)[0] + "|4194304"
        rc, text = self.run_main(
            allocation_mode="thick-generations",
            lvs_output=f"{name}|-wi------k|||{tags}\n{head}",
            declared_size=528 * 1024,
            reference_role="attached",
        )
        self.assertEqual(rc, 2)
        self.assertIn("current PVE size", text)

    def test_open_extend_intent_is_visible_and_blocks_recovery_check(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        values = {
            "v": "1", "tx": "4" * 32, "state": "OPEN", "op": "EXTEND",
            "object": name, "before": "b" * 32,
        }
        order = ("v", "tx", "state", "op", "object", "before")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        intent = ",".join([
            *(f"slt_tg_vgi_{key}={values[key]}" for key in order),
            f"slt_tg_vgi_sha256={digest}",
        ])
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output,
            vg_tags=intent,
        )
        self.assertEqual(rc, 2)
        self.assertIn("VG_INTENT_CLEAR=FAIL", text)
        self.assertIn("OPEN EXTEND intent blocks mutation", text)
        self.assertIn("sharedlvmthin thick-recover-resize test <volume>", text)
        self.assertIn("SAFE_FOR_MUTATION=NO", text)

    def test_malformed_vg_intent_fails_closed(self):
        ok, details = self.checker.vg_intent_health(
            "testvg|slt_tg_vgi_v=1,slt_tg_vgi_v=1", "test", "testvg"
        )
        self.assertFalse(ok)
        self.assertIn("malformed or ambiguous", details[0])

    def test_open_remove_intent_names_only_the_reference_gated_recovery(self):
        name, tags = self.anchor()
        output = f"{name}|-wi------k|||{tags}\n{self.head_line()}"
        values = {
            "v": "1", "tx": "7" * 32, "state": "OPEN", "op": "REMOVE",
            "object": name, "before": "c" * 32,
        }
        order = ("v", "tx", "state", "op", "object", "before")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        intent = ",".join([
            *(f"slt_tg_vgi_{key}={values[key]}" for key in order),
            f"slt_tg_vgi_sha256={digest}",
        ])
        rc, text = self.run_main(
            allocation_mode="thick-generations", lvs_output=output,
            vg_tags=intent,
        )
        self.assertEqual(rc, 2)
        self.assertIn("OPEN REMOVE intent blocks mutation", text)
        self.assertIn(
            "sharedlvmthin thick-recover-volume-delete test <volume>", text
        )
        self.assertIn("SAFE_FOR_MUTATION=NO", text)

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
    def head_line(sid="test", vol="vm-100-disk-0", generation="1"):
        values = {
            "v": "1", "sid": sid, "vol": vol,
            "role": "head", "generation": generation,
        }
        order = ("v", "sid", "vol", "role", "generation")
        canonical = "|".join(f"{key}={values[key]}" for key in order)
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:32]
        tags = ",".join([*(f"slt_tgo_{key}={values[key]}" for key in order),
                         f"slt_tgo_sha256={digest}"])
        return f"head|-wi------k|||{tags}|||{2**30}"


if __name__ == "__main__":
    unittest.main()
