import hashlib
import ipaddress
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PackageSourceTests(unittest.TestCase):
    def _run_thick_only_preinst_inventory(self, *, vg_attr="wz--n-"):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        storage = root / "storage.cfg"
        storage.write_text("dir: local\n        path /tmp\n", encoding="utf-8")
        marker = root / "package-flavor"
        commands = {
            "vgs": f"#!/bin/sh\nprintf '%s\\n' 'shared-vg|{vg_attr}'\n",
            "lvs": "#!/bin/sh\nexit 0\n",
        }
        for name, text in commands.items():
            command = root / name
            command.write_text(text, encoding="utf-8")
            command.chmod(0o755)
        source = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(marker)
        ).replace("STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}")
        source = source.replace(
            "# P0 upgrade fence for the exclusive Thin owner schema.",
            "exit 0\n\n# P0 upgrade fence for the exclusive Thin owner schema.",
        )
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

        source = (ROOT / "DEBIAN/preinst").read_text(encoding="utf-8")
        source = source.replace(
            "/usr/share/pve-sharedlvmthin/package-flavor", str(marker)
        ).replace("STORAGECFG=/etc/pve/storage.cfg", f"STORAGECFG={storage}")
        source = source.replace(
            "# The Thick-only package must never silently strand",
            "exit 0\n\n# The Thick-only package must never silently strand",
        )
        candidate = root / "preinst"
        candidate.write_text(source, encoding="utf-8")
        candidate.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{root}{os.pathsep}{env['PATH']}"
        env["DPKG_MAINTSCRIPT_PACKAGE"] = "pve-sharedlvmthin"
        result = subprocess.run(
            ["/bin/sh", str(candidate), action], env=env, text=True,
            capture_output=True, check=False,
        )
        invoked = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
        return result, invoked

    def _run_package_profile_gate(self, *, active_units=""):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        temp_path = Path(temp.name)
        package = temp_path / "candidate.deb"
        package.write_bytes(b"qualification-candidate\n")
        log = temp_path / "dpkg.log"

        commands = {
            "dpkg-deb": """#!/bin/sh
case "$3" in
    Package) printf '%s\\n' pve-sharedlvmthin-thick ;;
    Version) printf '%s\\n' 0.9.0~rc5.11~tg32 ;;
    Architecture) printf '%s\\n' all ;;
    *) exit 2 ;;
esac
""",
            "dpkg-query": "#!/bin/sh\nexit 1\n",
            "systemctl": """#!/bin/sh
if [ "$1" = list-units ]; then printf '%s' "$ACTIVE_UNITS"; exit 0; fi
exit 2
""",
            "dpkg": """#!/bin/sh
if [ "$1" = --audit ]; then exit 0; fi
printf '%s\\n' "$*" >>"$DPKG_LOG"
exit 0
""",
        }
        for name, source in commands.items():
            command = temp_path / name
            command.write_text(source, encoding="utf-8")
            command.chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{temp.name}{os.pathsep}{env['PATH']}"
        env["ACTIVE_UNITS"] = active_units
        env["DPKG_LOG"] = str(log)
        digest = hashlib.sha256(package.read_bytes()).hexdigest()
        hostname = subprocess.run(
            ["hostname"], text=True, capture_output=True, check=True
        ).stdout.strip()
        result = subprocess.run(
            [
                "/bin/bash",
                str(ROOT / "experiments/thick-generations/package-profile-gate.sh"),
                "--package",
                str(package),
                "--sha256",
                digest,
                "--expect-host",
                hostname,
            ],
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, log

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
                "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-'\n",
                encoding="utf-8",
            )
            vgs.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
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
    def test_inactive_guard_does_not_allow_managed_thin_package_removal(self):
        with tempfile.TemporaryDirectory() as temp:
            temp_path = Path(temp)
            log = temp_path / "systemctl.log"
            commands = {
                "systemctl": (
                    "#!/bin/sh\nprintf '%s\\n' \"$*\" >>\"$SYSTEMCTL_LOG\"\n"
                    "if [ \"$1\" = is-active ]; then exit 3; fi\nexit 0\n"
                ),
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-'\n",
                "lvs": "#!/bin/sh\nprintf '%s\\n' 'pve-slt-sid-test'\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
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
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz-pn-'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
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
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
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
                "vgs": "#!/bin/sh\nprintf '%s\\n' 'shared-vg|wz--n-'\n",
                "lvs": "#!/bin/sh\nexit 0\n",
            }
            for name, source in commands.items():
                command = temp_path / name
                command.write_text(source, encoding="utf-8")
                command.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{temp}{os.pathsep}{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
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
    def test_preinst_audits_disabled_storage_with_candidate_checker(self):
        result, invoked = self._run_preinst_candidate_audit(recovery_safe=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(invoked, ["disabled-thick"])
        self.assertIn("STATE=HEALTHY", result.stdout)

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
    def test_thick_only_preinst_refuses_partial_vg_inventory(self):
        result = self._run_thick_only_preinst_inventory(vg_attr="wz-pn-")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("LVM reports a partial", result.stderr)
        self.assertIn("absence of managed Thin objects is unproven", result.stderr)

    @unittest.skipUnless(os.name == "posix", "maintainer scripts require POSIX sh")
    def test_thick_only_preinst_accepts_complete_empty_thin_inventory(self):
        result = self._run_thick_only_preinst_inventory()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

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
        self.assertIn("rollback outcome is UNKNOWN", plugin)
        self.assertIn("continuing without retry", plugin)
        self.assertNotIn("if ($origin_removed)", plugin)

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
        self.assertIn("RECOVERY_PLAN=FINALIZE_THIN", source)
        self.assertIn("target disk topology is not exactly finalizable Thin", source)
        self.assertIn("BRIDGE_ADMISSION_ACTIVE=NO", source)
        self.assertIn("disk_manifest_sha256", source)
        self.assertIn("disk manifest digest mismatch", source)
        self.assertIn("CONTINUE_MATERIALIZE", source)
        self.assertIn("resume_progress_move", source)
        self.assertIn("insufficient capacity to resume materialization", source)
        self.assertIn("slt-bridge-admission-timeout", source)
        self.assertIn("admission_poll_seconds", source)
        self.assertIn("admission_last_heartbeat", source)
        planner = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-bridge-plan"
        ).read_text(encoding="utf-8")
        self.assertIn("CONTINUE_MATERIALIZE", planner)
        self.assertIn("CONTINUE_RETURN_THIN", planner)
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
        self.assertIn("sharedlvmthin-qmp-path-check", source)
        self.assertIn("RUNTIME_CONFIG_DIVERGENCE", source)
        self.assertIn("ssh_stream_base", source)
        self.assertIn("ssh_base=(/usr/bin/ssh -n", source)
        qmp_check = (
            ROOT
            / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-qmp-path-check"
        ).read_text(encoding="utf-8")
        self.assertIn('qmp_command(stream, "query-block")', qmp_check)
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
            "SharedLvmThinGuardEngine.pm",
            "SharedLvmThinMobility.pm",
            "SharedLvmThinRelay.pm",
            "SharedLvmThinWatchdog.pm",
        ):
            self.assertIn('"$STAGE/usr', build)
            self.assertIn(excluded, build)
            self.assertIn(excluded, release_gate)
        self.assertIn("sub _package_flavor", plugin)
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
        self.assertIn('"${1:-}" = "upgrade"', preinst)
        self.assertIn("grep -q '^sharedlvmthin:[[:space:]]'", preinst)
        self.assertIn('candidate_args="--preinstall"', preinst)
        self.assertIn("if ($2 in seen) exit 3", preinst)
        self.assertIn("candidate storage configuration is ambiguous", preinst)
        self.assertIn("not positively recovery-safe under candidate rules", preinst)
        self.assertIn("candidate recovery fence refused", preinst)
        self.assertIn("THICK_ANCHORS_HEALTHY=PASS", preinst)
        self.assertIn("VG_INTENT_CLEAR=PASS", preinst)
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
        common_chmod = postinst.index('chmod 0755 "$MATERIALIZER"')
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
        self.assertIn("OTHER_STATUS=$(dpkg-query", postrm)
        self.assertIn("installed $OTHER_PACKAGE conflicts with package-flavor marker", postrm)
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

        reproducible = (
            ROOT / "scripts/check-reproducible-packages.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("build_pair dual", reproducible)
        self.assertIn("build_pair thick-only", reproducible)
        self.assertIn('cmp -s "$first_deb" "$second_deb"', reproducible)
        self.assertIn("reproducible package builds: PASS", reproducible)

        profile_gate = (
            ROOT / "experiments/thick-generations/package-profile-gate.sh"
        ).read_text(encoding="utf-8")
        self.assertIn("RESULT=DRY_RUN_PASS", profile_gate)
        self.assertIn("RESULT=EXECUTE_PASS", profile_gate)
        self.assertIn("dpkg --no-act -i", profile_gate)
        self.assertIn('dpkg -i "$package"', profile_gate)
        self.assertIn("dpkg --verify-format=rpm --verify", profile_gate)
        self.assertIn("installed package files differ", profile_gate)
        self.assertIn("dpkg database reports unfinished", profile_gate)
        self.assertIn("profile replacement requires identical package versions", profile_gate)
        self.assertIn("opposite package profile remains installed", profile_gate)
        self.assertIn("installed package flavor marker does not match package identity", profile_gate)
        self.assertIn("installed CLI is missing thick-recover-prepare", profile_gate)
        self.assertNotIn(r"\${", profile_gate)
        self.assertIn("Reboot was not performed", profile_gate)
        self.assertNotIn("apt-get", profile_gate)
        self.assertNotIn("reboot -", profile_gate)

        release_gate = (ROOT / "docs/thick-generations-release-gate.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("| Thick-only clean install |", release_gate)
        self.assertIn("| Package profile replacement |", release_gate)
        self.assertIn("| Package rolling update |", release_gate)
        self.assertIn("| Package removal fence |", release_gate)
        self.assertIn("Published TG32 versus audit candidate", release_gate)
        self.assertIn("not rebuilt or silently replaced", release_gate)
        self.assertIn("| PREPARE cleanup recovery |", release_gate)
        self.assertIn("| Materialization admission |", release_gate)
        self.assertIn("| Worker scheduling ambiguity |", release_gate)
        self.assertIn("| Device-scoped capacity |", release_gate)
        self.assertIn("| Frontend-removal postcondition |", release_gate)
        self.assertIn("| Kernel dm-clone gate |", release_gate)
        self.assertIn("RUNTIME ROWS REMAIN OPEN", release_gate)

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
        self.assertNotIn("lvremove", worker)
        self.assertNotIn("pvcreate", worker)

    def test_thick_rollback_snapshot_permission_probe_is_device_scoped(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "_thick_verify_snapshot_readonly($vg, $source, $device)", source
        )
        self.assertNotIn(
            "_thick_verify_snapshot_readonly($vg, $source);", source
        )

    def test_linear_pivot_is_resumable_from_an_already_suspended_clone(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        start = source.index("if ($tr->{state}->{phase} eq 'HYDRATION_COMPLETE')")
        end = source.index("if ($tr->{state}->{phase} eq 'LINEAR_PIVOTED')", start)
        pivot = source[start:end]
        self.assertIn("my $already_suspended =", pivot)
        self.assertIn("if (!$already_suspended)", pivot)
        self.assertIn("did not enter suspended state before linear pivot", pivot)
        status_checks = list(
            re.finditer(
                r"_thick_verify_clone_status\(\s*\$front,\s*1,\s*"
                r"(?:int\(\$tr->\{size\} / 512\)|\$sectors),\s*"
                r"\$tr->\{geometry\}->\{region_sectors\},\s*\)",
                pivot,
            )
        )
        self.assertGreaterEqual(len(status_checks), 2)
        self.assertLess(
            pivot.index("did not enter suspended state before linear pivot"),
            status_checks[-1].start(),
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
        self.assertIn("'vg_uuid,lv_uuid,lv_name'", source)
        self.assertIn('my $expected_uuid = "LVM-$vg_uuid$lv_uuid"', source)
        self.assertEqual(source.count("['/sbin/lvchange', '--devices', $device, '-ay'"), 1)
        self.assertEqual(source.count("_thick_activate_exact_lvs("), 11)
        self.assertEqual(source.count("_thick_verify_active_lv_identity("), 4)
        self.assertEqual(source.count("['/sbin/lvchange', '--devices', $device, '-an'"), 1)
        self.assertEqual(source.count("_thick_deactivate_exact_lvs("), 13)

    def test_stable_thick_frontend_removal_has_an_absence_postcondition(self):
        source = (
            ROOT / "usr/share/perl5/PVE/Storage/Custom/SharedLvmThinPlugin.pm"
        ).read_text(encoding="utf-8")
        self.assertRegex(
            source,
            r"(?s)dmsetup', 'remove', '--retry', \$mapper\].*?"
            r"removal is unconfirmed; .*?underlying LVs remain active.*?"
            r"_block_device_exists\(\"/dev/mapper/\$mapper\"\)",
        )

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
                r"_thick_deactivate_exact_lvs\(\s*\$vg, \$device,\s*"
                r'"deactivating detached dm-clone metadata failed",\s*\$tr->\{meta\},\s*\);'
                r".*?lvremove.*?\$vg/\$tr->\{meta\}",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            cleanup,
            re.compile(
                r"_thick_deactivate_exact_lvs\(\s*\$vg, \$device,\s*"
                r'"deactivating superseded rollback HEAD failed",\s*\$tr->\{old\},\s*\);'
                r".*?lvremove.*?\$vg/\$tr->\{old\}",
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
        self.assertIn("_thick_free_image($storeid, $scfg, $volname, 0, 1)", worker)
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
                r"my @(?:head|anchor|new|meta)_create = \(\s*'/sbin/lvcreate'(?P<body>.*?)\);",
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


if __name__ == "__main__":
    unittest.main()


