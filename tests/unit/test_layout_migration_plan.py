import importlib.util
import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "experiments/thick-generations/layout-migration-plan.py"
SPEC = importlib.util.spec_from_file_location("layout_migration_plan", MODULE_PATH)
PLAN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PLAN)


class LayoutMigrationPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "storage.cfg"
        self.config.write_text(
            "dir: local\n\tpath /var/lib/vz\n"
            "sharedlvmthin: thin-a\n\tslt-vgname vg-a\n"
            "\tslt-allocation-mode thin\n"
            "sharedlvmthin: eager-a\n\tslt-vgname vg-a\n"
            "\tslt-allocation-mode thick-generations\n"
            "sharedlvmthin: lazy-b\n\tslt-vgname vg-b\n"
            "\tslt-allocation-mode thick-generations-lazy\n",
            encoding="utf-8", newline="\n",
        )
        self.deb = self.root / "candidate.deb"
        self.deb.write_bytes(b"candidate")
        self.now = 2_000_000_000
        self.nodes = ["pve01", "pve02", "pve03"]

    def evidence(self, node, **changes):
        record = {
            "schema": 1,
            "challenge": {"pve01": "1" * 32, "pve02": "2" * 32,
                          "pve03": "3" * 32}[node],
            "cluster_name": "cluster-a",
            "node": node,
            "nodeid": {"pve01": 1, "pve02": 2, "pve03": 3}[node],
            "boot_id": {"pve01": "11111111-1111-4111-8111-111111111111",
                        "pve02": "22222222-2222-4222-8222-222222222222",
                        "pve03": "33333333-3333-4333-8333-333333333333"}[node],
            "cluster_nodes": self.nodes,
            "quorate": True,
            "observed_at": self.now - 10,
            "corosync_conf_sha256": "c" * 64,
            "storage_config_sha256_before": PLAN.sha256_bytes(self.config.read_bytes()),
            "storage_config_sha256_after": PLAN.sha256_bytes(self.config.read_bytes()),
            "candidate_deb_sha256": "a" * 64,
            "installed": {
                "package": "pve-sharedlvmthin",
                "version": "0.9.0~rc5.11~tg32",
                "flavor": "dual",
                "artifact_sha256": "d" * 64,
                "dpkg_state": "installed",
            },
            "thinguard": {
                "service_active_state": "active",
                "daemon_pid": 1200,
                "daemon_starttime": 987654,
                "socket_inode": 123456,
                "samples": [
                    {"request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                     "observed_at": self.now - 12, "state": "IDLE",
                     "action": "NONE", "watchdog": "DISARMED",
                     "response_sha256": "e" * 64},
                    {"request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                     "observed_at": self.now - 10, "state": "IDLE",
                     "action": "NONE", "watchdog": "DISARMED",
                     "response_sha256": "f" * 64},
                ],
            },
            "workers": {
                "proc_inventory_complete": True,
                "systemd_inventory_complete": True,
                "storage_processes": [],
                "blocking_transient_units": [],
                "active_pve_tasks": [],
            },
            "consumer_processes": [
                {"unit": "pvedaemon.service", "pid": 2000,
                 "process_starttime": 12345},
            ],
            "vg_identities": [
                {"vg_name": "vg-a", "vg_uuid": "vg-uuid-a",
                 "pv_uuid": "pv-uuid-a", "wwid": "wwid-a"},
            ],
        }
        record.update(changes)
        path = self.root / f"{node}.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        return str(path)

    def arguments(self, evidence):
        return Namespace(
            cluster_name="cluster-a", expected_node=self.nodes,
            storage_config=str(self.config), candidate_deb=str(self.deb),
            node_evidence=evidence, max_age_sec=300, max_skew_sec=60,
            now=self.now,
        )

    @patch.object(PLAN, "candidate_identity")
    def test_exact_legacy_change_produces_non_authorizing_plan(self, identity):
        identity.return_value = {"package": "pve-sharedlvmthin",
                                 "version": "0.9.0~rc5.13~tg34",
                                 "architecture": "all", "deb_sha256": "a" * 64,
                                 "artifact_sha256": "b" * 64}
        evidence = [self.evidence(node) for node in self.nodes]
        with patch.object(PLAN, "debian_version_is_newer", return_value=True):
            result = PLAN.plan(self.arguments(evidence))
        self.assertEqual(result["verdict"], "READY_FOR_MAINTENANCE_PREPARE")
        self.assertEqual(result["authorization"], "NONE")
        self.assertFalse(result["mutation_performed"])
        self.assertEqual([item["storage_id"] for item in result["changes"]],
                         ["eager-a", "thin-a"])
        target, _ = PLAN.target_layout(self.config.read_bytes())
        self.assertEqual(target.count(b"slt-vg-layout mixed"), 2)
        self.assertNotEqual(result["baseline_storage_config_sha256"],
                            result["target_storage_config_sha256"])

    def test_explicit_isolated_alias_in_mixed_vg_refuses(self):
        self.config.write_text(self.config.read_text().replace(
            "\tslt-allocation-mode thin\n",
            "\tslt-allocation-mode thin\n\tslt-vg-layout isolated\n"),
            encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(PLAN.Refusal, "explicit isolated"):
            PLAN.target_layout(self.config.read_bytes())

    def test_no_legacy_mixed_vg_refuses(self):
        self.config.write_text(
            self.config.read_text().replace("\tslt-vgname vg-a\n", "\tslt-vgname vg-t\n", 1),
            encoding="utf-8", newline="\n")
        with self.assertRaisesRegex(PLAN.Refusal, "no legacy"):
            PLAN.target_layout(self.config.read_bytes())

    def test_renderer_reordering_is_accepted_but_semantic_drift_refuses(self):
        baseline = self.config.read_bytes()
        rendered = (
            "dir: local\n\tpath /var/lib/vz\n\n"
            "sharedlvmthin: thin-a\n\tslt-allocation-mode thin\n"
            "\tslt-vg-layout mixed\n\tslt-vgname vg-a\n\n"
            "sharedlvmthin: eager-a\n\tslt-allocation-mode thick-generations\n"
            "\tslt-vg-layout mixed\n\tslt-vgname vg-a\n\n"
            "sharedlvmthin: lazy-b\n\tslt-allocation-mode thick-generations-lazy\n"
            "\tslt-vgname vg-b\n\n").encode()
        self.assertEqual(len(PLAN.validate_target_layout(baseline, rendered)), 2)
        for bad in (rendered.replace(b"vg-b", b"vg-c"),
                    rendered + b"# foreign\n"):
            with self.assertRaises(PLAN.Refusal):
                PLAN.validate_target_layout(baseline, bad)

    def test_renderer_known_set_order_only_is_accepted(self):
        baseline = self.config.read_bytes().replace(
            b"\tpath /var/lib/vz\n",
            b"\tpath /var/lib/vz\n\tcontent vztmpl,import,backup,iso\n\tnodes PVE01,pve02\n")
        target, _ = PLAN.target_layout(baseline)
        rendered = target.replace(b"vztmpl,import,backup,iso", b"backup,vztmpl,iso,import").replace(
            b"PVE01,pve02", b"pve02,PVE01")
        self.assertEqual(len(PLAN.validate_target_layout(baseline, rendered)), 2)
        self.assertNotEqual(PLAN.sha256_bytes(target), PLAN.sha256_bytes(rendered))
        for old, new in ((b"backup,vztmpl,iso,import", b"backup,vztmpl,iso"),
                         (b"backup,vztmpl,iso,import", b"backup,vztmpl,iso,images"),
                         (b"pve02,PVE01", b"pve02,pve01"),
                         (b"pve02,PVE01", b"pve02,PVE03"),
                         (b"\tpath /var/lib/vz", b"\tpath /var/lib/other")):
            with self.subTest(new=new), self.assertRaises(PLAN.Refusal):
                PLAN.validate_target_layout(baseline, rendered.replace(old, new))

    def test_renderer_malformed_sets_refuse_even_when_unchanged(self):
        for key, values in (("nodes", ("", "pve01,", ",pve01", "pve01,,pve02", "pve01,pve01", "pve01, pve02")),
                            ("content", ("", "iso,", ",iso", "iso,,backup", "iso,iso", "iso,unknown", "ISO", "iso, backup"))):
            for value in values:
                baseline = self.config.read_bytes().replace(b"\tpath /var/lib/vz\n",
                    f"\tpath /var/lib/vz\n\t{key} {value}\n".encode())
                target, _ = PLAN.target_layout(baseline)
                with self.subTest(key=key, value=value), self.assertRaises(PLAN.Refusal):
                    PLAN.validate_target_layout(baseline, target)

    def test_renderer_does_not_normalize_unknown_comma_properties(self):
        baseline = self.config.read_bytes().replace(b"\tpath /var/lib/vz\n",
                                                  b"\tpath /var/lib/vz\n\toptions a,b\n")
        target, _ = PLAN.target_layout(baseline)
        with self.assertRaises(PLAN.Refusal):
            PLAN.validate_target_layout(baseline, target.replace(b"options a,b", b"options b,a"))

    def test_renderer_must_preserve_every_intended_layout_addition(self):
        baseline = self.config.read_bytes()
        target, _ = PLAN.target_layout(baseline)
        for bad in (baseline, target.replace(b"\tslt-vg-layout mixed\n", b"", 1),
                    target.replace(b"\tslt-vg-layout mixed\n", b""),
                    target.replace(b"\tslt-vg-layout mixed\n", b"\tslt-vg-layout isolated\n", 1)):
            with self.subTest(bad=bad), self.assertRaises(PLAN.Refusal):
                PLAN.validate_target_layout(baseline, bad)

    def test_parser_refuses_content_it_cannot_account_for(self):
        baseline = self.config.read_bytes()
        cases = (
            b"foreign top-level directive\n" + baseline,
            b"\tproperty-before-section value\n" + baseline,
            baseline + b"dir: local\n\tpath /elsewhere\n",
            baseline + b"sharedlvmthin: local\n\tslt-vgname vg-c\n",
            baseline + b"broken header:\n\tpath /elsewhere\n",
            baseline.replace(b"\tslt-vgname vg-a\n\tslt-allocation-mode",
                             b"\tslt-vgname vg-a\x0b\tslt-allocation-mode", 1),
            baseline.replace(b"\tslt-vgname vg-a\n\tslt-allocation-mode",
                             b"\tslt-vgname vg-a\x0c\tslt-allocation-mode", 1),
            baseline.replace(b"\tslt-vgname vg-a\n\tslt-allocation-mode",
                             b"\tslt-vgname vg-a\r\tslt-allocation-mode", 1),
            baseline.replace(b"\tslt-vgname vg-a\n\tslt-allocation-mode",
                             "\tslt-vgname vg-a\u0085\tslt-allocation-mode".encode(), 1),
            baseline.replace(b"\tslt-vgname vg-a\n\tslt-allocation-mode",
                             "\tslt-vgname vg-a\u2028\tslt-allocation-mode".encode(), 1),
            baseline.replace(b"\tslt-vgname vg-a\n\tslt-allocation-mode",
                             "\tslt-vgname vg-a\u2029\tslt-allocation-mode".encode(), 1),
            baseline.replace(b"\tslt-vgname vg-a\n",
                             b"\tslt-vgname vg-a\n\tslt-vgname vg-b\n", 1),
        )
        for bad in cases:
            with self.subTest(bad=bad[:40]), self.assertRaises(PLAN.Refusal):
                PLAN.parse_storage_config(bad)

    @patch.object(PLAN, "candidate_identity")
    def test_stale_or_busy_node_refuses(self, identity):
        identity.return_value = {"package": "pve-sharedlvmthin",
                                 "version": "0.9.0~rc5.13~tg34",
                                 "architecture": "all", "deb_sha256": "a" * 64,
                                 "artifact_sha256": "b" * 64}
        evidence = [self.evidence("pve01"), self.evidence(
            "pve02", workers={"proc_inventory_complete": True,
                               "systemd_inventory_complete": True,
                               "storage_processes": ["worker"],
                               "blocking_transient_units": [],
                               "active_pve_tasks": []}), self.evidence("pve03")]
        with self.assertRaisesRegex(PLAN.Refusal, "worker processes exist"):
            PLAN.plan(self.arguments(evidence))

    @patch.object(PLAN, "candidate_identity")
    def test_config_digest_and_boot_identity_are_mandatory(self, identity):
        identity.return_value = {"package": "pve-sharedlvmthin",
                                 "version": "0.9.0~rc5.13~tg34",
                                 "architecture": "all", "deb_sha256": "a" * 64,
                                 "artifact_sha256": "b" * 64}
        bad = self.evidence("pve02", storage_config_sha256_after="0" * 64)
        evidence = [self.evidence("pve01"), bad, self.evidence("pve03")]
        with self.assertRaisesRegex(PLAN.Refusal, "different or changing storage"):
            PLAN.plan(self.arguments(evidence))

    @patch.object(PLAN, "candidate_identity")
    def test_cached_guard_request_or_armed_watchdog_refuses(self, identity):
        identity.return_value = {"package": "pve-sharedlvmthin",
                                 "version": "0.9.0~rc5.13~tg34",
                                 "architecture": "all", "deb_sha256": "a" * 64,
                                 "artifact_sha256": "b" * 64}
        guard = {
            "service_active_state": "active",
            "daemon_pid": 1200, "daemon_starttime": 987654,
            "socket_inode": 123456,
            "samples": [
                {"request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                 "observed_at": self.now - 12, "state": "IDLE",
                 "action": "NONE", "watchdog": "DISARMED",
                 "response_sha256": "e" * 64},
                {"request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                 "observed_at": self.now - 10, "state": "IDLE",
                 "action": "NONE", "watchdog": "ARMED",
                 "response_sha256": "f" * 64},
            ],
        }
        evidence = [self.evidence("pve01"),
                    self.evidence("pve02", thinguard=guard),
                    self.evidence("pve03")]
        with self.assertRaisesRegex(PLAN.Refusal, "cached request"):
            PLAN.plan(self.arguments(evidence))

    def test_candidate_must_be_dual(self):
        result = type("Result", (), {"returncode": 0,
                                      "stdout": "pve-sharedlvmthin-thick\n1\nall\n",
                                      "stderr": ""})()
        with patch.object(PLAN.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(PLAN.Refusal, "DUAL"):
                PLAN.candidate_identity(self.deb)


if __name__ == "__main__":
    unittest.main()
