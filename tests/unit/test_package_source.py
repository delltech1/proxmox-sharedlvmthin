import hashlib
import ipaddress
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PostRebootHealthOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (
            ROOT / "experiments/thick-generations/package-post-reboot-gate.sh"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"python3 - \"\$health_file\".*?<<'PY'\n(.*?)\nPY\n",
            source,
            re.DOTALL,
        )
        if match is None:
            raise AssertionError("post-reboot health oracle heredoc is missing")
        cls.oracle = match.group(1)

    def run_oracle(self, *, result="PASS", checks=None, profile="dual", storages=None):
        if checks is None:
            checks = [{"name": "fixture", "status": "PASS", "message": "ok"}]
        if storages is None:
            storages = []
        package = (
            "pve-sharedlvmthin-thick" if profile == "thick-only"
            else "pve-sharedlvmthin"
        )
        document = {
            "result": result,
            "checks": checks,
            "platform": {
                "package_flavor": profile,
                "plugin_package": package,
                "plugin_version": "fixture-version",
            },
            "storages": storages,
        }
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "health.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    self.oracle,
                    str(path),
                    profile,
                    package,
                    "fixture-version",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

    def test_warn_with_only_pass_and_warn_checks_is_accepted(self):
        result = self.run_oracle(
            result="WARN",
            checks=[
                {"name": "pass", "status": "PASS", "message": "ok"},
                {"name": "warn", "status": "WARN", "message": "diagnostic"},
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_warn_with_fail_check_is_rejected(self):
        result = self.run_oracle(
            result="WARN",
            checks=[{"name": "bad", "status": "FAIL", "message": "unsafe"}],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("complete zero-failure check set", result.stderr)

    def test_empty_or_unknown_check_set_is_rejected(self):
        for checks in ([], [{"name": "missing"}], [{"status": "UNKNOWN"}]):
            with self.subTest(checks=checks):
                result = self.run_oracle(result="WARN", checks=checks)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("complete zero-failure check set", result.stderr)

    def test_thick_only_allows_remote_thin_but_rejects_local_thin(self):
        remote = {
            "id": "remote-thin",
            "node_applicable": False,
            "allocation_mode": "thin",
        }
        thick = {
            "id": "local-thick",
            "node_applicable": True,
            "allocation_mode": "thick-generations-lazy",
        }
        accepted = self.run_oracle(
            profile="thick-only", storages=[remote, thick]
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

        local = {**remote, "id": "local-thin", "node_applicable": True}
        rejected = self.run_oracle(profile="thick-only", storages=[local])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("local-thin", rejected.stderr)

    def test_thick_only_rejects_unknown_storage_scope(self):
        result = self.run_oracle(
            profile="thick-only",
            storages=[{"id": "ambiguous", "allocation_mode": "thin"}],
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unknown node scope", result.stderr)


class PackageSourceTests(unittest.TestCase):
    def test_storage_move_preflight_blocks_lazy_before_native_dispatch(self):
        source = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-storage-move-preflight"
        ).read_text(encoding="utf-8")
        volume_operation = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-volume-operation-preflight"
        ).read_text(encoding="utf-8")
        qmdestroy_contract = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmdestroy-contract-check"
        ).read_text(encoding="utf-8")
        vm_destroy_journal = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy"
        ).read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        excluded = (ROOT / "packaging/thick-only/excluded-paths.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("PVE::Cluster::cfs_update()", source)
        self.assertIn("PVE::QemuConfig->load_config($vmid)", source)
        self.assertIn("PVE::QemuServer::parse_drive($disk", source)
        self.assertIn("PVE::Storage::storage_check_enabled($storecfg", source)
        self.assertIn("sha256_hex($config_bytes", source)
        self.assertIn("LAZY_DORMANT", source)
        self.assertIn("LAZY_ACTIVE", source)
        self.assertIn("active Lazy owner is not this exact node/boot epoch", source)
        self.assertIn("Lazy Thick native Storage Move requires a stopped VM", source)
        self.assertIn("does not admit EFI or TPM state disks", source)
        self.assertIn("requires discard=ignore", source)
        self.assertIn("requires detect_zeroes=0", source)
        self.assertIn("STORAGE_MOVE_READY=NO", source)
        self.assertIn("STORAGE_MOVE_READY=YES", source)
        self.assertIn("storage-move-preflight)", cli)
        self.assertIn("sharedlvmthin-storage-move-preflight", build)
        self.assertIn("sharedlvmthin-storage-move-preflight", postinst)
        self.assertNotIn("sharedlvmthin-storage-move-preflight", excluded)
        self.assertIn("volume-operation-preflight)", cli)
        self.assertIn("sharedlvmthin-volume-operation-preflight", build)
        self.assertIn("sharedlvmthin-volume-operation-preflight", postinst)
        self.assertNotIn("sharedlvmthin-volume-operation-preflight", excluded)
        self.assertIn("operation must be 'resize' or 'attach'", volume_operation)
        self.assertIn("PVE::QemuServer::parse_drive", volume_operation)
        self.assertIn("PVE::Storage::storage_check_enabled", volume_operation)
        self.assertIn("_thick_read_anchor", volume_operation)
        self.assertIn("LAZY_DORMANT", volume_operation)
        self.assertIn("is not an unused disk", volume_operation)
        self.assertIn("CONFIG_SHA256", volume_operation)
        for forbidden in ("run_command", "system(", "qx/", "`", "lvchange", "dmsetup"):
            self.assertNotIn(forbidden, volume_operation)
        self.assertIn("qmdestroy-contract-check)", cli)
        self.assertIn("sharedlvmthin-qmdestroy-contract-check", build)
        self.assertIn("sharedlvmthin-qmdestroy-contract-check", postinst)
        self.assertNotIn("sharedlvmthin-qmdestroy-contract-check", excluded)
        self.assertIn("VDISK_FREE_FAILURE_POLICY=WARN_AND_CONTINUE", qmdestroy_contract)
        self.assertIn("NATIVE_CONFIG_CAS=ABSENT", qmdestroy_contract)
        self.assertIn("IPAM_CLEANUP_CALL=PINNED", qmdestroy_contract)
        self.assertNotIn("subprocess", qmdestroy_contract)
        self.assertIn("vm-destroy-observe)", cli)
        self.assertIn("sharedlvmthin-vm-destroy", build)
        self.assertIn("sharedlvmthin-vm-destroy", postinst)
        self.assertNotIn("sharedlvmthin-vm-destroy", excluded)
        self.assertIn('choices=["observe", "_append", "_observe-durable"]', vm_destroy_journal)
        self.assertIn(
            'exec /usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C LANG=C /usr/bin/python3 -I /usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy observe "$@"',
            cli,
        )
        self.assertNotIn("_append)", cli)
        self.assertNotIn("_observe-durable)", cli)
        self.assertIn('"authority": "NONE"', vm_destroy_journal)
        for forbidden in (
            "run_command(", "system(", "qx/", "`qm ", "lvchange", "dmsetup",
            "activate_volume", "deactivate_volume", "_thick_transition_anchor",
        ):
            self.assertNotIn(forbidden, source)

    def test_destructive_move_runners_preflight_before_detached_dispatch(self):
        for name in (
            "tg53-fiveway-mixed-wave.sh",
            "tg53-concurrent-snapshot-move.sh",
        ):
            source = (ROOT / "experiments/thick-generations" / name).read_text(
                encoding="utf-8"
            )
            preflight = source.index("storage-move-preflight")
            dispatch = source.index("tg53_dispatch_detached")
            self.assertLess(preflight, dispatch, name)
            self.assertIn("-o BatchMode=yes", source)

    def test_migration_preflight_is_read_only_mode_and_volume_aware(self):
        source = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-migration-preflight"
        ).read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        excluded = (ROOT / "packaging/thick-only/excluded-paths.txt").read_text(
            encoding="utf-8"
        )

        self.assertIn("PVE::Cluster::cfs_update()", source)
        self.assertIn("PVE::QemuConfig->load_config($vmid)", source)
        self.assertIn("PVE::QemuServer::foreach_volid", source)
        self.assertIn("$info->{is_attached}", source)
        self.assertIn("direct Thin live migration is unsupported", source)
        self.assertIn("thick-lazy-materialize $storeid $volname", source)
        self.assertIn("offline handoff requires LAZY_DORMANT", source)
        self.assertIn("MIGRATION_SAFE=NO", source)
        self.assertIn("MIGRATION_SAFE=YES", source)
        for forbidden in ("run_command(", "system(", "qx/", "`qm ", "lvchange", "dmsetup"):
            self.assertNotIn(forbidden, source)
        self.assertIn("migration-preflight)", cli)
        self.assertIn("sharedlvmthin-migration-preflight", build)
        self.assertIn("sharedlvmthin-migration-preflight", postinst)
        self.assertNotIn("sharedlvmthin-migration-preflight", excluded)

    def _isolated_preinst_source(self):
        """Return preinst without inheriting the qualification host's policy."""
        source = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        return source.replace(
            "UPDATE_POLICY_FILE=/var/lib/pve-sharedlvmthin/update-guard/policy.json",
            "UPDATE_POLICY_FILE=/__sharedlvmthin_unit_test_no_active_policy__",
        )

    def test_doctor_pvesm_status_cannot_leak_its_command_substitution_pipe(self):
        source = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertNotIn('PVE_STATUS="$(timeout --foreground 20 pvesm status', source)
        self.assertIn('PVE_STATUS_FILE="$(mktemp)"', source)
        self.assertIn(
            'timeout --foreground 20 pvesm status >"$PVE_STATUS_FILE" 2>/dev/null',
            source,
        )
        self.assertLess(
            source.index('pvesm status >"$PVE_STATUS_FILE"'),
            source.index('PVE_STATUS="$(cat "$PVE_STATUS_FILE")"'),
        )
        self.assertIn('rm -f -- "$PVE_STATUS_FILE"', source)

    def _write_absent_maintenance_probe(self, directory):
        probe = directory / "sharedlvmthin-package-maintenance-check"
        probe.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = --probe-hold ]; then\n"
            "    printf '%s\\n' MAINTENANCE_HOLD=ABSENT\n"
            "    exit 3\n"
            "fi\n"
            "exit 2\n",
            encoding="utf-8",
        )
        probe.chmod(0o755)

    @unittest.skipUnless(os.name == "posix", "postinst fixture requires POSIX")
    def test_maintenance_postinst_configures_package_without_opening_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            private_bin = root / "bin"
            private_bin.mkdir()
            tree = root / "root"
            paths = {
                "libexec": tree / "usr/libexec/pve-sharedlvmthin",
                "perl": tree / "usr/share/perl5/PVE/Storage/Custom",
                "share": tree / "usr/share/pve-sharedlvmthin",
                "sbin": tree / "usr/sbin",
                "state": tree / "var/lib/pve-sharedlvmthin",
                "etc": tree / "etc/pve-sharedlvmthin",
                "lvm": tree / "etc/lvm",
            }
            for path in paths.values():
                path.mkdir(parents=True, exist_ok=True)
            (paths["state"] / "maintenance").mkdir(mode=0o700)
            (paths["state"] / "maintenance/active.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (paths["share"] / "package-flavor").write_text("dual\n", encoding="ascii")
            (paths["share"] / "package-artifact-sha256").write_text(
                "a" * 64 + "\n", encoding="ascii"
            )
            perl_files = [
                paths["perl"] / "SharedLvmThinPlugin.pm",
                paths["libexec"] / "sharedlvmthin-thick-materialize",
                paths["libexec"] / "pve-sharedlvmthin-monitor",
                paths["libexec"] / "sharedlvmthin-thin-guardd",
            ]
            for path in perl_files:
                path.write_text("1;\n", encoding="ascii")
            for name in ("sharedlvmthin-health-json", "sharedlvmthin_pve_inventory.py",
                         "sharedlvmthin-web", "sharedlvmthin-vm-destroy-state-bootstrap"):
                (paths["libexec"] / name).write_text("pass\n", encoding="ascii")
            checker_log = root / "checker.log"
            checker = paths["libexec"] / "sharedlvmthin-package-maintenance-check"
            checker.write_text(
                "#!/usr/bin/python3\nimport sys\n"
                f"open({str(checker_log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            checker.chmod(0o755)
            for name in ("sharedlvmthin-compat-check", "sharedlvmthin-recovery-check",
                         "sharedlvmthin-upgrade-check", "sharedlvmthin-update-plan",
                         "sharedlvmthin-snapshot-observe",
                         "sharedlvmthin-migration-preflight",
                         "sharedlvmthin-storage-move-preflight",
                         "sharedlvmthin-volume-operation-preflight",
                         "sharedlvmthin-qmdestroy-contract-check",
                         "sharedlvmthin-vm-destroy",
                         "sharedlvmthin-vm-destroy-recovery",
                         "sharedlvmthin-vm-destroy-dispatch",
                         "sharedlvmthin-bridge-topology", "sharedlvmthin-thin-metadata-check",
                         "sharedlvmthin-qmp-path-check",
                         "sharedlvmthin-profile-replacement"):
                path = paths["libexec"] / name
                path.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
                path.chmod(0o755)
            for name in ("sharedlvmthin", "sharedlvmthin-web-configure"):
                path = paths["sbin"] / name
                path.write_text("#!/bin/sh\nexit 99\n", encoding="ascii")
                path.chmod(0o755)
            lvm_before = "administrator-owned\n"
            (paths["lvm"] / "lvmlocal.conf").write_text(lvm_before, encoding="utf-8")
            systemctl_log = root / "systemctl.log"
            (private_bin / "systemctl").write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{systemctl_log}'\nexit 99\n",
                encoding="utf-8",
            )
            (private_bin / "systemctl").chmod(0o755)
            (private_bin / "dpkg-query").write_text(
                "#!/bin/sh\nprintf '%s' '0.9.0~rc5.13~tg34'\n",
                encoding="ascii",
            )
            (private_bin / "dpkg-query").chmod(0o755)

            source = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
            replacements = {
                "/usr/libexec/pve-sharedlvmthin": str(paths["libexec"]),
                "/usr/share/perl5/PVE/Storage/Custom": str(paths["perl"]),
                "/usr/share/pve-sharedlvmthin": str(paths["share"]),
                "/usr/sbin": str(paths["sbin"]),
                "/var/lib/pve-sharedlvmthin": str(paths["state"]),
                "/etc/pve-sharedlvmthin": str(paths["etc"]),
                "/etc/lvm/lvmlocal.conf": str(paths["lvm"] / "lvmlocal.conf"),
            }
            for old, new in replacements.items():
                source = source.replace(old, new)
            postinst = root / "pve-sharedlvmthin.postinst"
            postinst.write_text(source, encoding="utf-8")
            postinst.chmod(0o755)
            control_checker = (
                root / "pve-sharedlvmthin.sharedlvmthin-package-maintenance-check"
            )
            control_checker.write_bytes(checker.read_bytes())
            control_checker.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{private_bin}:/usr/bin:/bin"
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
            result = subprocess.run(
                ["/bin/sh", str(postinst), "configure"], env=env, text=True,
                capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("OPERATIONAL_READY=NO", result.stdout)
            self.assertIn("PACKAGE_MAINTENANCE_HOLD=ACTIVE", result.stdout)
            self.assertIn("This is not a PASS", result.stdout)
            self.assertFalse(systemctl_log.exists(), "maintenance postinst called systemctl")
            self.assertEqual((paths["lvm"] / "lvmlocal.conf").read_text(), lvm_before)
            calls = checker_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(calls), 3)
            self.assertEqual(calls[0], "--probe-hold")
            self.assertNotIn("--record-phase", calls[1])
            self.assertIn("--record-phase PACKAGE_CONFIGURED_DEFERRED", calls[2])

    def _run_postrm_purge(self, *, other_status="", query_rc=0,
                          marker="dual\n", lvm_text=""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        flavor = root / "package-flavor"
        if marker is not None:
            flavor.write_text(marker, encoding="utf-8")
        lvm = root / "lvmlocal.conf"
        lvm.write_text(lvm_text, encoding="utf-8")
        config = root / "etc-state"
        runtime = root / "runtime-state"
        cache = root / "private-bin" / "__pycache__"
        for path in (config, runtime, cache):
            path.mkdir(parents=True)
        systemctl = root / "systemctl"
        systemctl.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        systemctl.chmod(0o755)
        dpkg_query = root / "dpkg-query"
        rows = "pve-sharedlvmthin|config-files\n"
        if other_status:
            rows += f"pve-sharedlvmthin-thick|{other_status}\n"
        dpkg_query.write_text(
            "#!/bin/sh\n"
            f"printf '%s' '{rows}'\n"
            f"exit {query_rc}\n",
            encoding="utf-8",
        )
        dpkg_query.chmod(0o755)
        source = (ROOT / "DEBIAN/postrm").read_text(encoding="utf-8")
        source = source.replace(
            "FLAVOR_FILE=/usr/share/pve-sharedlvmthin/package-flavor",
            f"FLAVOR_FILE={flavor}",
        ).replace(
            "LVMLOCAL=/etc/lvm/lvmlocal.conf", f"LVMLOCAL={lvm}"
        ).replace(
            "rm -rf /etc/pve-sharedlvmthin", f"rm -rf {config}"
        ).replace(
            "rm -rf /usr/libexec/pve-sharedlvmthin/__pycache__",
            f"rm -rf {cache}",
        ).replace(
            "rmdir /usr/libexec/pve-sharedlvmthin 2>/dev/null || true",
            f"rmdir {cache.parent} 2>/dev/null || true",
        )
        candidate = root / "postrm"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
        result = subprocess.run(
            ["/bin/sh", str(candidate), "purge"], env=env, text=True,
            capture_output=True, check=False,
        )
        return result, lvm, config, runtime

    def test_lazy_zero_l0_inventory_is_read_only_and_fail_closed(self):
        source = (
            ROOT / "experiments/thick-generations/lazy-zero-l0-inventory.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("--expect-host", source)
        self.assertIn("CONFIG_DM_CLONE", source)
        self.assertIn("CONFIG_DM_ZERO", source)
        self.assertIn("MODULE_DM_CLONE_PATH", source)
        self.assertIn("TARGET_ZERO=UNAVAILABLE", source)
        self.assertIn("RETEST_AFTER_EXPLICIT_DM_ZERO_LOAD", source)
        self.assertIn("MUTATION_PERFORMED=NO", source)
        self.assertNotIn("modprobe", source)
        self.assertNotRegex(source, r"dmsetup\s+(create|load|reload|resume|remove|status)")

    @staticmethod
    def _write_preinst_identity(
        root, *, package="pve-sharedlvmthin", flavor="dual",
        version="0.9.0~rc5.11~tg32", digest="a" * 64,
    ):
        values = {
            "sharedlvmthin-candidate-package": package,
            "sharedlvmthin-candidate-version": version,
            "sharedlvmthin-candidate-flavor": flavor,
            "sharedlvmthin-candidate-architecture": "all",
            "sharedlvmthin-candidate-artifact-sha256": digest,
        }
        for name, value in values.items():
            (root / name).write_text(value + "\n", encoding="utf-8")
        installed = root / "installed-package-artifact-sha256"
        installed.write_text(digest + "\n", encoding="utf-8")
        return installed

    def _run_preinst_freeze_admission(
        self, *, policy=True, architecture=True, payload=True,
        verifier_rc=0, preflight=False,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        storage = root / "storage.cfg"
        storage.write_text("dir: local\n        path /tmp\n", encoding="utf-8")
        self._write_preinst_identity(root)
        if not architecture:
            (root / "sharedlvmthin-candidate-architecture").unlink()
        policy_path = root / "active-policy.json"
        if policy:
            policy_path.write_text("{}\n", encoding="utf-8")
        call_log = root / "verifier-call"
        if payload:
            verifier = root / "sharedlvmthin-candidate-update-policy"
            verifier.write_text(
                "import os, sys\n"
                "from pathlib import Path\n"
                "Path(os.environ['FREEZE_CALL_LOG']).write_text('\\n'.join(sys.argv[1:]) + '\\n')\n"
                "raise SystemExit(int(os.environ['FREEZE_VERIFIER_RC']))\n",
                encoding="utf-8",
            )
            (root / "sharedlvmthin-candidate-qualified-tuples.json").write_text(
                "{}\n", encoding="utf-8",
            )
            (root / "sharedlvmthin_update_policy.py").write_text(
                "# isolated candidate policy fixture\n", encoding="utf-8",
            )
        source = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(root / "absent-flavor")
        ).replace(
            "INSTALLED_ARTIFACT_FILE=/usr/share/pve-sharedlvmthin/package-artifact-sha256",
            f"INSTALLED_ARTIFACT_FILE={root / 'installed-package-artifact-sha256'}",
        ).replace(
            "STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}",
        ).replace(
            "UPDATE_POLICY_FILE=/var/lib/pve-sharedlvmthin/update-guard/policy.json",
            f"UPDATE_POLICY_FILE={policy_path}",
        ).replace(
            "# dpkg has not unpacked the candidate payload yet.",
            "exit 0\n\n# dpkg has not unpacked the candidate payload yet.",
            1,
        )
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
        env["FREEZE_CALL_LOG"] = str(call_log)
        env["FREEZE_VERIFIER_RC"] = str(verifier_rc)
        action = ["preflight", "install"] if preflight else ["install"]
        result = subprocess.run(
            ["/bin/sh", str(candidate), *action], env=env, text=True,
            capture_output=True, check=False,
        )
        return result, call_log

    @unittest.skipUnless(os.name == "posix", "preinst admission requires POSIX shell")
    def test_preinst_freeze_admission_is_explicit_and_fail_closed(self):
        result, call_log = self._run_preinst_freeze_admission(policy=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(call_log.exists())

        result, _ = self._run_preinst_freeze_admission(architecture=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("architecture identity is missing or unsafe", result.stderr)

        result, _ = self._run_preinst_freeze_admission(payload=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("admission payload is missing or unsafe", result.stderr)

        for verifier_rc in (0, 3):
            with self.subTest(verifier_rc=verifier_rc):
                result, call_log = self._run_preinst_freeze_admission(
                    verifier_rc=verifier_rc,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                call = call_log.read_text(encoding="utf-8")
                self.assertIn("verify-prepared-freeze-package", call)
                self.assertIn("--target-architecture\nall", call)

        result, _ = self._run_preinst_freeze_admission(verifier_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active FREEZE refused package unpack", result.stderr)

        result, call_log = self._run_preinst_freeze_admission(
            architecture=False, payload=False, preflight=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(call_log.exists())

    def test_postinst_python_validation_leaves_no_unowned_bytecode(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        self.assertNotIn("python3 -m py_compile", postinst)
        self.assertIn('compile(path.read_bytes(), str(path), "exec")', postinst)
        self.assertIn("-name 'sharedlvmthin-health-json*.pyc'", postinst)
        self.assertIn("-name 'sharedlvmthin_pve_inventory*.pyc'", postinst)
        self.assertIn("-name 'sharedlvmthin-web*.pyc'", postinst)
        self.assertNotIn("-type f -name '*.pyc' -delete", postinst)
        self.assertIn('if [ -L "$PYTHON_CACHE" ]; then', postinst)

    def test_artifact_identity_covers_data_and_control_behavior(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            control = root / "control"
            (data / "usr/libexec").mkdir(parents=True)
            control.mkdir()
            worker = data / "usr/libexec/worker"
            preinst = control / "preinst"
            worker.write_text("worker-v1\n", encoding="utf-8")
            preinst.write_text("preinst-v1\n", encoding="utf-8")
            script = ROOT / "scripts/package-artifact-identity.py"

            def identity():
                result = subprocess.run(
                    [sys.executable, str(script), str(data), str(control)],
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout.strip()

            original = identity()
            worker.write_text("worker-v2\n", encoding="utf-8")
            data_changed = identity()
            self.assertNotEqual(original, data_changed)
            worker.write_text("worker-v1\n", encoding="utf-8")
            preinst.write_text("preinst-v2\n", encoding="utf-8")
            control_changed = identity()
            self.assertNotEqual(original, control_changed)
            if os.name == "posix":
                preinst.write_text("preinst-v1\n", encoding="utf-8")
                preinst.chmod(0o755)
                self.assertNotEqual(original, identity())

    def test_artifact_identity_excludes_only_derived_identity_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            control = root / "control"
            identity_dir = data / "usr/share/pve-sharedlvmthin"
            identity_dir.mkdir(parents=True)
            control.mkdir()
            (data / "payload").write_text("payload\n", encoding="utf-8")
            (control / "control").write_text("Package: example\n", encoding="utf-8")
            script = ROOT / "scripts/package-artifact-identity.py"

            def identity():
                return subprocess.run(
                    [sys.executable, str(script), str(data), str(control)],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()

            original = identity()
            (identity_dir / "package-artifact-sha256").write_text(
                "a" * 64 + "\n", encoding="utf-8"
            )
            (control / "sharedlvmthin-candidate-artifact-sha256").write_text(
                "b" * 64 + "\n", encoding="utf-8"
            )
            (control / "md5sums").write_text("derived\n", encoding="utf-8")
            self.assertEqual(original, identity())

    def test_artifact_identity_counts_debian_data_in_separate_layout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            control = root / "control"
            (data / "DEBIAN").mkdir(parents=True)
            control.mkdir()
            rogue = data / "DEBIAN/ordinary-data"
            rogue.write_text("one\n", encoding="utf-8")
            (control / "control").write_text("Package: example\n", encoding="utf-8")
            script = ROOT / "scripts/package-artifact-identity.py"

            def identity():
                return subprocess.run(
                    [sys.executable, str(script), str(data), str(control)],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()

            original = identity()
            rogue.write_text("two\n", encoding="utf-8")
            self.assertNotEqual(original, identity())

    def test_artifact_identity_matches_staged_and_extracted_layouts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            staged = root / "staged"
            staged_control = staged / "DEBIAN"
            (staged / "usr/libexec").mkdir(parents=True)
            staged_control.mkdir()
            (staged / "usr/libexec/worker").write_text("worker\n", encoding="utf-8")
            (staged_control / "control").write_text("Package: example\n", encoding="utf-8")
            extracted_data = root / "extracted-data"
            extracted_control = root / "extracted-control"
            (extracted_data / "usr/libexec").mkdir(parents=True)
            extracted_control.mkdir()
            (extracted_data / "usr/libexec/worker").write_text("worker\n", encoding="utf-8")
            (extracted_control / "control").write_text("Package: example\n", encoding="utf-8")
            script = ROOT / "scripts/package-artifact-identity.py"

            def identity(data, control):
                return subprocess.run(
                    [sys.executable, str(script), str(data), str(control)],
                    text=True, capture_output=True, check=True,
                ).stdout.strip()

            self.assertEqual(
                identity(staged, staged_control),
                identity(extracted_data, extracted_control),
            )

    def test_readme_states_current_thick_geometry_without_legacy_ambiguity(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        version = next(
            line.split(":", 1)[1].strip()
            for line in (ROOT / "DEBIAN/control").read_text(encoding="utf-8").splitlines()
            if line.startswith("Version:")
        )
        release_asset_version = version.replace("~", ".")
        self.assertIn("1 MiB\ndm-clone region candidate", readme)
        self.assertIn("including legacy 4/8 KiB objects", readme)
        self.assertIn("not the new dm-clone region default", readme)
        self.assertIn(
            f"pve-sharedlvmthin_{release_asset_version}_all.deb", readme
        )
        self.assertIn(
            f"pve-sharedlvmthin-thick_{release_asset_version}_all.deb", readme
        )
        self.assertNotIn("0.9.0.rc5.4.1.tg25", readme)

    @staticmethod
    def _prepare_prerm_dpkg_root(temp_path, env):
        root = temp_path / "dpkg-root"
        storage_dir = root / "etc/pve"
        storage_dir.mkdir(parents=True)
        (storage_dir / "storage.cfg").write_text("", encoding="utf-8")
        env["DPKG_ROOT"] = str(root)

    def _run_thick_only_preinst_inventory(
        self, *, vg_attr="wz--n-", vg_output=None,
        allocation_mode=None, vg_layout=None,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        storage = root / "storage.cfg"
        storage_text = "dir: local\n        path /tmp\n"
        if allocation_mode is not None:
            storage_text += (
                "sharedlvmthin: thick-test\n"
                "        slt-vgname shared-vg\n"
                f"        slt-allocation-mode {allocation_mode}\n"
            )
            if vg_layout is not None:
                storage_text += f"        slt-vg-layout {vg_layout}\n"
        storage.write_text(storage_text, encoding="utf-8")
        marker = root / "package-flavor"
        if vg_output is None:
            vg_output = f"printf '%s\\n' 'shared-vg|{vg_attr}'"
        commands = {
            "vgs": f"#!/bin/sh\n{vg_output}\n",
            "lvs": "#!/bin/sh\nexit 0\n",
        }
        for name, text in commands.items():
            command = root / name
            command.write_text(text, encoding="utf-8")
            command.chmod(0o755)
        source = self._isolated_preinst_source()
        installed_artifact = self._write_preinst_identity(
            root, package="pve-sharedlvmthin-thick", flavor="thick-only"
        )
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(marker)
        ).replace(
            "INSTALLED_ARTIFACT_FILE=/usr/share/pve-sharedlvmthin/package-artifact-sha256",
            f"INSTALLED_ARTIFACT_FILE={installed_artifact}",
        ).replace("STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}")
        source = source.replace(
            "# P0 upgrade fence for the exclusive Thin owner schema.",
            "exit 0\n\n# P0 upgrade fence for the exclusive Thin owner schema.",
        )
        # This helper isolates the Thick-only inventory boundary. Candidate
        # recovery auditing has its own fixtures and must not intercept a
        # synthetic storage definition before the boundary under test.
        source = source.replace(
            'if [ "$IS_UPGRADE" -eq 1 ]; then',
            'IS_UPGRADE=0\nif [ "$IS_UPGRADE" -eq 1 ]; then',
            1,
        )
        self._write_absent_maintenance_probe(root)
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin-thick"
        return subprocess.run(
            ["/bin/sh", str(candidate), "install"], env=env, text=True,
            capture_output=True, check=False,
        )

    def _run_dual_preinst_layout_gate(
        self, storage_text, *, maintenance=False, awk_rc=None,
    ):
        """Execute the real TG48 layout gate and stop before later audits."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        storage = root / "storage.cfg"
        storage.write_text(storage_text, encoding="utf-8")
        installed_artifact = self._write_preinst_identity(root)
        source = self._isolated_preinst_source()
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(root / "absent-flavor")
        ).replace(
            "INSTALLED_ARTIFACT_FILE=/usr/share/pve-sharedlvmthin/package-artifact-sha256",
            f"INSTALLED_ARTIFACT_FILE={installed_artifact}",
        ).replace("STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}")
        source = source.replace(
            "if [ \"$IS_UPGRADE\" -eq 1 ]; then",
            "exit 0\n\nif [ \"$IS_UPGRADE\" -eq 1 ]; then",
            1,
        )
        maintenance_probe = root / "sharedlvmthin-package-maintenance-check"
        if maintenance:
            maintenance_probe.write_text(
                "#!/bin/sh\n"
                "[ \"$1\" = --probe-hold ] && exit 0\n"
                "[ \"$1\" = --phase ] && exit 0\n"
                "exit 2\n",
                encoding="utf-8",
            )
        else:
            self._write_absent_maintenance_probe(root)
        maintenance_probe.chmod(0o755)
        hostname = root / "hostname"
        hostname.write_text("#!/bin/sh\nprintf '%s\\n' node-a\n", encoding="utf-8")
        hostname.chmod(0o755)
        if awk_rc is not None:
            awk = root / "awk"
            awk.write_text(f"#!/bin/sh\nexit {awk_rc}\n", encoding="utf-8")
            awk.chmod(0o755)
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
        return subprocess.run(
            ["/bin/sh", str(candidate), "install"], env=env, text=True,
            capture_output=True, check=False,
        )

    def _run_preinst_candidate_audit(
        self, *, recovery_safe=True, disabled=True, duplicate=False,
        installed_marker=True, action="upgrade", systemctl_ok=True,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        marker = root / "package-flavor"
        if installed_marker:
            marker.write_text("dual\n", encoding="utf-8")
        storage = root / "storage.cfg"
        storage_text = (
            "sharedlvmthin: disabled-thick\n"
            + ("        disable 1\n" if disabled else "")
            + "        vgname shared-vg\n"
            + "        slt-allocation-mode thick-generations\n"
        )
        if duplicate:
            storage_text += (
                "sharedlvmthin: disabled-thick\n"
                "        vgname other-vg\n"
                "        slt-allocation-mode thick-generations\n"
            )
        storage.write_text(storage_text, encoding="utf-8")
        calls = root / "recovery.calls"
        recovery = root / "sharedlvmthin-candidate-recovery-check"
        records = (
            "THICK_ANCHORS_HEALTHY=PASS\nVG_INTENT_CLEAR=PASS\n"
            "STATE=HEALTHY\nSAFE_FOR_MUTATION=YES\n"
            if recovery_safe
            else "THICK_ANCHORS_HEALTHY=FAIL\nVG_INTENT_CLEAR=FAIL\n"
                 "STATE=RECOVERY_REQUIRED\nSAFE_FOR_MUTATION=NO\n"
        )
        recovery.write_text(
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >>'{calls}'\n"
            f"printf '%s' '{records}'\n"
            f"exit {0 if recovery_safe else 2}\n",
            encoding="utf-8",
        )
        recovery.chmod(0o755)
        systemctl = root / "systemctl"
        systemctl.write_text(
            f"#!/bin/sh\nexit {0 if systemctl_ok else 1}\n", encoding="utf-8"
        )
        systemctl.chmod(0o755)

        source = self._isolated_preinst_source()
        installed_artifact = self._write_preinst_identity(root)
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(marker)
        ).replace(
            "INSTALLED_ARTIFACT_FILE=/usr/share/pve-sharedlvmthin/package-artifact-sha256",
            f"INSTALLED_ARTIFACT_FILE={installed_artifact}",
        ).replace("STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}")
        source = source.replace(
            "# The Thick-only package must never silently strand",
            "exit 0\n\n# The Thick-only package must never silently strand",
        )
        self._write_absent_maintenance_probe(root)
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
        if action == "preflight-upgrade":
            arguments = ["/bin/sh", str(candidate), "preflight", "upgrade"]
        else:
            arguments = ["/bin/sh", str(candidate), action]
        if action in ("upgrade", "preflight-upgrade"):
            arguments += ["0.9.0~rc5.10~tg31", "0.9.0~rc5.11~tg32"]
        result = subprocess.run(
            arguments, env=env, text=True,
            capture_output=True, check=False,
        )
        invoked = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return result, invoked

    def _run_preinst_legacy_inventory(self, *, dmsetup_rc=0, lvs_rc=0):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        marker = root / "package-flavor"
        marker.write_text("dual\n", encoding="utf-8")
        storage = root / "storage.cfg"
        storage.write_text("dir: local\n        path /tmp\n", encoding="utf-8")
        recovery = root / "sharedlvmthin-candidate-recovery-check"
        recovery.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        recovery.chmod(0o755)
        commands = {
            "systemctl": "#!/bin/sh\nexit 0\n",
            "dmsetup": f"#!/bin/sh\nexit {dmsetup_rc}\n",
            "lvs": f"#!/bin/sh\nexit {lvs_rc}\n",
        }
        for name, text in commands.items():
            command = root / name
            command.write_text(text, encoding="utf-8")
            command.chmod(0o755)
        source = self._isolated_preinst_source()
        installed_artifact = self._write_preinst_identity(root)
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(marker)
        ).replace(
            "INSTALLED_ARTIFACT_FILE=/usr/share/pve-sharedlvmthin/package-artifact-sha256",
            f"INSTALLED_ARTIFACT_FILE={installed_artifact}",
        ).replace("STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}")
        self._write_absent_maintenance_probe(root)
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
        return subprocess.run(
            [
                "/bin/sh", str(candidate), "upgrade",
                "0.9.0~rc5.10~tg31", "0.9.0~rc5.11~tg32",
            ], env=env, text=True,
            capture_output=True, check=False,
        )

    def _run_preinst_artifact_identity(
        self, *, action="upgrade", old_version="0.9.0~rc5.11~tg32",
        new_version="0.9.0~rc5.11~tg32", candidate_digest="a" * 64,
        installed_digest="a" * 64, package="pve-sharedlvmthin",
        flavor="dual", installed_kind="regular",
        candidate_kind="regular", return_late_marker=False,
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        installed = self._write_preinst_identity(
            root, package=package, flavor=flavor, version=new_version,
            digest=candidate_digest,
        )
        candidate_artifact = root / "sharedlvmthin-candidate-artifact-sha256"
        if candidate_kind == "missing":
            candidate_artifact.unlink()
        elif candidate_kind == "trailing":
            candidate_artifact.write_bytes(
                (candidate_digest + "\nUNTERMINATED").encode("ascii")
            )
        elif candidate_kind == "nul":
            candidate_artifact.write_bytes(candidate_digest.encode("ascii") + b"\0\n")
        if installed_kind == "missing":
            installed.unlink()
        elif installed_kind == "malformed":
            installed.write_text("not-a-digest\n", encoding="utf-8")
        elif installed_kind == "trailing":
            installed.write_bytes(
                ((installed_digest or "a" * 64) + "\nUNTERMINATED").encode("ascii")
            )
        elif installed_kind == "nul":
            installed.write_bytes(
                (installed_digest or "a" * 64).encode("ascii") + b"\0\n"
            )
        elif installed_kind == "symlink":
            target = root / "installed-target"
            target.write_text((installed_digest or "a" * 64) + "\n", encoding="utf-8")
            installed.unlink()
            installed.symlink_to(target)
        elif installed_digest is not None:
            installed.write_text(installed_digest + "\n", encoding="utf-8")

        source = self._isolated_preinst_source()
        late_marker = root / "late-stage-reached"
        source = source.replace(
            "INSTALLED_ARTIFACT_FILE=/usr/share/pve-sharedlvmthin/package-artifact-sha256",
            f"INSTALLED_ARTIFACT_FILE={installed}",
        ).replace(
            "# dpkg has not unpacked the candidate payload yet.",
            f"printf reached >'{late_marker}'\nexit 0\n\n"
            "# dpkg has not unpacked the candidate payload yet.",
        )
        self._write_absent_maintenance_probe(root)
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["DPKG_MAINTSCRIPT_PACKAGE"] = package
        arguments = ["/bin/sh", str(candidate), action]
        if action == "upgrade":
            arguments += [old_version, new_version]
        result = subprocess.run(
            arguments, env=env, text=True, capture_output=True, check=False,
        )
        if return_late_marker:
            return result, late_marker.exists()
        return result

    def _run_package_profile_gate(
        self, *, active_units="", expected_current="none", preinst_source=None,
        execute=False, terminate_during_preinst=False, installed_name="",
        installed_version="0.9.0~rc5.10~tg31",
        installed_status="ii", select_policy="",
    ):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        temp_path = Path(temp.name)
        package = temp_path / "candidate.deb"
        package.write_bytes(b"qualification-candidate\n")
        log = temp_path / "dpkg.log"
        control_source = temp_path / "candidate-control"
        control_source.mkdir()
        for name in (
            "sharedlvmthin-candidate-recovery-check",
            "sharedlvmthin-package-maintenance-check",
            "sharedlvmthin-candidate-update-policy",
            "sharedlvmthin_update_policy.py",
            "sharedlvmthin-candidate-qualified-tuples.json",
            "sharedlvmthin_pve_inventory.py",
            "sharedlvmthin-candidate-package",
            "sharedlvmthin-candidate-version",
            "sharedlvmthin-candidate-flavor",
            "sharedlvmthin-candidate-artifact-sha256",
        ):
            path = control_source / name
            value = "a" * 64 if name == "sharedlvmthin-candidate-artifact-sha256" \
                else "fixture"
            path.write_text(value + "\n", encoding="utf-8")
        for name in (
            "sharedlvmthin-candidate-recovery-check",
            "sharedlvmthin-package-maintenance-check",
            "sharedlvmthin-candidate-update-policy",
        ):
            (control_source / name).chmod(0o755)
        candidate_preinst = control_source / "preinst"
        ready_marker = temp_path / "preinst-ready"
        execute_env_log = temp_path / "execute-env.log"
        rendered_preinst = preinst_source or "#!/bin/sh\nexit 0\n"
        rendered_preinst = rendered_preinst.replace("@PACKAGE@", str(package))
        rendered_preinst = rendered_preinst.replace("@READY@", str(ready_marker))
        candidate_preinst.write_text(rendered_preinst, encoding="utf-8")
        candidate_preinst.chmod(0o755)

        commands = {
            "dpkg-deb": """#!/bin/sh
if [ "$1" = --control ]; then
    mkdir -p "$3"
    cp -a "$CANDIDATE_CONTROL_SOURCE/." "$3/"
    exit 0
fi
if [ "$1" = --extract ]; then
    mkdir -p "$3/usr/libexec/pve-sharedlvmthin" "$3/usr/share/pve-sharedlvmthin"
    : >"$3/usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy"
    printf '{}\n' >"$3/usr/share/pve-sharedlvmthin/pve-qualified-tuples.json"
    printf '%064d\n' 0 >"$3/usr/share/pve-sharedlvmthin/package-artifact-sha256"
    exit 0
fi
case "$3" in
    Package) printf '%s\\n' pve-sharedlvmthin-thick ;;
    Version) printf '%s\\n' 0.9.0~rc5.11~tg32 ;;
    Architecture) printf '%s\\n' all ;;
    *) exit 2 ;;
esac
""",
            "dpkg-query": """#!/bin/sh
package=
for argument in "$@"; do package=$argument; done
[ -n "$INSTALLED_NAME" ] && [ "$package" = "$INSTALLED_NAME" ] || exit 1
case "$2" in
    *Status-Abbrev*) printf '%s\\n' "$INSTALLED_STATUS" ;;
    *Version*) printf '%s\\n' "$INSTALLED_VERSION" ;;
    *) exit 2 ;;
esac
""",
            "systemctl": """#!/bin/sh
if [ "$1" = list-units ]; then printf '%s' "$ACTIVE_UNITS"; exit 0; fi
exit 2
""",
            "dpkg": f"""#!/bin/sh
if [ "$1" = --audit ]; then exit 0; fi
if [ "$1" = --no-act ]; then
    printf '%s sha256=%s\\n' "$*" "$(sha256sum "$3" | awk '{{print $1}}')" >>"{log}"
elif [ "$1" = -i ]; then
    printf 'PYTHONPATH=%s PERL5LIB=%s PERL5OPT=%s\\n' \
        "${{PYTHONPATH-}}" "${{PERL5LIB-}}" "${{PERL5OPT-}}" >"{execute_env_log}"
    exit 77
else
    printf '%s\\n' "$*" >>"{log}"
fi
exit 0
""",
            "python3": """#!/bin/sh
if [ "$2" = prepare-direct-package ]; then
    echo DIRECT_PACKAGE_TXID=dddddddddddddddddddddddddddddddd
    echo DIRECT_PACKAGE_PREPARED=PASS
    exit 0
fi
exec /usr/bin/python3 "$@"
""",
        }
        fixture_values = {
            "dpkg-deb": {"CANDIDATE_CONTROL_SOURCE": str(control_source)},
            "dpkg-query": {"INSTALLED_NAME": installed_name,
                           "INSTALLED_VERSION": installed_version,
                           "INSTALLED_STATUS": installed_status},
            "systemctl": {"ACTIVE_UNITS": active_units},
        }
        for name, source in commands.items():
            # Fixture values live in mock executables, not inherited env: the
            # gate must execute the REAL absolute /usr/bin/env -i sanitizer.
            assignments = ''.join(f'{key}={shlex.quote(value)}\n'
                                  for key, value in fixture_values.get(name, {}).items())
            source = source.replace('#!/bin/sh\n', '#!/bin/sh\n' + assignments, 1)
            command = temp_path / name
            command.write_text(source, encoding="utf-8")
            command.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{temp.name}{os.pathsep}{env['PATH']}"
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        hostname = subprocess.run(
            ["hostname"], text=True, capture_output=True, check=True
        ).stdout.strip()
        # Test-only copy changes exactly the two fixed PATH endpoints. Never
        # weaken the checked-in gate or intercept /usr/bin/env itself.
        gate_source = (ROOT / "experiments/thick-generations/package-profile-gate.sh").read_text(encoding="utf-8")
        fixed_path = 'PATH=/usr/sbin:/usr/bin:/sbin:/bin'
        self.assertEqual(gate_source.count(fixed_path), 2)
        self.assertIn('clean_env=(/usr/bin/env -i ' + fixed_path + ' LC_ALL=C)', gate_source)
        gate_source = gate_source.replace(fixed_path,
            'PATH=' + shlex.quote(f'{temp_path}:/usr/sbin:/usr/bin:/sbin:/bin'))
        isolated_gate = temp_path / 'package-profile-gate.sh'
        isolated_gate.write_text(gate_source, encoding='utf-8')
        arguments = [
                "/bin/bash",
                str(isolated_gate),
                "--package",
                str(package),
                "--sha256",
                digest,
                "--expect-host",
                hostname,
                "--expect-current",
                expected_current,
            ]
        if execute:
            arguments.append("--execute")
        if select_policy:
            arguments.extend(("--select-update-policy", select_policy))
        if terminate_during_preinst:
            process = subprocess.Popen(
                arguments, env=env, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
            for _ in range(100):
                if ready_marker.exists() or process.poll() is not None:
                    break
                import time
                time.sleep(0.05)
            self.assertTrue(ready_marker.exists(), "candidate preinst did not start")
            os.killpg(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=10)
            result = subprocess.CompletedProcess(
                arguments, process.returncode, stdout, stderr
            )
        else:
            result = subprocess.run(
                arguments, env=env, text=True, capture_output=True, check=False,
            )
        return result, log

    def test_package_profile_gate_environment_is_fixed_and_absolute(self):
        source = (ROOT / "experiments/thick-generations/package-profile-gate.sh").read_text(encoding="utf-8")
        self.assertIn('export PATH=/usr/sbin:/usr/bin:/sbin:/bin\n', source)
        self.assertIn('clean_env=(/usr/bin/env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C)', source)
        self.assertNotIn('clean_env=(env ', source)

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_default_is_a_real_dry_run(self):
        result, log = self._run_package_profile_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("RESULT=DRY_RUN_PASS", result.stdout)
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("--no-act -i "))

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_refuses_active_thick_unit_before_dpkg(self):
        result, log = self._run_package_profile_gate(
            active_units="pve-sharedlvmthin-tg-deadbeef.service loaded active running\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Thick transaction unit blocks package work", result.stderr)
        self.assertFalse(log.exists())

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_refuses_unexpected_current_profile(self):
        result, log = self._run_package_profile_gate(expected_current="dual")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "current package profile mismatch: expected dual, observed none",
            result.stderr,
        )
        self.assertFalse(log.exists())

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_passes_exact_install_preinst_arguments(self):
        result, _ = self._run_package_profile_gate(
            preinst_source="#!/bin/sh\nprintf 'PREINST_ARGS=%s\\n' \"$*\"\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PREINST_ARGS=preflight install", result.stdout)

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_passes_exact_upgrade_preinst_arguments(self):
        result, _ = self._run_package_profile_gate(
            expected_current="thick-only",
            installed_name="pve-sharedlvmthin-thick",
            installed_version="0.9.0~rc5.10~tg31",
            preinst_source="#!/bin/sh\nprintf 'PREINST_ARGS=%s\\n' \"$*\"\n",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "PREINST_ARGS=preflight upgrade 0.9.0~rc5.10~tg31 0.9.0~rc5.11~tg32",
            result.stdout,
        )

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_accepts_apt_held_installed_profile(self):
        result, _ = self._run_package_profile_gate(
            expected_current="thick-only",
            installed_name="pve-sharedlvmthin-thick",
            installed_status="hi",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("CURRENT_PACKAGE=pve-sharedlvmthin-thick", result.stdout)
        self.assertIn("RESULT=DRY_RUN_PASS", result.stdout)

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_uses_pinned_copy_after_original_replacement(self):
        original_digest = hashlib.sha256(b"qualification-candidate\n").hexdigest()
        result, log = self._run_package_profile_gate(
            preinst_source=(
                "#!/bin/sh\n"
                "printf 'attacker replacement\\n' > '@PACKAGE@'\n"
                "exit 0\n"
            )
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(calls), 1)
        self.assertIn(f"sha256={original_digest}", calls[0])
        self.assertIn("/candidate.deb", calls[0])

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_scrubs_import_injection_environment(self):
        old_values = {
            name: os.environ.get(name)
            for name in ("PYTHONPATH", "PERL5LIB", "BASH_ENV", "ENV")
        }
        try:
            for name in old_values:
                os.environ[name] = "/attacker-controlled"
            result, _ = self._run_package_profile_gate(
                preinst_source=(
                    "#!/bin/sh\n"
                    "[ -z \"${PYTHONPATH+x}${PERL5LIB+x}${BASH_ENV+x}${ENV+x}\" ]\n"
                )
            )
        finally:
            for name, value in old_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    def test_package_profile_gate_signal_never_reaches_install(self):
        result, log = self._run_package_profile_gate(
            execute=True,
            terminate_during_preinst=True,
            preinst_source=(
                "#!/bin/sh\n"
                "trap 'exit 130' HUP INT TERM\n"
                ": > '@READY@'\n"
                "while :; do sleep 1; done\n"
            ),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("RESULT=EXECUTE_PASS", result.stdout)
        self.assertFalse(log.exists(), "dpkg was reached after termination")

    @unittest.skipUnless(os.name == "posix", "qualification gate requires Linux")
    @unittest.skipIf(os.environ.get("SLT_PORTABLE_CI") == "1",
                     "execute environment gate requires a qualified package host")
    def test_package_profile_gate_execute_uses_clean_environment(self):
        old_values = {
            name: os.environ.get(name)
            for name in ("PYTHONPATH", "PERL5LIB", "PERL5OPT")
        }
        try:
            for name in old_values:
                os.environ[name] = "/attacker-controlled"
            result, _ = self._run_package_profile_gate(
                execute=True, select_policy="freeze"
            )
        finally:
            for name, value in old_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.assertEqual(result.returncode, 77)
        # The mocked dpkg writes this line before deliberately refusing the
        # install, so no package or post-install behavior is simulated.
        temp_root = Path(result.args[result.args.index("--package") + 1]).parent
        observed = (temp_root / "execute-env.log").read_text(encoding="utf-8")
        self.assertEqual(observed, "PYTHONPATH= PERL5LIB= PERL5OPT=\n")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_refused_removal_does_not_stop_services(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            systemctl = temp_path / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >>"$SYSTEMCTL_LOG"\n'
                'if [ "$1" = is-active ]; then exit 0; fi\n'
                "exit 0\n",
                encoding="utf-8",
            )
            lvs = temp_path / "lvs"
            lvs.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'pve-slt-sid-test'\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            lvs.chmod(0o755)
            vgs = temp_path / "vgs"
            vgs.write_text(
                "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                encoding="utf-8",
            )
            vgs.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed Thin objects still exist", result.stderr)
            self.assertFalse(log.exists(), "inventory refusal must precede service access")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_removal_refuses_storage_classifier_failure_before_service_access(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "awk": "#!/bin/sh\nexit 42\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("storage configuration classifier failed (42)", result.stderr)
            self.assertFalse(log.exists(), "classifier failure must precede services")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_removal_detects_no_space_storage_header_before_service_access(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            storage = Path(env["DPKG_ROOT"]) / "etc/pve/storage.cfg"
            storage.write_text(
                "sharedlvmthin:local-id\n"
                "\tslt-vgname isolated-thin-vg\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("storage configuration still exists", result.stderr)
            self.assertFalse(log.exists(), "storage refusal must precede services")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_failed_upgrade_prerm_refuses_without_service_access(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            systemctl = temp_path / "systemctl"
            systemctl.write_text(
                "#!/bin/sh\n"
                'printf "%s\\n" "$*" >>"$SYSTEMCTL_LOG"\n'
                "exit 0\n",
                encoding="utf-8",
            )
            systemctl.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "failed-upgrade", "0.9.0"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("previous prerm failed", result.stderr)
            self.assertFalse(log.exists(), "failed-upgrade refusal must not touch services")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_inactive_guard_does_not_allow_managed_thin_package_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": (
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                    "if [ \"$1\" = is-active ]; then exit 3; fi\nexit 0\n"
                ),
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'pve-slt-sid-test'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("regardless of ThinGuard state", result.stderr)
            self.assertFalse(log.exists(), "inventory refusal must precede service access")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_partial_vg_refuses_dual_package_removal_before_service_access(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 3\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz-pn-|'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("partial or ambiguous VG", result.stderr)
            self.assertFalse(log.exists(), "partial inventory must refuse before service access")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_empty_inventory_stops_and_confirms_active_guard_before_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            state = temp_path / "guard.state"
            state.write_text("active\n", encoding="utf-8")
            commands = {
                "systemctl": (
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                    "if [ \"$1\" = show ]; then cat \"$GUARD_STATE_FILE\"; exit 0; fi\n"
                    "if [ \"$1\" = stop ]; then printf '%s\\n' inactive >\"$GUARD_STATE_FILE\"; exit 0; fi\n"
                    "exit 0\n"
                ),
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            env["GUARD_STATE_FILE"] = str(state)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text(encoding="utf-8").splitlines()
            guard_calls = [line for line in calls if "thin-guard" in line]
            self.assertEqual(
                guard_calls[:4],
                [
                    "show --property ActiveState --value pve-sharedlvmthin-thin-guard.service",
                    "stop pve-sharedlvmthin-thin-guard.service",
                    "show --property ActiveState --value pve-sharedlvmthin-thin-guard.service",
                    "disable pve-sharedlvmthin-thin-guard.service",
                ],
            )

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_no_volume_groups_is_complete_removal_inventory(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": (
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                    "if [ \"$1\" = show ]; then printf '%s\\n' inactive; exit 0; fi\n"
                    "if [ \"$1\" = is-enabled ]; then exit 1; fi\n"
                    "exit 0\n"
                ),
                "vgs": "#!/bin/sh\nexit 0\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("partial or ambiguous VG", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_removal_refuses_separator_only_or_extra_vg_fields(self):
        for row in ("||", "|||garbage"):
            with self.subTest(row=row), tempfile.TemporaryDirectory() as temp:
                temp_path = Path(temp)
                log = temp_path / "systemctl.log"
                commands = {
                    "systemctl": (
                        "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                        "exit 0\n"
                    ),
                    "vgs": f"#!/bin/sh\nprintf '%s\\n' '{row}'\n",
                    "lvs": "#!/bin/sh\nexit 0\n",
                }
                for name, source in commands.items():
                    command = temp_path / name
                    command.write_text(source, encoding="utf-8")
                    command.chmod(0o755)
                env = os.environ.copy()
                env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
                env["SYSTEMCTL_LOG"] = str(log)
                self._prepare_prerm_dpkg_root(temp_path, env)
                result = subprocess.run(
                    ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                    env=env, text=True, capture_output=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("partial or ambiguous VG", result.stderr)
                self.assertFalse(log.exists())

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_ambiguous_guard_state_refuses_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": (
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                    "if [ \"$1\" = show ]; then printf '%s\\n' unknown; exit 0; fi\n"
                    "exit 0\n"
                ),
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("ambiguous ThinGuard state", result.stderr)
            calls = log.read_text(encoding="utf-8")
            self.assertNotIn("stop ", calls)
            self.assertNotIn("disable ", calls)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_ordinary_removal_refuses_managed_thick_objects(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'slt_tg_v=5,slt_tg_sid=thick'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin-thick"
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove"],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed Thick objects still exist", result.stderr)
            self.assertFalse(log.exists(), "Thick refusal must precede service access")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_in_favour_arguments_alone_cannot_replace_thick_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'slt_tg_v=5,slt_tg_sid=thick'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin-thick"
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                [
                    "/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove",
                    "in-favour", "pve-sharedlvmthin", "0.9.0~rc5.11~tg32",
                ],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed Thick objects still exist", result.stderr)
            self.assertFalse(log.exists(), "lifecycle arguments are not authorization")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_deconfigure_arguments_alone_cannot_replace_dual_profile(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": (
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                    "if [ \"$1\" = show ]; then printf '%s\\n' inactive; fi\nexit 0\n"
                ),
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'slt_tg_v=5,slt_tg_sid=thick'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                [
                    "/bin/sh", str(ROOT / "DEBIAN/prerm"), "deconfigure",
                    "in-favour", "pve-sharedlvmthin-thick", "0.9.0~rc5.13~tg34",
                    "removing", "pve-sharedlvmthin", "0.9.0~rc5.13~tg34",
                ],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed Thick objects still exist", result.stderr)
            self.assertFalse(log.exists(), "lifecycle arguments are not authorization")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_unrelated_in_favour_target_cannot_bypass_thick_removal_fence(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'slt_tgo_v=1,slt_tgo_sid=thick'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin-thick"
            self._prepare_prerm_dpkg_root(temp_path, env)
            result = subprocess.run(
                [
                    "/bin/sh", str(ROOT / "DEBIAN/prerm"), "remove",
                    "in-favour", "unrelated-package", "1.0",
                ],
                env=env, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("managed Thick objects still exist", result.stderr)
            self.assertFalse(log.exists(), "unrelated replacement must not reach services")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_authorized_profile_replacement_still_refuses_vg_intent(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            ready = temp_path / "ready.json"
            ready.write_text("fixture\n", encoding="ascii")
            helper = temp_path / "profile-helper"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "dpkg-query": "#!/bin/sh\nprintf '%s' '0.9.0~rc5.31~tg52'\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|slt_tg_vgi_open'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'slt_tg_v=5,slt_tg_sid=thick'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            helper.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin-thick"
            self._prepare_prerm_dpkg_root(temp_path, env)
            source = (ROOT / "DEBIAN/prerm").read_text(encoding="utf-8")
            source = source.replace(
                "/usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement",
                str(helper),
            ).replace(
                "/var/lib/pve-sharedlvmthin/profile-replacement/ready.json",
                str(ready),
            )
            prerm = temp_path / "prerm"
            prerm.write_text(source, encoding="utf-8")
            result = subprocess.run(
                ["/bin/sh", str(prerm), "remove"], env=env, text=True,
                capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Thick VG mutation intent still exists", result.stderr)
            self.assertFalse(log.exists(), "VG intent refusal must precede services")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_profile_replacement_refuses_classifier_execution_error(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            ready = temp_path / "ready.json"
            ready.write_text("fixture\n", encoding="ascii")
            helper = temp_path / "profile-helper"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
            commands = {
                "systemctl": "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\nexit 0\n",
                "dpkg-query": "#!/bin/sh\nprintf '%s' '0.9.0~rc5.31~tg52'\n",
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-|safe'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
                "awk": "#!/bin/sh\nexit 127\n",
            }
            for name, source_text in commands.items():
                command = temp_path / name
                command.write_text(source_text, encoding="utf-8")
                command.chmod(0o755)
            helper.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin-thick"
            self._prepare_prerm_dpkg_root(temp_path, env)
            source = (ROOT / "DEBIAN/prerm").read_text(encoding="utf-8")
            source = source.replace(
                "/usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement",
                str(helper),
            ).replace(
                "/var/lib/pve-sharedlvmthin/profile-replacement/ready.json",
                str(ready),
            )
            prerm = temp_path / "prerm"
            prerm.write_text(source, encoding="utf-8")
            result = subprocess.run(
                ["/bin/sh", str(prerm), "remove"], env=env, text=True,
                capture_output=True, check=False,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("VG audit classifier failed (127)", result.stderr)
            self.assertFalse(log.exists(), "classifier failure must precede services")

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_audits_disabled_storage_with_candidate_checker(self):
        result, invoked = self._run_preinst_candidate_audit(recovery_safe=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, ["disabled-thick"])
        self.assertIn("STATE=HEALTHY", result.stdout)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_private_upgrade_preflight_uses_bootstrap_recovery_probe(self):
        result, invoked = self._run_preinst_candidate_audit(
            recovery_safe=True, action="preflight-upgrade",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, ["--preinstall disabled-thick"])

    def test_preinst_uses_maintenance_mode_only_after_verified_prepare(self):
        preinst = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        self.assertIn(
            'if [ "$PREFLIGHT_ONLY" -eq 1 ] || \\\n'
            '           [ "$FREEZE_PACKAGE_PREPARED" -eq 1 ] || \\\n'
            '           [ "$MAINTENANCE_PREPARE" -eq 1 ] || \\\n'
            '           [ "$CANDIDATE_PREINSTALL" -eq 1 ]; then\n'
            '            candidate_args="--preinstall"',
            preinst,
        )
        self.assertIn('0) FREEZE_PACKAGE_PREPARED=1 ;;', preinst)
        self.assertLess(
            preinst.index("MAINTENANCE_PREPARE=1"),
            preinst.index("if ! audit_candidate_storages"),
        )

    def test_candidate_freeze_verifier_ships_its_adjacent_policy_module(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        preinst = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        gate = (
            ROOT / "experiments/thick-generations/package-profile-gate.sh"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"$STAGE/DEBIAN/sharedlvmthin_update_policy.py"', build,
        )
        self.assertIn("CANDIDATE_UPDATE_POLICY_LIB=", preinst)
        self.assertIn('[ ! -L "$CANDIDATE_UPDATE_POLICY_LIB" ]', preinst)
        self.assertIn("sharedlvmthin_update_policy.py", gate)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_allows_exact_same_version_artifact(self):
        result = self._run_preinst_artifact_identity()
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_same_version_different_artifact(self):
        result = self._run_preinst_artifact_identity(installed_digest="b" * 64)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate artifact differs", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_same_version_without_installed_identity(self):
        result = self._run_preinst_artifact_identity(installed_kind="missing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed artifact identity is unavailable", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_same_version_symlink_identity(self):
        result = self._run_preinst_artifact_identity(installed_kind="symlink")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed artifact identity is unavailable", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_unterminated_tail_in_installed_identity(self):
        result = self._run_preinst_artifact_identity(installed_kind="trailing")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed artifact identity is unavailable", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_nul_in_installed_identity(self):
        result = self._run_preinst_artifact_identity(installed_kind="nul")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("installed artifact identity is unavailable", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_missing_candidate_identity_before_later_audit(self):
        result, late_reached = self._run_preinst_artifact_identity(
            candidate_kind="missing", return_late_marker=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(late_reached)
        self.assertIn("candidate artifact identity is invalid", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_unterminated_tail_in_candidate_identity(self):
        result, late_reached = self._run_preinst_artifact_identity(
            candidate_kind="trailing", return_late_marker=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(late_reached)
        self.assertIn("candidate artifact identity is invalid", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_nul_in_candidate_identity(self):
        result, late_reached = self._run_preinst_artifact_identity(
            candidate_kind="nul", return_late_marker=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(late_reached)
        self.assertIn("candidate artifact identity is invalid", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_uses_debian_version_equivalence(self):
        result = self._run_preinst_artifact_identity(
            old_version="0.9.0~rc5.11~tg32-0", installed_digest="b" * 64,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("candidate artifact differs", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_allows_higher_version_without_legacy_identity(self):
        result = self._run_preinst_artifact_identity(
            old_version="0.9.0~rc5.10~tg31", installed_kind="missing",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_identity_gate_does_not_claim_cross_profile_immutability(self):
        result = self._run_preinst_artifact_identity(
            action="install", package="pve-sharedlvmthin-thick",
            flavor="thick-only", installed_kind="missing",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_abort_upgrade_is_inert_without_candidate_identity(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "DEBIAN/preinst"), "abort-upgrade", "1"],
            text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_unsafe_disabled_storage_before_unpack(self):
        result, invoked = self._run_preinst_candidate_audit(recovery_safe=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invoked, ["disabled-thick"])
        self.assertIn("not positively recovery-safe under candidate rules", result.stderr)
        self.assertIn("No package files were replaced", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_unsafe_enabled_storage_under_candidate_rules(self):
        result, invoked = self._run_preinst_candidate_audit(
            recovery_safe=False, disabled=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invoked, ["disabled-thick"])
        self.assertIn("candidate recovery fence refused", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_rejects_duplicate_storage_before_probe(self):
        result, invoked = self._run_preinst_candidate_audit(duplicate=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invoked, [])
        self.assertIn("candidate storage configuration is ambiguous", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_unavailable_systemd_unit_inventory(self):
        result, invoked = self._run_preinst_candidate_audit(systemctl_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(invoked, [])
        self.assertIn("materialization units could not be enumerated", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_first_install_on_cluster_node_audits_existing_storage(self):
        result, invoked = self._run_preinst_candidate_audit(
            installed_marker=False, action="install", recovery_safe=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, ["--preinstall disabled-thick"])

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_failed_legacy_dm_inventory(self):
        result = self._run_preinst_legacy_inventory(dmsetup_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("active device-mapper inventory failed or timed out", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_preinst_refuses_failed_legacy_lvs_inventory(self):
        result = self._run_preinst_legacy_inventory(lvs_rc=1)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy Thin LV inventory failed or timed out", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_preinst_refuses_partial_vg_inventory(self):
        result = self._run_thick_only_preinst_inventory(vg_attr="wz-pn-")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LVM reports a partial", result.stderr)
        self.assertIn("absence of managed Thin objects is unproven", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_preinst_accepts_complete_empty_thin_inventory(self):
        result = self._run_thick_only_preinst_inventory()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_preinst_accepts_host_with_no_volume_groups(self):
        result = self._run_thick_only_preinst_inventory(vg_output="exit 0")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_preinst_refuses_separator_only_or_extra_vg_fields(self):
        for output in ("printf '%s\\n' '|'", "printf '%s\\n' '||garbage'"):
            with self.subTest(output=output):
                result = self._run_thick_only_preinst_inventory(vg_output=output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("LVM reports a partial", result.stderr)
                self.assertIn("or ambiguous VG", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_preinst_accepts_eager_and_lazy_only_on_isolated_vg(self):
        for mode in ("thick-generations", "thick-generations-lazy"):
            result = self._run_thick_only_preinst_inventory(
                allocation_mode=mode, vg_layout="isolated",
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        for mode, layout in (
            ("thin", "isolated"),
            ("thick-generations-lazy", "mixed"),
            ("unknown", "isolated"),
        ):
            result = self._run_thick_only_preinst_inventory(
                allocation_mode=mode, vg_layout=layout,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not explicitly Eager/Lazy Thick on an isolated VG", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_dual_preinst_requires_separate_thin_and_thick_vgs(self):
        separated = (
            "sharedlvmthin: thin-a\n"
            "        slt-vgname vg-thin\n"
            "        slt-allocation-mode thin\n"
            "        slt-vg-layout isolated\n"
            "        nodes node-a,node-b\n"
            "sharedlvmthin: eager-a\n"
            "        slt-vgname vg-thick\n"
            "        slt-allocation-mode thick-generations\n"
            "        slt-vg-layout isolated\n"
            "        nodes node-a,node-b\n"
            "sharedlvmthin: lazy-a\n"
            "        slt-vgname vg-thick\n"
            "        slt-allocation-mode thick-generations-lazy\n"
            "        slt-vg-layout isolated\n"
            "        nodes node-a,node-b\n"
        )
        result = self._run_dual_preinst_layout_gate(separated)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        same_vg = separated.replace("slt-vgname vg-thin", "slt-vgname vg-thick")
        result = self._run_dual_preinst_layout_gate(same_vg)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("separate Thin and Thick VGs", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_dual_preinst_refuses_retired_mixed_even_under_prepare(self):
        mixed = (
            "sharedlvmthin: thin-a\n"
            "        slt-vgname vg-thin\n"
            "        slt-allocation-mode thin\n"
            "        slt-vg-layout mixed\n"
            "        nodes node-a,node-b\n"
        )
        for maintenance in (False, True):
            with self.subTest(maintenance=maintenance):
                result = self._run_dual_preinst_layout_gate(
                    mixed, maintenance=maintenance,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("accepts only 'slt-vg-layout isolated'", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_dual_preinst_ignores_remote_only_retired_layout(self):
        remote = (
            "sharedlvmthin: remote-thin\n"
            "        slt-vgname remote-vg\n"
            "        slt-allocation-mode thin\n"
            "        slt-vg-layout mixed\n"
            "        nodes node-z\n"
            "sharedlvmthin: local-eager\n"
            "        slt-vgname local-thick-vg\n"
            "        slt-allocation-mode thick-generations\n"
            "        slt-vg-layout isolated\n"
            "        nodes node-a,node-b\n"
        )
        result = self._run_dual_preinst_layout_gate(remote)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_dual_preinst_refuses_interrupted_layout_proof(self):
        isolated = (
            "sharedlvmthin: eager-a\n"
            "        slt-vgname vg-thick\n"
            "        slt-allocation-mode thick-generations\n"
            "        slt-vg-layout isolated\n"
            "        nodes node-a,node-b\n"
        )
        for awk_rc in (3, 127, 137):
            with self.subTest(awk_rc=awk_rc):
                result = self._run_dual_preinst_layout_gate(
                    isolated, awk_rc=awk_rc,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("layout proof failed or was interrupted", result.stderr)

    def test_thin_metadata_check_is_bounded_snapshot_only_and_packaged(self):
        helper = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-metadata-check"
        ).read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("reserve_metadata_snap", helper)
        self.assertIn("release_metadata_snap", helper)
        self.assertIn("--metadata-snap", helper)
        self.assertIn("--kill-after=5", helper)
        self.assertIn("_with_mutation_lock", helper)
        self.assertIn("--devices", helper)
        self.assertNotIn("thin_repair", helper)
        self.assertNotIn("--auto-repair", helper)
        self.assertNotIn("--clear-needs-check-flag", helper)
        self.assertIn("sharedlvmthin-thin-metadata-check", build)
        self.assertIn("sharedlvmthin-thin-metadata-check", postinst)
        self.assertIn("thin-metadata-check", cli)

    def test_thin_guard_daemon_is_static_fail_closed_and_not_enabled(self):
        daemon = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guardd"
        ).read_text(encoding="utf-8")
        unit = (
            ROOT
            / "lib/systemd/system/pve-sharedlvmthin-thin-guard.service"
        ).read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        inventory = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thin-guard-inventory"
        ).read_text(encoding="utf-8")
        self.assertIn("allow_real_watchdog => 1", daemon)
        self.assertIn("protected Thin runtime predates this process", daemon)
        self.assertIn("STOP_WATCHDOG_REFRESH", daemon)
        self.assertIn("Restart=no", unit)
        self.assertIn("[Install]", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        self.assertNotIn("enable pve-sharedlvmthin-thin-guard", postinst)
        self.assertNotIn("start pve-sharedlvmthin-thin-guard", postinst)
        self.assertNotIn('restart "$THIN_GUARD_SERVICE"', postinst)
        self.assertIn("Active ThinGuard preserved without restart", postinst)
        self.assertIn("slt-thin-peer-connect-timeout", inventory)
        self.assertIn("slt-thin-peer-probe-timeout", inventory)
        self.assertIn("BatchMode=yes", inventory)
        self.assertIn("NumberOfPasswordPrompts=0", inventory)
        self.assertNotIn("ConnectTimeout=5'", inventory)
        self.assertIn("capture_json(1300", daemon)

    def test_timing_hardening_is_fail_closed_and_documented(self):
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        helper = (ROOT / "lab/fix2-rolling-node-cycle.sh").read_text(
            encoding="utf-8"
        )
        timing = (ROOT / "docs/TIMING-AND-SCALE-SAFETY.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("sub _thin_peer_probe_timing", plugin)
        self.assertIn("timeout => $probe_timeout", plugin)
        self.assertIn("sub _thick_close_timeout", plugin)
        self.assertIn("after ${close_timeout}s close wait", plugin)
        self.assertNotIn("per_vm_timeout", helper)
        self.assertNotIn("timeout --foreground", helper)
        self.assertIn("A timeout is never fencing", timing)
        self.assertIn("no arbitrary total wall-clock deadline", timing)
        self.assertIn("CLOCK_MONOTONIC", plugin)
        self.assertIn("sub _thick_disable_background_hydration", plugin)
        self.assertIn("'disable_hydration'", plugin)
        self.assertIn("'slt-tg-command-deadline-sec'", plugin)
        self.assertIn('"${command_timeout}s"', plugin)
        self.assertIn("sub _thick_command_deadline", plugin)
        self.assertIn("did not confirm disabled background hydration", plugin)
        self.assertIn("background hydration state is unknown", plugin)
        self.assertIn("foreground guest access", timing)
        self.assertIn("BLOCKED_DSTATE", timing)
        self.assertIn("rollback outcome is UNKNOWN", plugin)
        self.assertIn("continuing without retry", plugin)
        self.assertNotIn("if ($origin_removed)", plugin)

    def test_dm_clone_io_fault_gate_is_disposable_bounded_and_exact(self):
        gate = (
            ROOT
            / "experiments/thick-generations/hydration-io-fault-qualification.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("mktemp -d /var/tmp/sltg-io-fault.", gate)
        self.assertIn("timeout --foreground --kill-after=5 30 dmsetup", gate)
        self.assertIn('[[ $fault_side == source || $fault_side == destination ]]', gate)
        self.assertIn('dm_bounded load "$fault_mapper" --readonly', gate)
        self.assertIn('dm_bounded load "$fault_mapper" --table "0 $sectors error"', gate)
        self.assertIn('dm_bounded message "$clone_map" 0 disable_hydration', gate)
        self.assertIn('[[ " $status_disabled " == *" no_hydration "* ]]', gate)
        self.assertIn('[[ $destination_hash == "$source_hash" ]]', gate)
        self.assertIn('cmp "$workdir/source.img" "$workdir/destination.img"', gate)
        self.assertNotIn("/dev/sd", gate)

    def test_live_hydration_observer_is_read_only_and_progress_based(self):
        observer = (
            ROOT
            / "experiments/thick-generations/live-hydration-observer.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("dmsetup status --noflush", observer)
        self.assertIn("MAX_PROVEN_PROGRESS_GAP_SECONDS", observer)
        self.assertIn("CLOCK_MONOTONIC", observer)
        self.assertIn("PROGRESS_REGRESSION", observer)
        self.assertIn("GEOMETRY_CHANGED", observer)
        self.assertIn("OBSERVED_PROGRESS_BYTES", observer)
        self.assertIn("AVERAGE_OBSERVED_BYTES_PER_SECOND", observer)
        self.assertIn("WORKER_ABSENT_CLONE_INCOMPLETE", observer)
        self.assertIn("WORKER_IDENTITY_CHANGED", observer)
        self.assertIn("WORKER_STARTTIME", observer)
        self.assertIn("OBSERVATION_LIMIT_UNKNOWN 3", observer)
        self.assertNotIn("SAMPLE_LIMIT_REACHED 0", observer)
        self.assertIn("PIVOT_OBSERVED", observer)
        self.assertIn("MODE=READ_ONLY", observer)
        for forbidden in (
            "dmsetup message", "dmsetup suspend", "dmsetup resume",
            "dmsetup remove", "lvchange", "lvremove", "vgchange",
        ):
            self.assertNotIn(forbidden, observer)

        fake_matrix = (
            ROOT
            / "experiments/thick-generations/live-observer-fake-matrix.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("PROGRESS_REGRESSION", fake_matrix)
        self.assertIn("GEOMETRY_CHANGED", fake_matrix)
        self.assertIn("LIMIT_UNKNOWN=PASS", fake_matrix)
        self.assertIn("unexpected dmsetup mutation", fake_matrix)
        self.assertIn("FAKE_MATRIX=PASS", fake_matrix)

    def test_full_hydration_gate_records_comparable_progress_metrics(self):
        gate = (
            ROOT
            / "experiments/thick-generations/full-hydration-qualification.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("HYDRATION_PROGRESS_REGRESSION=FAIL", gate)
        self.assertIn("MAX_NO_PROGRESS_SECONDS", gate)
        self.assertIn("AVERAGE_BYTES_PER_SECOND", gate)
        self.assertIn("MAX_HYDRATING_REGIONS", gate)
        self.assertIn('dmsetup status --noflush "$mapper"', gate)
        self.assertIn('blockdev --flushbufs "$destination_loop"', gate)
        self.assertIn("VERIFICATION_ELAPSED_NANOSECONDS", gate)
        self.assertIn("CLOCK_MONOTONIC", gate)
        self.assertNotIn("\nsync\n", gate)
        self.assertIn(
            "hydration_threshold + hydration_batch_size - 1", gate
        )
        self.assertIn("MAX_ALLOWED_HYDRATING_REGIONS", gate)
        self.assertNotIn(
            "virtual_bytes * 1000000000 / elapsed_ns", gate,
            "large qualification sizes must not overflow bash arithmetic",
        )

    def test_first_write_probe_is_identity_bound_and_progress_proven(self):
        probe = (
            ROOT
            / "experiments/thick-generations/first-write-latency-probe.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("actual_uuid == \"$expected_uuid\"", probe)
        self.assertIn("source dependency mismatch", probe)
        self.assertIn("must be distinct devices", probe)
        self.assertIn("source length mismatch", probe)
        self.assertIn("source device is not read-only", probe)
        self.assertIn("background hydration must be positively disabled", probe)
        self.assertIn("zero-hydrated clone", probe)
        self.assertIn("$new_hydrated -eq $probe", probe)
        self.assertIn("P50_NANOSECONDS", probe)
        self.assertIn("P95_NANOSECONDS", probe)
        self.assertIn("P99_NANOSECONDS", probe)
        self.assertIn('REGION_BYTES=$((region_sectors * 512))', probe)
        self.assertIn("WRITE_IO_BYTES=4096", probe)
        self.assertIn("SAMPLE_%04d_REGION", probe)
        self.assertIn("SAMPLE_%04d_BLOCK", probe)
        self.assertIn("SAMPLE_%04d_NANOSECONDS", probe)
        self.assertIn("-k4,4n", probe)
        self.assertIn("DATA_COMPARE=PASS", probe)
        self.assertIn("dmsetup status --noflush", probe)
        self.assertIn("CLOCK_MONOTONIC", probe)
        self.assertNotIn("date +%s%N", probe)
        for forbidden in (
            "dmsetup message", "dmsetup suspend", "dmsetup resume",
            "dmsetup remove", "lvremove", "vgremove",
        ):
            self.assertNotIn(forbidden, probe)

    def test_foreground_fairness_probe_is_identity_bound_and_non_lifecycle(self):
        probe = (
            ROOT
            / "experiments/thick-generations/foreground-fairness-probe.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("background hydration was not enabled before admission deadline", probe)
        self.assertIn("materialization worker disappeared before background enable", probe)
        self.assertIn("caller must prove hot-region hydration", probe)
        self.assertIn("HOT_REGION_PREPARATION=CALLER_PROVEN", probe)
        self.assertIn("exact materialization worker is absent", probe)
        self.assertIn("source dependency mismatch", probe)
        self.assertIn("must be distinct devices", probe)
        self.assertIn("CLOCK_MONOTONIC", probe)
        self.assertIn("FOREGROUND_IO_BYTES=4096", probe)
        self.assertIn("READ_P99_NANOSECONDS", probe)
        self.assertIn("WRITE_P99_NANOSECONDS", probe)
        self.assertIn("BACKGROUND_PROGRESS_REGIONS", probe)
        self.assertIn("MAX_BACKGROUND_INFLIGHT_REGIONS", probe)
        self.assertIn("no concurrent background hydration activity was proven", probe)
        self.assertIn("SLTG_FAIRNESS_START_WAIT_SEC", probe)
        self.assertIn("BACKGROUND_ADMISSION_WAIT_NANOSECONDS", probe)
        self.assertIn("never authorizes a retry", probe)
        self.assertIn("SAMPLE_%04d_READ_NS", probe)
        self.assertIn("DATA_COMPARE=PASS", probe)
        for forbidden in (
            "dmsetup message", "dmsetup suspend", "dmsetup resume",
            "dmsetup remove", "lvremove", "vgremove",
        ):
            self.assertNotIn(forbidden, probe)

    def test_thick_status_labels_four_kib_region_as_legacy_evidence(self):
        status = (
            ROOT / "docs/thick-generations-poc-status.md"
        ).read_text(encoding="utf-8")
        self.assertIn("LEGACY_FOREGROUND_REGION_SIZE_4_KIB=PASS", status)
        self.assertNotIn("\nFOREGROUND_REGION_SIZE_4_KIB=PASS", status)

    def test_known_issues_discloses_region_latency_tradeoff(self):
        known = (ROOT / "docs/known-issues.md").read_text(encoding="utf-8")
        self.assertIn("a small first write may wait for", known)
        self.assertIn("first-write and concurrent foreground p50/p95/p99/max", known)
        self.assertIn("compatibility state, not the new-transition default", known)

    def test_materialization_slot_admission_is_atomic_with_open_intent(self):
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = plugin.index("sub _thick_volume_snapshot {")
        end = plugin.index("sub _thick_free_image {", start)
        body = plugin[start:end]
        lock = body.index("$class->_with_vg_lock($storeid, $scfg, sub {")
        inventory = body.index("$class->_thick_list_volumes_scoped", lock)
        admission = body.index("$class->_thick_materialization_admission", inventory)
        capacity = body.index("$class->_thick_capacity_gate", admission)
        intent = body.index("$class->_set_vg_intent", capacity)
        self.assertLess(lock, inventory)
        self.assertLess(inventory, admission)
        self.assertLess(admission, capacity)
        self.assertLess(capacity, intent)
        self.assertNotIn("}, $device);", body[lock:admission])

    def test_one_tib_lun_preflight_is_read_only_and_identity_bound(self):
        gate = (
            ROOT
            / "experiments/thick-generations/one-tib-lun-initiator-preflight.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('"$device" == "/dev/mapper/$expected_wwid"', gate)
        self.assertIn('"${dm_uuid,,}" == "mpath-${expected_wwid,,}"', gate)
        self.assertIn("minimum_bytes=1099511627776", gate)
        self.assertIn('paths=("$sysfs"/slaves/*)', gate)
        self.assertIn('holders=("$sysfs"/holders/*)', gate)
        self.assertIn('wipefs -n "$device"', gate)
        self.assertIn("BACKING_ALLOCATION=UNPROVEN_REQUIRES_TARGET_EVIDENCE", gate)
        self.assertIn("MUTATIONS=NONE", gate)
        for forbidden in (
            "pvcreate", "vgcreate", "pvremove", "vgremove", "mkfs",
            "wipefs -a", "dmsetup create", "iscsiadm --mode node --login",
        ):
            self.assertNotIn(forbidden, gate)

    def test_materialized_migration_bridge_uses_supported_fail_closed_path(self):
        source = (ROOT / "usr/sbin/sharedlvmthin-migrate-bridge").read_text(
            encoding="utf-8"
        )
        self.assertIn("qm disk move", source)
        self.assertIn("qm migrate", source)
        self.assertIn("SAFE_FOR_MUTATION=YES", source)
        self.assertIn("insufficient physical VG capacity", source)
        self.assertIn("MATERIALIZED_THICK", source)
        self.assertIn("MIGRATED_THICK", source)
        self.assertIn("sharedlvmthin-bridge-admission", source)
        self.assertIn("sharedlvmthin-migrate-bridge inspect <vmid> [transaction]", source)
        self.assertIn("sharedlvmthin-migrate-bridge resume <vmid> [transaction]", source)
        self.assertIn("sharedlvmthin-migrate-bridge plan <vmid> [transaction]", source)
        self.assertIn("sharedlvmthin-migrate-bridge finalize-thick <vmid> [transaction]", source)
        self.assertIn("finalize-Thick requires a version-4 migrated target", source)
        self.assertIn("foreign bridge admission blocks explicit Thick settlement", source)
        self.assertIn("resume_publish_state COMPLETE_THICK", source)
        self.assertIn("RECOVERY_PLAN=FINALIZE_THIN", source)
        self.assertIn("target disk topology is not exactly finalizable Thin", source)
        self.assertIn("BRIDGE_ADMISSION_ACTIVE=NO", source)
        self.assertIn("BRIDGE=FINALIZATION_PENDING", source)
        self.assertIn("BRIDGE_RESUME=FINALIZATION_PENDING", source)
        self.assertIn("DATA_MOVES_COMPLETED=YES", source)
        self.assertIn("final_health_is_safe", source)
        self.assertIn("disk_manifest_sha256", source)
        self.assertIn("disk manifest digest mismatch", source)
        self.assertIn("CONTINUE_MATERIALIZE", source)
        self.assertIn("resume_progress_move", source)
        self.assertIn("MATERIALIZE_FAILED", source)
        self.assertIn("RETURNING_THIN", source)
        self.assertIn("RETURN_FAILED", source)
        self.assertNotIn("resume_publish_state MOVE_FAILED", source)
        self.assertIn("insufficient capacity to resume materialization", source)
        self.assertIn("slt-bridge-admission-timeout", source)
        self.assertIn("admission_poll_seconds", source)
        self.assertIn("admission_last_heartbeat", source)
        planner = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-plan"
        ).read_text(encoding="utf-8")
        self.assertIn("CONTINUE_MATERIALIZE", planner)
        self.assertIn("CONTINUE_RETURN_THIN", planner)
        self.assertIn('"MATERIALIZE_FAILED"', planner)
        self.assertIn('"RETURN_FAILED"', planner)
        self.assertNotIn('"MOVE_FAILED"', planner)
        self.assertIn("VG admission belongs to another transaction", planner)
        self.assertIn("progress_percent", source)
        self.assertIn("updated_at", source)
        self.assertIn("PROGRESS_AGE_SECONDS", source)
        self.assertIn("PROGRESS_UPDATE_FRESH", source)
        self.assertIn("bridge state updated_at is in the future", source)
        self.assertIn("run_progress_move", source)
        self.assertIn("PIPESTATUS[0]", source)
        self.assertIn("ambiguous bridge recovery", source)
        self.assertIn('${#resume_files[@]}" -eq 1', source)
        self.assertNotIn("-printf '%T@ %p", source)
        self.assertIn("BRIDGE_STATE=AMBIGUOUS", source)
        self.assertIn("bridge state transaction does not match", source)
        self.assertIn("now - last_persist", source)
        self.assertNotIn("bridge currently requires same-VG Thin/Thick aliases", source)
        self.assertIn("bridge_admission_order", source)
        self.assertIn("bridge_acquire_local_set", source)
        self.assertIn('bridge_thick_capacity "$thick_store" "$thick_vg"', source)
        self.assertIn("PVE_SLT_BRIDGE_TX", source)
        self.assertIn("version=4", source)
        self.assertIn("thin_vg_uuid", source)
        self.assertIn("thick_vg_uuid", source)
        self.assertIn("RETURN_ADMISSION_PENDING", source)
        self.assertIn("RETURN_HEALTH_PENDING", source)
        self.assertIn("DATA_MOVES_COMPLETED=NO", source)
        self.assertIn("bridge_thick_capacity", source)
        self.assertIn("PVE::SharedLvmThinSafety::evaluate_allocation_reserve", source)
        self.assertIn("slt-vg-reserve-percent", source)
        self.assertIn("PHYSICAL_GROWTH_BYTES", source)
        self.assertIn("admission outcome is UNKNOWN", source)
        self.assertIn("remote return admission outcome is UNKNOWN", source)
        self.assertIn('grep -qx BRIDGE_ADMISSION_ACQUIRED <<<"$output"', source)
        self.assertIn("    return 0\n}", source)
        self.assertIn('[ "$admission_rc" -eq 75 ]', source)
        self.assertIn("sharedlvmthin-qmp-path-check", source)
        self.assertIn("RUNTIME_CONFIG_DIVERGENCE", source)
        self.assertIn("ssh_stream_base", source)
        self.assertIn("ssh_base=(/usr/bin/ssh -n", source)
        run_tail = source[source.rindex("phase=MIGRATED_THICK\nwrite_state"):]
        self.assertLess(run_tail.index("ssh_base=(/usr/bin/ssh -n"),
                        run_tail.index('if [ "$return_thin" -eq 1 ]; then'))
        finalize = source[source.index('if [ "$bridge_action" = finalize-thick ]; then'):
                          source.index("    recovery_plan=", source.index(
                              'if [ "$bridge_action" = finalize-thick ]; then'))]
        pre_health = finalize.index(
            '"${ssh_base[@]}" -- env PVE_SLT_BRIDGE_TX="$tx"')
        release = finalize.index('release "${finalize_release[i]}"')
        post_health = finalize.rindex(
            '"${ssh_base[@]}" -- sharedlvmthin recovery-check')
        complete = finalize.index("resume_publish_state COMPLETE_THICK")
        self.assertLess(pre_health, release)
        self.assertLess(release, post_health)
        self.assertLess(post_health, complete)
        qmp_check = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check"
        ).read_text(encoding="utf-8")
        self.assertIn('qmp_command(stream, "query-block")', qmp_check)
        self.assertEqual(qmp_check.count('qmp_command(stream, "query-block-jobs")'), 2)
        self.assertIn("duplicate runtime block records", qmp_check)
        self.assertIn("runtime/config path divergence", qmp_check)
        for mutation in ("qm set", "qm disk", "lvchange", "lvremove", "dmsetup"):
            self.assertNotIn(mutation, qmp_check)
        self.assertNotIn("thin_repair", source)

    def test_remote_thin_evidence_helper_is_read_only_and_exact(self):
        source = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-remote-thin-evidence"
        ).read_text(encoding="utf-8")
        self.assertIn("dmsetup ls --target thin-pool", source)
        self.assertIn("dmsetup info", source)
        self.assertIn('-o uuid "$name"', source)
        self.assertIn('"$observed" = "$expected_uuid"', source)
        self.assertNotIn('-o uuid "$mapper"', source)
        for mutation in ("lvchange", "lvcreate", "lvremove", "dmsetup remove"):
            self.assertNotIn(mutation, source)

    def test_publishable_sources_contain_no_lab_or_personal_identifiers(self):
        forbidden = (
            "192.168." + "50.",
            "10." + "240.",
            "stan" + "islav",
            "ba" + "ran",
            "oke" + ".dev",
        )
        private_networks = tuple(
            ipaddress.ip_network(value)
            for value in (
                "10." + "0.0.0/8",
                "172." + "16.0.0/12",
                "192." + "168.0.0/16",
            )
        )
        ipv4 = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
        excluded_parts = {".git", "dist", "dist-lab", "outputs", "__pycache__"}
        for path in ROOT.rglob("*"):
            if not path.is_file() or excluded_parts.intersection(path.parts):
                continue
            try:
                content = path.read_text(encoding="utf-8").lower()
            except (UnicodeDecodeError, OSError):
                continue
            for marker in forbidden:
                self.assertNotIn(marker, content, f"private marker in {path}")
            for candidate in ipv4.findall(content):
                try:
                    address = ipaddress.ip_address(candidate)
                except ValueError:
                    continue
                self.assertFalse(
                    any(address in network for network in private_networks),
                    f"private IPv4 address {candidate} in {path}",
                )

    def test_rc5_version(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertRegex(
            control,
            r"(?m)^Version: 0\.9\.0~rc5(?:\.\d+)+(?:~tg\d+(?:\+fix\d+)?)?$",
        )

    def test_experimental_thick_build_has_distinct_package_version(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertRegex(control, r"(?m)^Version: .*~tg\d+(?:\+fix\d+)?$")

    def test_combined_package_metadata_advertises_both_modes(self):
        control = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        self.assertIn(
            "Description: Shared LVM Thin and Thick Generations storage for Proxmox VE",
            control,
        )
        self.assertIn("one LVM thin pool per VM", control)
        self.assertIn("fully allocated Thick Generations", control)

    def test_thick_only_package_is_a_restricted_shared_core_flavor(self):
        dual = (ROOT / "DEBIAN/control").read_text(encoding="utf-8")
        thick = (ROOT / "packaging/thick-only/control").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        release_gate = (ROOT / "scripts/check-release.sh").read_text(encoding="utf-8")
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        preinst = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        prerm = (ROOT / "DEBIAN/prerm").read_text(encoding="utf-8")
        postrm = (ROOT / "DEBIAN/postrm").read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        exclusion_manifest = (
            ROOT / "packaging/thick-only/excluded-paths.txt"
        ).read_text(encoding="utf-8")

        self.assertIn("Package: pve-sharedlvmthin\n", dual)
        self.assertIn("Conflicts: pve-sharedlvmthin-thick", dual)
        self.assertIn("Package: pve-sharedlvmthin-thick", thick)
        self.assertIn("Conflicts: pve-sharedlvmthin\n", thick)
        self.assertIn("Replaces: pve-sharedlvmthin\n", thick)
        expected_predepends = (
            "Pre-Depends: python3, systemd, lvm2, multipath-tools, "
            "libpve-cluster-api-perl,\n"
            " libpve-storage-perl"
        )
        self.assertIn(expected_predepends, dual)
        self.assertIn(expected_predepends, thick)
        self.assertNotIn("Pre-Depends: pve-manager", dual)
        self.assertIn("FLAVOR=${2:-${PACKAGE_FLAVOR:-dual}}", build)
        self.assertIn('printf \'%s\\n\' "$FLAVOR"', build)
        self.assertIn("sharedlvmthin-migrate-bridge", build)
        for excluded in (
            "sharedlvmthin-bridge-admission",
            "sharedlvmthin-bridge-plan",
            "sharedlvmthin-qmp-path-check",
            "SharedLvmThinGuardClient.pm",
            "SharedLvmThinGuardEngine.pm",
            "SharedLvmThinMobility.pm",
            "SharedLvmThinPeerAudit.pm",
            "SharedLvmThinRelay.pm",
            "SharedLvmThinWatchdog.pm",
            "sharedlvmthin-package-maintenance-check",
        ):
            self.assertIn(excluded, exclusion_manifest)
        self.assertIn("excluded-paths.txt", build)
        self.assertIn("excluded-paths.txt", release_gate)
        self.assertIn("sub _package_flavor", plugin)
        self.assertIn("'slt-vg-layout'", plugin)
        self.assertIn("enum => ['isolated', 'mixed']", plugin)
        self.assertIn("mixed Thin/Thick VG layout is unsupported", plugin)
        self.assertIn("_vg_layout rejects it for every operation", plugin)
        self.assertIn("VG_MODE_CONFLICT", plugin)
        self.assertNotIn("use PVE::SharedLvmThinGuardClient;", plugin)
        self.assertIn("require PVE::SharedLvmThinGuardClient;", plugin)
        self.assertNotIn("use PVE::SharedLvmThinPeerAudit", plugin)
        self.assertIn("require PVE::SharedLvmThinPeerAudit;", plugin)
        self.assertIn("Thin allocation mode is unavailable", plugin)
        self.assertIn('if [ "$PACKAGE_FLAVOR" = dual ]; then', cli)
        usage_block = cli.index('echo "Usage:"')
        thin_help = cli.index('echo "  sharedlvmthin thin-recover-orphan', usage_block)
        help_guard = cli.rfind('if [ "$PACKAGE_FLAVOR" = dual ]; then', 0, thin_help)
        help_guard_end = cli.index("\n        fi", thin_help)
        self.assertGreaterEqual(help_guard, 0)
        self.assertGreater(help_guard_end, thin_help)
        self.assertIn("managed Thin", preinst)
        self.assertIn("$1 ~ /pve-slt-sid-/", preinst)
        self.assertIn("allocation modes cannot be proven safe", preinst)
        self.assertIn("LVM inventory failed", preinst)
        self.assertIn('if ! MANAGED_LVS=$(lvs --readonly', preinst)
        self.assertIn('if ! VOLUME_GROUPS=$(vgs --readonly', preinst)
        self.assertIn('substr($2, 4, 1) == "p"', preinst)
        self.assertIn("pve-sharedlvmthin-tg-*", preinst)
        self.assertIn("No package files were replaced", preinst)
        self.assertIn("ordinary upgrades as well as dual <-> Thick-only", preinst)
        self.assertIn("audit_candidate_storages", preinst)
        self.assertIn("candidate recovery checker is missing", preinst)
        self.assertIn("sharedlvmthin-candidate-recovery-check", preinst)
        self.assertIn("sharedlvmthin-package-maintenance-check", preinst)
        self.assertIn("--record-phase PREINST_ACCEPTED", preinst)
        self.assertIn('"${1:-}" = "upgrade"', preinst)
        self.assertIn("grep -q '^sharedlvmthin:[[:space:]]'", preinst)
        self.assertIn('candidate_args="--preinstall"', preinst)
        self.assertIn("if ($2 in seen) exit 3", preinst)
        self.assertIn("candidate storage configuration is ambiguous", preinst)
        self.assertIn("not positively recovery-safe under candidate rules", preinst)
        self.assertIn("candidate recovery fence refused", preinst)
        self.assertIn("TG48 requires", preinst)
        self.assertIn("separate Thin and Thick VGs", preinst)
        self.assertIn("A maintenance\n# receipt cannot bypass", preinst)
        self.assertIn("THICK_ANCHORS_HEALTHY=PASS", preinst)
        self.assertIn("VG_INTENT_CLEAR=PASS", preinst)
        self.assertIn("required legacy Thin inventory command is unavailable", preinst)
        self.assertIn("active device-mapper inventory failed or timed out", preinst)
        self.assertIn("legacy Thin LV inventory failed or timed out", preinst)
        self.assertIn("timeout --foreground --kill-after=10 120", preinst)
        self.assertNotIn(
            "lvs --readonly --noheadings --separator '|' -o vg_name,lv_name,lv_tags,segtype 2>/dev/null || true",
            preinst,
        )
        self.assertNotIn(
            "dmsetup info -c --noheadings -o name 2>/dev/null | sed",
            preinst,
        )
        self.assertLess(
            preinst.index("if ! audit_candidate_storages"),
            preinst.index('if [ "$PACKAGE_FLAVOR" = "thick-only" ]'),
        )
        self.assertLess(
            preinst.index('if [ "$IS_UPGRADE" -eq 1 ]'),
            preinst.index('if [ "$PACKAGE_FLAVOR" = "thick-only" ]'),
        )
        self.assertIn("Removed obsolete SharedLvmThin-managed Thin autogrow policy", postinst)
        self.assertIn("Administrator-owned LVM settings were not modified", postinst)
        self.assertIn("lvmlocal.conf.before-thick-only", postinst)
        self.assertIn("malformed SharedLvmThin-managed LVM policy markers", postinst)
        self.assertIn("stale Thin component remains", postinst)
        self.assertIn("ThinGuard runtime state is unavailable", postinst)
        self.assertIn("ThinGuard remains '$THIN_GUARD_STATE'", postinst)
        self.assertIn('systemctl is-enabled --quiet "$THIN_GUARD_SERVICE"', postinst)
        self.assertIn("ThinGuard remains enabled", postinst)
        stale_gate = postinst.index("stale Thin component remains")
        daemon_reload = postinst.index("systemctl daemon-reload")
        self.assertGreater(stale_gate, daemon_reload)
        common_chmod = postinst.index('chmod 0755 "$MATERIALIZER"')
        self.assertIn("sharedlvmthin-migration-preflight", postinst)
        dual_chmod = postinst.index('chmod 0755 "$MONITOR"')
        qmp_chmod = postinst.index("sharedlvmthin-qmp-path-check")
        self.assertGreater(qmp_chmod, dual_chmod)
        self.assertGreater(dual_chmod, common_chmod)
        self.assertIn("if (open || seen) exit 2", postinst)
        thick_branch = postinst.index('if [ "$PACKAGE_FLAVOR" = "thick-only" ]')
        managed_cleanup = postinst.index('sed -i "/^$LVM_BEGIN$/,/^$LVM_END$/d"')
        dual_policy = postinst.index("Installed fail-closed SharedLvmThin dmeventd autogrow policy")
        self.assertLess(thick_branch, managed_cleanup)
        self.assertLess(managed_cleanup, dual_policy)
        syntax_guard = postinst.index('if [ "$PACKAGE_FLAVOR" = "dual" ]; then')
        monitor_syntax = postinst.index('perl -c "$MONITOR"')
        guard_syntax = postinst.index('perl -c "$THIN_GUARDD"')
        self.assertLess(syntax_guard, monitor_syntax)
        self.assertLess(syntax_guard, guard_syntax)
        self.assertIn('systemctl stop "$THIN_GUARD_SERVICE"', prerm)
        self.assertIn('systemctl disable "$THIN_GUARD_SERVICE"', prerm)
        self.assertIn("managed Thin objects still exist", prerm)
        self.assertIn("managed Thick objects still exist", prerm)
        self.assertIn("a Thick VG mutation intent still exists", prerm)
        self.assertIn("SharedLvmThin storage configuration still exists", prerm)
        self.assertIn("maintscript arguments therefore cannot", prerm)
        self.assertIn("sharedlvmthin-profile-replacement", prerm)
        self.assertIn("exact profile replacement authorization was refused", prerm)
        self.assertIn("partial or ambiguous VG", prerm)
        self.assertIn("ThinGuard stop is unconfirmed", prerm)
        self.assertLess(
            prerm.index("pve-slt-sid-"),
            prerm.index('systemctl stop "$THIN_GUARD_SERVICE"'),
        )
        self.assertLess(
            prerm.index("managed Thin objects still exist"),
            prerm.index('systemctl stop "$SERVICE"'),
        )
        self.assertIn("PURGED_FLAVOR=dual", postrm)
        self.assertIn("OTHER_PACKAGE=pve-sharedlvmthin-thick", postrm)
        self.assertIn("PACKAGE_STATES=$(dpkg-query", postrm)
        self.assertIn("OTHER_STATUS=$(printf", postrm)
        self.assertIn("payload-bearing $OTHER_PACKAGE conflicts with package-flavor marker", postrm)
        self.assertIn("FLAVOR_FILE=/usr/share/pve-sharedlvmthin/package-flavor", postrm)
        self.assertIn('ACTIVE_FLAVOR=$(sed -n', postrm)
        self.assertIn('if [ "$ACTIVE_FLAVOR" != "$PURGED_FLAVOR" ]', postrm)
        preserve = postrm.index("preserving state owned by active")
        cleanup = postrm.index("rm -rf /etc/pve-sharedlvmthin")
        self.assertLess(preserve, cleanup)
        self.assertIn("require_dual_mode", cli)
        self.assertIn("pve-sharedlvmthin-thick", cli)
        self.assertIn(
            "Thin dmeventd/autogrow policy is not applicable to Thick-only flavor",
            cli,
        )
        self.assertIn("DEFAULT_ALLOCATION_MODE=thick-generations", cli)
        self.assertIn("thick-only) CONTROL=", build)
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "Thin recovery operations are unavailable in the Thick-only package",
            worker,
        )

        health = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json"
        ).read_text(encoding="utf-8")
        self.assertIn('return "pve-sharedlvmthin-thick", flavor', health)
        self.assertIn('"plugin_package": plugin_package', health)
        self.assertNotIn('package_version("pve-sharedlvmthin")', health)
        self.assertIn('check("plugin_package_identity"', health)

        release_check = (ROOT / "scripts/check-release.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('dpkg-deb --control "$PACKAGE" "$TMP/control"', release_check)
        self.assertIn('sh -n "$TMP/control/$maintscript"', release_check)
        self.assertIn("candidate control-archive recovery checker is missing", release_check)
        self.assertIn('cmp -s "$CANDIDATE_RECOVERY" "$PAYLOAD_RECOVERY"', release_check)
        self.assertIn('python3 -m py_compile "$CANDIDATE_RECOVERY"', release_check)
        self.assertIn("package that builds but cannot configure", release_check)
        self.assertIn('DOC_DIR="$STAGE/usr/share/doc/$PACKAGE_NAME"', build)
        self.assertIn('DOC_DIR="$TMP/root/usr/share/doc/$PACKAGE_NAME"', release_check)
        self.assertIn("dual-package documentation namespace leaked", release_check)
        self.assertIn("required shared Thick component is missing", release_check)
        self.assertIn("required program is not executable", release_check)
        self.assertIn("checksum manifest does not cover the exact data payload", release_check)
        self.assertIn("symbolic links are not permitted", release_check)
        self.assertIn("development-only layout migration content leaked", release_check)
        self.assertIn("layout-migration-", release_check)
        self.assertIn('md5sum --strict -c "$TMP/control/md5sums"', release_check)
        self.assertIn("usr/share/perl5/PVE/SharedLvmThinThick.pm", release_check)
        self.assertIn("sharedlvmthin-thick-materialize", release_check)

        parity = (ROOT / "scripts/compare-package-profiles.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("restricted build profile, not a fork", parity)
        self.assertIn("cmp -s", parity)
        self.assertIn("stat -c '%a'", parity)
        self.assertIn("dual/Thick-only shared payload parity: PASS", parity)
        self.assertIn("unexpected Dual-only payload file", parity)
        self.assertIn("package-artifact-sha256", parity)
        self.assertIn("artifact identities are invalid or not profile-specific", parity)
        self.assertIn("excluded-paths.txt", parity)

        exclusions = (
            ROOT / "packaging/thick-only/excluded-paths.txt"
        ).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(exclusions), len(set(exclusions)))
        self.assertIn("usr/sbin/sharedlvmthin-migrate-bridge", exclusions)
        self.assertIn("usr/share/perl5/PVE/SharedLvmThinGuard.pm", exclusions)
        self.assertIn("usr/share/perl5/PVE/SharedLvmThinGuardClient.pm", exclusions)
        self.assertIn("usr/share/perl5/PVE/SharedLvmThinPeerAudit.pm", exclusions)
        self.assertIn("excluded-paths.txt", build)
        self.assertIn("stale Thick-only exclusion", build)
        self.assertIn("excluded-paths.txt", release_check)

        reproducible = (
            ROOT / "scripts/check-reproducible-packages.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("build_pair dual", reproducible)
        self.assertIn("build_pair thick-only", reproducible)
        self.assertIn('cmp -s "$first_deb" "$second_deb"', reproducible)
        self.assertIn("reproducible package builds: PASS", reproducible)

        monitor_test = (
            ROOT / "tests/unit/monitor_adversarial.t"
        ).read_text(encoding="utf-8")
        self.assertIn("require PVE::Tools", monitor_test)
        self.assertIn("requires the Proxmox PVE::Tools runtime", monitor_test)

        profile_gate = (
            ROOT / "experiments/thick-generations/package-profile-gate.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("RESULT=DRY_RUN_PASS", profile_gate)
        self.assertIn("RESULT=EXECUTE_PASS", profile_gate)
        self.assertIn('dpkg --force-hold --no-act -i "$candidate_deb"', profile_gate)
        self.assertIn('dpkg --no-act -i "$candidate_deb"', profile_gate)
        self.assertIn('"$replacement_helper" execute', profile_gate)
        self.assertIn('dpkg -i "$candidate_deb"', profile_gate)
        self.assertIn("--settle-recovery", profile_gate)
        self.assertIn("dpkg --verify-format=rpm --verify", profile_gate)
        self.assertIn("installed package files differ", profile_gate)
        self.assertIn("dpkg database reports unfinished", profile_gate)
        self.assertIn("profile replacement requires identical package versions", profile_gate)
        self.assertNotIn("upgrade-check --fail-fast", profile_gate)
        self.assertIn('dpkg-deb --control "$candidate_deb" "$candidate_control"', profile_gate)
        self.assertIn('"$candidate_control/preinst"', profile_gate)
        self.assertIn('preflight "${preinst_action[@]}"', profile_gate)
        self.assertIn('DPKG_MAINTSCRIPT_PACKAGE="$target_name"', profile_gate)
        self.assertIn("trap 'exit 130' HUP INT TERM", profile_gate)
        self.assertNotIn('dpkg -i "$package"', profile_gate)
        self.assertGreaterEqual(profile_gate.count("sharedlvmthin upgrade-check"), 1)
        self.assertIn("sharedlvmthin update-policy qualify-runtime", profile_gate)
        self.assertIn("sharedlvmthin update-policy finalize-runtime", profile_gate)
        self.assertNotIn("sleep 5", profile_gate)
        self.assertIn("--expect-current", profile_gate)
        self.assertIn("--select-update-policy", profile_gate)
        self.assertIn("An already active FREEZE policy is part of the package transaction", profile_gate)
        self.assertIn("select_update_policy=freeze", profile_gate)
        self.assertIn("active FREEZE must be changed in a separate settled policy operation", profile_gate)
        self.assertIn("settle-direct-package", profile_gate)
        self.assertIn('--bootstrap-policy "${select_update_policy:-none}"', profile_gate)
        self.assertNotIn('sharedlvmthin update-policy select "$select_update_policy"',
                         profile_gate)
        self.assertIn("current package profile mismatch", profile_gate)
        self.assertLess(
            profile_gate.index("installed_name="),
            profile_gate.index('case "$installed_name" in'),
        )
        self.assertLess(
            profile_gate.index('dpkg-query -W -f=\'${db:Status-Abbrev}\''),
            profile_gate.index('case "$installed_name" in'),
        )
        self.assertIn("opposite package profile remains installed", profile_gate)
        self.assertIn("installed package flavor marker does not match package identity", profile_gate)
        self.assertIn("installed CLI is missing thick-recover-prepare", profile_gate)
        self.assertIn("help_rc=$?", profile_gate)
        self.assertIn("[[ $help_rc -eq 64 ]]", profile_gate)
        self.assertIn("installed CLI help returned unexpected status", profile_gate)
        self.assertIn('doctor_output="$temp/doctor.out"', profile_gate)
        self.assertIn("[[ $doctor_rc -ne 0 && $doctor_rc -ne 1 ]]", profile_gate)
        self.assertIn("installed Doctor did not prove a zero-failure result", profile_gate)
        self.assertIn("installed Dual CLI is missing thin-adopt-owner-model", profile_gate)
        self.assertIn("installed Dual profile is missing required payload", profile_gate)
        self.assertIn("installed Thick-only CLI unexpectedly exposes Thin commands", profile_gate)
        self.assertIn("installed Thick-only profile retains excluded payload", profile_gate)
        self.assertIn("cannot prove ThinGuard runtime state", profile_gate)
        self.assertIn("retains active ThinGuard state", profile_gate)
        self.assertIn("retains enabled ThinGuard state", profile_gate)
        for excluded_runtime_path in (f"/{relative}" for relative in exclusions):
            self.assertIn(excluded_runtime_path, profile_gate)
            self.assertIn(excluded_runtime_path, postinst)
        self.assertIn(
            '[ -e "$FORBIDDEN_THIN_PATH" ] || [ -L "$FORBIDDEN_THIN_PATH" ]',
            postinst,
        )
        self.assertNotIn(r"\${", profile_gate)
        self.assertIn("Reboot was not performed", profile_gate)
        self.assertNotIn("apt-get", profile_gate)
        self.assertNotIn("reboot -", profile_gate)

        plugin_source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        activate_storage = plugin_source.split("sub activate_storage {", 1)[1].split(
            "\nsub deactivate_storage {", 1
        )[0]
        self.assertIn("_assert_loaded_runtime_identity", activate_storage)
        self.assertNotIn("_assert_package_operations_released", activate_storage)
        for effect_token in ("run_command(", "_with_vg_lock", "lvchange", "vgchange",
                             "dmsetup", "systemd-run"):
            self.assertNotIn(effect_token, activate_storage)

        post_reboot_gate = (
            ROOT / "experiments/thick-generations/package-post-reboot-gate.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("post-reboot Doctor did not prove a zero-failure result", post_reboot_gate)
        self.assertIn("[[ $doctor_rc -ne 0 && $doctor_rc -ne 1 ]]", post_reboot_gate)
        self.assertIn("post-reboot CLI help returned unexpected status", post_reboot_gate)
        self.assertIn("[[ $help_rc -eq 64 ]]", post_reboot_gate)
        self.assertIn('doc.get("result") not in ("PASS", "WARN")', post_reboot_gate)
        self.assertIn("complete zero-failure check set", post_reboot_gate)
        self.assertIn("or not checks or any(", post_reboot_gate)
        self.assertIn('item.get("status") not in ("PASS", "WARN")', post_reboot_gate)
        for excluded_runtime_path in (f"/{relative}" for relative in exclusions):
            self.assertIn(excluded_runtime_path, post_reboot_gate)
        self.assertIn("/proc/sys/kernel/random/boot_id", post_reboot_gate)
        self.assertIn("reboot is unproven", post_reboot_gate)
        self.assertIn("installed_count", post_reboot_gate)
        self.assertIn("ii*|hi*)", post_reboot_gate)
        self.assertIn("dpkg --audit", post_reboot_gate)
        self.assertIn("dpkg --verify-format=rpm --verify", post_reboot_gate)
        self.assertIn("package files differ from their dpkg manifest", post_reboot_gate)
        self.assertIn("package-flavor", post_reboot_gate)
        self.assertIn("pve-sharedlvmthin-tg-*", post_reboot_gate)
        self.assertIn("installed Dual profile is missing required payload", post_reboot_gate)
        self.assertIn("sharedlvmthin-bridge-admission", post_reboot_gate)
        self.assertIn("sharedlvmthin-qmp-path-check", post_reboot_gate)
        self.assertIn("sharedlvmthin compat-check", post_reboot_gate)
        self.assertIn("sharedlvmthin doctor --quick", post_reboot_gate)
        self.assertIn("sharedlvmthin upgrade-check", post_reboot_gate)
        self.assertNotIn("upgrade-check --fail-fast", post_reboot_gate)
        self.assertIn("sharedlvmthin-health-json", post_reboot_gate)
        self.assertIn('doc.get("result") not in ("PASS", "WARN")', post_reboot_gate)
        self.assertIn("Thick-only health JSON contains non-Thick storage", post_reboot_gate)
        self.assertIn('item["node_applicable"]', post_reboot_gate)
        self.assertIn("unknown node scope", post_reboot_gate)
        self.assertIn("cannot prove ThinGuard runtime state", post_reboot_gate)
        self.assertIn("found active ThinGuard state", post_reboot_gate)
        self.assertIn("found enabled ThinGuard state", post_reboot_gate)
        self.assertIn('echo "HEALTH_JSON=PASS"', post_reboot_gate)
        self.assertIn("RESULT=POST_REBOOT_PASS", post_reboot_gate)
        self.assertNotIn("systemctl restart", post_reboot_gate)
        self.assertNotIn("systemctl reboot", post_reboot_gate)
        self.assertNotIn("dpkg -i", post_reboot_gate)

        browser_gate = (
            ROOT / "experiments/thick-generations/web-dual-mode-live-check.js"
        ).read_text(encoding="utf-8")
        self.assertIn("SLT_EXPECT_PROFILE", browser_gate)
        self.assertIn("package profile mismatch", browser_gate)
        self.assertIn("Thick-only health data contains a non-Thick storage", browser_gate)
        self.assertIn("WEB_PACKAGE_LIVE_BROWSER=PASS", browser_gate)

        release_gate = (ROOT / "docs/thick-generations-release-gate.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("| Thick-only clean install |", release_gate)

        self.assertIn("| Package profile replacement |", release_gate)
        self.assertIn("| Package rolling update |", release_gate)
        self.assertIn("| Package removal fence |", release_gate)
        qualification_plan = (
            ROOT / "docs/original-cluster-qualification-plan.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Exercise package removal separately", qualification_plan)
        self.assertIn("Prove an unrelated", qualification_plan)
        self.assertIn(
            "`remove in-favour` package name also refuses", qualification_plan
        )
        self.assertIn("anchor, generation, transition object and VG intent", qualification_plan)
        self.assertIn("Published TG32 versus audit candidate", release_gate)
        self.assertIn("not rebuilt or silently replaced", release_gate)
        self.assertIn("| PREPARE cleanup recovery |", release_gate)
        self.assertIn("| Materialization admission |", release_gate)
        self.assertIn("| Worker scheduling ambiguity |", release_gate)
        self.assertIn("| Device-scoped capacity |", release_gate)
        self.assertIn("| Frontend-removal postcondition |", release_gate)
        self.assertIn("| Kernel dm-clone gate |", release_gate)
        self.assertIn("| dm-clone hydration I/O faults |", release_gate)
        known_issues = (ROOT / "docs/known-issues.md").read_text(encoding="utf-8")
        self.assertIn("upstream dm-clone known issues", known_issues)
        self.assertIn("retried indefinitely by the kernel", known_issues)
        self.assertIn("LOCAL PARTIAL: 191 Windows-compatible tests pass", release_gate)
        self.assertIn("Do not substitute an older green PR check", release_gate)

    def test_runtime_flavor_marker_is_fail_closed(self):
        source = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "missing-package-flavor"
            candidate = Path(directory) / "sharedlvmthin"
            candidate.write_text(
                source.replace(
                    "/usr/share/pve-sharedlvmthin/package-flavor", str(marker)
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                ["/bin/bash", str(candidate), "help"], text=True,
                capture_output=True, check=False,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("package flavor marker is missing or unsafe", result.stderr)

    def test_postrm_refuses_malformed_managed_lvm_fragment(self):
        original = (
            "devices { scan = [ \\\"/dev\\\" ] }\n"
            "# BEGIN PVE-SHAREDLVMTHIN MANAGED AUTOGROW\n"
            "activation { thin_pool_autoextend_threshold = 50 }\n"
            "administrator_setting = 1\n"
        )
        result, lvm, config, runtime = self._run_postrm_purge(lvm_text=original)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("malformed SharedLvmThin-managed", result.stderr)
        self.assertEqual(lvm.read_text(encoding="utf-8"), original)
        self.assertTrue(config.exists())
        self.assertTrue(runtime.exists())

    def test_postrm_preserves_state_for_unpacked_opposite_profile(self):
        result, _lvm, config, runtime = self._run_postrm_purge(
            other_status="unpacked", marker=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("preserving shared state", result.stderr)
        self.assertTrue(config.exists())
        self.assertTrue(runtime.exists())

    def test_postrm_preserves_state_when_package_database_is_unreadable(self):
        result, _lvm, config, runtime = self._run_postrm_purge(
            query_rc=2, marker=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("package database state is unavailable", result.stderr)
        self.assertTrue(config.exists())
        self.assertTrue(runtime.exists())

    def test_postrm_preserves_state_for_ambiguous_multiline_flavor(self):
        result, _lvm, config, runtime = self._run_postrm_purge(
            marker="dual\nthick-only\n",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("invalid active SharedLvmThin package flavor", result.stderr)
        self.assertTrue(config.exists())
        self.assertTrue(runtime.exists())

    def test_postrm_exact_fragment_removal_preserves_admin_lines(self):
        original = (
            "admin_before = 1\n"
            "# BEGIN PVE-SHAREDLVMTHIN MANAGED AUTOGROW\n"
            "activation { thin_pool_autoextend_threshold = 50 }\n"
            "# END PVE-SHAREDLVMTHIN MANAGED AUTOGROW\n"
            "admin_after = 1\n"
        )
        result, lvm, _config, runtime = self._run_postrm_purge(lvm_text=original)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            lvm.read_text(encoding="utf-8"),
            "admin_before = 1\nadmin_after = 1\n",
        )
        self.assertTrue(runtime.exists())
        self.assertIn("Preserving /var/lib/pve-sharedlvmthin", result.stdout)

    def test_postrm_never_deletes_shared_runtime_or_maintenance_lock(self):
        postrm = (ROOT / "DEBIAN/postrm").read_text(encoding="utf-8")
        self.assertNotIn("rm -rf /var/lib/pve-sharedlvmthin", postrm)
        self.assertNotIn("rm -f /var/lib/pve-sharedlvmthin", postrm)
        self.assertIn("maintenance/.maintenance.lock", postrm)

    def test_maintainer_scripts_use_only_exact_tristate_hold_probe(self):
        preinst = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        for source in (preinst, postinst):
            self.assertNotIn('[ -e "$MAINTENANCE_ACTIVE" ]', source)
            self.assertNotIn('[ -L "$MAINTENANCE_ACTIVE" ]', source)
            self.assertIn('"$MAINTENANCE_CHECK" --probe-hold', source)
            self.assertIn('[ "$maintenance_probe_rc" -eq 3 ]', source)
            self.assertIn("hold state is unsafe or unknown", source)
        self.assertIn(
            '$POSTINST_DIR/$MAINTENANCE_CONTROL_PACKAGE.'
            'sharedlvmthin-package-maintenance-check', postinst)
        self.assertIn('MAINTENANCE_CONTROL_PACKAGE=pve-sharedlvmthin\n', postinst)
        self.assertIn(
            'MAINTENANCE_CONTROL_PACKAGE=pve-sharedlvmthin-thick', postinst)
        copy = ('cp "$ROOT/usr/libexec/pve-sharedlvmthin/'
                'sharedlvmthin-package-maintenance-check"')
        self.assertIn(copy, build)
        self.assertLess(build.index(copy), build.index('if [ "$FLAVOR" = "thick-only" ]'))

    def test_public_support_claims_separate_thin_and_materialized_thick_mobility(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        migration = (ROOT / "docs/migration.md").read_text(encoding="utf-8")
        plan = (
            ROOT / "docs/original-cluster-qualification-plan.md"
        ).read_text(encoding="utf-8")
        gate = (
            ROOT / "docs/thick-generations-release-gate.md"
        ).read_text(encoding="utf-8")
        scale = (ROOT / "docs/scale-qualification.md").read_text(encoding="utf-8")
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")

        self.assertIn("Direct in-place shared dm-thin live migration", migration)
        self.assertIn("Thin -> Thick -> normal PVE live migration -> Thin", migration)
        self.assertIn("A running VM whose disks\nstart in Thin can migrate online", readme)
        self.assertIn("Only direct in-place shared dm-thin migration", readme)
        self.assertIn("LIVE REFUSED BY DESIGN", gate)
        self.assertIn("TG26 correction", scale)
        self.assertNotIn("- [x] Offline and online migration between nodes.", plan)
        self.assertIn("direct in-place Thin live migration is unsupported", plugin)
        self.assertIn("Materialized Migration Bridge for online VM migration", plugin)

    def test_iscsi_acl_revocation_is_not_documented_as_fencing(self):
        migration = (ROOT / "docs/migration.md").read_text(encoding="utf-8")
        requirements = (ROOT / "docs/storage-requirements.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Revoking an iSCSI initiator ACL is not accepted", migration)
        self.assertRegex(migration, r"established\s+session continues to issue writes")
        self.assertIn("never treats an iSCSI ACL revoke as confirmed fencing", requirements)

    def test_doctor_accepts_elastic_and_legacy_thresholds(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text()
        self.assertIn('pass "Thin autoextend threshold = 50% (elastic early-grow policy)"', doctor)
        self.assertIn('pass "Thin autoextend threshold = 80% (legacy fixed/proportional policy)"', doctor)

    def test_doctor_reports_two_node_and_forced_quorum_topologies(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("Two-node cluster is quorate (2/2) without qdevice", doctor)
        self.assertIn("forced single-node quorum without qdevice", doctor)
        self.assertIn("EXPECTED_VOTES", doctor)
        self.assertNotIn("pvecm expected", doctor)

    def test_doctor_requires_dmeventd_only_for_locally_active_pool(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text()
        self.assertIn("-o lv_name,segtype,lv_attr", doctor)
        self.assertIn('$3 ~ /^....a/', doctor)
        self.assertIn("no local per-VM thin pool currently requires monitoring", doctor)

    def test_compat_requires_dmeventd_only_for_exact_managed_runtime(self):
        compat = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check"
        ).read_text(encoding="utf-8")
        self.assertIn("managed_thin_runtime_present", compat)
        self.assertIn("-o vg_name,lv_name,segtype,lv_attr,lv_tags", compat)
        self.assertIn("dmsetup info -c --noheadings -o name", compat)
        self.assertIn("${pool//-/--}-tpool", compat)
        self.assertIn(
            "PASS=service:dm-event.service:not-required-no-local-managed-thin-mapper",
            compat,
        )
        self.assertIn(
            "FAIL=service:dm-event.service:inactive-with-local-managed-thin-mapper",
            compat,
        )
        self.assertIn(
            "FAIL=service:dm-event.service:runtime-evidence-unknown",
            compat,
        )

    def test_doctor_skips_disabled_storage_in_all_operational_gates(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("storage_is_disabled", doctor)
        self.assertIn("storage is explicitly disabled; operational probes skipped", doctor)
        self.assertIn("PVE storage intentionally disabled", doctor)
        self.assertGreaterEqual(doctor.count('storage_is_disabled "$SID"'), 2)

    def test_doctor_skips_storage_outside_local_node_scope(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("storage_is_applicable_on_node", doctor)
        self.assertIn("storage is not assigned to this PVE node", doctor)
        self.assertGreaterEqual(
            doctor.count('storage_is_applicable_on_node "$SID"'), 2
        )

    def test_doctor_never_hides_inactive_pool_reservation(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("physical reservation=${POOL_GIB} GiB", doctor)
        self.assertIn("this reservation is unavailable to other VM pools", doctor)
        self.assertIn(
            "payload and reserved slack are unavailable while the pool is inactive",
            doctor,
        )

    def test_doctor_explains_same_vg_capacity_aliasing(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("_verify_same_vg_alias_configuration", doctor)
        self.assertIn("unsafe same-VG storage alias topology", doctor)
        self.assertIn("both aliases truthfully report the same physical VG capacity", doctor)
        self.assertIn("do not sum their PVE capacity values", doctor)

    def test_package_does_not_contain_private_keys(self):
        forbidden = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts:
                continue
            if path.name.endswith(".key") or path.name in {"id_rsa", "id_ed25519"}:
                forbidden.append(path)
        self.assertEqual(forbidden, [])

    def test_build_writes_portable_checksum_manifest(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        self.assertIn('(cd "$OUT" && sha256sum "$PACKAGE" >SHA256SUMS)', build)
        self.assertIn(">DEBIAN/md5sums", build)
        self.assertIn('chmod 0644 "$STAGE/DEBIAN/md5sums"', build)
        self.assertNotIn('sha256sum "$OUT/$PACKAGE" >"$OUT/SHA256SUMS"', build)

    def test_build_pins_cross_distribution_deb_compression(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        self.assertIn('dpkg-deb -Zxz --root-owner-group --build', build)

    def test_build_marks_every_installed_program_executable(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        programs = []
        for directory in (
            ROOT / "usr/sbin",
            ROOT / "usr/libexec/pve-sharedlvmthin",
            ROOT / "usr/share/initramfs-tools/hooks",
        ):
            for path in directory.iterdir():
                if path.is_file() and path.read_bytes().startswith(b"#!"):
                    programs.append(path.relative_to(ROOT).as_posix())
        missing = [path for path in programs if f'$STAGE/{path}' not in build]
        self.assertEqual(missing, [])

    def test_product_facing_sources_do_not_contain_slovak_or_czech_markers(self):
        product = [
            ROOT / "usr/share/pve-sharedlvmthin/web/index.html",
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-web",
            ROOT / "usr/sbin/sharedlvmthin",
            ROOT / "usr/sbin/sharedlvmthin-web-configure",
        ]
        marker = re.compile(r"\b(áno|chyba|úložisko|prihlás|zdravie|súbor)\b", re.I)
        hits = []
        for path in product:
            if marker.search(path.read_text(encoding="utf-8")):
                hits.append(path)
        self.assertEqual(hits, [])

    def test_installed_executable_sources_use_unix_line_endings(self):
        paths = list((ROOT / "usr/sbin").glob("*"))
        paths += list((ROOT / "usr/libexec/pve-sharedlvmthin").glob("*"))
        paths += list((ROOT / "DEBIAN").glob("*"))
        paths = [path for path in paths if path.is_file()]
        offenders = [str(path.relative_to(ROOT)) for path in paths if b"\r\n" in path.read_bytes()]
        self.assertEqual(offenders, [])

    def test_postinst_does_not_claim_operational_after_failed_health_check(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        self.assertNotIn("fully installed and operational", postinst)
        self.assertIn(
            "Operational status: NOT READY (installation preflight or PVE service refresh failed)",
            postinst,
        )
        self.assertIn("must NOT be treated as operational", postinst)
        self.assertIn('DOCTOR_RC', postinst)
        self.assertIn('installation preflight passed with diagnostic warnings', postinst)
        self.assertIn('sharedlvmthin doctor --quick', postinst)
        self.assertIn('The full Doctor was not run automatically', postinst)
        self.assertNotIn('Operational status: HEALTH CHECK PASSED', postinst)

    def test_postinst_refreshes_only_active_pve_storage_consumers(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")

        for service in (
            "pvedaemon.service",
            "pvestatd.service",
            "pveproxy.service",
            "pve-ha-lrm.service",
        ):
            self.assertIn(service, postinst)
        self.assertIn('systemctl is-active --quiet "$PVE_SERVICE"', postinst)
        self.assertIn('systemctl try-restart "$PVE_SERVICE"', postinst)
        self.assertNotIn('systemctl restart "$PVE_SERVICE"', postinst)
        self.assertNotIn("pve-ha-crm.service", postinst)
        self.assertNotIn("corosync.service", postinst)
        self.assertIn("PVE_REFRESH_OK=0", postinst)
        self.assertIn("--record-phase PACKAGE_CONFIGURED_DEFERRED", postinst)
        self.assertIn("OPERATIONAL_READY=NO", postinst)
        self.assertIn("PACKAGE_MAINTENANCE_HOLD=ACTIVE", postinst)
        self.assertIn("installation preflight: DEFERRED", postinst)
        self.assertIn("This is not a PASS", postinst)
        self.assertIn("lvmlocal.conf changes are deferred", postinst)
        self.assertIn("dashboard enable/start/configuration deferred", postinst)
        self.assertIn(
            '[ "$HEALTH_OK" -eq 1 ] && [ "$PVE_REFRESH_OK" -eq 1 ]',
            postinst,
        )

    def test_upgrade_check_is_packaged_and_exposed_by_cli(self):
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        checker = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check"

        self.assertTrue(checker.is_file())
        self.assertIn("sharedlvmthin-upgrade-check", build)
        self.assertIn("sharedlvmthin-candidate-recovery-check", build)
        self.assertIn("sharedlvmthin_pve_inventory.py", build)
        self.assertIn("candidate preinst PVE inventory parser differs", (
            ROOT / "scripts/check-release.sh"
        ).read_text(encoding="utf-8"))
        self.assertIn("sharedlvmthin-upgrade-check", postinst)
        self.assertIn("upgrade-check)", cli)
        self.assertIn("sharedlvmthin upgrade-check", cli)

    def test_web_configurator_unit_detection_is_pipefail_safe(self):
        configurator = (
            ROOT / "usr/sbin/sharedlvmthin-web-configure"
        ).read_text(encoding="utf-8")
        self.assertIn('systemctl cat "$SERVICE"', configurator)
        self.assertNotIn("systemctl list-unit-files |", configurator)

    def test_lvm_autogrow_install_is_fail_closed_and_purge_is_scoped(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        postrm = (ROOT / "DEBIAN/postrm").read_text(encoding="utf-8")
        self.assertIn("did not overwrite administrator-owned LVM settings", postinst)
        self.assertIn("BEGIN PVE-SHAREDLVMTHIN MANAGED AUTOGROW", postinst)
        self.assertNotIn("rm -f /etc/lvm/lvmlocal.conf", postrm)
        self.assertIn("BEGIN PVE-SHAREDLVMTHIN MANAGED AUTOGROW", postrm)
        self.assertIn("/usr/libexec/pve-sharedlvmthin/__pycache__", postrm)

    def test_shared_lvm_autoactivation_is_verified_not_best_effort(self):
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn("_disable_and_verify_autoactivation", plugin)
        self.assertIn("lv_autoactivation", plugin)
        self.assertNotIn("could not disable autoactivation", plugin)
        self.assertIn("safe shared-storage autoactivation state is unconfirmed", plugin)

    def test_doctor_batches_per_vg_lvm_diagnostics(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn('LVS_REPORT="$(', doctor)
        self.assertIn("lv_autoactivation,pool_lv,lv_when_full", doctor)
        self.assertIn("no_space_timeout", doctor)
        self.assertNotIn(
            'lvs --readonly --binary --noheadings -o lv_autoactivation "$VG/$POOL"',
            doctor,
        )
        self.assertNotIn('--select "pool_lv=$POOL"', doctor)

    def test_doctor_classifies_thick_mode_and_interrupted_anchors(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn('inside && $1 == "slt-allocation-mode"', doctor)
        self.assertIn(
            "Thick Generations mode uses independent thick LVs; "
            "thin-pool headroom and autogrow policy do not apply",
            doctor,
        )
        self.assertIn("sharedlvmthin-health-json", doctor)
        self.assertIn("ANCHOR_STATE ANCHOR_WORKER ANCHOR_REASON", doctor)
        self.assertIn("new mutations must remain blocked", doctor)

    def test_doctor_collects_thick_inventory_once_and_does_not_relabel_thin_pools(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("THICK_HEALTH_JSON_COLLECTED=0", doctor)
        self.assertIn('[ "$THICK_HEALTH_JSON_COLLECTED" -eq 0 ]', doctor)
        self.assertIn("THICK_HEALTH_JSON_COLLECTED=1", doctor)
        self.assertIn("timeout --foreground 90", doctor)
        self.assertIn(
            "Thin pools sharing this VG belong to the canonical Thin alias",
            doctor,
        )

    def test_bounded_install_preflight_does_not_claim_full_volume_audit(self):
        doctor = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        self.assertIn('DOCTOR_MODE="${DOCTOR_MODE:-full}"', doctor)
        self.assertIn('[ "$DOCTOR_MODE" = "quick" ]', doctor)
        self.assertIn('per-volume and Thick anchor diagnostics require the full Doctor', doctor)
        self.assertIn('DOCTOR_MODE=quick doctor', doctor)
        self.assertIn('timeout --foreground --kill-after=10 60', postinst)
        self.assertIn('/usr/sbin/sharedlvmthin doctor --quick', postinst)

    def test_recovery_check_is_read_only_and_fail_closed(self):
        checker = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-recovery-check"
        ).read_text(encoding="utf-8")
        self.assertIn("SAFE_FOR_MUTATION", checker)
        self.assertIn("RECOVERY_REQUIRED", checker)
        self.assertIn("unscoped_dstate_tasks", checker)
        self.assertIn("one outstanding LVM/PVE probe maximum", checker)
        self.assertIn("--readonly suppresses that runtime query", checker)
        for command in ("pvcreate", "vgcreate", "lvcreate", "lvremove", "wipefs", "thin_repair"):
            self.assertNotIn(f'["/sbin/{command}"', checker)

    def test_thick_resume_derives_exact_persisted_transaction(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-resume <storage-id> <volume>", cli)
        self.assertIn('--resume "$2" "$3"', cli)
        self.assertIn("_thick_read_anchor", worker)
        self.assertIn("has no resumable materialization transition", worker)
        for phase in (
            "PREPARED", "SOURCE_READY", "COMMITTED", "HYDRATING",
            "HYDRATION_COMPLETE", "LINEAR_PIVOTED",
        ):
            self.assertIn(f"$phase ne '{phase}'", worker)
        self.assertIn("anchor-scoped materialization does not match transaction", worker)
        self.assertIn("a different VG intent targets this materialization anchor", worker)
        self.assertIn("$operation ne 'SNAPSHOT' && $operation ne 'ROLLBACK'", worker)
        self.assertIn("$operation eq 'ROLLBACK' ? 'DM_PIVOT' : 'DM_CUTOVER'", worker)
        self.assertIn("_read_vg_intent($scfg, $vg, $device)", worker)
        self.assertNotIn("_read_vg_intent($vg, $device)", worker)
        self.assertNotIn("lvremove", worker)
        self.assertNotIn("pvcreate", worker)

    def test_materialization_worker_serializes_normal_same_vg_contention(self):
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("if !$class->_is_thick_mode($scfg)", worker)
        self.assertIn("active storage mutation worker still targets VG", worker)
        self.assertIn("MATERIALIZATION_WAIT", worker)
        self.assertIn("_thick_read_anchor($storeid, $scfg, $volname)", worker)
        self.assertIn("queued materialization identity changed", worker)
        self.assertIn("exceeded its bounded VG-admission wait", worker)
        self.assertIn("sleep 5", worker)
        self.assertNotIn("Restart=always", worker)

    def test_thick_rollback_snapshot_permission_probe_is_device_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "_thick_verify_snapshot_readonly($scfg, $vg, $source, $device)", source
        )
        self.assertNotIn(
            "_thick_verify_snapshot_readonly($vg, $source, $device)", source
        )
        start = source.index("sub _thick_verify_snapshot_readonly")
        end = source.index("sub _thick_filesystem_path", start)
        permissions = source[start:end]
        self.assertIn("my ($class, $scfg, $vg, $lv, $device)", permissions)
        self.assertIn("$class->_thick_command_deadline($scfg)", permissions)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", permissions)

    def test_linear_pivot_is_resumable_from_an_already_suspended_clone(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("if ($tr->{state}->{phase} eq 'HYDRATION_COMPLETE')")
        end = source.index("if ($tr->{state}->{phase} eq 'LINEAR_PIVOTED')", start)
        pivot = source[start:end]
        self.assertIn("my $already_suspended =", pivot)
        self.assertIn("if (!$already_suspended)", pivot)
        self.assertIn("$class->_thick_suspend_mapper_exact(", pivot)
        self.assertNotIn("'suspend', '--noflush', $front", pivot)
        status_checks = list(
            re.finditer(
                r"_thick_verify_clone_status\(\s*\$front,\s*1,\s*"
                r"(?:int\(\$tr->\{size\} / 512\)|\$sectors),\s*"
                r"\$tr->\{geometry\}->\{region_sectors\},\s*"
                r"\$class->_thick_command_deadline\(\$scfg\),\s*\)",
                pivot,
            )
        )
        self.assertGreaterEqual(len(status_checks), 2)
        self.assertLess(
            pivot.index("$class->_thick_suspend_mapper_exact("),
            status_checks[-1].start(),
        )

    def test_thick_snapshot_table_boundaries_drain_io_and_are_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_volume_snapshot")
        end = source.index("sub _thick_free_image", start)
        transition = source[start:end]
        self.assertEqual(
            transition.count("$class->_thick_suspend_mapper_exact("), 2
        )
        self.assertNotRegex(
            transition, r"'/sbin/dmsetup', '--verifyudev', 'suspend'"
        )
        self.assertNotIn("'suspend', '--noflush', $front", transition)

    def test_thick_suspend_state_proof_is_deadline_scoped_everywhere(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_mapper_is_suspended")
        end = source.index("sub _thick_verify_active_lv_identity", start)
        helper = source[start:end]
        self.assertIn("my ($class, $mapper, $command_timeout) = @_;", helper)
        self.assertIn("'--kill-after=5s', \"${command_timeout}s\"", helper)
        self.assertIn("'/sbin/dmsetup', 'info'", helper)
        calls = re.findall(r"_thick_mapper_is_suspended\((.*?)\)", source, re.DOTALL)
        self.assertEqual(len(calls), 11)
        self.assertEqual(
            sum("_thick_command_deadline($scfg" in call for call in calls), 8
        )
        self.assertEqual(
            sum("$mapper, $command_timeout" in call for call in calls), 2
        )

    def test_thick_clone_destination_is_zeroed_before_publication(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("if ($tr->{state}->{phase} eq 'PREPARED')")
        end = source.index("if ($tr->{state}->{phase} eq 'SOURCE_READY')", start)
        prepared = source[start:end]
        self.assertIn("_zero_new_thick_generation(", prepared)
        self.assertIn('int($tr->{size})', prepared)
        self.assertIn("/sbin/blockdev', '--flushbufs'", prepared)
        self.assertLess(
            prepared.index("_zero_new_thick_generation("),
            prepared.index("phase => 'SOURCE_READY'"),
        )
        self.assertIn("no_discard_passdown", source)

    def test_every_direct_thick_write_has_active_lv_uuid_proof(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn("sub _thick_verify_active_lv_identity", source)
        self.assertIn("sub _thick_activate_exact_lvs", source)
        self.assertIn("'--activationmode', 'complete'", source)
        self.assertIn("sub _thick_frontend_open_count", source)
        self.assertIn("invalid thick-generations open-count command deadline", source)
        self.assertEqual(
            source.count("$class->_thick_frontend_open_count(\n"),
            10,
        )
        helper_end = source.index("sub _thick_schedule_materialization")
        thick_after_helper = source[helper_end:source.index("sub volume_snapshot_needs_fsfreeze")]
        self.assertNotIn("'-o', 'open'", thick_after_helper)
        self.assertIn("'vg_uuid,lv_uuid,lv_name'", source)
        self.assertIn('my $expected_uuid = "LVM-$vg_uuid$lv_uuid"', source)
        activate_start = source.index("sub _thick_activate_exact_lvs")
        activate_end = source.index("sub _thick_lv_mapper_name", activate_start)
        activate = source[activate_start:activate_end]
        self.assertIn("my ($class, $scfg, $vg, $device, $errmsg, @lvs)", activate)
        self.assertIn("my $command_timeout = $class->_thick_command_deadline($scfg)", activate)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", activate)
        self.assertIn("'/sbin/lvchange', '--devices', $device, '--activationmode', 'complete'", activate)
        self.assertIn("activation result is UNKNOWN because complete exact identity is unproven", activate)
        self.assertEqual(source.count("_thick_activate_exact_lvs("), 13)
        self.assertEqual(source.count("_thick_verify_active_lv_identity("), 26)
        identity_start = source.index("sub _thick_verify_active_lv_identity")
        identity_end = source.index("sub _thick_activate_exact_lvs", identity_start)
        identity = source[identity_start:identity_end]
        self.assertIn("uuid,major,minor,suspended", identity)
        self.assertIn("_thick_block_node_devno(", identity)
        self.assertIn("return $devno", identity)
        activation_start = source.index("sub _thick_activate_volume")
        activation_end = source.index("sub _thick_deactivate_volume", activation_start)
        activation = source[activation_start:activation_end]
        self.assertNotIn("'kernel-only'", activation)
        self.assertNotRegex(
            source,
            r"->\_thick_verify_active_lv_identity\(\s*\$vg,\s*[^,()]+,\s*\$device\s*\)",
        )
        self.assertEqual(
            source.count("'/sbin/lvchange', '--devices', $device, '-an'"),
            1,
        )
        self.assertEqual(source.count("_thick_deactivate_exact_lvs("), 19)

        deactivate_start = source.index("sub _thick_deactivate_exact_lvs")
        deactivate_end = source.index("sub _thick_fault_point", deactivate_start)
        deactivate = source[deactivate_start:deactivate_end]
        self.assertIn("my ($class, $scfg, $vg, $device, $errmsg, @lvs)", deactivate)
        self.assertIn("my $command_timeout = $class->_thick_command_deadline($scfg)", deactivate)
        self.assertEqual(deactivate.count("_dm_kernel_inventory($command_timeout)"), 2)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", deactivate)
        self.assertIn("deactivation result is UNKNOWN", deactivate)
        self.assertIn("no retry attempted", deactivate)

        calls = re.findall(
            r"\$class->_thick_deactivate_exact_lvs\(\s*([^,]+),",
            source,
        )
        self.assertEqual(len(calls), 19)
        self.assertTrue(all(call.strip() == "$scfg" for call in calls))

        activate_calls = re.findall(
            r"\$class->_thick_activate_exact_lvs\(\s*([^,]+),",
            source,
        )
        self.assertEqual(len(activate_calls), 13)
        self.assertTrue(all(call.strip() == "$scfg" for call in activate_calls))

    def test_every_direct_thick_lvm_mutation_is_device_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        lines = source.splitlines()
        current_sub = None
        mutations = []
        command = re.compile(
            r"'/sbin/(?:lvcreate|lvchange|lvremove|lvextend|lvrename)'"
        )
        for index, line in enumerate(lines):
            declaration = re.match(r"^sub\s+([A-Za-z0-9_]+)", line)
            if declaration:
                current_sub = declaration.group(1)
            if not current_sub or not current_sub.startswith("_thick_"):
                continue
            if not command.search(line):
                continue
            window = " ".join(lines[index : index + 12])
            mutations.append((index + 1, current_sub))
            self.assertIn(
                "'--devices'",
                window,
                f"direct Thick LVM mutation in {current_sub}:{index + 1} is unscoped",
            )
            self.assertIn(
                "$device",
                window,
                f"direct Thick LVM mutation in {current_sub}:{index + 1} lacks the pinned mapper",
            )
        self.assertGreater(len(mutations), 0, "no direct Thick LVM mutations were audited")

    def test_thick_resize_publication_never_uses_noflush(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_recover_resize")
        end = source.index("sub volume_resize", start)
        resize = source[start:end]
        self.assertNotIn("'suspend', '--noflush'", resize)
        self.assertEqual(resize.count("$class->_thick_suspend_mapper_exact("), 2)
        self.assertNotIn("'/sbin/dmsetup', '--verifyudev', 'suspend'", resize)
        self.assertGreaterEqual(
            resize.count("$class->_thick_mapper_is_suspended("),
            2,
        )
        self.assertEqual(resize.count("$class->_thick_resume_mapper_exact("), 2)
        self.assertIn(
            "'/usr/bin/timeout', '--foreground', '--kill-after=5s'",
            resize,
        )
        self.assertNotIn("['/sbin/dmsetup', 'table', $mapper]", resize)

    def test_thick_resize_metadata_extend_is_bounded_and_reconciled(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_volume_resize")
        end = source.index("sub volume_resize", start)
        resize = source[start:end]
        self.assertIn("$class->_thick_command_deadline($scfg)", resize)
        self.assertIn("'/sbin/lvextend', '--devices', $device", resize)
        self.assertNotIn("['/sbin/lvextend'", resize)
        self.assertIn("lvextend reported an error, but its exact postcondition", resize)
        self.assertIn("no retry attempted", resize)

    def test_thick_data_plane_zero_and_flush_are_not_metadata_timed(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")

        boundaries = (
            ("_zero_new_thick_generation", "_thick_alloc_image"),
            ("_thick_alloc_image", "_lazy_alloc_image"),
            ("_lazy_alloc_image", "alloc_image"),
            ("_thick_volume_snapshot", "_thick_free_image"),
            ("_thick_recover_resize", "_thick_volume_resize"),
            ("_thick_volume_resize", "volume_resize"),
        )
        sections = {}
        expected_data_commands = {
            "_zero_new_thick_generation": 2,
            "_thick_alloc_image": 1,
            "_lazy_alloc_image": 1,
            "_thick_volume_snapshot": 3,
            "_thick_recover_resize": 2,
            "_thick_volume_resize": 2,
        }
        for name, following in boundaries:
            start = source.index(f"sub {name}")
            end = source.index(f"sub {following}", start)
            body = source[start:end]
            sections[name] = body
            commands = re.findall(
                r"run_command\(\s*\[(.*?)\]\s*,\s*errmsg\s*=>",
                body,
                re.DOTALL,
            )
            data_commands = [
                command
                for command in commands
                if any(
                    tool in command
                    for tool in (
                        "'/usr/sbin/blkdiscard'",
                        "'/usr/bin/dd'",
                        "'/sbin/blockdev'",
                    )
                )
            ]
            self.assertEqual(len(data_commands), expected_data_commands[name])
            for command in data_commands:
                self.assertNotIn(
                    "'/usr/bin/timeout'",
                    command,
                    f"{name} incorrectly applies the metadata deadline to data I/O",
                )

        zero = sections["_zero_new_thick_generation"]
        self.assertLess(zero.index("'/usr/sbin/blkdiscard'"), zero.index("'/usr/bin/dd'"))
        self.assertIn("return 'blkzeroout' if !$@", zero)
        self.assertIn("full direct-write fallback failed", zero)

        alloc = sections["_thick_alloc_image"]
        alloc_zero = alloc.index("_zero_new_thick_generation(")
        alloc_flush = alloc.index("'/sbin/blockdev', '--flushbufs'", alloc_zero)
        alloc_publish = alloc.index("phase => 'MATERIALIZED'", alloc_flush)
        self.assertLess(alloc_zero, alloc_flush)
        self.assertLess(alloc_flush, alloc_publish)
        self.assertIn("OPEN VG intent", alloc[alloc_zero:alloc_publish])

        lazy_alloc = sections["_lazy_alloc_image"]
        self.assertIn("return $class->_thick_alloc_image(", lazy_alloc)
        self.assertLess(
            lazy_alloc.index("return $class->_thick_alloc_image("),
            lazy_alloc.index("_lazy_prepare_move_allocation"),
        )
        self.assertIn("state-[A-Za-z0-9]", lazy_alloc)
        free_start = source.index("sub free_image")
        free_end = source.index("sub _thin_recover_orphan", free_start)
        free_image = source[free_start:free_end]
        self.assertIn("Auxiliary objects on a Lazy-default storage", free_image)
        self.assertLess(
            free_image.index("return $class->_thick_free_image"),
            free_image.index("return $class->_lazy_free_image"),
        )
        self.assertEqual(lazy_alloc.count("_zero_new_thick_generation("), 1)
        self.assertIn('"/dev/$vg/$metadata", $geometry->{metadata_bytes}', lazy_alloc)
        self.assertIn("'/sbin/blockdev', '--flushbufs', \"/dev/$vg/$metadata\"", lazy_alloc)
        self.assertLess(
            lazy_alloc.index("'/sbin/blockdev', '--flushbufs', \"/dev/$vg/$metadata\""),
            lazy_alloc.index("phase => 'LAZY_DORMANT'"),
        )

        snapshot = sections["_thick_volume_snapshot"]
        snapshot_zero = snapshot.index("_zero_new_thick_generation(")
        snapshot_flush = snapshot.index("'/sbin/blockdev', '--flushbufs'", snapshot_zero)
        snapshot_publish = snapshot.index("phase => 'SOURCE_READY'", snapshot_flush)
        self.assertLess(snapshot_zero, snapshot_flush)
        self.assertLess(snapshot_flush, snapshot_publish)

        for name in ("_thick_recover_resize", "_thick_volume_resize"):
            resize = sections[name]
            tail_zero = resize.index("'/usr/bin/dd'")
            tail_flush = resize.index("'/sbin/blockdev', '--flushbufs'", tail_zero)
            publish = resize.index("_thick_resume_mapper_exact(", tail_flush)
            clear = resize.index("_clear_vg_intent(", publish)
            self.assertLess(tail_zero, tail_flush)
            self.assertLess(tail_flush, publish)
            self.assertLess(publish, clear)
            self.assertIn("OPEN EXTEND intent", resize)

    def test_all_new_thick_transitions_use_one_geometry_selector(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        alloc_start = source.index("sub _thick_alloc_image")
        alloc_end = source.index("sub _lazy_alloc_image", alloc_start)
        lazy_start = alloc_end
        lazy_end = source.index("sub alloc_image", lazy_start)
        snapshot_start = source.index("sub _thick_volume_snapshot")
        snapshot_end = source.index("sub _thick_free_image", snapshot_start)
        resume_start = source.index("sub _thick_resume_transition")
        resume_end = source.index("sub _thick_volume_snapshot", resume_start)
        self.assertEqual(
            source[alloc_start:alloc_end].count("$class->_thick_new_geometry("),
            1,
        )
        self.assertEqual(
            source[lazy_start:lazy_end].count("$class->_thick_new_geometry("),
            1,
        )
        self.assertEqual(
            source[snapshot_start:snapshot_end].count("$class->_thick_new_geometry("),
            1,
        )
        self.assertNotIn("clone_geometry(", source[alloc_start:alloc_end])
        self.assertNotIn("clone_geometry(", source[lazy_start:lazy_end])
        self.assertNotIn("clone_geometry(", source[snapshot_start:snapshot_end])
        self.assertIn(
            "clone_geometry(int($size), int($state->{region}))",
            source[resume_start:resume_end],
        )
        self.assertNotIn("_thick_new_geometry(", source[resume_start:resume_end])

    def test_thick_object_creation_is_deadline_scoped_under_open_intent(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        boundaries = (
            ("_thick_alloc_image", "_lazy_alloc_image", 2, "op => 'ALLOC'"),
            ("_lazy_alloc_image", "alloc_image", 3, "op => 'ALLOC'"),
            ("_thick_volume_snapshot", "_thick_free_image", 2, "state => 'OPEN'"),
        )
        total = 0
        for name, following, expected, intent_marker in boundaries:
            start = source.index(f"sub {name}")
            end = source.index(f"sub {following}", start)
            body = source[start:end]
            commands = re.findall(
                r"my @(?:head|anchor|new|meta|data|metadata)_create = \((.*?)\);",
                body,
                re.DOTALL,
            )
            self.assertEqual(len(commands), expected)
            total += len(commands)
            self.assertLess(body.index(intent_marker), body.index(commands[0]))
            for command in commands:
                self.assertIn("'/sbin/lvcreate'", command)
                self.assertIn("'--devices', $device", command)
                self.assertIn("'--setactivationskip', 'y'", command)
                self.assertIn("'--setautoactivation', 'n'", command)
            create_calls = re.findall(
                r"\$class->_thick_create_lv_exact\(\s*\$scfg,\s*\$device,\s*\\@(\w+)_create",
                body,
            )
            self.assertEqual(len(create_calls), expected)
        self.assertEqual(total, 7)

        helper_start = source.index("sub _thick_create_lv_exact")
        helper_end = source.index("sub _thick_create_mapper_exact", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertEqual(
            helper.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            1,
        )
        self.assertIn("creation result is UNKNOWN", helper)
        self.assertIn("exact signed", helper)
        self.assertIn("no retry attempted", helper)
        self.assertIn("continuing without retry", helper)
        self.assertEqual(source.count("$class->_thick_create_lv_exact("), 7)

    def test_async_materialization_submission_is_bounded_and_reconciled(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_schedule_materialization")
        end = source.index("sub _thick_snapshot_name", start)
        scheduler = source[start:end]
        self.assertIn(
            "my ($class, $scfg, $storeid, $volname, $snap, $operation, $tx, $timeout)",
            scheduler,
        )
        self.assertEqual(scheduler.count("'/usr/bin/systemd-run'"), 1)
        self.assertIn("$class->_thick_command_deadline($scfg)", scheduler)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", scheduler)
        self.assertEqual(scheduler.count("'/usr/bin/systemctl', 'show'"), 1)
        self.assertIn("for my $suffix ('service', 'timer')", scheduler)
        for evidence in (
            "Id,LoadState,ActiveState,Transient,ExecStart,Triggers",
            "service command mismatch",
            "timer identity mismatch",
            "neither queued nor running",
            "scheduling result is UNKNOWN",
            "no retry attempted",
            "continuing without retry",
        ):
            self.assertIn(evidence, scheduler)

        self.assertEqual(source.count("$class->_thick_schedule_materialization("), 1)
        call = re.search(
            r"\$class->_thick_schedule_materialization\(\s*"
            r"\$scfg,\s*\$storeid,\s*\$volname,\s*\$snap,\s*"
            r"\$operation,\s*\$intent\{tx\},\s*\$timeout",
            source,
        )
        self.assertIsNotNone(call)
        outside = source[:start] + source[end:]
        self.assertNotIn("'/usr/bin/systemd-run'", outside)

    def test_hydration_messages_are_one_shot_and_kernel_reconciled(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_set_background_hydration")
        end = source.index("sub _thick_fail_stalled_hydration", start)
        family = source[start:end]
        self.assertEqual(family.count("'/sbin/dmsetup', 'message'"), 1)
        self.assertEqual(family.count("'/sbin/dmsetup', 'status', '--noflush'"), 1)
        self.assertIn("$class->_thick_verify_clone_status(", family)
        self.assertIn("'enable_hydration'", family)
        self.assertIn("'disable_hydration'", family)
        self.assertIn("no_hydration", family)
        self.assertIn("result is UNKNOWN", family)
        self.assertIn("no retry attempted", family)
        self.assertIn("continuing without retry", family)
        self.assertIn("sub _thick_enable_background_hydration", family)
        self.assertIn("sub _thick_disable_background_hydration", family)

        outside = source[:start] + source[end:]
        self.assertNotIn("'/sbin/dmsetup', 'message'", outside)
        self.assertEqual(source.count("$class->_thick_enable_background_hydration("), 2)
        call = re.search(
            r"\$class->_thick_enable_background_hydration\(\s*"
            r"\$front,\s*\$class->_thick_command_deadline\(\$scfg\),\s*"
            r"int\(\$tr->\{size\} / 512\),\s*"
            r"\$tr->\{geometry\}->\{region_sectors\}",
            source,
        )
        self.assertIsNotNone(call)
        lazy_call = re.search(
            r"\$class->_thick_enable_background_hydration\(\s*"
            r"\$clone,\s*\$class->_thick_command_deadline\(\$scfg\),\s*"
            r"\$sectors,\s*int\(\$state->\{region\}\)",
            source,
        )
        self.assertIsNotNone(lazy_call)

    def test_lazy_linear_pivot_recovery_classifies_only_exact_kernel_states(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _lazy_front_pivot_state")
        end = source.index("sub _lazy_verify_guest_discard_config", start)
        classifier = source[start:end]
        for state in (
            "LINEAR_ACTIVE",
            "CLONE_ACTIVE",
            "CLONE_ACTIVE_LINEAR_PENDING",
            "CLONE_SUSPENDED_LINEAR_PENDING",
        ):
            self.assertIn(state, classifier)
        self.assertIn("'table', '--inactive', $mapper", classifier)
        self.assertIn("$tables_loaded eq 'Both'", classifier)
        self.assertNotIn("Live & Inactive", classifier)
        self.assertIn("outside the exact recoverable graph states", classifier)
        self.assertIn("UUID mismatch", classifier)
        self.assertIn("_thick_verify_mapper_node_ready", classifier)

        guard_start = source.index("sub _lazy_install_discard_guard")
        guard_end = source.index("sub _lazy_verify_private_io_guard", guard_start)
        guard = source[guard_start:guard_end]
        self.assertIn("$write_error = $@", guard)
        self.assertIn("exact zero", guard)
        self.assertIn("continuing without retry", guard)
        self.assertLess(guard.index("$write_error = $@"), guard.index("open(my $in"))

        activate_start = source.index("sub _lazy_activate_volume_locked")
        activate_end = source.index("sub _thick_deactivate_volume", activate_start)
        activate = source[activate_start:activate_end]
        self.assertIn("$state->{phase} eq 'LAZY_CLAIMED'", activate)
        self.assertIn("_lazy_require_local_owner(", activate)
        self.assertIn("thick-lazy-materialize", source)
        self.assertIn("No target activation effect was issued", source)
        self.assertIn("epoch on local node", source)

        materialize_start = source.index("sub _lazy_materialize_volume")
        materialize_end = source.index("sub _lazy_recover_v5_pivot", materialize_start)
        materialize = source[materialize_start:materialize_end]
        self.assertIn("_lazy_front_pivot_state(", materialize)
        self.assertIn("if ($front_state ne 'LINEAR_ACTIVE')", materialize)
        self.assertIn("if ($front_state eq 'CLONE_ACTIVE')", materialize)
        self.assertIn("found a pivoted frontend before hydration completion", materialize)
        self.assertRegex(
            materialize,
            r"_lazy_mapper_identity\(\s*\$scfg, \$front, \$front_uuid, \$linear_table",
        )
        self.assertNotRegex(
            materialize,
            r"_lazy_verify_public_io_guard\(\s*\$scfg, \$front, \$front_uuid, \$linear_table",
        )
        self.assertIn("_lazy_verify_private_io_guard(", materialize)
        self.assertNotIn("_lazy_install_discard_guard(", materialize)
        self.assertIn("$clone, 1, $sectors", materialize)
        self.assertIn("authority changed before pivot", materialize)
        pivot_publish = materialize.index("phase => 'LINEAR_PIVOTED'")
        pivot_intent = materialize.index("op => 'DM_PIVOT'", pivot_publish)
        first_cleanup = materialize.index("removing Lazy Thick $description after pivot", pivot_publish)
        self.assertLess(pivot_publish, pivot_intent)
        self.assertLess(pivot_intent, first_cleanup)

        recover_start = source.index("sub _lazy_recover_v5_pivot_locked")
        recover_end = source.index("sub _command_lines", recover_start)
        recover = source[recover_start:recover_end]
        self.assertIn("found clone without zero source", recover)
        self.assertIn("$clone, 1, $sectors", recover)
        self.assertIn("removing recovered detached Lazy clone", recover)
        self.assertIn("removing recovered detached Lazy zero source", recover)
        self.assertIn("recovered Lazy clone absence is unproven", recover)
        no_intent = recover.index("if (!$intent)")
        no_intent_cutover = recover.index("_lazy_front_pivot_state(", no_intent)
        no_intent_identity = recover.index("_lazy_mapper_identity(", no_intent_cutover)
        no_intent_set = recover.index("_set_vg_intent(", no_intent_identity)
        self.assertLess(no_intent_cutover, no_intent_identity)
        self.assertLess(no_intent_identity, no_intent_set)
        self.assertIn("int($anchor_state->{v} // 0) != 5", recover)
        self.assertIn("$anchor_state->{sid} ne $storeid", recover)
        self.assertIn("$anchor_state->{op} ne 'ALLOC'", recover)
        self.assertIn("$anchor_state->{source} ne $data", recover)

        lock_start = source.index("sub _with_lazy_volume_executor_lock")
        lock_end = source.index("sub _lazy_deactivate_volume", lock_start)
        lock = source[lock_start:lock_end]
        self.assertIn("LOCK_EX | LOCK_NB", lock)
        self.assertIn("fcntl($executor_lock, F_SETFD, 0)", lock)
        self.assertIn("another Lazy Thick executor is still live", lock)
        for wrapper in (
            "sub _lazy_activate_volume",
            "sub _lazy_deactivate_volume",
            "sub _lazy_materialize_volume",
            "sub _lazy_recover_v5_pivot",
        ):
            wrapper_start = source.index(wrapper)
            wrapper_body = source[wrapper_start:source.index("\n}\n", wrapper_start) + 3]
            self.assertIn("_with_lazy_volume_executor_lock(", wrapper_body)


    def test_thick_runtime_never_infers_managed_mapper_presence_from_udev_node(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_resume_transition")
        end = source.index("sub volume_snapshot", start)
        thick_runtime = source[start:end]
        self.assertNotRegex(
            thick_runtime,
            r'_block_device_exists\("/dev/mapper/\$',
        )
        self.assertIn("sub _thick_managed_mapper_present", source)
        self.assertIn("sub _thick_mapper_name_present", source)
        self.assertIn("_dm_kernel_inventory($class->_thick_command_deadline($scfg))", source)

    def test_thick_frontend_postconditions_bind_live_kernel_and_block_node_identity(self):
        source = (ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm").read_text()
        for start_name, end_name in (
            ("sub _thick_verify_frontend", "sub _thick_block_node_devno"),
            ("sub _thick_verify_source_mapper", "sub _thick_mapper_is_suspended"),
            ("sub _thick_verify_clone_frontend", "sub _thick_wait_for_hydration"),
        ):
            section = source[source.index(start_name):source.index(end_name, source.index(start_name))]
            self.assertIn("uuid,readonly,major,minor,suspended", section)
            self.assertIn("_thick_verify_mapper_node_ready", section)
        node = source[source.index("sub _thick_block_node_devno"):
                      source.index("sub _thick_mapper_name_present")]
        self.assertIn("'/usr/bin/stat', '-Lc', '%f|%t|%T'", node)
        self.assertIn("is not a block device", node)
        clone = source[source.index("sub _thick_verify_clone_frontend"):
                       source.index("sub _thick_wait_for_hydration")]
        self.assertIn("ordered device roles mismatch", clone)
        self.assertIn("my @actual_roles = ($table_meta, $table_destination, $table_source)", clone)
        committed_start = source.index("if ($tr->{state}->{phase} eq 'COMMITTED')")
        committed_end = source.index("if ($tr->{state}->{phase} eq 'HYDRATING')", committed_start)
        committed = source[committed_start:committed_end]
        self.assertIn("0 $sectors clone $meta_devno $new_devno $source_devno", committed)
        self.assertIn("$inactive->[0] ne $clone_table", committed)
        self.assertLess(
            committed.index("_thick_verify_active_lv_identity("),
            committed.index("_thick_load_inactive_table_exact("),
        )

    def test_thick_capacity_probe_is_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_capacity_gate")
        end = source.index("sub _thick_materialization_admission", start)
        capacity = source[start:end]
        self.assertIn("$class->_thick_command_deadline($scfg) . 's'", capacity)
        self.assertIn("'/sbin/vgs', '--readonly', '--devices', $device", capacity)
        self.assertNotIn("['/sbin/vgs', '--readonly'", capacity)

    def test_thick_snapshot_permission_mutation_is_bounded_and_reconciled(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_ensure_snapshot_readonly")
        end = source.index("sub _thick_filesystem_path", start)
        helper = source[start:end]
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", helper)
        self.assertIn("'/sbin/lvchange', '--devices', $device, '-pr'", helper)
        self.assertIn("outcome is UNKNOWN", helper)
        self.assertIn("no retry", helper)
        self.assertEqual(source.count("_thick_ensure_snapshot_readonly("), 1)

    def test_partial_allocation_recovery_commands_are_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_recover_partial_allocation")
        end = source.index("sub _thick_recover_lazy_orphan_allocation", start)
        recovery = source[start:end]
        self.assertIn("$class->_thick_command_deadline($scfg)", recovery)
        self.assertEqual(
            recovery.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            1,
        )
        self.assertNotIn("'/sbin/lvs', '--readonly'", recovery)
        self.assertIn("_dm_kernel_inventory($command_timeout)", recovery)
        self.assertIn("_thick_lv_mapper_name($vg, $object)", recovery)
        self.assertIn("_thick_deactivate_exact_lvs(", recovery)
        self.assertEqual(recovery.count("'/sbin/lvremove', '--devices', $device"), 1)
        self.assertIn(
            "my @remove = $lazy ? ($metadata, $head, $anchor) : ($head, $anchor)",
            recovery,
        )
        self.assertIn(
            "for my $object (grep { exists($objects->{$_}) } @remove)",
            recovery,
        )
        self.assertNotIn("['/sbin/lvremove'", recovery)

    def test_lazy_orphan_allocation_recovery_is_exact_and_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_recover_lazy_orphan_allocation")
        end = source.index("sub _thick_recover_orphan_allocation", start)
        recovery = source[start:end]
        self.assertIn("$class->_require_no_vg_intent($scfg, $vg, $device)", recovery)
        self.assertIn("phase} ne 'LAZY_DORMANT'", recovery)
        self.assertIn("publication} != 1", recovery)
        self.assertIn("@related != 3", recovery)
        self.assertIn("_dm_kernel_inventory($command_timeout)", recovery)
        self.assertIn("_thick_deactivate_exact_lvs(", recovery)
        self.assertEqual(
            recovery.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"), 1
        )
        self.assertEqual(recovery.count("'/sbin/lvremove', '--devices', $device"), 1)
        self.assertNotIn("['/sbin/lvremove'", recovery)

    def test_remaining_explicit_recovery_removals_are_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        boundaries = (
            ("_thick_recover_volume_delete", "_thick_recover_unpublished_prepare", 2),
            ("_thick_recover_unpublished_prepare", "_zero_new_thick_generation", 1),
        )
        for name, following, removals in boundaries:
            start = source.index(f"sub {name}")
            end = source.index(f"sub {following}", start)
            body = source[start:end]
            self.assertIn("$class->_thick_command_deadline($scfg)", body)
            self.assertEqual(
                body.count("'/sbin/lvremove', '--devices', $device"),
                removals,
            )
            self.assertNotIn("['/sbin/lvremove'", body)
        unpublished_start = source.index("sub _thick_recover_unpublished_prepare")
        unpublished_end = source.index("sub _zero_new_thick_generation", unpublished_start)
        unpublished = source[unpublished_start:unpublished_end]
        self.assertIn("_dm_kernel_inventory($command_timeout)", unpublished)
        self.assertNotIn("_dm_kernel_inventory()", unpublished)

    def test_destructive_thick_lifecycle_removals_are_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        boundaries = (
            ("_thick_free_image", "_lazy_free_image", 2),
            ("_lazy_free_image", "free_image", 1),
            ("_thick_volume_snapshot_delete", "_thick_recover_orphan_tree", 1),
            ("_thick_recover_snapshot_delete", "_volume_snapshot_delete_locked", 1),
        )
        for name, following, removals in boundaries:
            start = source.index(f"sub {name}")
            end = source.index(f"sub {following}", start)
            body = source[start:end]
            self.assertIn("$class->_thick_command_deadline($scfg)", body)
            self.assertEqual(
                body.count("'/sbin/lvremove', '--devices', $device"),
                removals,
            )
            self.assertNotIn("['/sbin/lvremove'", body)

    def test_every_thick_scoped_inventory_is_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_list_volumes_scoped")
        end = source.index("sub _block_device_exists", start)
        helper = source[start:end]
        self.assertIn("my ($class, $scfg, $vg, $device)", helper)
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", helper)
        calls = re.findall(
            r"\$class->_thick_list_volumes_scoped\(\s*([^,]+),",
            source,
        )
        self.assertEqual(len(calls), 53)
        self.assertTrue(all(call.strip() == "$scfg" for call in calls))

    def test_thick_autoactivation_boundary_is_bounded_and_mode_isolated(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        verify_start = source.index("sub _thick_verify_autoactivation_disabled")
        disable_start = source.index("sub _thick_disable_and_verify_autoactivation")
        verify = source[verify_start:disable_start]
        disable = source[disable_start:source.index("sub _verify_snapshot_postcondition", disable_start)]
        self.assertIn("$class->_thick_command_deadline($scfg)", verify)
        self.assertIn("'/sbin/lvs', '--readonly', '--devices', $device", verify)
        self.assertIn("$class->_thick_command_deadline($scfg)", disable)
        self.assertIn("'/sbin/lvchange', '--devices', $device", disable)
        self.assertIn("outcome is UNKNOWN", disable)
        self.assertIn("no retry attempted", disable)

        verify_calls = re.findall(
            r"\$class->_thick_verify_autoactivation_disabled\(\s*([^,]+),",
            source,
        )
        disable_calls = re.findall(
            r"\$class->_thick_disable_and_verify_autoactivation\(\s*([^,]+),",
            source,
        )
        self.assertEqual(len(verify_calls), 22)
        self.assertEqual(len(disable_calls), 2)
        self.assertTrue(all(call.strip() == "$scfg" for call in verify_calls))
        self.assertTrue(all(call.strip() == "$scfg" for call in disable_calls))

        generic_allowed = {
            "_disable_and_verify_autoactivation",
            "_thin_prepare_import_pool",
            "_alloc_image_locked",
            "_activate_thin_volume_locked",
            "_thin_adopt_owner_model",
            "_volume_resize_locked",
            "_volume_snapshot_locked",
            "_volume_snapshot_rollback_locked",
        }
        for match in re.finditer(
            r"^sub (?P<name>\S+) \{(?P<body>.*?)(?=^sub |\Z)",
            source,
            re.MULTILINE | re.DOTALL,
        ):
            if re.search(
                r"(?<!_thick)_(?:disable_and_verify_autoactivation|verify_autoactivation_disabled)\(",
                match.group("body"),
            ):
                self.assertIn(
                    match.group("name"),
                    generic_allowed,
                    f"Thick path {match.group('name')} fell back to generic autoactivation helper",
                )

    def test_every_thick_tag_mutation_is_bounded_and_reconciled(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _change_exact_tags")
        end = source.index("sub _thick_verify_allocation_state", start)
        helper = source[start:end]
        self.assertIn("my ($class, $scfg, $vg, $lv", helper)
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertGreaterEqual(
            helper.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            2,
        )
        self.assertIn("tag mutation outcome is UNKNOWN", helper)
        self.assertIn("continuing without retry", helper)
        calls = re.findall(
            r"\$class->_change_exact_tags\(\s*([^,]+),",
            source,
        )
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(call.strip() == "$scfg" for call in calls))
        transitions = re.findall(
            r"\$class->_thick_transition_anchor\(\s*([^,]+),",
            source,
        )
        self.assertEqual(len(transitions), 15)
        self.assertTrue(all(call.strip() == "$scfg" for call in transitions))

    def test_entire_vg_intent_family_is_config_scoped_and_reconciled(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        expected = {
            "_vg_state_digest": 10,
            "_vg_tags": 1,
            "_read_vg_intent": 17,
            "_require_no_vg_intent": 17,
            "_require_exact_vg_intent": 11,
            "_set_vg_intent": 9,
            "_clear_vg_intent": 17,
        }
        for name, count in expected.items():
            calls = re.findall(rf"\$class->{name}\(\s*([^,]+),", source)
            self.assertEqual(len(calls), count, name)
            self.assertTrue(all(call.strip() == "$scfg" for call in calls), name)

        start = source.index("sub _vg_state_digest")
        end = source.index("sub _thick_require_transition_intent", start)
        family = source[start:end]
        self.assertGreaterEqual(
            family.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            4,
        )
        self.assertIn("setting mutation intent on VG", family)
        self.assertIn("clearing mutation intent on VG", family)
        self.assertIn("outcome is UNKNOWN", family)
        self.assertIn("continuing without retry", family)

    def test_foreign_admission_uses_persistent_only_transition_proof(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        admission_start = source.index("sub _thick_foreign_intent_admission")
        admission_end = source.index("sub _with_vg_lock", admission_start)
        admission = source[admission_start:admission_end]
        self.assertIn("$operation, $intent, $lvs, 1", admission)

        resume_start = source.index("sub _thick_resume_transition")
        resume_end = source.index(
            "sub _thick_reconstruct_missing_transition_runtime", resume_start
        )
        resume = source[resume_start:resume_end]
        persistent_return = resume.index("return $persistent if $persistent_only")
        runtime_probe = resume.index("$class->_thick_managed_mapper_present")
        anchor_mutation = resume.index("$class->_thick_transition_anchor", runtime_probe)
        self.assertLess(persistent_return, runtime_probe)
        self.assertLess(persistent_return, anchor_mutation)
        self.assertIn("before mapper inspection", resume)

    def test_thick_tree_delete_preflights_every_object_under_one_vg_lock(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        bodies = {
            match.group("name"): match.group("body")
            for match in re.finditer(
                r"^sub (?P<name>\S+) \{(?P<body>.*?)(?=^sub |\Z)",
                source, re.MULTILINE | re.DOTALL,
            )
        }
        wrapper = bodies["_thick_free_image"]
        self.assertEqual(wrapper.count("$class->_with_vg_lock("), 1)
        self.assertLess(wrapper.index("$class->_with_vg_lock("),
                        wrapper.index("$class->_require_no_vg_intent($scfg, $vg, $device)"))
        self.assertIn("$class->_thick_list_volumes_scoped($scfg, $vg, $device)", wrapper)
        self.assertLess(wrapper.index("$class->_thick_remove_unreferenced_tree_locked("),
                        wrapper.index("$class->_thick_free_image_single_locked("))
        tree = bodies["_thick_remove_unreferenced_tree_locked"]
        check = tree[tree.index("my $check = sub {"):tree.index("my $current = $check->();")]
        for guard in (
            "_verify_mutation_quorum($storeid, $scfg)",
            "_verify_storage_identity($storeid, $scfg, $device)",
            "_require_no_vg_intent($scfg, $vg, $device)",
            "_assert_no_active_storage_worker($vg)",
            "_thick_assert_tree_references($storeid, $volname, $admission)",
            "_thick_list_volumes_scoped($scfg, $vg, $device)",
            "_thick_verify_autoactivation_disabled($scfg, $vg, $name, $device)",
            "_thick_verify_snapshot_readonly($scfg, $vg, $name, $device)",
            "_thick_tree_check_kernel_rows($scfg, $volname, $current,",
        ):
            self.assertIn(guard, check)
        self.assertIn("for my $name (keys %remaining)", check)
        self.assertIn("for my $name (values %{$current->{snapshots}})", check)
        self.assertLess(tree.index("$class->_thick_tree_peer_absence("),
                        tree.index("$class->_thick_volume_snapshot_delete_locked("))
        for name in ("_thick_remove_unreferenced_tree_locked",
                     "_thick_volume_snapshot_delete_locked", "_thick_free_image_single_locked"):
            self.assertNotIn("$class->_with_vg_lock(", bodies[name])
        peers = bodies["_thick_tree_peer_absence"]
        self.assertIn("PVE::Cluster::get_nodelist()", peers)
        self.assertIn("ref($nodes) ne 'ARRAY'", peers)
        self.assertIn("@$nodes > 16", peers)
        self.assertIn("offline or unknown", peers)
        self.assertIn("+ 60", peers)
        self.assertIn("timeout => 5", bodies["_thick_tree_kernel_rows"])
        admission = bodies["_thick_tree_delete_admission"]
        self.assertIn("_thick_pve_reference_files($storeid, $volname)", admission)
        self.assertIn("_thick_validate_destroy_frames(", admission)
        self.assertIn("_thick_destroy_reference_digest(", admission)
        self.assertIn("_thick_destroy_reference_digest(", bodies["_thick_assert_tree_references"])
        self.assertIn("ne $admission->{digest}", bodies["_thick_assert_tree_references"])
        self.assertIn(
            "_thick_assert_tree_references($storeid, $volname, $tree_admission)",
            bodies["_thick_volume_snapshot_delete_locked"],
        )
        self.assertIn(
            "_thick_assert_tree_references($storeid, $volname, $admission)",
            bodies["_thick_free_image_single_locked"],
        )
        frames = bodies["_thick_validate_destroy_frames"]
        for symbol in ("PVE::Storage::vdisk_free", "PVE::QemuServer::destroy_vm",
                       "PVE::AbstractConfig::lock_config", "PVE::AbstractConfig::lock_config_full"):
            self.assertIn(symbol, frames)
        self.assertIn("::free_image", frames)
        self.assertIn("@DB::args", bodies["_thick_destroy_call_frames"])
        self.assertNotIn("/proc/", frames)

    def test_snapshot_delete_waits_for_one_exact_transition_outside_all_locks(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_wait_snapshot_delete_admission")
        end = source.index("sub _thick_volume_snapshot_delete", start)
        wait = source[start:end]
        for identity in (
            "anchor_uuid", "tx", "old", "new", "generation", "snapshot",
            "snapshot_uuid",
        ):
            self.assertIn(identity, wait)
        self.assertIn("_thick_progress_clock() + $budget", wait)
        self.assertIn("_thick_observation_pause($wait_ms)", wait)
        self.assertIn("no delete effect started", wait)
        self.assertNotIn("_with_vg_lock", wait)
        self.assertNotIn("_with_thick_transition_executor_lock", wait)
        self.assertNotIn("_thick_schedule_materialization", wait)
        self.assertNotIn("lvremove", wait)

        locked_start = source.index("sub _thick_volume_snapshot_delete_locked")
        locked_end = source.index("sub _thick_recover_orphan_tree", locked_start)
        locked = source[locked_start:locked_end]
        recheck = locked.index("snapshot-delete admission changed before")
        intent = locked.index("my %intent = (")
        remove = locked.index("'/sbin/lvremove'")
        self.assertLess(recheck, intent)
        self.assertLess(intent, remove)
        self.assertIn("snapshot identity changed after admission", locked)

    def test_lazy_allocation_capabilities_are_one_shot_and_rechecked_before_claim(self):
        source = (ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm").read_text(encoding="utf-8")
        bodies = {match.group("name"): match.group("body") for match in re.finditer(
            r"^sub (?P<name>\S+) \{(?P<body>.*?)(?=^sub |\Z)", source, re.MULTILINE | re.DOTALL)}
        allocation = bodies["_lazy_alloc_image"]
        self.assertLess(allocation.index("_lazy_prepare_move_allocation("), allocation.index("_with_vg_lock("))
        self.assertGreater(allocation.index("_lazy_record_move_allocation("), allocation.index("publication postcondition failed"))
        consume = bodies["_lazy_consume_move_allocation"]
        self.assertLess(consume.index("delete $lazy_move_allocation"), consume.index("_lazy_recheck_move_capability("))
        recheck = bodies["_lazy_recheck_move_capability"]
        for marker in ("$cap->{pid} != $$", "$cap->{expires}", "$cap->{anchor_digest}",
                       "_thick_pve_reference_files", "_lazy_move_context", "_lazy_move_source_policy"):
            self.assertIn(marker, recheck)
        restore_consume = bodies["_lazy_consume_unreferenced_allocation"]
        self.assertLess(
            restore_consume.index("delete $lazy_restore_allocation"),
            restore_consume.index("_lazy_recheck_restore_capability("),
        )
        dispatcher = bodies["_lazy_recheck_allocation_capability"]
        self.assertIn("$cap->{kind} eq 'move'", dispatcher)
        self.assertIn("$cap->{kind} eq 'restore'", dispatcher)
        self.assertIn("capability kind '$cap->{kind}' is invalid", dispatcher)
        activation = bodies["_lazy_activate_volume_locked"]
        claim = activation.index("phase => 'LAZY_CLAIMED'")
        locked_recheck = activation.index("_lazy_recheck_allocation_capability(")
        self.assertLess(activation.index("_with_vg_lock("), locked_recheck)
        self.assertLess(locked_recheck, claim)
        policy = bodies["_lazy_verify_guest_discard_config"]
        self.assertIn("if !$matched && $scfg && $state", policy)
        self.assertIn("if $matched != 1", policy)
        context = bodies["_lazy_move_context"]
        self.assertIn("$source->{running}", context)
        self.assertIn("defined($source->{snapname})", context)
        self.assertIn("/usr/share/perl5/PVE/API2/Qemu.pm", context)

    def test_every_thick_publication_resume_is_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        helper_start = source.index("sub _thick_resume_mapper_exact")
        helper_end = source.index("sub _thick_schedule_materialization", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertEqual(
            helper.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            1,
        )
        self.assertIn("'/sbin/dmsetup', '--verifyudev'", helper)
        self.assertIn("'resume', $mapper", helper)
        self.assertIn("_thick_mapper_is_suspended($mapper, $command_timeout)", helper)
        self.assertIn("exact live postcondition", helper)
        self.assertIn("no retry attempted", helper)
        self.assertIn("continuing without retry", helper)

        start = source.index("sub _thick_reconstruct_missing_transition_runtime")
        end = source.index("sub volume_snapshot", start)
        thick_runtime = source[start:end]
        resumes = re.findall(
            r"\$class->_thick_resume_mapper_exact\(\s*"
            r"\$scfg,\s*\$(front|mapper)",
            thick_runtime,
        )
        self.assertEqual(resumes, ["front", "front", "mapper", "mapper"])
        self.assertNotRegex(
            thick_runtime,
            r"'/sbin/dmsetup', '--verifyudev', 'resume'",
        )

    def test_every_thick_publication_suspend_is_reconciled_without_retry(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        helper_start = source.index("sub _thick_suspend_mapper_exact")
        helper_end = source.index("sub _thick_resume_mapper_exact", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertEqual(
            helper.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            1,
        )
        self.assertIn("'suspend', $mapper", helper)
        self.assertIn("_thick_mapper_is_suspended($mapper, $command_timeout)", helper)
        self.assertIn("exact suspended postcondition", helper)
        self.assertIn("no retry attempted", helper)
        self.assertIn("continuing without retry", helper)

        start = source.index("sub _thick_reconstruct_missing_transition_runtime")
        end = source.index("sub volume_snapshot", start)
        thick_runtime = source[start:end]
        suspends = re.findall(
            r"\$class->_thick_suspend_mapper_exact\(\s*"
            r"\$scfg,\s*\$(front|mapper)",
            thick_runtime,
        )
        self.assertEqual(suspends, ["front", "front", "mapper", "mapper"])
        self.assertNotRegex(
            thick_runtime,
            r"'/sbin/dmsetup', '--verifyudev', 'suspend'",
        )

    def test_every_thick_publication_reload_and_proof_is_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        helper_start = source.index("sub _thick_load_inactive_table_exact")
        helper_end = source.index("sub _thick_suspend_mapper_exact", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertEqual(
            helper.count("'/usr/bin/timeout', '--foreground', '--kill-after=5s'"),
            2,
        )
        self.assertIn("'/sbin/dmsetup', '--verifyudev'", helper)
        self.assertIn("'/sbin/dmsetup', 'table', '--inactive'", helper)
        self.assertIn("exact inactive-table", helper)
        self.assertIn("postcondition is unproven", helper)
        self.assertIn("no retry attempted", helper)
        self.assertIn("continuing without retry", helper)

        start = source.index("sub _thick_reconstruct_missing_transition_runtime")
        end = source.index("sub volume_snapshot", start)
        thick_runtime = source[start:end]
        calls = re.findall(
            r"\$class->_thick_load_inactive_table_exact\(\s*"
            r"\$scfg,\s*'(load|reload)',\s*\$(?:front|mapper)",
            thick_runtime,
        )
        self.assertEqual(calls, ["load", "reload", "reload", "reload"])
        self.assertNotRegex(
            thick_runtime,
            r"'/sbin/dmsetup', '--verifyudev', '(?:load|reload)'",
        )

    def test_every_direct_thick_dm_create_and_remove_has_exact_postcondition(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("sub _thick_activate_volume")
        end = source.index("sub volume_snapshot", start)
        thick_runtime = source[start:end]

        creates = list(
            re.finditer(r"'/sbin/dmsetup', '--verifyudev', 'create',", thick_runtime)
        )
        self.assertEqual(len(creates), 10)
        self.assertEqual(thick_runtime.count("$class->_thick_create_mapper_exact("), 10)
        create_calls = re.findall(
            r"\$class->_thick_create_mapper_exact\(\s*([^,]+),",
            thick_runtime,
        )
        self.assertTrue(all(call.strip() == "$scfg" for call in create_calls))
        for create in creates:
            following = thick_runtime[create.end():create.end() + 900]
            self.assertRegex(
                following,
                r"\$class->(?:_thick_verify_(?:frontend|source_mapper|clone_frontend)|_lazy_mapper_identity)\(",
            )

        removes = list(
            re.finditer(
                r"'/sbin/dmsetup', (?:'--verifyudev', )?'remove',",
                thick_runtime,
            )
        )
        self.assertEqual(len(removes), 7)
        self.assertEqual(thick_runtime.count("$class->_thick_remove_mapper_exact("), 7)
        remove_calls = re.findall(
            r"\$class->_thick_remove_mapper_exact\(\s*([^,]+),",
            thick_runtime,
        )
        self.assertTrue(all(call.strip() == "$scfg" for call in remove_calls))
        for remove in removes:
            following = thick_runtime[remove.end():remove.end() + 1800]
            self.assertRegex(
                following,
                r"(?:removal is unconfirmed|still exists after removal|remains after pivot|recovered Lazy .* remains)",
            )

        create_helper = source[
            source.index("sub _thick_create_mapper_exact"):
            source.index("sub _thick_remove_mapper_exact")
        ]
        remove_helper = source[
            source.index("sub _thick_remove_mapper_exact"):
            source.index("sub _thick_schedule_materialization")
        ]
        for helper in (create_helper, remove_helper):
            self.assertIn("$class->_thick_command_deadline($scfg)", helper)
            self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", helper)

    def test_async_materializer_and_activation_share_bounded_transition_latch(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        worker = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")

        lazy_start = source.index("sub _with_lazy_volume_executor_lock")
        thick_start = source.index("sub _with_thick_transition_executor_lock")
        lazy = source[lazy_start:thick_start]
        thick = source[thick_start:source.index("sub _lazy_deactivate_volume", thick_start)]
        self.assertIn("pve-sharedlvmthin-lazy-$storeid-$volname.lock", lazy)
        self.assertIn(
            "pve-sharedlvmthin-thick-transition-$storeid-$volname.lock", thick
        )
        self.assertNotIn("pve-sharedlvmthin-lazy-$storeid-$volname.lock", thick)
        self.assertIn("'slt-mutation-admission-timeout'", thick)
        self.assertIn("no activation effect was issued", thick)
        self.assertIn("F_SETFD, 0", thick)

        activation_start = source.index("sub _thick_activate_volume")
        activation_end = source.index("sub _lazy_activate_volume", activation_start)
        activation = source[activation_start:activation_end]
        self.assertLess(
            activation.index("_with_thick_transition_executor_lock"),
            activation.index("_thick_verify_published_transition_frontend"),
        )
        self.assertGreaterEqual(activation.count("_thick_read_anchor("), 2)
        self.assertIn("if $locked_state->{phase} eq 'MATERIALIZED'", activation)

        lazy_activation = source[
            source.index("sub _lazy_activate_volume_locked"):
            source.index("sub _thick_deactivate_volume")
        ]
        self.assertIn("($initial_state->{op} // '') =~ /^(?:SNAPSHOT|ROLLBACK)$/", lazy_activation)

        lock_call = worker.index("_with_thick_transition_executor_lock")
        snapshot_call = worker.index("_thick_volume_snapshot", lock_call)
        complete = worker.index("MATERIALIZATION_COMPLETE", snapshot_call)
        self.assertLess(lock_call, snapshot_call)
        self.assertLess(snapshot_call, complete)

    def test_thick_mapper_identity_postconditions_are_deadline_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        functions = (
            ("sub _thick_verify_frontend", "sub _thick_frontend_present"),
            ("sub _thick_verify_source_mapper", "sub _thick_mapper_is_suspended"),
            ("sub _thick_verify_clone_frontend", "sub _thick_wait_for_hydration"),
        )
        for start_marker, end_marker in functions:
            start = source.index(start_marker)
            body = source[start:source.index(end_marker, start)]
            probes = list(re.finditer(r"'/sbin/dmsetup', '(?:info|table|deps)'", body))
            self.assertEqual(len(probes), 3)
            self.assertIn("$class->_thick_command_deadline($scfg)", body)
            for probe in probes:
                prefix = body[max(0, probe.start() - 220):probe.start()]
                self.assertIn("'/usr/bin/timeout'", prefix)
                self.assertIn("'--foreground'", prefix)
                self.assertIn("'--kill-after=5s'", prefix)
                self.assertIn('"${command_timeout}s"', prefix)

        activation_start = source.index("sub _thick_activate_volume")
        activation_end = source.index("sub _thick_deactivate_volume", activation_start)
        activation = source[activation_start:activation_end]
        blockdev = activation.index("'/sbin/blockdev', '--getsz'")
        prefix = activation[max(0, blockdev - 240):blockdev]
        self.assertIn("'/usr/bin/timeout'", prefix)
        self.assertIn("$class->_thick_command_deadline($scfg) . 's'", prefix)

    def test_stable_thick_frontend_removal_has_an_absence_postcondition(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"(?s)dmsetup', 'remove', '--retry', \$mapper\].*?"
            r"removal is unconfirmed; .*?underlying LVs remain active.*?"
            r"_thick_verify_mapper_absent\(",
        )
        helper_start = source.index("sub _thick_verify_mapper_absent")
        helper_end = source.index("sub _thick_source_mapper_name", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("_dm_kernel_inventory($class->_thick_command_deadline($scfg))", helper)
        self.assertIn("exists($inventory->{$mapper})", helper)

        runtime_start = source.index("sub _thick_activate_volume")
        runtime_end = source.index("sub volume_snapshot", runtime_start)
        runtime = source[runtime_start:runtime_end]
        self.assertEqual(runtime.count("$class->_thick_verify_mapper_absent("), 12)

    def test_post_pivot_cleanup_deactivates_before_destructive_remove(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        cleanup = source[
            source.index("if ($tr->{state}->{phase} eq 'LINEAR_PIVOTED')") :
            source.index("sub _thick_free_image")
        ]
        self.assertRegex(
            cleanup,
            re.compile(
                r"_thick_deactivate_exact_lvs\(\s*\$scfg, \$vg, \$device,\s*"
                r'"deactivating detached dm-clone metadata failed",\s*\$tr->\{meta\},\s*\);'
                r".*?_thick_remove_exact_lv\(\s*\$scfg, \$vg, \$device, \$tr->\{meta\}",
                re.DOTALL,
            ),
        )
        self.assertEqual(cleanup.count("$class->_thick_remove_exact_lv("), 2)
        self.assertNotIn("['/sbin/lvremove'", cleanup)

        helper_start = source.index("sub _thick_remove_exact_lv")
        helper_end = source.index("sub _thick_fault_point", helper_start)
        helper = source[helper_start:helper_end]
        self.assertIn("$class->_thick_command_deadline($scfg)", helper)
        self.assertIn("'/usr/bin/timeout', '--foreground', '--kill-after=5s'", helper)
        self.assertIn("removal result is UNKNOWN", helper)
        self.assertIn("no retry attempted", helper)
        self.assertRegex(
            cleanup,
            re.compile(
                r"_thick_deactivate_exact_lvs\(\s*\$scfg, \$vg, \$device,\s*"
                r'"deactivating superseded rollback HEAD failed",\s*\$tr->\{old\},\s*\);'
                r".*?_thick_remove_exact_lv\(\s*\$scfg, \$vg, \$device, \$tr->\{old\}",
                re.DOTALL,
            ),
        )

    def test_snapshot_delete_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "thick-recover-delete <storage-id> <volume>",
            cli,
        )
        self.assertIn('--recover-delete "$2" "$3"', cli)
        self.assertIn("_thick_recover_snapshot_delete", worker)
        self.assertIn("SNAPSHOT_DELETE_RECOVERY_START", worker)

    def test_volume_delete_recovery_is_an_explicit_derived_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "thick-recover-volume-delete <storage-id> <volume>", cli
        )
        self.assertIn('--recover-volume-delete "$2" "$3"', cli)
        self.assertIn("VOLUME_DELETE_RECOVERY_START", worker)
        self.assertIn("_thick_recover_volume_delete", worker)
        self.assertIn("sub _thick_recover_volume_delete", plugin)

    def test_unpublished_prepare_recovery_is_an_explicit_derived_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-prepare <storage-id> <volume>", cli)
        self.assertIn('--recover-prepare "$2" "$3"', cli)
        self.assertIn("UNPUBLISHED_PREPARE_RECOVERY_START", worker)
        self.assertIn("_thick_recover_unpublished_prepare", worker)
        self.assertIn("sub _thick_recover_unpublished_prepare", plugin)

    def test_resize_recovery_is_an_explicit_derived_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        plugin = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-resize <storage-id> <volume>", cli)
        self.assertIn('--recover-resize "$2" "$3"', cli)
        self.assertIn("RESIZE_RECOVERY_START", worker)
        self.assertIn("_thick_recover_resize", worker)
        self.assertIn("sub _thick_recover_resize", plugin)
        self.assertNotIn("<old-size>", cli)
        self.assertNotIn("<new-size>", cli)

    def test_snapshot_delete_recovery_explains_stale_cluster_lock(self):
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("no recovery mutation was started", worker)
        self.assertIn("Do not remove or bypass the lock", worker)
        self.assertIn("Proxmox's 120-second", worker)
        self.assertIn("stale-lock window and re-run", worker)

    def test_empty_allocation_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-empty-alloc <storage-id> <volume>", cli)
        self.assertIn('--recover-empty-alloc "$2" "$3"', cli)
        self.assertIn("_thick_recover_empty_allocation", worker)
        self.assertIn("EMPTY_ALLOCATION_RECOVERY_START", worker)

    def test_partial_allocation_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-partial-alloc <storage-id> <volume>", cli)
        self.assertIn('--recover-partial-alloc "$2" "$3"', cli)
        self.assertIn("_thick_recover_partial_allocation", worker)
        self.assertIn("PARTIAL_ALLOCATION_RECOVERY_START", worker)

    def test_orphan_allocation_recovery_is_an_explicit_command(self):
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        worker = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-thick-materialize"
        ).read_text(encoding="utf-8")
        self.assertIn("thick-recover-orphan-alloc <storage-id> <volume>", cli)
        self.assertIn("thick-recover-orphan-tree <storage-id> <volume>", cli)
        self.assertIn("thin-recover-orphan <storage-id> <volume>", cli)
        self.assertIn('--recover-orphan-alloc "$2" "$3"', cli)
        self.assertIn('--recover-orphan-tree "$2" "$3"', cli)
        self.assertIn("ORPHAN_ALLOCATION_RECOVERY_START", worker)
        self.assertIn("ORPHAN_TREE_RECOVERY_START", worker)
        self.assertIn("_thick_recover_orphan_allocation($scfg, $storeid, $volname)", worker)
        self.assertIn("_thick_recover_orphan_tree($scfg, $storeid, $volname)", worker)
        self.assertIn("_thin_recover_orphan($scfg, $storeid, $volname)", worker)

    def test_snapshot_delete_crash_hooks_remain_production_inert(self):
        plugin = (
            ROOT
            / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        for point in range(5):
            self.assertEqual(
                plugin.count(f"_thick_fault_point('D{point}'"),
                1,
                f"D{point} must identify exactly one snapshot-delete boundary",
            )
        self.assertIn("sub _thick_fault_point {\n    return;\n}", plugin)

    def test_all_thin_metadata_mutations_enter_the_canonical_lock_helper(self):
        plugin = (
            ROOT
            / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        public_mutations = (
            "alloc_image",
            "free_image",
            "volume_resize",
            "volume_snapshot",
            "volume_snapshot_delete",
            "volume_snapshot_rollback",
        )
        for name in public_mutations:
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing public mutation {name}")
            self.assertIn(
                "_with_mutation_lock",
                match.group("body"),
                f"{name} must serialize with same-VG thin and thick aliases",
            )

    def test_thick_lvm_commands_are_scoped_to_the_pinned_mapper(self):
        plugin = (
            ROOT
            / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        thick_lifecycle = (
            "_thick_alloc_image",
            "_thick_activate_volume",
            "_thick_deactivate_volume",
            "_thick_free_image",
            "_thick_volume_resize",
            "_thick_volume_snapshot",
            "_thick_volume_snapshot_delete",
            "_thick_recover_snapshot_delete",
        )
        checked = 0
        for name in thick_lifecycle:
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing Thick Generations lifecycle {name}")
            for command in re.findall(r"\[(.*?)\]", match.group("body"), re.DOTALL):
                if not re.search(r"'/sbin/(?:lv|vg)(?:create|remove|extend|change|convert)'", command):
                    continue
                checked += 1
                self.assertIn(
                    "'--devices', $device",
                    command,
                    f"{name} contains a global LVM command: {command.strip()}",
                )

        self.assertGreater(checked, 0)

        scoped_inventory_paths = thick_lifecycle + (
            "_thick_read_anchor",
            "_thick_find_snapshot",
            "_thick_list_images",
            "_thick_verify_allocation_state",
            "_thick_resume_transition",
        )
        for name in scoped_inventory_paths:
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            self.assertIsNotNone(match, f"missing Thick Generations inventory path {name}")
            self.assertNotIn(
                "PVE::Storage::LVMPlugin::lvm_list_volumes",
                match.group("body"),
                f"{name} can still issue a global LVM inventory",
            )

        for name in ("_thick_activate_volume", "_thick_deactivate_volume"):
            match = re.search(
                rf"sub {name} \{{(?P<body>.*?)(?=\nsub )",
                plugin,
                re.DOTALL,
            )
            body = match.group("body")
            self.assertLess(
                body.index("_verify_storage_identity"),
                body.index("_thick_list_volumes_scoped"),
                f"{name} must prove storage identity before its first LVM inventory",
            )

    def test_fresh_lv_creation_is_noninteractive_about_stale_signatures(self):
        source = (ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm").read_text(
            encoding="utf-8"
        )
        commands = re.findall(
            r"\[(?:\s*)'/sbin/lvcreate'(?P<body>.*?)\]",
            source,
            flags=re.S,
        )
        commands.extend(
            re.findall(
                r"my @(?:head|anchor|new|meta)_create = \(\s*"
                r"(?:'/usr/bin/timeout'.*?"
                r"\$class->_thick_command_deadline\(\$scfg\) \. 's',\s*)?"
                r"'/sbin/lvcreate'(?P<body>.*?)\);",
                source,
                flags=re.S,
            )
        )
        fresh = [
            body for body in commands
            if ("'-L'" in body and "'-s'" not in body)
            or ("'-V'" in body and "'--thinpool'" in body)
        ]
        self.assertGreaterEqual(len(fresh), 6)
        for body in fresh:
            self.assertIn("'--yes'", body)
            self.assertIn("'--wipesignatures', 'y'", body)
            if "'--setactivationskip', 'y'" in body:
                self.assertIn("'--ignoreactivationskip'", body)

        snapshots = [body for body in commands if "'-s'" in body]
        self.assertGreaterEqual(len(snapshots), 2)
        for body in snapshots:
            self.assertNotIn("'--wipesignatures'", body)

    def test_thick_allocation_creates_owned_non_autoactivated_objects_atomically(self):
        source = (ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm").read_text(
            encoding="utf-8"
        )
        match = re.search(
            r"sub _thick_alloc_image \{(?P<body>.*?)\n\}\n\nsub _verify_storage_identity",
            source,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        body = match.group("body")
        prepared = body[:body.index("    eval {")]
        self.assertGreaterEqual(prepared.count("'--setautoactivation', 'n'"), 2)
        self.assertIn("push @head_create, map { ('--addtag', $_) } @$head_tags", prepared)
        self.assertIn("push @anchor_create, map { ('--addtag', $_) } @$anchor_tags", prepared)
        self.assertNotIn("_change_exact_tags", prepared)
        self.assertNotIn("_disable_and_verify_autoactivation", prepared)

    def test_thick_transition_objects_are_owned_atomically_at_lvcreate(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        match = re.search(
            r"sub _thick_volume_snapshot \{(?P<body>.*?)(?=\nsub _thick_free_image)",
            source,
            flags=re.S,
        )
        self.assertIsNotNone(match)
        before_c2 = match.group("body")[:match.group("body").index(
            "$class->_thick_fault_point('C2'"
        )]
        self.assertGreaterEqual(before_c2.count("'--setautoactivation', 'n'"), 2)
        self.assertIn(
            "push @new_create, map { ('--addtag', $_) } @$new_tags", before_c2
        )
        self.assertIn(
            "push @meta_create, map { ('--addtag', $_) } @$meta_tags", before_c2
        )
        self.assertNotIn("tagging thick snapshot destination", before_c2)
        self.assertNotIn("tagging dm-clone metadata", before_c2)

    def test_package_prunes_only_initramfs_staging_lvm_recovery_copies(self):
        hook = ROOT / "usr/share/initramfs-tools/hooks/zz-pve-sharedlvmthin-lvm-prune"
        source = hook.read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")

        self.assertIn('PREREQ="lvm2"', source)
        self.assertIn('/var/tmp/mkinitramfs_*|/tmp/mkinitramfs_*', source)
        self.assertIn('$DESTDIR/etc/lvm/archive', source)
        self.assertIn('$DESTDIR/etc/lvm/backup', source)
        self.assertNotIn('rm -rf -- /etc/lvm', source)
        self.assertIn("zz-pve-sharedlvmthin-lvm-prune", build)
        self.assertIn("update-initramfs -u -k all", postinst)
        self.assertIn("lsinitramfs", postinst)
        self.assertIn("^etc/lvm/(archive|backup)/", postinst)
        self.assertIn("rebuild skipped", postinst)

    def test_initramfs_rebuild_failure_is_not_ignored(self):
        postinst = (ROOT / "DEBIAN/postinst").read_text(encoding="utf-8")
        rebuild = postinst.index("update-initramfs -u -k all")
        preflight = postinst.index("HEALTH_OK=0")
        self.assertLess(rebuild, preflight)
        self.assertNotIn("update-initramfs -u -k all || true", postinst)

    def test_compatibility_gate_is_packaged_and_checks_runtime_contract(self):
        checker = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-compat-check"
        source = checker.read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertIn("api-and-hook-contract", source)
        self.assertIn("volume_snapshot_rollback", source)
        self.assertIn("initramfs-no-host-lvm-recovery-copies", source)
        self.assertIn("sharedlvmthin-recovery-check", source)
        self.assertIn("sharedlvmthin-compat-check", build)
        self.assertIn("volume_snapshot_needs_fsfreeze", source)
        self.assertIn("compat-check)", cli)
        self.assertIn("modprobe --dry-run --show-depends dm-clone", source)
        self.assertIn("dmsetup targets", source)
        self.assertIn("unsupported-version", source)
        self.assertIn('lsinitramfs "$image" >"$initrd_inventory"', source)
        self.assertIn("FAIL=initramfs-inventory-unavailable", source)
        self.assertNotIn("lsinitramfs \"$image\" 2>/dev/null |", source)
        self.assertIn('awk \'', source)
        self.assertIn('>"$storage_inventory"', source)
        self.assertNotIn("done < <(awk", source)

    def test_hard_failover_audit_is_exact_read_only_and_pipefail_safe(self):
        audit = (
            ROOT / "experiments/thin-guard/bulk-hard-failover-audit.sh"
        ).read_text(encoding="utf-8")
        relocate = (
            ROOT / "experiments/thin-guard/bulk-ha-relocate.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("HARD_FAILOVER_AUDIT_FAILURES", audit)
        self.assertIn("pve-slt-owner-node-$node", audit)
        self.assertIn("mapper_count != 1", audit)
        self.assertIn("/sbin/dmsetup", audit)
        self.assertNotIn('grep -Fxq "$mapper"', audit)
        self.assertNotIn("lvchange", audit)
        self.assertNotIn("dmsetup remove", audit)
        self.assertIn("HA_RELOCATION_SUBMIT_FAILURES", relocate)
        self.assertIn("timeout --foreground --kill-after=5 60", relocate)

    def test_control_plane_admission_model_is_shared_and_explicitly_unreleased(self):
        admission = ROOT / "usr/share/perl5/PVE/SharedLvmAdmission.pm"
        release_check = (ROOT / "scripts/check-release.sh").read_text(
            encoding="utf-8"
        )
        design = (ROOT / "docs/control-plane-mutation-admission.md").read_text(
            encoding="utf-8"
        )
        compatibility = (ROOT / "docs/upstream-compatibility-gate.md").read_text(
            encoding="utf-8"
        )

        self.assertTrue(admission.is_file())
        self.assertIn("usr/share/perl5/PVE/SharedLvmAdmission.pm", release_check)
        self.assertIn("not implemented or released", design)
        self.assertIn("COMPATIBLE", compatibility)
        self.assertIn("RETEST_REQUIRED", compatibility)
        self.assertIn("BLOCKED", compatibility)
        self.assertIn("both DUAL and Thick-only", compatibility)

    def test_upstream_inventory_is_packaged_read_only_and_not_cluster_authority(self):
        inventory = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-upstream-inventory"
        ).read_text(encoding="utf-8")
        build = (ROOT / "scripts/build.sh").read_text(encoding="utf-8")
        release = (ROOT / "scripts/check-release.sh").read_text(encoding="utf-8")
        cli = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")

        self.assertIn('"kind": "inventory"', inventory)
        self.assertIn('baseline.get("kind") != "qualification"', inventory)
        self.assertIn('"cluster_upgrade_authorized": False', inventory)
        self.assertNotIn("apt-get", inventory)
        self.assertIn('"modprobe"', inventory)
        self.assertNotIn("systemctl restart", inventory)
        self.assertIn("sharedlvmthin-upstream-inventory", build)
        self.assertIn("sharedlvmthin-upstream-inventory", release)
        self.assertIn("upstream-inventory)", cli)
        self.assertIn("sharedlvmthin-candidate-inspect", build)
        self.assertIn("sharedlvmthin-candidate-inspect", release)
        self.assertIn("candidate-inspect)", cli)
        self.assertIn("sharedlvmthin-contract-check", build)
        self.assertIn("sharedlvmthin-contract-check", release)
        self.assertIn("contract-check)", cli)
        self.assertIn("sharedlvmthin-compat-gate", build)
        self.assertIn("sharedlvmthin-compat-gate", release)
        self.assertIn("compatibility-gate)", cli)
        self.assertIn("sharedlvmthin-compat-aggregate", build)
        self.assertIn("sharedlvmthin-compat-aggregate", release)
        self.assertIn("compatibility-aggregate)", cli)
        self.assertIn("sharedlvmthin-lab-evidence-check", build)
        self.assertIn("sharedlvmthin-lab-evidence-check", release)
        self.assertIn("lab-evidence-check)", cli)
        self.assertTrue(
            (ROOT / "usr/share/pve-sharedlvmthin/pve-compatibility-contracts.json").is_file()
        )
        self.assertTrue(
            (ROOT / "usr/share/pve-sharedlvmthin/pve-lab-scenarios.json").is_file()
        )
        self.assertIn("--scenario-registry", release)

    def test_snapshot_orchestration_contract_is_detected_without_pve_mutation(self):
        update_plan = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-plan"
        ).read_text(encoding="utf-8")
        fixture = (
            ROOT / "experiments/thick-generations/qemu-snapshot-orchestration-fixture.pl"
        ).read_text(encoding="utf-8")
        manifest = json.loads(
            (ROOT / "usr/share/pve-sharedlvmthin/pve-qualified-tuples.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertIn("scan_qemu_snapshot_contract", update_plan)
        self.assertIn("VMSTATE_ALLOCATION_BEFORE_RUNTIME_QUERIES", update_plan)
        self.assertIn("SAVEVM_END_DEACTIVATION_COUPLED", update_plan)
        self.assertIn("SAVEVM_FINALIZE_POLL_UNBOUNDED", update_plan)
        self.assertIn("EMPTY_RUNNING_NETS_HOST_MTU_WRITABLE", update_plan)
        self.assertNotIn("write_text", fixture)
        self.assertIn("VMSTATE_QUERY_ORDER=", fixture)
        self.assertIn("savevm-end-injected", fixture)
        self.assertIn("timeout", fixture)
        for tuple_id in (
            "tg53-api15-upstream-9.2.20-k17",
            "tg53-api15-upstream-9.2.20-k19",
        ):
            row = next(item for item in manifest["tuples"] if item["id"] == tuple_id)
            self.assertIn("snapshot-qmp-failure-prefix", row["required_tests"])
            self.assertIn("snapshot-finalize-bounded-wait", row["required_tests"])


if __name__ == "__main__":
    unittest.main()


