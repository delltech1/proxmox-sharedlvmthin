import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-contract-check"
CATALOGUE = ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_contract_check", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ContractCatalogueTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()
        cls.document = cls.module.strict_load(CATALOGUE)

    def test_catalogue_is_complete_and_all_static_tests_exist(self):
        self.assertEqual(self.module.validate_catalogue(self.document, ROOT), [])

    def test_contract_ids_are_unique_and_cover_both_profiles(self):
        contracts = self.document["contracts"]
        ids = [item["id"] for item in contracts]
        self.assertEqual(len(ids), len(set(ids)))
        scopes = {scope for item in contracts for scope in item["scope"]}
        self.assertEqual(scopes, {"dual", "thick-only"})
        self.assertIn("cluster.rolling-upgrade", ids)
        self.assertIn("scale.large-cluster", ids)

    def test_missing_test_file_fails_closed(self):
        document = json.loads(json.dumps(self.document))
        document["contracts"][0]["static_tests"] = ["tests/unit/does-not-exist.t"]
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertTrue(any("static test is missing" in error for error in errors))

    def test_duplicate_json_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"schema":1,"schema":1}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                self.module.strict_load(path)

    def test_every_literal_external_command_and_upstream_symbol_is_covered(self):
        self.assertEqual(self.module.validate_catalogue(self.document, ROOT), [])
        coverage = self.document["source_dependency_coverage"]
        self.assertIn("/sbin/dmsetup", coverage["commands"])
        self.assertIn("PVE::Storage::Plugin::cluster_lock_storage",
                      coverage["perl_symbols"])
        self.assertEqual(
            set(coverage["native_operations"]),
            set(self.module.NATIVE_OPERATION_PATTERNS),
        )
        self.assertEqual(
            set(coverage["native_operation_tests"]),
            set(coverage["native_operations"]),
        )
        self.assertEqual(
            set(coverage["hooks"]),
            self.module.inventory_literal_tuple(ROOT, "REQUIRED_HOOKS"),
        )
        self.assertEqual(
            set(coverage["perl_symbols"]),
            self.module.inventory_literal_tuple(ROOT, "REQUIRED_PVE_SYMBOLS"),
        )
        runtime = self.module.inventory_runtime_commands(ROOT)
        covered = {Path(path).name for path in coverage["commands"]}
        self.assertEqual(runtime - covered, set())

    def test_runtime_command_without_contract_binding_fails_closed(self):
        document = json.loads(json.dumps(self.document))
        for path in list(document["source_dependency_coverage"]["commands"]):
            if Path(path).name == "modprobe":
                del document["source_dependency_coverage"]["commands"][path]
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn("runtime command has no contract binding: modprobe", errors)

    def test_thick_tree_peer_coverage_binds_configured_nodelist_and_behavior_tests(self):
        symbol = "PVE::Cluster::get_nodelist"
        coverage = self.document["source_dependency_coverage"]
        self.assertEqual(coverage["perl_symbols"][symbol], "cluster.mutation-admission")
        self.assertIn(symbol, self.module.inventory_literal_tuple(ROOT, "REQUIRED_PVE_SYMBOLS"))
        contract = next(item for item in self.document["contracts"]
                        if item["id"] == "cluster.mutation-admission")
        self.assertIn("tests/unit/thick_tree_delete.t", contract["static_tests"])
        document = json.loads(json.dumps(self.document))
        del document["source_dependency_coverage"]["perl_symbols"][symbol]
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn(f"uncovered upstream Perl dependency: {symbol}", errors)

    def test_qmdestroy_admission_upstream_call_contracts_are_exactly_covered(self):
        coverage = self.document["source_dependency_coverage"]["perl_symbols"]
        symbols = self.module.inventory_literal_tuple(ROOT, "REQUIRED_PVE_SYMBOLS")
        for symbol in ("PVE::AbstractConfig::lock_config", "PVE::AbstractConfig::lock_config_full",
                       "PVE::Storage::vdisk_free", "PVE::QemuServer::destroy_vm"):
            self.assertIn(symbol, coverage)
            self.assertIn(symbol, symbols)
        files = self.module.inventory_literal_tuple(ROOT, "CRITICAL_FILES")
        self.assertIn("/usr/share/perl5/PVE/AbstractConfig.pm", files)
        self.assertIn("/usr/share/perl5/PVE/API2/Qemu.pm", files)
        self.assertIn("libpve-guest-common-perl", self.module.inventory_literal_tuple(ROOT, "PACKAGES"))

    def test_lazy_move_admission_has_exact_upstream_and_behavior_bindings(self):
        coverage = self.document["source_dependency_coverage"]["perl_symbols"]
        symbols = self.module.inventory_literal_tuple(ROOT, "REQUIRED_PVE_SYMBOLS")
        for symbol in ("PVE::QemuServer::clone_disk", "PVE::QemuServer::OVMF::create_efidisk", "PVE::Storage::vdisk_alloc",
                       "PVE::Storage::activate_volumes",
                       "PVE::Storage::deactivate_volumes"):
            self.assertEqual(coverage[symbol], "pve.qemu-raw-lifecycle")
            self.assertIn(symbol, symbols)
        contract = next(item for item in self.document["contracts"]
                        if item["id"] == "pve.qemu-raw-lifecycle")
        self.assertIn("tests/unit/lazy_move_admission.t", contract["static_tests"])

    def test_runtime_hook_without_contract_binding_fails_closed(self):
        document = json.loads(json.dumps(self.document))
        del document["source_dependency_coverage"]["hooks"]["volume_resize"]
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn(
            "runtime hook has no contract/test binding: volume_resize", errors
        )

    def test_stale_runtime_command_binding_fails_closed(self):
        document = json.loads(json.dumps(self.document))
        document["source_dependency_coverage"]["commands"]["/bin/false"] = (
            "package.dual-lifecycle"
        )
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn("stale runtime command contract binding: false", errors)

    def test_runtime_symbol_inventory_is_bidirectional(self):
        document = json.loads(json.dumps(self.document))
        del document["source_dependency_coverage"]["perl_symbols"][
            "PVE::Storage::APIVER"
        ]
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn(
            "runtime PVE symbol has no contract/test binding: PVE::Storage::APIVER",
            errors,
        )

    def test_exact_surface_test_id_must_exist(self):
        document = json.loads(json.dumps(self.document))
        document["source_dependency_coverage"]["exact_surface_tests"]["commands"][
            "id"
        ] = "UpstreamInventoryEvaluationTests.test_does_not_exist"
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn("exact surface test id is absent: commands", errors)

    def test_unknown_coverage_contract_fails_closed(self):
        document = json.loads(json.dumps(self.document))
        document["source_dependency_coverage"]["commands"]["/bin/false"] = (
            "missing.contract"
        )
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertTrue(any("invalid commands coverage row" in error for error in errors))

    def test_new_uncovered_dependency_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usr/libexec").mkdir(parents=True)
            (root / "usr/libexec/probe").write_text(
                "#!/bin/sh\n/usr/bin/uncovered-tool\n"
                "PVE::Future::API::new_hook()\n", encoding="utf-8")
            errors = self.module.validate_catalogue(self.document, root)
            self.assertIn(
                "uncovered external command dependency: /usr/bin/uncovered-tool",
                errors,
            )
            self.assertIn(
                "uncovered upstream Perl dependency: PVE::Future::API::new_hook",
                errors,
            )

    def test_new_native_pve_operation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usr/sbin").mkdir(parents=True)
            (root / "usr/sbin/probe").write_text(
                "#!/bin/sh\nqm clone 100 101\n", encoding="utf-8")
            errors = self.module.validate_catalogue(self.document, root)
            self.assertIn("uncovered native PVE operation: qm.clone", errors)

    def test_new_imported_pve_symbol_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usr/libexec").mkdir(parents=True)
            (root / "usr/libexec/probe").write_text(
                "use PVE::Future qw(new_probe);\n", encoding="utf-8"
            )
            errors = self.module.validate_catalogue(self.document, root)
            self.assertIn(
                "uncovered upstream Perl dependency: PVE::Future::new_probe",
                errors,
            )

    def test_nested_native_subcommand_is_discovered(self):
        self.assertEqual(
            self.module.discover_native_operations(
                "timeout 10 env X=1 ssh node -- qm disk import 100 image store"
            ),
            {"qm.disk-import"},
        )
        self.assertEqual(
            self.module.discover_native_operations("pct migrate 100 node-b"),
            {"pct.migrate"},
        )
        for source in (
            'subprocess.run(["qm", "clone", "100", "101"])',
            "run_command(['/usr/sbin/qm', 'clone', '100', '101'])",
            '["/usr/sbin/qm", "disk", "import", "100"]',
            '\"/usr/sbin/qm\" \"clone\" 100 101',
        ):
            with self.subTest(source=source):
                expected = {"qm.disk-import"} if "disk" in source else {"qm.clone"}
                self.assertEqual(
                    self.module.discover_native_operations(source), expected,
                )

    def test_arrow_style_pve_method_is_normalized(self):
        self.assertEqual(
            self.module.discover_pve_method_symbols(
                "my $members = PVE::Cluster->get_members();"
            ),
            {"PVE::Cluster::get_members"},
        )

    def test_new_argv_native_pve_operation_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "usr/libexec").mkdir(parents=True)
            (root / "usr/libexec/probe").write_text(
                'subprocess.run(["qm", "clone", "100", "101"])\n',
                encoding="utf-8",
            )
            errors = self.module.validate_catalogue(self.document, root)
            self.assertIn("uncovered native PVE operation: qm.clone", errors)

    def test_maintainer_scripts_are_in_dependency_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "DEBIAN").mkdir(parents=True)
            (root / "DEBIAN/preinst").write_text(
                "#!/bin/sh\n/usr/bin/uncovered-maintainer-tool\n",
                encoding="utf-8",
            )
            errors = self.module.validate_catalogue(self.document, root)
            self.assertIn(
                "uncovered external command dependency: "
                "/usr/bin/uncovered-maintainer-tool", errors,
            )

    def test_native_operation_without_exact_test_binding_fails_closed(self):
        document = json.loads(json.dumps(self.document))
        del document["source_dependency_coverage"]["native_operation_tests"]["qm.migrate"]
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn("native PVE operation test registry is incomplete", errors)

    def test_ready_scenario_without_validator_or_assertions_fails_closed(self):
        registry_path = ROOT / "usr/share/pve-sharedlvmthin/pve-lab-scenarios.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        scenario = registry["scenarios"]["web-ui-registration"]
        scenario["status"] = "READY"
        scenario["blocked_reason"] = None
        scenario["runner"] = {
            "entrypoint": "usr/sbin/sharedlvmthin", "subcommand": "web-ui-registration",
            "revision": 1, "code_files": ["usr/sbin/sharedlvmthin"],
        }
        scenario["cases"][0]["oracle"]["validator"] = None
        scenario["cases"][0]["oracle"]["required_assertions"] = []
        errors, _blocked = self.module.validate_scenario_registry(
            registry, self.document, ROOT,
        )
        self.assertIn(
            "ready lab scenario has no executable oracle: web-ui-registration", errors
        )

    def test_native_operation_cannot_name_unrelated_test(self):
        document = json.loads(json.dumps(self.document))
        binding = document["source_dependency_coverage"]["native_operation_tests"]["qm.migrate"]
        binding["static_test"] = "tests/unit/plugin_lifecycle.t"
        errors = self.module.validate_catalogue(document, ROOT)
        self.assertIn(
            "unverifiable native PVE operation test binding: qm.migrate", errors,
        )


if __name__ == "__main__":
    unittest.main()
