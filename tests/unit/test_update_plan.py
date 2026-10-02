import importlib.machinery
import importlib.util
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-plan"
MANIFEST = ROOT / "usr/share/pve-sharedlvmthin/pve-qualified-tuples.json"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_update_plan", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class UpdatePlanTests(unittest.TestCase):
    def test_current_build_has_at_least_one_explicit_tuple_entry(self):
        dual = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        version = next(line.split(":", 1)[1].strip() for line in dual.splitlines()
                       if line.startswith("Version:"))
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        matches = [row for row in manifest["tuples"]
                   if version in row["plugin_versions"]]
        self.assertTrue(matches)
        self.assertTrue(any(row["status"] == "RETEST_REQUIRED" for row in matches))

    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.document = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_manifest_keeps_old_and_new_tuples(self):
        ids = {item["id"] for item in self.document["tuples"]}
        self.assertIn("tg52-api14-control-plane", ids)
        self.assertIn("tg52-api15-san-original", ids)
        self.assertIn("tg52-api15-upstream-20260929", ids)

    def test_original_san_tuple_is_exact_lab_tested(self):
        item = next(row for row in self.document["tuples"]
                    if row["id"] == "tg52-api15-san-original")
        matched = self.module.match_tuple(
            self.document, item["packages"], item["running_kernel"], "dual", 15,
            plugin_version=item["plugin_versions"][-1],
        )
        self.assertEqual(matched["status"], "EXACT_LAB_TESTED")
        self.assertIn("san-dataplane", matched["scopes"])

    def test_new_control_plane_tuple_remains_retest_required(self):
        item = next(row for row in self.document["tuples"]
                    if row["id"] == "tg52-api15-upstream-20260929")
        matched = self.module.match_tuple(
            self.document, item["packages"], item["running_kernel"], "dual", 15,
            plugin_version=item["plugin_versions"][-1],
        )
        self.assertEqual(matched["status"], "RETEST_REQUIRED")
        self.assertNotIn("san-dataplane", matched["scopes"])
        self.assertIn("vma-file-and-stdin-restore", matched["required_tests"])

    def test_new_tuples_have_complete_operation_scoped_qualification(self):
        groups = set(self.document["operation_groups"])
        self.assertGreaterEqual(len(groups), 8)
        for item in self.document["tuples"]:
            if "operation_qualifications" not in item:
                continue
            matrix = self.module.operation_qualifications(self.document, item)
            self.assertEqual(set(matrix), groups, item["id"])
            self.assertIn(
                matrix["snapshot-vmstate"]["status"],
                {"BLOCKED", "RETEST_REQUIRED"},
                item["id"],
            )

    def test_operation_matrix_refuses_omission_and_evidence_free_pass(self):
        item = next(row for row in self.document["tuples"]
                    if row["id"] == "tg53-api15-upstream-9.2.20-k17")
        malformed = json.loads(json.dumps(item))
        malformed["operation_qualifications"].pop("resize")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.module.operation_qualifications(self.document, malformed)

        malformed = json.loads(json.dumps(item))
        entry = malformed["operation_qualifications"]["read-only-inventory"]
        entry["status"] = "QUALIFIED"
        entry.pop("evidence")
        entry.pop("required_tests")
        with self.assertRaisesRegex(ValueError, "no evidence"):
            self.module.operation_qualifications(self.document, malformed)

        malformed = json.loads(json.dumps(item))
        entry = malformed["operation_qualifications"]["read-only-inventory"]
        entry["evidence"] = ["does-not-exist"]
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            self.module.operation_qualifications(self.document, malformed)

    def test_operation_matrix_is_advisory_and_legacy_tuple_remains_readable(self):
        legacy = {"id": "legacy"}
        document = {"schema": 1, "catalogue": "pve-qualified-tuples-v1"}
        self.assertEqual(self.module.operation_qualifications(document, legacy), {})
        self.assertEqual(self.document["operation_qualification_mode"], "ADVISORY_ONLY")
        self.assertEqual(self.document["operation_authorization"], "NONE")

    def test_tuple_match_binds_plugin_version_and_refuses_ambiguity(self):
        item = next(row for row in self.document["tuples"]
                    if row["id"] == "tg53-api15-upstream-9.2.20-k17")
        self.assertIsNone(self.module.match_tuple(
            self.document, item["packages"], item["running_kernel"], "dual", 15,
            plugin_version="unknown-build",
        ))
        duplicate = json.loads(json.dumps(self.document))
        copy = json.loads(json.dumps(item))
        copy["id"] = "duplicate"
        duplicate["tuples"].append(copy)
        with self.assertRaisesRegex(ValueError, "multiple"):
            self.module.match_tuple(
                duplicate, item["packages"], item["running_kernel"], "dual", 15,
                plugin_version=item["plugin_versions"][-1],
            )

    def test_operation_matrix_preserves_partial_migration_evidence(self):
        item = next(row for row in self.document["tuples"]
                    if row["id"] == "tg53-api15-upstream-9.2.20-k17")
        matrix = self.module.operation_qualifications(self.document, item)
        migration = matrix["migration"]
        self.assertEqual(migration["status"], "RETEST_REQUIRED")
        self.assertIn("thick-lazy-live-roundtrip", migration["evidence"])
        self.assertIn("mixed-node-live-migration", migration["required_tests"])

    def test_known_broken_storage_qemu_pair_is_detected(self):
        versions = {
            "libpve-storage-perl": "9.1.11",
            "qemu-server": "9.2.7",
        }
        def compare(left, operator, right):
            values = {"9.1.11": (9, 1, 11), "9.2.7": (9, 2, 7), "9.2.10": (9, 2, 10)}
            return values[left] >= values[right] if operator == "ge" else values[left] < values[right]
        with mock.patch.object(self.module, "compare", side_effect=compare):
            rule = self.module.incompatible(self.document, versions)
        self.assertEqual(rule["id"], "storage-9.1.11-requires-qemu-9.2.10")

    def test_candidate_is_observation_not_runtime_tuple(self):
        original = next(row for row in self.document["tuples"]
                        if row["id"] == "tg52-api15-san-original")
        current = dict(original["packages"])
        candidate = dict(current)
        candidate["libpve-storage-perl"] = "99.0"
        matched = self.module.match_tuple(
            self.document, current, original["running_kernel"], "dual", 15,
            plugin_version=original["plugin_versions"][-1],
        )
        self.assertEqual(matched["id"], original["id"])
        self.assertNotEqual(candidate, current)

    def test_probe_environment_drops_python_and_perl_injection(self):
        completed = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with mock.patch.dict("os.environ", {"PYTHONPATH": "/tmp/x", "PERL5OPT": "-Mx"}), \
                mock.patch.object(self.module.subprocess, "run", return_value=completed) as run:
            ok, output, error = self.module.run(["true"])
        self.assertTrue(ok)
        environment = run.call_args.kwargs["env"]
        self.assertNotIn("PYTHONPATH", environment)
        self.assertNotIn("PERL5OPT", environment)
        self.assertEqual(output, "ok")
        self.assertEqual(error, "")

    def test_configuration_scan_reports_known_qemu_upgrade_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "101.conf").write_text(
                "machine: pc-i440fx-4.2\n"
                "scsi14: san:vm-101-disk-0,size=1G\n"
                "[snap]\n"
                "running-nets-host-mtu:\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root),
            )
        codes = {row["code"] for row in findings}
        self.assertEqual(
            codes,
            {"LEGACY_MACHINE_LT_5", "HIGH_SCSI_SLOT", "EMPTY_RUNNING_NETS_HOST_MTU"},
        )
        mtu = next(row for row in findings if row["code"] == "EMPTY_RUNNING_NETS_HOST_MTU")
        self.assertEqual(mtu["node"], self.module.os.uname().nodename)
        self.assertEqual(mtu["count"], 1)
        self.assertEqual(mtu["occurrences"], [{
            "line": 4,
            "section": "snap",
            "section_kind": "snapshot",
            "canonical_empty_line": True,
        }])
        self.assertEqual(mtu["repair_class"], "REPAIR_ELIGIBILITY_UNKNOWN")
        expected = hashlib.sha256(
            b"machine: pc-i440fx-4.2\n"
            b"scsi14: san:vm-101-disk-0,size=1G\n"
            b"[snap]\n"
            b"running-nets-host-mtu:\n"
        ).hexdigest()
        self.assertEqual(mtu["raw_config_sha256"], expected)

    def test_empty_mtu_detector_blocks_ambiguous_current_pending_and_whitespace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "103.conf").write_text(
                " running-nets-host-mtu : \n"
                "[PENDING]\n"
                "running-nets-host-mtu:\n"
                "[snap-a]\n"
                "running-nets-host-mtu:\n"
                "running-nets-host-mtu:\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root), node_name="node-test",
            )
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["code"], "EMPTY_RUNNING_NETS_HOST_MTU")
        self.assertEqual(finding["node"], "node-test")
        self.assertEqual(finding["count"], 4)
        self.assertEqual(
            [row["section_kind"] for row in finding["occurrences"]],
            ["current", "pending", "snapshot", "snapshot"],
        )
        self.assertFalse(finding["occurrences"][0]["canonical_empty_line"])
        self.assertEqual(finding["repair_class"], "REPAIR_BLOCKED_AMBIGUOUS_CONFIG")

    def test_empty_mtu_detector_ignores_valid_zero_and_text_lookalikes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "104.conf").write_text(
                "machine: pc-q35-9.2\n"
                "description: running-nets-host-mtu:  \n"
                "# running-nets-host-mtu:\n"
                "[ram-zero]\nrunning-nets-host-mtu: net0=0\n"
                "[ram-multi]\nrunning-nets-host-mtu: net0=1500,net1=9000\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root), node_name="node-test",
            )
        self.assertEqual(findings, [])

    def test_configuration_scan_accepts_modern_ordinary_vm(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "102.conf").write_text(
                "machine: pc-q35-9.2\nscsi0: san:vm-102-disk-0,size=1G\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            self.assertEqual(
                self.module.scan_configuration_risks(str(root), str(run_root), str(proc_root)),
                [],
            )

    def test_configuration_scan_fails_visible_when_vm_directory_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            findings = self.module.scan_configuration_risks(directory)
        self.assertEqual(findings[0]["code"], "VM_CONFIG_SCAN_UNKNOWN")

    def test_running_old_lsi_qemu_without_pci4_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "etc-pve"
            vm_dir = root / "qemu-server"
            run_root = base / "run-qemu"
            proc_root = base / "proc"
            vm_dir.mkdir(parents=True)
            run_root.mkdir()
            (proc_root / "321").mkdir(parents=True)
            (vm_dir / "101.conf").write_text(
                "machine: pc-i440fx-9.2\nscsihw: lsi\n"
                "net0: virtio=00:11:22:33:44:55,bridge=vmbr0\n",
                encoding="utf-8",
            )
            (run_root / "101.pid").write_text("321\n", encoding="ascii")
            (proc_root / "321" / "cmdline").write_bytes(
                b"/usr/bin/kvm\x00-id\x00101\x00-device\x00pci-bridge,id=pci.1\x00"
            )
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root),
            )
        self.assertEqual([row["code"] for row in findings], ["RUNNING_LSI_VM_WITHOUT_PCI4"])

    def test_running_lsi_qemu_with_pci4_is_not_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "etc-pve"
            vm_dir = root / "qemu-server"
            run_root = base / "run-qemu"
            proc_root = base / "proc"
            vm_dir.mkdir(parents=True)
            run_root.mkdir()
            (proc_root / "322").mkdir(parents=True)
            (vm_dir / "102.conf").write_text(
                "scsihw: lsi\nnet0: virtio=00:11:22:33:44:56,bridge=vmbr0\n",
                encoding="utf-8",
            )
            (run_root / "102.pid").write_text("322\n", encoding="ascii")
            (proc_root / "322" / "cmdline").write_bytes(
                b"/usr/bin/kvm\x00-id\x00102\x00-device\x00pci-bridge,id=pci.4\x00"
            )
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root),
            )
        self.assertEqual(findings, [])

    def test_running_no_nic_vm_reports_ram_snapshot_empty_mtu_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "etc-pve"
            vm_dir = root / "qemu-server"
            run_root = base / "run-qemu"
            proc_root = base / "proc"
            vm_dir.mkdir(parents=True)
            run_root.mkdir()
            (proc_root / "401").mkdir(parents=True)
            (vm_dir / "201.conf").write_text(
                "machine: pc-q35-10.2\nscsihw: virtio-scsi-single\n",
                encoding="utf-8",
            )
            (run_root / "201.pid").write_text("401\n", encoding="ascii")
            (proc_root / "401" / "cmdline").write_bytes(
                b"/usr/bin/kvm\x00-id\x00201\x00"
            )
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root),
            )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "RUNNING_RAM_SNAPSHOT_EMPTY_MTU_RISK")
        self.assertEqual(findings[0]["network_topology"], "NO_NETWORK")
        self.assertEqual(findings[0]["configured_networks"], [])

    def test_running_e1000_only_vm_reports_ram_snapshot_empty_mtu_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "etc-pve"
            vm_dir = root / "qemu-server"
            run_root = base / "run-qemu"
            proc_root = base / "proc"
            vm_dir.mkdir(parents=True)
            run_root.mkdir()
            (proc_root / "402").mkdir(parents=True)
            (vm_dir / "202.conf").write_text(
                "machine: pc-q35-10.2\nscsihw: virtio-scsi-single\n"
                "net0: e1000=00:11:22:33:44:57,bridge=vmbr0\n",
                encoding="utf-8",
            )
            (run_root / "202.pid").write_text("402\n", encoding="ascii")
            (proc_root / "402" / "cmdline").write_bytes(
                b"/usr/bin/kvm\x00-id\x00202\x00"
            )
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root),
            )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["code"], "RUNNING_RAM_SNAPSHOT_EMPTY_MTU_RISK")
        self.assertEqual(findings[0]["network_topology"], "NON_VIRTIO_ONLY")
        self.assertEqual(findings[0]["configured_networks"], ["net0"])

    def test_running_virtio_vm_does_not_report_static_empty_mtu_risk(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "etc-pve"
            vm_dir = root / "qemu-server"
            run_root = base / "run-qemu"
            proc_root = base / "proc"
            vm_dir.mkdir(parents=True)
            run_root.mkdir()
            (proc_root / "403").mkdir(parents=True)
            (vm_dir / "203.conf").write_text(
                "machine: pc-q35-10.2\n"
                "net0: virtio=00:11:22:33:44:58,bridge=vmbr0\n",
                encoding="utf-8",
            )
            (run_root / "203.pid").write_text("403\n", encoding="ascii")
            (proc_root / "403" / "cmdline").write_bytes(
                b"/usr/bin/kvm\x00-id\x00203\x00"
            )
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root),
            )
        self.assertNotIn(
            "RUNNING_RAM_SNAPSHOT_EMPTY_MTU_RISK",
            {row["code"] for row in findings},
        )

    def test_qemu_snapshot_contract_finds_api14_ordering_and_cleanup_edges(self):
        source = r'''
sub __snapshot_save_vmstate {
    my $statefile = PVE::Storage::vdisk_alloc();
    my $runningmachine = get_current_qemu_machine();
    my $runningcpu = get_cpu_from_running_vm();
    my $nets_host_mtu = get_nets_host_mtu();
    $conf->{'running-nets-host-mtu'} = $nets_host_mtu;
}
sub __snapshot_create_vol_snapshots_hook {
    if ($hook eq "after") {
        eval {
            mon_cmd($vmid, "savevm-end");
            PVE::Storage::deactivate_volumes($storecfg, [$snap->{vmstate}]);
        };
    } elsif ($hook eq "after-freeze") {
        for (;;) { mon_cmd($vmid, "query-savevm"); sleep(1); }
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "QemuConfig.pm"
            path.write_text(source, encoding="utf-8")
            findings = self.module.scan_qemu_snapshot_contract(str(path))
        self.assertEqual(
            {row["code"] for row in findings},
            {
                "VMSTATE_ALLOCATION_BEFORE_RUNTIME_QUERIES",
                "SAVEVM_END_DEACTIVATION_COUPLED",
                "SAVEVM_FINALIZE_POLL_UNBOUNDED",
                "EMPTY_RUNNING_NETS_HOST_MTU_WRITABLE",
            },
        )
        self.assertEqual(len({row["source_sha256"] for row in findings}), 1)

    def test_qemu_snapshot_contract_recognizes_new_query_before_alloc_order(self):
        source = r'''
sub __snapshot_save_vmstate {
    my $runningmachine = get_current_qemu_machine();
    my $runningcpu = get_cpu_from_running_vm();
    my $nets_host_mtu = get_nets_host_mtu();
    my $statefile = PVE::Storage::vdisk_alloc();
    $conf->{'running-nets-host-mtu'} = $nets_host_mtu;
}
sub __snapshot_create_vol_snapshots_hook {
    if ($hook eq "after") {
        eval {
            mon_cmd($vmid, "savevm-end");
            PVE::Storage::deactivate_volumes($storecfg, [$snap->{vmstate}]);
        };
    } elsif ($hook eq "after-unfreeze") {
        for (;;) { mon_cmd($vmid, "query-savevm"); sleep(1); }
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "QemuConfig.pm"
            path.write_text(source, encoding="utf-8")
            findings = self.module.scan_qemu_snapshot_contract(str(path))
        codes = {row["code"] for row in findings}
        self.assertNotIn("VMSTATE_ALLOCATION_BEFORE_RUNTIME_QUERIES", codes)
        self.assertIn("SAVEVM_END_DEACTIVATION_COUPLED", codes)
        self.assertIn("SAVEVM_FINALIZE_POLL_UNBOUNDED", codes)
        self.assertIn("EMPTY_RUNNING_NETS_HOST_MTU_WRITABLE", codes)

    def test_qemu_snapshot_contract_unknown_is_explicit(self):
        findings = self.module.scan_qemu_snapshot_contract("/definitely/absent/QemuConfig.pm")
        self.assertEqual(findings[0]["code"], "QEMU_SNAPSHOT_CONTRACT_UNKNOWN")

    def test_historical_ram_snapshot_detects_partial_two_virtio_mtu_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "204.conf").write_text(
                # The current NIC set deliberately differs. Historical validation
                # must be bound only to the exact snapshot section.
                "net0: e1000=00:11:22:33:44:60,bridge=vmbr0\n"
                "[ram-two]\n"
                "net0: virtio=00:11:22:33:44:61,bridge=vmbr0\n"
                "net1: virtio=00:11:22:33:44:62,bridge=vmbr1\n"
                "vmstate: eager:vm-204-state-ram-two\n"
                "running-nets-host-mtu: net0=1500\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root), node_name="node-test",
            )
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["code"], "RAM_SNAPSHOT_MTU_SET_MISMATCH")
        self.assertEqual(finding["snapshot"], "ram-two")
        self.assertEqual(finding["expected_virtio_nics"], ["net0", "net1"])
        self.assertEqual(finding["recorded_nics"], ["net0"])
        self.assertEqual(finding["missing_nics"], ["net1"])
        self.assertEqual(finding["unexpected_nics"], [])

    def test_historical_ram_snapshot_accepts_complete_two_virtio_mtu_set(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "205.conf").write_text(
                "[ram-two]\n"
                "net0: virtio=00:11:22:33:44:63,bridge=vmbr0\n"
                "net1: virtio=00:11:22:33:44:64,bridge=vmbr1\n"
                "vmstate: eager:vm-205-state-ram-two\n"
                "running-nets-host-mtu: net0=1500,net1=9000\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root), node_name="node-test",
            )
        self.assertEqual(findings, [])

    def test_historical_ram_snapshot_requires_mtu_map_for_virtio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "206.conf").write_text(
                "[ram]\n"
                "net0: virtio=00:11:22:33:44:65,bridge=vmbr0\n"
                "vmstate: eager:vm-206-state-ram\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root), node_name="node-test",
            )
        self.assertEqual([row["code"] for row in findings], [
            "RAM_SNAPSHOT_MTU_SET_MISSING",
        ])
        self.assertEqual(findings[0]["expected_virtio_nics"], ["net0"])

    def test_duplicate_snapshot_section_and_vmstate_are_ambiguous(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vm_dir = root / "qemu-server"
            vm_dir.mkdir()
            (vm_dir / "207.conf").write_text(
                "[ram]\n"
                "vmstate: eager:vm-207-state-one\n"
                "[ram]\n"
                "vmstate: eager:vm-207-state-two\n",
                encoding="utf-8",
            )
            run_root = root / "run"
            proc_root = root / "proc"
            run_root.mkdir()
            proc_root.mkdir()
            findings = self.module.scan_configuration_risks(
                str(root), str(run_root), str(proc_root), node_name="node-test",
            )
        self.assertEqual({row["code"] for row in findings}, {
            "DUPLICATE_VM_CONFIG_SECTION", "RAM_SNAPSHOT_VMSTATE_AMBIGUOUS",
        })
        duplicate = next(
            row for row in findings if row["code"] == "DUPLICATE_VM_CONFIG_SECTION"
        )
        self.assertEqual(duplicate["section"], "ram")
        self.assertEqual(duplicate["line"], 3)


if __name__ == "__main__":
    unittest.main()
