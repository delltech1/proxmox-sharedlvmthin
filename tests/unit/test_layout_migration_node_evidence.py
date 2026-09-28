import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "experiments/thick-generations/layout-migration-node-evidence.py"
SPEC = importlib.util.spec_from_file_location("layout_migration_node_evidence", MODULE_PATH)
COLLECT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(COLLECT)


class LayoutMigrationNodeEvidenceTests(unittest.TestCase):
    def storage(self, *, mismatch=False, third=False):
        extra = (
            "sharedlvmthin: extra\n\tslt-vgname vg-a\n"
            "\tslt-allocation-mode thick-generations-lazy\n"
            "\tslt-expected-vg-uuid VGa\n\tslt-expected-pv-uuid PVa\n"
            "\tslt-expected-wwid WWIDa\n" if third else ""
        )
        thick_pv = "OTHER" if mismatch else "PVa"
        return (
            "sharedlvmthin: thin-a\n\tslt-vgname vg-a\n"
            "\tslt-allocation-mode thin\n\tslt-expected-vg-uuid VGa\n"
            "\tslt-expected-pv-uuid PVa\n\tslt-expected-wwid WWIDa\n"
            "sharedlvmthin: thick-a\n\tslt-vgname vg-a\n"
            "\tslt-allocation-mode thick-generations\n"
            "\tslt-expected-vg-uuid VGa\n"
            f"\tslt-expected-pv-uuid {thick_pv}\n"
            "\tslt-expected-wwid WWIDa\n"
            "sharedlvmthin: thin-b\n\tslt-vgname vg-b\n"
            "\tslt-allocation-mode thin\n\tslt-expected-vg-uuid VGb\n"
            "\tslt-expected-pv-uuid PVb\n\tslt-expected-wwid WWIDb\n"
            "sharedlvmthin: lazy-b\n\tslt-vgname vg-b\n"
            "\tslt-allocation-mode thick-generations-lazy\n"
            "\tslt-expected-vg-uuid VGb\n\tslt-expected-pv-uuid PVb\n"
            "\tslt-expected-wwid WWIDb\n" + extra
        ).encode()

    def test_exact_two_mixed_vgs_are_derived_from_pinned_config(self):
        result = COLLECT.expected_mixed_vgs(self.storage())
        self.assertEqual(result, [
            {"vg_name": "vg-a", "vg_uuid": "VGa", "pv_uuid": "PVa",
             "wwid": "WWIDa"},
            {"vg_name": "vg-b", "vg_uuid": "VGb", "pv_uuid": "PVb",
             "wwid": "WWIDb"},
        ])

    def test_alias_identity_mismatch_and_third_alias_refuse(self):
        with self.assertRaisesRegex(COLLECT.Refusal, "disagree"):
            COLLECT.expected_mixed_vgs(self.storage(mismatch=True))
        with self.assertRaisesRegex(COLLECT.Refusal, "exactly two aliases"):
            COLLECT.expected_mixed_vgs(self.storage(third=True))

    @patch.object(COLLECT, "command")
    def test_physical_lvm_identity_must_match_mapper_wwid(self, command):
        command.side_effect = [
            json.dumps({"report": [{"vg": [
                {"vg_name": "vg-a", "vg_uuid": "VGa"},
                {"vg_name": "vg-b", "vg_uuid": "VGb"},
            ]}]}).encode(),
            json.dumps({"report": [{"pv": [
                {"vg_name": "vg-a", "pv_uuid": "PVa",
                 "pv_name": "/dev/mapper/WWIDa"},
                {"vg_name": "vg-b", "pv_uuid": "PVb",
                 "pv_name": "/dev/mapper/WWIDb"},
            ]}]}).encode(),
        ]
        expected = COLLECT.expected_mixed_vgs(self.storage())
        self.assertEqual(COLLECT.actual_lvm_identities(expected), expected)

    def test_source_contains_no_mutating_control_or_lvm_command(self):
        source = MODULE_PATH.read_text(encoding="utf-8")
        for forbidden in (
            'command(["systemctl", "start"', 'command(["systemctl", "stop"',
            'command(["systemctl", "restart"', 'command(["dpkg"',
            'command(["pvesm", "set"', 'command(["lvcreate"',
            'command(["lvremove"', 'command(["lvchange"',
            'command(["vgchange"', 'command(["dmsetup", "create"',
            'command(["dmsetup", "remove"',
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('"authorizes_mutation": False', source)
        self.assertIn('"mutation_performed": False', source)

    def test_regular_reader_refuses_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.write_bytes(b"x")
            link = root / "link"
            link.symlink_to(target)
            with self.assertRaisesRegex(COLLECT.Refusal, "non-symlink"):
                COLLECT.regular_bytes(link, "fixture", 100)

    def test_proc_style_zero_stat_size_is_read_bounded(self):
        data = COLLECT.pseudo_bytes(Path("/proc/sys/kernel/random/boot_id"),
                                    "boot identity", 128)
        self.assertRegex(data.decode().strip(),
                         r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")

    def test_storage_writer_classifier_covers_surviving_children(self):
        for command in (
            b"/usr/sbin/lvcreate\0-L\01G\0vg\0",
            b"/usr/sbin/lvm\0lvconvert\0--merge\0vg/lv\0",
            b"/usr/bin/dd\0if=/dev/zero\0of=/dev/vg/lv\0",
            b"/usr/sbin/blkdiscard\0/dev/vg/lv\0",
            b"/usr/bin/perl\0/usr/lib/pve/sharedlvmthin-thick-materialize\0",
        ):
            self.assertTrue(COLLECT.storage_writer_command(command))
        for command in (b"", b"/usr/bin/python3\0collector.py\0",
                        b"/usr/sbin/pvs\0--reportformat\0json\0"):
            self.assertFalse(COLLECT.storage_writer_command(command))

    def test_empty_proc_cmdline_is_explicitly_supported(self):
        with tempfile.TemporaryDirectory() as temp:
            empty = Path(temp) / "cmdline"
            empty.write_bytes(b"")
            self.assertEqual(COLLECT.pseudo_bytes(empty, "kernel cmdline", 1024,
                                                 allow_empty=True), b"")

    def test_empty_cmdline_requires_kernel_or_zombie_identity(self):
        self.assertTrue(COLLECT.empty_cmdline_is_inert("S", 0x00200000))
        self.assertTrue(COLLECT.empty_cmdline_is_inert("Z", 0))
        self.assertFalse(COLLECT.empty_cmdline_is_inert("D", 0))

    @patch.object(COLLECT, "active_cluster_tasks", return_value=[])
    @patch.object(COLLECT, "command", return_value=b"")
    @patch.object(COLLECT.Path, "iterdir")
    @patch.object(COLLECT, "pseudo_bytes")
    def test_exited_proc_enoent_is_the_only_benign_cmdline_race(
            self, pseudo, iterdir, command, tasks):
        entry = Path("/proc/424242")
        iterdir.return_value = [entry]
        error = COLLECT.Refusal("gone")
        error.__cause__ = FileNotFoundError(2, "gone")
        pseudo.side_effect = error
        self.assertEqual(COLLECT.worker_evidence(["pve01"])["storage_processes"], [])

    @patch.object(COLLECT, "command")
    def test_active_tasks_use_supported_node_local_api(self, command):
        command.side_effect = [
            json.dumps([{"node":"pve01","upid":"UPID:pve01:1:2:3:x::u:"}]).encode(),
            b"[]",
        ]
        rows = COLLECT.active_cluster_tasks(["pve01", "pve02"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(command.call_args_list[0].args[0],
                         ["pvesh","get","/nodes/pve01/tasks","--source",
                          "active","--start","0","--limit","500",
                          "--output-format","json"])

    @patch.object(COLLECT, "command")
    @patch.object(COLLECT, "regular_bytes")
    @patch.object(COLLECT.socket, "gethostname", return_value="pve02")
    def test_pmxcfs_membership_uses_real_id_field(self, hostname, reader, command):
        reader.return_value = json.dumps({"nodelist": {
            "pve01": {"id": 1, "online": 1},
            "pve02": {"id": 2, "online": 1},
            "pve03": {"id": 3, "online": 1},
        }}).encode()
        command.return_value = b"Quorate:          Yes\n"
        self.assertEqual(
            COLLECT.membership(["pve01", "pve02", "pve03"]),
            ("pve02", 2, True, ["pve01", "pve02", "pve03"]),
        )


if __name__ == "__main__":
    unittest.main()
