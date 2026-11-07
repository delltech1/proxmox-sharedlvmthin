import importlib.machinery
import importlib.util
import contextlib
import datetime
import io
import json
import hashlib
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-aggregate"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_compat_aggregate", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class CompatibilityAggregateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    def test_cli_without_candidate_preserves_original_schema_fields(self):
        expected = {"kind", "schema", "phase", "run_id", "collection_plan_sha256",
                    "cluster_id_sha256", "upgrade_plan_sha256", "lab_runner_sha256",
                    "plugin_artifacts", "required_transitions", "class_descriptors",
                    "catalogue_sha256", "scenario_registry_sha256", "collected_at",
                    "verdict", "cluster_upgrade_authorized", "expected_nodes",
                    "observed_nodes", "classes", "errors", "limitations"}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            common = ["--collection-plan", str(root / "missing-plan"),
                      "--node-result", str(root / "missing-node")]
            output = root / "result.json"
            with contextlib.redirect_stdout(io.StringIO()):
                status = self.module.main(common + ["--output", str(output)])
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(status, 2)
            self.assertEqual(set(result), expected)
            self.assertFalse(result["cluster_upgrade_authorized"])
            output = root / "candidate-result.json"
            with contextlib.redirect_stdout(io.StringIO()):
                status = self.module.main(common + ["--output", str(output),
                    "--candidate-deb", str(root / "candidate.deb"),
                    "--candidate-package", "sample-test"])
            result = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(status, 2)
            self.assertEqual(set(result), expected | {"local_candidate_verified"})
            self.assertIsNone(result["local_candidate_verified"])
            self.assertFalse(result["cluster_upgrade_authorized"])

    @unittest.skipUnless(os.name == "posix" and os.path.exists("/usr/bin/dpkg-deb")
                         and os.path.isdir("/proc/self/fd"), "requires Linux dpkg-deb")
    def test_local_candidate_archive_is_bound_to_exact_plan_row(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pkg" / "DEBIAN").mkdir(parents=True)
            (root / "pkg" / "DEBIAN" / "control").write_text(
                "Package: sample-test\nVersion: 1.0\nArchitecture: all\n"
                "Maintainer: Test\nDescription: test\n", encoding="utf-8")
            deb = root / "candidate.deb"
            subprocess.run(["/usr/bin/dpkg-deb", "--build", str(root / "pkg"), str(deb)],
                           check=True, stdout=subprocess.DEVNULL)
            row = {"package": "sample-test", "version": "1.0", "architecture": "all",
                   "sha256": hashlib.sha256(deb.read_bytes()).hexdigest()}
            plan = {"candidate_packages": [row]}
            self.assertEqual(self.module.verify_candidate_deb(deb, "sample-test", plan), row)
            for key, value in (("sha256", "a" * 64), ("version", "2.0"),
                               ("architecture", "amd64")):
                with self.subTest(key=key):
                    changed = dict(row, **{key: value})
                    with self.assertRaisesRegex(ValueError, "differs from plan"):
                        self.module.verify_candidate_deb(deb, "sample-test", {"candidate_packages": [changed]})
            with self.assertRaisesRegex(ValueError, "identity differs"):
                self.module.verify_candidate_deb(deb, "other-package", {"candidate_packages": [
                    dict(row, package="other-package")]})
            with self.assertRaisesRegex(ValueError, "exactly one"):
                self.module.verify_candidate_deb(deb, "sample-test", {"candidate_packages": [row, row]})
            link = root / "link.deb"
            link.symlink_to(deb)
            with self.assertRaises(OSError):
                self.module.verify_candidate_deb(link, "sample-test", plan)
            oversized = root / "oversized.deb"
            with oversized.open("wb") as handle:
                handle.truncate(512 * 1024 * 1024 + 1)
            with self.assertRaisesRegex(ValueError, "bounded regular"):
                self.module.verify_candidate_deb(oversized, "sample-test", plan)
            with mock.patch.object(self.module.selectors.DefaultSelector, "select", return_value=[]):
                with self.assertRaisesRegex(ValueError, "timed out"):
                    self.module.verify_candidate_deb(deb, "sample-test", plan)

            real_popen = subprocess.Popen
            def substituted(code):
                def launch(_argv, **kwargs):
                    return real_popen([sys.executable, "-c", code], **kwargs)
                return launch

            for code, reason in (("import sys; sys.stdout.write('x'*5000)", "exceeds limit"),
                                 ("raise SystemExit(7)", "inspection failed")):
                with self.subTest(reason=reason), mock.patch.object(
                        self.module.subprocess, "Popen", side_effect=substituted(code)):
                    with self.assertRaisesRegex(ValueError, reason):
                        self.module.verify_candidate_deb(deb, "sample-test", plan)

            pidfile = root / "child.pid"
            code = ("import os,time; child=os.fork(); "
                    f"open({str(pidfile)!r},'w').write(str(child)) if child else None; "
                    "os._exit(0) if child else time.sleep(60)")
            with mock.patch.object(self.module.subprocess, "Popen", side_effect=substituted(code)):
                with self.assertRaisesRegex(ValueError, "timed out"):
                    self.module.verify_candidate_deb(deb, "sample-test", plan, subprocess_timeout=0.5)
            child_pid = int(pidfile.read_text())
            for _ in range(100):
                statfile = Path(f"/proc/{child_pid}/stat")
                if not statfile.exists() or statfile.read_text().split(") ", 1)[1][0] == "Z":
                    break
                time.sleep(0.01)
            else:
                self.fail("timed-out dpkg-deb descendant remains live")

            def replace_path(argv, **kwargs):
                deb.rename(root / "original.deb")
                deb.write_bytes(b"replacement")
                return real_popen(argv, **kwargs)
            with mock.patch.object(self.module.subprocess, "Popen", side_effect=replace_path):
                with self.assertRaisesRegex(ValueError, "path changed"):
                    self.module.verify_candidate_deb(deb, "sample-test", plan)

    @staticmethod
    def node(host, fingerprint="a" * 64, candidates=None):
        return {
            "host": host,
            "boot_id": "11111111-1111-1111-1111-111111111111",
            "collected_at": "2026-09-23T08:00:00+00:00",
            "contract_fingerprint": fingerprint,
            "catalogue_sha256": "c" * 64,
            "package_flavor": "dual",
            "running_kernel": "test",
            "plugin_payload_sha256": "9" * 64,
            "candidates": candidates,
            "result_sha256": "d" * 64,
        }

    def test_exact_node_set_groups_identical_contracts(self):
        result = self.module.aggregate(
            [self.node("node-a"), self.node("node-b")], ["node-a", "node-b"]
        )
        self.assertEqual(result["verdict"], "RETEST_REQUIRED")
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["classes"][0]["nodes"], ["node-a", "node-b"])
        self.assertEqual(result["classes"][0]["package_flavor"], "dual")

    def test_missing_node_blocks(self):
        result = self.module.aggregate([self.node("node-a")], ["node-a", "node-b"])
        self.assertEqual(result["verdict"], "BLOCKED")

    def test_duplicate_host_blocks(self):
        result = self.module.aggregate(
            [self.node("node-a"), self.node("node-a")], ["node-a"]
        )
        self.assertEqual(result["verdict"], "BLOCKED")

    def test_candidate_sets_must_match(self):
        result = self.module.aggregate(
            [self.node("node-a", candidates=None), self.node("node-b", candidates=[])],
            ["node-a", "node-b"],
        )
        self.assertEqual(result["verdict"], "BLOCKED")

    def test_collection_plan_rejects_duplicate_nodes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "plan.json"
            now = datetime.datetime.now(datetime.timezone.utc)
            plan = {
                "kind": "cluster-compatibility-plan",
                "schema": 1,
                "run_id": "11111111-1111-1111-1111-111111111111",
                "phase": "pre-upgrade",
                "cluster_id_sha256": "a" * 64,
                "upgrade_plan_sha256": "b" * 64,
                "collector_sha256": "c" * 64,
                "lab_runner_sha256": "e" * 64,
                "valid_from": (now - datetime.timedelta(minutes=1)).isoformat(),
                "valid_until": (now + datetime.timedelta(minutes=1)).isoformat(),
                "eligible_nodes": [
                    {"host": "node-a", "storage_scope_sha256": "d" * 64},
                    {"host": "node-a", "storage_scope_sha256": "d" * 64},
                ],
                "candidate_packages": [],
                "plugin_artifacts": {"dual_sha256": "f" * 64, "thick_sha256": "1" * 64},
                "required_transitions": [],
                "class_descriptors": [{"contract_fingerprint": "a" * 64,
                                       "package_flavor": "dual", "running_kernel": "test",
                                       "plugin_payload_sha256": "9" * 64}],
            }
            path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate"):
                self.module.load_plan(path, now)

    def _bundle(self, root, *, compat="COMPATIBILITY=PASS\n",
                qualification_ready=True, blocked_scenarios=None):
        now = datetime.datetime.now(datetime.timezone.utc)
        boot = "11111111-1111-1111-1111-111111111111"
        contract = {
            "package_flavor": "dual",
            "running_kernel": "test-kernel",
            "installed_profile_payload": {"payload_sha256": "9" * 64},
            "storage_contract": {"canonical_sha256": "d" * 64},
            "compatibility_contract_catalogue": {"ok": True, "sha256": "c" * 64},
            "critical_files": {
                "/usr/share/pve-sharedlvmthin/pve-lab-scenarios.json": "d" * 64,
            },
        }
        fingerprint = hashlib.sha256(
            json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        inventory = {
            "inventory": {
                "kind": "inventory", "collection_complete": True,
                "host": "node-a", "contract_fingerprint": fingerprint,
                "boot_id_file_sha256": hashlib.sha256((boot + "\n").encode()).hexdigest(),
                "contract": contract,
            },
            "evaluation": None,
        }
        outputs = {
            "contract-catalogue": json.dumps({
                "valid": True,
                "upgrade_authorized": False,
                "qualification_ready": qualification_ready,
                "scenario_registry_sha256": "d" * 64,
                "blocked_scenarios": blocked_scenarios or [],
            }) + "\n",
            "qmdestroy-contract": (
                "QMDestroy_CONTRACT_VERSION=1\n"
                "NATIVE_CONFIG_CAS=ABSENT\n"
                "VDISK_FREE_FAILURE_POLICY=WARN_AND_CONTINUE\n"
                "FINAL_CONFIG_REMOVAL=AFTER_DESTROY_VM\n"
                "FLEECING_CLEANUP_CALL=PINNED\n"
                "IPAM_CLEANUP_CALL=PINNED\n"
                f"CONTRACT_SHA256={'e' * 64}\n"
                "CONTRACT_VARIANT=API15\n"
                "MUTATION_ADAPTER=QUALIFIED\n"
                "QMDestroy_CONTRACT=QUALIFIED\n"
            ),
            "upstream-inventory": json.dumps(inventory) + "\n",
            "compat-check": compat,
            "upgrade-check": "UPGRADE_SCAN_COMPLETE=YES\nUPGRADE_SAFE=YES\n",
            "upstream-inventory-final": json.dumps(inventory) + "\n",
        }
        tools = {
            "contract-catalogue": "sharedlvmthin-contract-check",
            "qmdestroy-contract": "sharedlvmthin-qmdestroy-contract-check",
            "upstream-inventory": "sharedlvmthin-upstream-inventory",
            "compat-check": "sharedlvmthin-compat-check",
            "upgrade-check": "sharedlvmthin-upgrade-check",
            "upstream-inventory-final": "sharedlvmthin-upstream-inventory",
        }
        steps = []
        for name, text in outputs.items():
            stdout = root / f"{name}.stdout"
            stderr = root / f"{name}.stderr"
            stdout.write_text(text, encoding="utf-8")
            stderr.write_bytes(b"")
            steps.append({
                "name": name, "argv": [f"/usr/libexec/pve-sharedlvmthin/{tools[name]}"],
                "returncode": 0, "complete": True, "passed": True,
                "timed_out": False, "error": None,
                "stdout": {"size": stdout.stat().st_size, "truncated": False,
                           "sha256": self.module.sha256(stdout)},
                "stderr": {"size": 0, "truncated": False,
                           "sha256": self.module.sha256(stderr)},
            })
        plan = {
            "kind": "cluster-compatibility-plan", "schema": 1,
            "run_id": boot, "phase": "pre-upgrade",
            "cluster_id_sha256": "a" * 64, "upgrade_plan_sha256": "b" * 64,
            "collector_sha256": "e" * 64,
            "lab_runner_sha256": "f" * 64,
            "valid_from": (now - datetime.timedelta(minutes=1)).isoformat(),
            "valid_until": (now + datetime.timedelta(minutes=1)).isoformat(),
            "eligible_nodes": [{"host": "node-a", "storage_scope_sha256": "d" * 64}],
            "candidate_packages": [],
            "plugin_artifacts": {"dual_sha256": "1" * 64, "thick_sha256": "2" * 64},
            "required_transitions": [],
            "class_descriptors": [{"contract_fingerprint": fingerprint,
                                   "package_flavor": "dual", "running_kernel": "test-kernel",
                                   "plugin_payload_sha256": "9" * 64}],
        }
        plan_path = root / "plan.json"
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        gate = {
            "kind": "node-compatibility-gate", "schema": 1,
            "phase": "pre-upgrade", "run_id": boot,
            "collection_plan_sha256": self.module.sha256(plan_path),
            "cluster_id_sha256": "a" * 64, "upgrade_plan_sha256": "b" * 64,
            "collector_sha256": "e" * 64,
            "lab_runner_sha256": "f" * 64,
            "plugin_artifacts": {"dual_sha256": "1" * 64, "thick_sha256": "2" * 64},
            "class_descriptors": plan["class_descriptors"],
            "expected_storage_scope_sha256": "d" * 64,
            "started_at": (now - datetime.timedelta(seconds=10)).isoformat(),
            "collected_at": now.isoformat(), "host": "node-a",
            "boot_id_at_start": boot, "boot_id_at_finish": boot,
            "verdict": "RETEST_REQUIRED", "upgrade_authorized": False,
            "blocking_steps": [], "steps": steps,
        }
        result_path = root / "gate-result.json"
        result_path.write_text(json.dumps(gate), encoding="utf-8")
        loaded_plan = self.module.load_plan(plan_path, now)
        return result_path, loaded_plan, self.module.sha256(plan_path), now

    def test_bundle_is_recomputed_from_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path, plan, plan_sha, now = self._bundle(Path(directory))
            result = self.module.inspect_bundle(result_path, plan, plan_sha, now, 3600)
            self.assertEqual(result["host"], "node-a")

    def test_contradictory_terminal_marker_blocks_even_with_passed_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path, plan, plan_sha, now = self._bundle(
                Path(directory), compat="COMPATIBILITY=PASS\nCOMPATIBILITY=FAIL\n"
            )
            with self.assertRaisesRegex(ValueError, "terminal marker"):
                self.module.inspect_bundle(result_path, plan, plan_sha, now, 3600)

    def test_unexecutable_required_scenario_blocks_aggregate(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path, plan, plan_sha, now = self._bundle(
                Path(directory), qualification_ready=False,
                blocked_scenarios=["live-migration"],
            )
            with self.assertRaisesRegex(ValueError, "unexecutable required scenarios"):
                self.module.inspect_bundle(result_path, plan, plan_sha, now, 3600)


if __name__ == "__main__":
    unittest.main()
