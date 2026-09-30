import importlib.machinery
import importlib.util
import hashlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-upstream-inventory"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_upstream_inventory", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class UpstreamInventoryEvaluationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    @staticmethod
    def inventory(fingerprint="a" * 64, complete=True):
        return {
            "kind": "inventory",
            "schema": 1,
            "catalogue": "pve-upstream-contracts-v1",
            "collection_complete": complete,
            "errors": [] if complete else ["probe failed"],
            "contract_fingerprint": fingerprint,
            "host": "node-a",
            "contract": {
                "compatibility_contract_catalogue": {
                    "ok": True,
                    "sha256": "c" * 64,
                    "contract_ids": ["contract.one", "contract.two"],
                    "surface_test_ids": [
                        "tests/unit/test_upstream_inventory.py::Surface.test_commands",
                        "tests/unit/test_upstream_inventory.py::Surface.test_hooks",
                    ],
                },
            },
        }

    def qualification(self, fingerprint="a" * 64):
        return {
            "kind": "qualification",
            "schema": 1,
            "catalogue": "pve-upstream-contracts-v1",
            "contract_fingerprint": fingerprint,
            "qualification": {"verdict": "UNQUALIFIED"},
        }

    def surface_report(self, fingerprint="a" * 64, node="node-a",
                       plugin_commit="deadbeef"):
        report = {
            "kind": "exact-surface-test-report", "schema": 1,
            "catalogue_sha256": "c" * 64,
            "contract_fingerprint": fingerprint,
            "node": node, "plugin_commit": plugin_commit,
            "results": [
                {"id": "tests/unit/test_upstream_inventory.py::Surface.test_commands",
                 "status": "PASS"},
                {"id": "tests/unit/test_upstream_inventory.py::Surface.test_hooks",
                 "status": "PASS"},
            ],
        }
        digest = hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return report, digest

    def test_incomplete_current_inventory_is_blocked(self):
        result = self.module.evaluate(self.inventory(complete=False), None)
        self.assertEqual(result["verdict"], "BLOCKED")

    def test_complete_plugin_hook_surface_is_fingerprinted(self):
        catalogue = json.loads((
            ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"
        ).read_text(encoding="utf-8"))
        covered = set(catalogue["source_dependency_coverage"]["hooks"])
        requested = os.environ.get("SLT_EXACT_SURFACE_ITEM")
        if os.environ.get("SLT_EXACT_SURFACE_KIND") == "hooks" and requested:
            self.assertIn(requested, covered)
            self.assertIn(requested, self.module.REQUIRED_HOOKS)
        else:
            self.assertEqual(set(self.module.REQUIRED_HOOKS), covered)

    def test_upstream_perl_symbol_registry_matches_contract_catalogue(self):
        import json
        catalogue = json.loads((
            ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"
        ).read_text(encoding="utf-8"))
        covered = set(catalogue["source_dependency_coverage"]["perl_symbols"])
        requested = os.environ.get("SLT_EXACT_SURFACE_ITEM")
        if os.environ.get("SLT_EXACT_SURFACE_KIND") == "perl_symbols" and requested:
            self.assertIn(requested, covered)
            self.assertIn(requested, self.module.REQUIRED_PVE_SYMBOLS)
        else:
            self.assertEqual(set(self.module.REQUIRED_PVE_SYMBOLS), covered)

    def test_every_covered_runtime_command_is_in_inventory(self):
        for command in ("systemd-run", "blkdiscard", "blockdev", "lvrename",
                        "thin_check", "sharedlvmthin"):
            self.assertIn(command, self.module.COMMANDS)

    def test_command_evidence_binds_binary_to_package_owner(self):
        digest = {"ok": True, "sha256": "a" * 64, "mode": 0o755,
                  "uid": 0, "gid": 0, "size": 123}
        def owner_probe(argv):
            return {"ok": True, "rc": 0,
                    "stdout": f"coreutils: {argv[-1]}", "error": ""}
        requested = os.environ.get("SLT_EXACT_SURFACE_ITEM")
        if os.environ.get("SLT_EXACT_SURFACE_KIND") == "commands" and requested:
            catalogue = json.loads((
                ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json"
            ).read_text(encoding="utf-8"))
            self.assertIn(requested, catalogue["source_dependency_coverage"]["commands"])
            commands = [Path(requested).name]
            internal = set(catalogue["source_dependency_coverage"][
                "exact_surface_tests"
            ]["internal_command_exceptions"])
            if commands[0] in internal:
                source = ROOT / "usr/sbin" / commands[0]
                self.assertTrue(source.is_file())
                self.assertFalse(source.is_symlink())
                return
            self.assertIn(commands[0], self.module.COMMANDS)
        else:
            commands = self.module.COMMANDS
        for command in commands:
            with self.subTest(command=command), \
                    unittest.mock.patch.object(
                        self.module.shutil, "which", return_value=f"/usr/bin/{command}"
                    ), unittest.mock.patch.object(
                        self.module.os.path, "islink", return_value=False
                    ), unittest.mock.patch.object(
                        self.module.os.path, "realpath", return_value=f"/usr/bin/{command}"
                    ), unittest.mock.patch.object(
                        self.module, "_sha256", return_value=digest
                    ), unittest.mock.patch.object(
                        self.module, "_run", side_effect=owner_probe
                    ):
                evidence = self.module._command_evidence(command)
                self.assertTrue(evidence["ok"])
                self.assertEqual(evidence["owners"], ["coreutils"])

    def test_command_evidence_refuses_unowned_replacement(self):
        digest = {"ok": True, "sha256": "b" * 64, "mode": 0o755,
                  "uid": 0, "gid": 0, "size": 123}
        probe = {"ok": False, "rc": 1, "stdout": "", "error": "no path found"}
        with unittest.mock.patch.object(self.module.shutil, "which",
                                         return_value="/usr/bin/sync"), \
                unittest.mock.patch.object(self.module.os.path, "islink",
                                             return_value=False), \
                unittest.mock.patch.object(self.module.os.path, "realpath",
                                             return_value="/usr/bin/sync"), \
                unittest.mock.patch.object(self.module, "_sha256",
                                             return_value=digest), \
                unittest.mock.patch.object(self.module, "_run",
                                             return_value=probe):
            evidence = self.module._command_evidence("sync")
        self.assertFalse(evidence["ok"])
        self.assertEqual(evidence["error"], "package owner unavailable")

    def test_loaded_upstream_modules_are_critical_files(self):
        for path in (
                "/usr/share/perl5/PVE/JSONSchema.pm",
                "/usr/share/perl5/PVE/QemuConfig.pm",
                "/usr/share/perl5/PVE/QemuServer.pm",
                "/usr/share/perl5/PVE/QemuServer/QemuImage.pm",
                "/usr/share/perl5/PVE/QemuServer/Drive.pm",
                "/usr/share/perl5/PVE/SSHInfo.pm",
                "/usr/share/perl5/PVE/Storage/Common.pm",
                "/usr/share/perl5/PVE/Storage/LVMPlugin.pm"):
            self.assertIn(path, self.module.CRITICAL_FILES)

    def test_apt_candidate_drift_does_not_change_runtime_contract(self):
        package_data = {
            "libpve-storage-perl": {
                "installed": "libpve-storage-perl|9.1.10|all|installed|ok",
                "installed_probe_ok": True,
                "candidate": "9.1.11",
                "candidate_probe_ok": True,
            }
        }
        runtime = {
            name: {
                "installed": evidence["installed"],
                "installed_probe_ok": evidence["installed_probe_ok"],
            }
            for name, evidence in package_data.items()
        }
        before = hashlib.sha256(json.dumps(runtime, sort_keys=True).encode()).hexdigest()
        package_data["libpve-storage-perl"]["candidate"] = "9.1.12"
        runtime_after = {
            name: {
                "installed": evidence["installed"],
                "installed_probe_ok": evidence["installed_probe_ok"],
            }
            for name, evidence in package_data.items()
        }
        after = hashlib.sha256(json.dumps(runtime_after, sort_keys=True).encode()).hexdigest()
        self.assertEqual(before, after)

    def test_candidate_observation_failure_is_not_runtime_failure(self):
        evidence = {
            "installed": "libpve-storage-perl|9.1.10|all|installed|ok",
            "installed_probe_ok": True,
            "candidate": None,
            "candidate_probe_ok": False,
        }
        runtime_errors = []
        update_errors = []
        if not evidence["installed_probe_ok"]:
            runtime_errors.append("installed package evidence incomplete")
        if not evidence["candidate_probe_ok"]:
            update_errors.append("APT candidate evidence unavailable")
        self.assertEqual(runtime_errors, [])
        self.assertEqual(update_errors, ["APT candidate evidence unavailable"])

    def test_fixture_matches_upstream_lvm_name_boundaries(self):
        fixture = ROOT / "tests/unit/lib/PVE/Storage/Plugin.pm"
        probe = (
            "use PVE::Storage::Plugin; "
            "for my $n (qw(a a+ b- -b ab a.b a_b a-b)) { "
            "my $v=PVE::Storage::Plugin::parse_lvm_name($n,1); "
            "print $n, '=', (defined($v)?'ok':'bad'), qq(\\n); }"
        )
        result = subprocess.run(
            ["perl", f"-I{fixture.parent.parent}", "-e", probe],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), [
            "a=bad", "a+=bad", "b-=bad", "-b=bad",
            "ab=ok", "a.b=ok", "a_b=ok", "a-b=ok",
        ])

    def test_scenario_registry_is_a_critical_owned_file(self):
        self.assertIn(
            "/usr/share/pve-sharedlvmthin/pve-lab-scenarios.json",
            self.module.CRITICAL_FILES,
        )

    def test_missing_baseline_requires_retest(self):
        result = self.module.evaluate(self.inventory(), None)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_changed_exact_contract_requires_retest(self):
        baseline = self.qualification("b" * 64)
        baseline["qualification"] = {"verdict": "COMPATIBLE"}
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_unqualified_identical_capture_is_not_compatible(self):
        baseline = self.qualification()
        baseline["qualification"] = {"verdict": "UNQUALIFIED"}
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_incomplete_qualification_is_not_compatible(self):
        baseline = self.qualification()
        baseline["qualification"] = {
            "verdict": "COMPATIBLE",
            "contract_fingerprint": "a" * 64,
            "plugin_commit": "deadbeef",
        }
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_only_exact_fully_qualified_tuple_is_compatible(self):
        baseline = self.qualification()
        report, report_digest = self.surface_report()
        baseline["qualification"] = {
            "verdict": "COMPATIBLE",
            "contract_fingerprint": "a" * 64,
            "plugin_commit": "deadbeef",
            "dual_sha256": "1" * 64,
            "thick_sha256": "2" * 64,
            "lab_matrix_id": "lab-2026-09-23",
            "contract_catalogue_sha256": "c" * 64,
            "passed_contract_ids": ["contract.one", "contract.two"],
            "surface_test_report": report,
            "surface_test_report_sha256": report_digest,
            "scope": "node",
            "node": "node-a",
        }
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "COMPATIBLE")
        self.assertFalse(result["cluster_upgrade_authorized"])

    def test_surface_test_report_requires_exact_pass_set_and_digest(self):
        baseline = self.qualification()
        report, report_digest = self.surface_report()
        baseline["qualification"] = {
            "verdict": "COMPATIBLE", "scope": "node", "node": "node-a",
            "contract_fingerprint": "a" * 64, "plugin_commit": "deadbeef",
            "dual_sha256": "1" * 64, "thick_sha256": "2" * 64,
            "lab_matrix_id": "lab-2026-09-23",
            "contract_catalogue_sha256": "c" * 64,
            "passed_contract_ids": ["contract.one", "contract.two"],
            "surface_test_report": report,
            "surface_test_report_sha256": report_digest,
        }
        baseline["qualification"]["surface_test_report"]["results"][0][
            "status"
        ] = "SKIP"
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        self.assertIn("digest mismatch", result["reasons"][0])

        report, report_digest = self.surface_report()
        report["results"][0]["status"] = "SKIP"
        baseline["qualification"]["surface_test_report"] = report
        baseline["qualification"]["surface_test_report_sha256"] = hashlib.sha256(
            json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        self.assertIn("not PASS", result["reasons"][0])

    def test_qualification_cannot_be_rebound_to_another_fingerprint(self):
        baseline = self.qualification()
        baseline["qualification"] = {
            "verdict": "COMPATIBLE",
            "contract_fingerprint": "b" * 64,
            "plugin_commit": "deadbeef",
            "dual_sha256": "1" * 64,
            "thick_sha256": "2" * 64,
            "lab_matrix_id": "lab-2026-09-23",
            "contract_catalogue_sha256": "c" * 64,
            "passed_contract_ids": ["contract.one", "contract.two"],
            "scope": "node",
            "node": "node-a",
        }
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_missing_contract_coverage_is_not_compatible(self):
        baseline = self.qualification()
        baseline["qualification"] = {
            "verdict": "COMPATIBLE",
            "scope": "node",
            "node": "node-a",
            "contract_fingerprint": "a" * 64,
            "plugin_commit": "deadbeef",
            "dual_sha256": "1" * 64,
            "thick_sha256": "2" * 64,
            "lab_matrix_id": "lab-2026-09-23",
            "contract_catalogue_sha256": "c" * 64,
            "passed_contract_ids": ["contract.one"],
        }
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_inventory_document_cannot_masquerade_as_qualification(self):
        result = self.module.evaluate(self.inventory(), self.inventory())
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_other_node_qualification_cannot_authorize_current_node(self):
        baseline = self.qualification()
        baseline["qualification"] = {
            "verdict": "COMPATIBLE",
            "scope": "node",
            "node": "node-b",
            "contract_fingerprint": "a" * 64,
            "plugin_commit": "deadbeef",
            "dual_sha256": "1" * 64,
            "thick_sha256": "2" * 64,
            "lab_matrix_id": "lab-2026-09-23",
        }
        result = self.module.evaluate(self.inventory(), baseline)
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            self.module._strict_json_load(io.StringIO('{"kind":"qualification","kind":"inventory"}'))

    def test_command_environment_cannot_redirect_perl_or_python_modules(self):
        completed = subprocess.CompletedProcess(["probe"], 0, stdout="ok\n", stderr="")
        with mock.patch.dict(
            os.environ,
            {"PERL5LIB": "/tmp/evil", "PERL5OPT": "-Mevil", "PYTHONPATH": "/tmp/evil"},
            clear=False,
        ), mock.patch.object(self.module.subprocess, "run", return_value=completed) as run:
            result = self.module._run(["probe"])
        self.assertTrue(result["ok"])
        environment = run.call_args.kwargs["env"]
        for key in ("PERL5LIB", "PERL5OPT", "PYTHONPATH", "PYTHONHOME"):
            self.assertNotIn(key, environment)
        self.assertEqual(environment["LC_ALL"], "C")
        self.assertEqual(environment["PATH"], self.module.SAFE_PATH)

    def test_file_hash_refuses_symlink_and_records_identity(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            link = Path(directory) / "link"
            target.write_bytes(b"payload")
            link.symlink_to(target)
            evidence = self.module._sha256(target)
            self.assertTrue(evidence["ok"])
            self.assertEqual(evidence["size"], 7)
            self.assertIn("uid", evidence)
            self.assertIn("gid", evidence)
            self.assertFalse(self.module._sha256(link)["ok"])

    def test_both_installed_package_profiles_are_rejected(self):
        observations = [
            {"ok": True, "stdout": "pve-sharedlvmthin|1|all|installed|ok"},
            {"ok": True, "rc": 0, "stdout": "installed|ok"},
        ]
        with mock.patch.object(self.module, "_run", side_effect=observations):
            result = self.module._installed_profile_payload("dual")
        self.assertFalse(result["ok"])
        self.assertIn("opposite package profile", result["error"])

    def test_ambiguous_opposite_profile_probe_is_rejected(self):
        observations = [
            {"ok": True, "stdout": "pve-sharedlvmthin|1|all|installed|ok"},
            {"ok": False, "rc": 2, "stdout": ""},
        ]
        with mock.patch.object(self.module, "_run", side_effect=observations):
            result = self.module._installed_profile_payload("dual")
        self.assertFalse(result["ok"])
        self.assertIn("state is unavailable", result["error"])


if __name__ == "__main__":
    unittest.main()
