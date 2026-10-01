import importlib.machinery
import importlib.util
import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy"
CORE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py"
APT_GUARD = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-apt-guard"


def load_script():
    loader = importlib.machinery.SourceFileLoader("slt_update_policy_cli", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    core_spec = importlib.util.spec_from_file_location("slt_update_policy_core_test", CORE)
    core = importlib.util.module_from_spec(core_spec)
    core_spec.loader.exec_module(core)
    return module, core


def load_apt_guard():
    loader = importlib.machinery.SourceFileLoader("slt_apt_guard_test", str(APT_GUARD))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    core_spec = importlib.util.spec_from_file_location("slt_apt_guard_core_test", CORE)
    core = importlib.util.module_from_spec(core_spec)
    core_spec.loader.exec_module(core)
    return module, core


class UpdatePolicyRuntimeTests(unittest.TestCase):
    def test_new_binary_from_watched_source_family_is_not_unrelated(self):
        guard, core = load_apt_guard()
        manifest = {
            "watched_packages": ["lvm2"],
            "watched_package_patterns": [],
            "watched_source_packages": ["lvm2"],
            "watched_source_patterns": [],
        }
        action = {
            "package": "liblvm-future1", "old_version": "-", "old_arch": "-",
            "old_multiarch": "-", "direction": "<", "new_version": "2.4-1",
            "new_arch": "amd64", "new_multiarch": "same",
            "action": "/var/cache/apt/archives/liblvm-future1_2.4-1_amd64.deb",
        }
        control = ("Package: liblvm-future1\nVersion: 2.4-1\nArchitecture: amd64\n"
                   "Source: lvm2 (2.4-1)\nMulti-Arch: same\n\n")
        completed = mock.Mock(returncode=0, stdout=control, stderr="")
        with mock.patch.object(guard, "run", return_value=completed):
            extended = guard.extend_manifest_for_source_families(
                {"protocol": 3, "actions": [action]}, manifest, core
            )
        self.assertIn("liblvm-future1", extended["watched_packages"])
        self.assertEqual(core.relevant_actions(
            {"protocol": 3, "actions": [action]}, extended), [action])

    def test_truly_unrelated_payload_remains_unrelated(self):
        guard, core = load_apt_guard()
        manifest = {
            "watched_packages": ["lvm2"], "watched_package_patterns": [],
            "watched_source_packages": ["lvm2"], "watched_source_patterns": [],
        }
        action = {
            "package": "bash", "old_version": "1", "old_arch": "amd64",
            "old_multiarch": "no", "direction": "<", "new_version": "2",
            "new_arch": "amd64", "new_multiarch": "no",
            "action": "/var/cache/apt/archives/bash_2_amd64.deb",
        }
        control = "Package: bash\nVersion: 2\nArchitecture: amd64\n\n"
        with mock.patch.object(
                guard, "run", return_value=mock.Mock(returncode=0, stdout=control, stderr="")):
            extended = guard.extend_manifest_for_source_families(
                {"protocol": 3, "actions": [action]}, manifest, core
            )
        self.assertIs(extended, manifest)
        self.assertEqual(core.relevant_actions(
            {"protocol": 3, "actions": [action]}, extended), [])

    def test_payload_protocol_identity_mismatch_fails_closed(self):
        guard, core = load_apt_guard()
        manifest = {
            "watched_packages": [], "watched_package_patterns": [],
            "watched_source_packages": ["lvm2"], "watched_source_patterns": [],
        }
        action = {
            "package": "liblvm-future1", "old_version": "-", "old_arch": "-",
            "old_multiarch": "-", "direction": "<", "new_version": "2.4-1",
            "new_arch": "amd64", "new_multiarch": "same", "action": "/payload.deb",
        }
        control = ("Package: different-package\nVersion: 2.4-1\nArchitecture: amd64\n"
                   "Source: lvm2\n\n")
        with mock.patch.object(
                guard, "run", return_value=mock.Mock(returncode=0, stdout=control, stderr="")):
            with self.assertRaisesRegex(RuntimeError, "identity differs"):
                guard.extend_manifest_for_source_families(
                    {"protocol": 3, "actions": [action]}, manifest, core
                )

    def test_payload_source_promotion_precedes_its_configure_row(self):
        guard, core = load_apt_guard()
        manifest = {
            "watched_packages": [], "watched_package_patterns": [],
            "watched_source_packages": ["lvm2"], "watched_source_patterns": [],
        }
        payload = {
            "package": "liblvm-future1", "old_version": "-", "old_arch": "-",
            "old_multiarch": "-", "direction": "<", "new_version": "2.4-1",
            "new_arch": "amd64", "new_multiarch": "same", "action": "/payload.deb",
        }
        configure = dict(payload, action="**CONFIGURE**")
        control = ("Package: liblvm-future1\nVersion: 2.4-1\nArchitecture: amd64\n"
                   "Source: lvm2\n\n")
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "dpkg-deb":
                return mock.Mock(returncode=0, stdout=control, stderr="")
            raise AssertionError("configure row must reuse authenticated payload family")

        with mock.patch.object(guard, "run", side_effect=run):
            extended = guard.extend_manifest_for_source_families(
                {"protocol": 3, "actions": [configure, payload]}, manifest, core
            )
        self.assertIn("liblvm-future1", extended["watched_packages"])
        self.assertEqual(len(calls), 1)

    def test_unrelated_configure_row_reuses_authenticated_archive_source(self):
        guard, core = load_apt_guard()
        manifest = {
            "watched_packages": [], "watched_package_patterns": [],
            "watched_source_packages": ["lvm2"], "watched_source_patterns": [],
        }
        payload = {
            "package": "bash", "old_version": "1", "old_arch": "amd64",
            "old_multiarch": "no", "direction": "<", "new_version": "2",
            "new_arch": "amd64", "new_multiarch": "no", "action": "/bash.deb",
        }
        configure = dict(payload, action="**CONFIGURE**")
        control = "Package: bash\nVersion: 2\nArchitecture: amd64\n\n"
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if argv[0] == "dpkg-deb":
                return mock.Mock(returncode=0, stdout=control, stderr="")
            raise AssertionError("configure row must reuse its authenticated archive source")

        with mock.patch.object(guard, "run", side_effect=run):
            extended = guard.extend_manifest_for_source_families(
                {"protocol": 3, "actions": [configure, payload]}, manifest, core
            )
        self.assertIs(extended, manifest)
        self.assertEqual(len(calls), 1)

    def test_source_family_classification_has_one_global_deadline(self):
        guard, _core = load_apt_guard()
        action = {
            "package": "future", "old_version": "-", "old_arch": "-",
            "old_multiarch": "-", "direction": "<", "new_version": "1",
            "new_arch": "amd64", "new_multiarch": "no", "action": "/future.deb",
        }
        with mock.patch.object(guard.time, "monotonic", return_value=101), \
             mock.patch.object(guard, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "deadline exceeded"):
                guard.payload_source(action, deadline=100)
        run.assert_not_called()

    def test_runtime_imports_disable_package_directory_bytecode(self):
        for path in (SCRIPT, ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-apt-guard"):
            source = path.read_text(encoding="utf-8")
            self.assertIn("sys.dont_write_bytecode = True", source)
            self.assertLess(source.index("sys.dont_write_bytecode = True"), source.index("exec_module"))

    def test_postcheck_settlement_binds_version_architecture_and_dpkg_state(self):
        expected = {
            "libpve-storage-perl": {"version": "9.1.11", "architecture": "all"},
            "old-runtime": None,
        }
        good = (
            "libpve-storage-perl|libpve-storage-perl|9.1.11|all|installed\n"
            "old-runtime|old-runtime|1|amd64|config-files\n"
        )
        with mock.patch.object(
            self.module, "run",
            return_value=mock.Mock(returncode=0, stdout=good, stderr=""),
        ):
            self.assertEqual(self.module.exact_package_settlement(expected), [])

        wrong_arch = good.replace("9.1.11|all|installed", "9.1.11|amd64|installed")
        with mock.patch.object(
            self.module, "run",
            return_value=mock.Mock(returncode=0, stdout=wrong_arch, stderr=""),
        ):
            failures = self.module.exact_package_settlement(expected)
        self.assertEqual(len(failures), 1)
        self.assertIn("expected 9.1.11|all|installed", failures[0])

        removal_still_installed = good.replace(
            "old-runtime|old-runtime|1|amd64|config-files",
            "old-runtime|old-runtime|1|amd64|installed",
        )
        with mock.patch.object(
            self.module, "run",
            return_value=mock.Mock(returncode=0, stdout=removal_still_installed, stderr=""),
        ):
            failures = self.module.exact_package_settlement(expected)
        self.assertEqual(failures, ["old-runtime: expected ABSENT, got installed runtime"])

    def test_runtime_build_identity_is_exact_and_symlink_safe(self):
        marker = self.module.RUNTIME_BUILD_ID_FILE
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("a" * 64 + "\n", encoding="ascii")
        self.assertEqual(self.module.exact_runtime_build_id(), "a" * 64)
        marker.write_text("a" * 64 + "\nextra\n", encoding="ascii")
        with self.assertRaisesRegex(RuntimeError, "malformed"):
            self.module.exact_runtime_build_id()
        marker.unlink()
        target = marker.parent / "target"
        target.write_text("a" * 64 + "\n", encoding="ascii")
        marker.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, "unsafe"):
            self.module.exact_runtime_build_id()

    def test_runtime_release_is_published_before_package_latch_is_removed(self):
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("def command_postcheck(")
        end = source.index("\ndef command_recover(", start)
        handler = source[start:end]
        publish = handler.index("atomic_json(RUNTIME_RELEASE, runtime_release)")
        release = handler.index("unlink_durable(POST_GATE)")
        self.assertLess(publish, release)
        self.assertIn('"QUALIFIED" if receipt.get("verdict")', handler)
        self.assertIn('else "UNQUALIFIED"', handler)

    def test_direct_runtime_qualification_requires_exact_package_transition(self):
        args = mock.Mock(package="pve-sharedlvmthin", version="1",
                         artifact_sha256="a" * 64, plan_digest="b" * 64,
                         transaction_id="d" * 32)
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(
                    self.module, "manifest_and_lib",
                    return_value=({}, self.core),
                ):
            with self.assertRaisesRegex(RuntimeError, "prepared package transition"):
                self.module.command_qualify_runtime(args)
        self.assertFalse(self.module.RUNTIME_RELEASE.exists())

    def test_direct_runtime_qualification_publishes_boot_bound_receipt(self):
        self.module.STATE.mkdir(parents=True, exist_ok=True)
        self.module.STATE.chmod(0o700)
        self.module.RUNTIME_BUILD_ID_FILE.write_text("c" * 64 + "\n", encoding="ascii")
        self.module.PACKAGE_ARTIFACT_FILE.write_text("a" * 64 + "\n", encoding="ascii")
        args = mock.Mock(package="pve-sharedlvmthin", version="1",
                         artifact_sha256="a" * 64, plan_digest="b" * 64,
                         transaction_id="d" * 32)
        original_read_text = Path.read_text

        def read_text(path, *positional, **keywords):
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return "11111111-2222-3333-4444-555555555555\n"
            return original_read_text(path, *positional, **keywords)

        def version(name):
            return "1" if name == "pve-sharedlvmthin" else None

        transition = {
            "schema": 2, "state": "PREPARED", "txid": "d" * 32,
            "hostname": "test-node",
            "boot_id": "11111111-2222-3333-4444-555555555555",
            "target_package": "pve-sharedlvmthin", "target_version": "1",
            "candidate_sha256": "b" * 64,
            "candidate_artifact_sha256": "a" * 64,
            "candidate_manifest_sha256": "e" * 64,
        }
        self.module.atomic_json(self.module.PACKAGE_TRANSITION, transition)
        library = mock.Mock()
        library.digest.side_effect = lambda value: (
            "e" * 64 if value == {"schema": 1} else "f" * 64
        )
        def run_result(argv, **_kwargs):
            stdout = ("RUNTIME_QUALIFICATION_READY=YES\n"
                      if "--runtime-qualification" in argv else "")
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        tuple_proof = {
            "tuple": {"id": "fixture-tuple", "status": "RETEST_REQUIRED"},
            "manifest_sha256": "1" * 64,
            "tuple_sha256": "2" * 64,
            "identity_sha256": "3" * 64,
        }
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "package_version", side_effect=version), \
                mock.patch.object(self.module, "assert_listed_runtime_tuple",
                                  return_value=tuple_proof), \
                mock.patch.object(self.module, "manifest_and_lib",
                                  return_value=({"schema": 1}, library)), \
                mock.patch.object(self.module, "run", side_effect=run_result), \
                mock.patch.object(self.module.pathlib.Path, "read_text", new=read_text), \
                mock.patch.object(self.module.os, "uname",
                                  return_value=mock.Mock(release="7.0.0-test",
                                                         nodename="test-node")), \
                redirect_stdout(io.StringIO()):
            self.module.command_qualify_runtime(args)
        release = self.module.read_json(self.module.RUNTIME_RELEASE)
        self.assertEqual(release["qualified"], "QUALIFIED")
        self.assertEqual(release["runtime_build_id"], "c" * 64)
        self.assertEqual(release["boot_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(release["kernel_release"], "7.0.0-test")
        self.assertEqual(release["artifact_sha256"], "a" * 64)
        self.assertEqual(release["runtime_tuple_id"], "fixture-tuple")
        self.assertEqual(release["runtime_manifest_sha256"], "1" * 64)

    def test_runtime_tuple_admission_refuses_unlisted_installed_identity(self):
        library = mock.Mock()
        library.observed_tuple.return_value = None
        manifest = {"watched_packages": ["qemu-server"], "tuples": []}
        def version(name):
            return {"pve-sharedlvmthin": "1", "qemu-server": "9.2.10"}.get(name)
        with mock.patch.object(self.module.pathlib.Path, "read_text", return_value="dual"), \
                mock.patch.object(self.module, "package_version", side_effect=version), \
                mock.patch.object(self.module, "run", return_value=mock.Mock(
                    returncode=0, stdout="15", stderr="")), \
                mock.patch.object(self.module.os, "uname",
                                  return_value=mock.Mock(release="7.0.14-test")):
            with self.assertRaisesRegex(RuntimeError, "unlisted"):
                self.module.assert_listed_runtime_tuple(
                    manifest, library, "pve-sharedlvmthin", "1")

    def test_postcheck_style_release_survives_boot_id_change_with_real_tuple_verifier(self):
        manifest = {
            "watched_packages": ["qemu-server"],
            "tuples": [{
                "id": "postcheck-fixture", "status": "RETEST_REQUIRED",
                "profiles": ["dual"], "api": 15,
                "running_kernel": "7.0.14-test",
                "plugin_versions": ["1"],
                "packages": {"qemu-server": "9.2.10"},
            }],
        }

        def version(name):
            return {"pve-sharedlvmthin": "1", "qemu-server": "9.2.10"}.get(name)

        with mock.patch.object(self.module.pathlib.Path, "read_text", return_value="dual"), \
                mock.patch.object(self.module, "package_version", side_effect=version), \
                mock.patch.object(self.module, "run", return_value=mock.Mock(
                    returncode=0, stdout="15", stderr="")), \
                mock.patch.object(self.module.os, "uname",
                                  return_value=mock.Mock(release="7.0.14-test")):
            proof = self.module.assert_listed_runtime_tuple(
                manifest, self.core, "pve-sharedlvmthin", "1")
            release = self.module.build_bound_runtime_release(
                qualified="QUALIFIED", runtime_build_id="a" * 64,
                plan_digest="b" * 64,
                boot_id="11111111-2222-3333-4444-555555555555",
                kernel_release="7.0.14-test",
                plugin_package="pve-sharedlvmthin", plugin_version="1",
                verdict="ALLOW_QUALIFIED", artifact_sha256="c" * 64,
                runtime_tuple=proof,
            )
            # Boot identity is deliberately not part of the storage/PVE tuple;
            # the same payload and kernel must remain exactly re-verifiable.
            release["boot_id"] = "99999999-2222-3333-4444-555555555555"
            observed = self.module.assert_bound_runtime_tuple(
                release, manifest, self.core)
        self.assertEqual(observed["tuple"]["id"], "postcheck-fixture")
        self.assertEqual(release["runtime_tuple_id"], "postcheck-fixture")
        self.assertEqual(release["runtime_manifest_sha256"], self.core.digest(manifest))

    def test_postcheck_rechecks_bound_tuple_before_removing_latch(self):
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("def command_postcheck(")
        end = source.index("\ndef command_qualify_runtime(", start)
        handler = source[start:end]
        publish = handler.index("atomic_json(RUNTIME_RELEASE, runtime_release)")
        recheck = handler.index(
            "assert_bound_runtime_tuple(runtime_release, manifest, library)")
        release = handler.index("unlink_durable(POST_GATE)")
        self.assertLess(publish, recheck)
        self.assertLess(recheck, release)
        for field in ("runtime_tuple_id", "runtime_tuple_status",
                      "runtime_manifest_sha256", "runtime_tuple_sha256",
                      "runtime_identity_sha256"):
            self.assertIn(field, source[source.index("def build_bound_runtime_release"):start])

    def test_profile_gate_uses_read_only_qualification_before_settlement(self):
        source = (ROOT / "experiments/thick-generations/package-profile-gate.sh").read_text(
            encoding="utf-8")
        qualify = source.index("sharedlvmthin update-policy qualify-runtime")
        settle = source.rindex("sharedlvmthin update-policy settle-freeze-package")
        finalize = source.index("sharedlvmthin update-policy finalize-runtime")
        doctor = source.index("sharedlvmthin doctor --quick")
        final_check = source.rindex("sharedlvmthin upgrade-check")
        self.assertLess(qualify, settle)
        self.assertLess(doctor, finalize)
        self.assertLess(settle, finalize)
        self.assertLess(finalize, final_check)

    def test_boot_requalification_preserves_payload_and_changes_only_boot_runtime(self):
        self.module.STATE.mkdir(parents=True, exist_ok=True)
        self.module.STATE.chmod(0o700)
        self.module.RUNTIME_BUILD_ID_FILE.write_text("c" * 64 + "\n", encoding="ascii")
        self.module.PACKAGE_ARTIFACT_FILE.write_text("a" * 64 + "\n", encoding="ascii")
        previous = {
            "schema": 1, "qualified": "QUALIFIED", "runtime_build_id": "c" * 64,
            "plan_digest": "b" * 64,
            "boot_id": "11111111-2222-3333-4444-555555555555",
            "kernel_release": "7.0.0-old", "plugin_package": "pve-sharedlvmthin",
            "plugin_version": "1", "verdict": "PACKAGE_PROFILE_GATE",
            "artifact_sha256": "a" * 64,
        }
        self.module.atomic_json(self.module.RUNTIME_RELEASE, previous)
        original_read_text = Path.read_text

        def read_text(path, *positional, **keywords):
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return "99999999-2222-3333-4444-555555555555\n"
            return original_read_text(path, *positional, **keywords)

        def version(name):
            return "1" if name == "pve-sharedlvmthin" else None

        def boot_run_result(argv, **_kwargs):
            if "--runtime-qualification" in argv:
                stdout = "RUNTIME_QUALIFICATION_READY=YES\n"
            elif "--runtime-operational" in argv:
                stdout = "UPGRADE_SAFE=YES\n"
            else:
                stdout = ""
            return mock.Mock(returncode=0, stdout=stdout, stderr="")
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "package_version", side_effect=version), \
                mock.patch.object(self.module, "manifest_and_lib",
                                  return_value=({}, mock.Mock())), \
                mock.patch.object(self.module, "assert_bound_runtime_tuple"), \
                mock.patch.object(self.module, "run", side_effect=boot_run_result), \
                mock.patch.object(self.module.pathlib.Path, "read_text", new=read_text), \
                mock.patch.object(self.module.os, "uname",
                                  return_value=mock.Mock(release="7.0.0-new")), \
                redirect_stdout(io.StringIO()):
            self.module.command_requalify_boot(mock.Mock())
        successor = self.module.read_json(self.module.RUNTIME_RELEASE)
        self.assertEqual(successor["boot_id"], "99999999-2222-3333-4444-555555555555")
        self.assertEqual(successor["kernel_release"], "7.0.0-new")
        self.assertEqual(successor["previous_boot_id"], previous["boot_id"])
        self.assertEqual(successor["runtime_build_id"], previous["runtime_build_id"])
        self.assertEqual(successor["artifact_sha256"], previous["artifact_sha256"])
        self.assertFalse(self.module.RUNTIME_QUALIFICATION.exists())

    def test_boot_requalification_replays_crash_before_successor_publication(self):
        self.module.STATE.mkdir(parents=True, exist_ok=True)
        self.module.STATE.chmod(0o700)
        self.module.RUNTIME_BUILD_ID_FILE.write_text("c" * 64 + "\n", encoding="ascii")
        self.module.PACKAGE_ARTIFACT_FILE.write_text("a" * 64 + "\n", encoding="ascii")
        old_boot = "11111111-2222-3333-4444-555555555555"
        new_boot = "99999999-2222-3333-4444-555555555555"
        previous = {
            "schema": 1, "qualified": "QUALIFIED", "runtime_build_id": "c" * 64,
            "plan_digest": "b" * 64, "boot_id": old_boot,
            "kernel_release": "7.0.0-old", "plugin_package": "pve-sharedlvmthin",
            "plugin_version": "1", "verdict": "PACKAGE_PROFILE_GATE",
            "artifact_sha256": "a" * 64, "qualification_txid": "d" * 32,
        }
        pending = {
            "schema": 1, "state": "BOOT_QUALIFYING", "txid": "e" * 32,
            "runtime_build_id": "c" * 64, "artifact_sha256": "a" * 64,
            "plan_digest": "b" * 64, "boot_id": new_boot,
            "kernel_release": "7.0.0-new", "plugin_package": "pve-sharedlvmthin",
            "plugin_version": "1", "previous_boot_id": old_boot,
        }
        self.module.atomic_json(self.module.RUNTIME_RELEASE, previous)
        self.module.atomic_json(self.module.RUNTIME_QUALIFICATION, pending)
        original_read_text = Path.read_text

        def read_text(path, *positional, **keywords):
            if str(path) == "/proc/sys/kernel/random/boot_id":
                return new_boot + "\n"
            return original_read_text(path, *positional, **keywords)

        def version(name):
            return "1" if name == "pve-sharedlvmthin" else None

        def run_result(argv, **_kwargs):
            if "--runtime-qualification" in argv:
                stdout = "RUNTIME_QUALIFICATION_READY=YES\n"
            elif "--runtime-operational" in argv:
                stdout = "UPGRADE_SAFE=YES\n"
            else:
                stdout = ""
            return mock.Mock(returncode=0, stdout=stdout, stderr="")

        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "package_version", side_effect=version), \
                mock.patch.object(self.module, "manifest_and_lib",
                                  return_value=({}, mock.Mock())), \
                mock.patch.object(self.module, "assert_bound_runtime_tuple"), \
                mock.patch.object(self.module, "run", side_effect=run_result), \
                mock.patch.object(self.module.pathlib.Path, "read_text", new=read_text), \
                mock.patch.object(self.module.os, "uname",
                                  return_value=mock.Mock(release="7.0.0-new")), \
                redirect_stdout(io.StringIO()):
            self.module.command_requalify_boot(mock.Mock())
        successor = self.module.read_json(self.module.RUNTIME_RELEASE)
        self.assertEqual(successor["boot_id"], new_boot)
        self.assertEqual(successor["previous_boot_id"], old_boot)
        self.assertEqual(successor["qualification_txid"], "e" * 32)
        self.assertFalse(self.module.RUNTIME_QUALIFICATION.exists())

    def test_post_reboot_settlement_is_explicit_and_after_health_proof(self):
        source = (ROOT / "experiments/thick-generations/package-post-reboot-gate.sh").read_text(
            encoding="utf-8")
        self.assertIn("--settle-runtime", source)
        requalify = source.rindex("sharedlvmthin update-policy requalify-boot")
        self.assertLess(source.rindex("health_file=\n"), requalify)
        post_replay = source.index('"$0" \\\n', requalify)
        self.assertGreater(post_replay, requalify)
        self.assertGreater(source.index("POST_SETTLEMENT_RECHECK=PASS", post_replay),
                           post_replay)
        self.assertGreater(source.index("RESULT=POST_REBOOT_PASS", post_replay),
                           post_replay)

    def test_internal_runtime_modes_bypass_public_cli_argument_surface(self):
        source = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy"
        ).read_text(encoding="utf-8")
        self.assertNotIn(
            '"/usr/sbin/sharedlvmthin", "upgrade-check", "--runtime-', source,
        )
        self.assertIn(
            '"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check",\n'
            '                 "--runtime-qualification"',
            source,
        )
        self.assertIn(
            '"/usr/libexec/pve-sharedlvmthin/sharedlvmthin-upgrade-check",\n'
            '                 "--runtime-operational"',
            source,
        )

    def test_direct_bootstrap_accepts_only_named_legacy_policy_predecessors(self):
        source = (
            ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"WARN", "QUALIFIED_AUTO", "MANUAL_OVERRIDE", "QUALIFIED_ONLY"',
            source,
        )
        self.assertNotIn('"UNSELECTED", "QUALIFIED_ONLY"', source)

    def test_apt_guard_refuses_open_package_transition_before_policy_evaluation(self):
        source = (ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-apt-guard").read_text(
            encoding="utf-8"
        )
        self.assertIn('PACKAGE_TRANSITION = STATE / "package-transition.json"', source)
        transition = source.index("if PACKAGE_TRANSITION.exists():")
        evaluation = source.index("result = library.evaluate(")
        self.assertLess(transition, evaluation)
        self.assertIn('refuse("PACKAGE_TRANSITION_PENDING"', source[transition:evaluation])
        self.assertIn('RUNTIME_QUALIFICATION = STATE / "runtime-qualification-pending.json"',
                      source)
        self.assertIn('refuse("RUNTIME_QUALIFICATION_PENDING"',
                      source[transition:evaluation])

    def setUp(self):
        self.module, self.core = load_script()
        self.temporary = tempfile.TemporaryDirectory()
        state = Path(self.temporary.name) / "state"
        self.module.STATE = state
        self.module.POLICY = state / "policy.json"
        self.module.FREEZE = state / "freeze-owned.json"
        self.module.PROPOSAL = state / "proposal.json"
        self.module.AUTH = state / "authorization.json"
        self.module.POST_GATE = state / "post-gate-required.json"
        self.module.LAST_POST_GATE = state / "last-post-gate.json"
        self.module.RUNTIME_RELEASE = state / "runtime-release.json"
        self.module.RUNTIME_QUALIFICATION = state / "runtime-qualification-pending.json"
        self.module.LAST_RUNTIME_FINALIZATION = state / "last-runtime-finalization.json"
        self.module.RUNTIME_BUILD_ID_FILE = state / "runtime-build-id"
        self.module.PACKAGE_ARTIFACT_FILE = state / "package-artifact-sha256"
        self.module.MAINTENANCE_ACTIVE = state / "maintenance-active.json"
        self.module.PACKAGE_TRANSITION = state / "package-transition.json"
        self.module.LAST_PACKAGE_TRANSITION = state / "last-package-transition.json"
        self.module.FREEZE_MIGRATION = state / "freeze-schema-migration.json"
        self.module.LAST_FREEZE_MIGRATION = state / "last-freeze-schema-migration.json"
        self.module.LAST_POLICY_MIGRATION = state / "last-policy-schema-migration.json"
        self.module.LOCK = state / "policy.lock"

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def manifest():
        return {"watched_packages": ["qemu-server", "libpve-storage-perl"],
                "watched_package_patterns": []}

    @staticmethod
    def records():
        return [
            {"package": "libpve-storage-perl", "binary": "libpve-storage-perl",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]

    def test_freeze_preserves_admin_hold_and_releases_only_owned(self):
        holds = {"qemu-server", "bash"}
        installed = ["qemu-server", "libpve-storage-perl", "bash"]

        def mark(operation, packages):
            if operation == "hold":
                holds.update(packages)
            else:
                holds.difference_update(packages)

        with mock.patch.object(self.module, "installed_packages", return_value=installed), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "package_state_records", return_value=self.records()), \
                mock.patch.object(self.module, "apt_mark", side_effect=mark), \
                mock.patch.object(
                    self.module, "release_holds",
                    side_effect=lambda packages: mark("unhold", packages),
                ):
            record = self.module.enter_freeze(self.manifest(), self.core)
            self.assertEqual(record["preexisting"], ["qemu-server"])
            self.assertEqual(record["owned"], ["libpve-storage-perl"])
            self.assertEqual(record["schema"], 3)
            self.assertEqual(record["baseline_targets"],
                             ["libpve-storage-perl", "qemu-server"])
            self.assertEqual(record["hold_targets"],
                             ["libpve-storage-perl", "qemu-server"])
            self.assertEqual(record["phase"], "ACTIVE")
            released = self.module.leave_freeze()
        self.assertEqual(released, ["libpve-storage-perl"])
        self.assertEqual(holds, {"qemu-server", "bash"})

    def test_legacy_policy_names_change_only_through_explicit_migration(self):
        self.module.atomic_json(self.module.POLICY, {
            "schema": 1, "mode": "MANUAL_OVERRIDE", "required_scope": "san-dataplane"
        })
        with mock.patch.object(self.module, "require_root"), redirect_stdout(io.StringIO()):
            self.module.command_migrate_policy_schema(mock.Mock())
        migrated = self.module.read_json(self.module.POLICY)
        self.assertEqual(migrated["schema"], 2)
        self.assertEqual(migrated["mode"], "WARN")
        receipt = self.module.read_json(self.module.LAST_POLICY_MIGRATION)
        self.assertEqual(receipt["from_policy"]["mode"], "MANUAL_OVERRIDE")
        self.assertEqual(receipt["to_policy"]["mode"], "WARN")
        with mock.patch.object(self.module, "require_root"):
            with self.assertRaisesRegex(RuntimeError, "schema-1"):
                self.module.command_migrate_policy_schema(mock.Mock())

    def test_schema2_migration_releases_only_owned_plugin_hold(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        records = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        ledger = {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest),
            "targets": ["pve-sharedlvmthin", "qemu-server"],
            "baseline": records,
            "owned": ["pve-sharedlvmthin", "qemu-server"], "preexisting": [],
        }
        self.module.atomic_json(self.module.FREEZE, ledger)
        holds = {"pve-sharedlvmthin", "qemu-server", "foreign"}

        def release(packages):
            holds.difference_update(packages)

        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records", return_value=records), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "release_holds", side_effect=release):
            result = self.module.migrate_freeze_schema(manifest, self.core)
            replay = self.module.migrate_freeze_schema(manifest, self.core)
        self.assertEqual(result, replay)
        self.assertEqual(result["schema"], 3)
        self.assertEqual(result["baseline_targets"],
                         ["pve-sharedlvmthin", "qemu-server"])
        self.assertEqual(result["hold_targets"], ["qemu-server"])
        self.assertEqual(result["owned"], ["qemu-server"])
        self.assertEqual(result["preexisting"], [])
        self.assertEqual(holds, {"qemu-server", "foreign"})
        self.assertFalse(self.module.FREEZE_MIGRATION.exists())
        self.assertEqual(self.module.read_json(self.module.LAST_FREEZE_MIGRATION)["state"],
                         "COMPLETE")

    def test_schema2_migration_preserves_admin_plugin_hold(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        records = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        self.module.atomic_json(self.module.FREEZE, {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest),
            "targets": ["pve-sharedlvmthin", "qemu-server"], "baseline": records,
            "owned": ["qemu-server"], "preexisting": ["pve-sharedlvmthin"],
        })
        holds = {"pve-sharedlvmthin", "qemu-server"}
        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records", return_value=records), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "release_holds") as release:
            result = self.module.migrate_freeze_schema(manifest, self.core)
        release.assert_called_once_with([])
        self.assertEqual(result["preexisting"], ["pve-sharedlvmthin"])
        self.assertEqual(holds, {"pve-sharedlvmthin", "qemu-server"})

    def _exercise_schema2_migration_crash(self, boundary):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        records = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        parent = {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest),
            "targets": ["pve-sharedlvmthin", "qemu-server"],
            "baseline": records,
            "owned": ["pve-sharedlvmthin", "qemu-server"], "preexisting": [],
        }
        self.module.atomic_json(self.module.FREEZE, parent)
        holds = {"pve-sharedlvmthin", "qemu-server"}
        original_create = self.module.create_json
        original_atomic = self.module.atomic_json
        original_unlink = self.module.unlink_durable
        crashed = False

        def create(path, value):
            nonlocal crashed
            original_create(path, value)
            if boundary == "receipt" and path == self.module.FREEZE_MIGRATION and not crashed:
                crashed = True
                raise RuntimeError("simulated crash after receipt")

        def atomic(path, value):
            nonlocal crashed
            original_atomic(path, value)
            matched = ((boundary == "migrating" and path == self.module.FREEZE
                        and value.get("phase") == "MIGRATING")
                       or (boundary == "active" and path == self.module.FREEZE
                           and value.get("phase") == "ACTIVE")
                       or (boundary == "complete" and path == self.module.LAST_FREEZE_MIGRATION))
            if matched and not crashed:
                crashed = True
                raise RuntimeError(f"simulated crash after {boundary}")

        def release(packages):
            nonlocal crashed
            holds.difference_update(packages)
            if boundary == "selection" and not crashed:
                crashed = True
                raise RuntimeError("simulated crash after selection effect")

        def unlink(path):
            nonlocal crashed
            original_unlink(path)
            if boundary == "unlink" and path == self.module.FREEZE_MIGRATION and not crashed:
                crashed = True
                raise RuntimeError("simulated crash after receipt unlink")

        common = (
            mock.patch.object(self.module, "installed_packages",
                              return_value=["pve-sharedlvmthin", "qemu-server"]),
            mock.patch.object(self.module, "package_state_records", return_value=records),
            mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)),
            mock.patch.object(self.module, "release_holds", side_effect=release),
            mock.patch.object(self.module, "create_json", side_effect=create),
            mock.patch.object(self.module, "atomic_json", side_effect=atomic),
            mock.patch.object(self.module, "unlink_durable", side_effect=unlink),
        )
        with common[0], common[1], common[2], common[3], common[4], common[5], common[6]:
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                self.module.migrate_freeze_schema(manifest, self.core)
        self.assertTrue(crashed)

        # Replay with the same durable state and exact observed selection delta.
        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records", return_value=records), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "release_holds",
                                  side_effect=lambda packages: holds.difference_update(packages)):
            result = self.module.migrate_freeze_schema(manifest, self.core)
            replay = self.module.migrate_freeze_schema(manifest, self.core)
        self.assertEqual(result, replay)
        self.assertEqual(result["schema"], 3)
        self.assertEqual(result["phase"], "ACTIVE")
        self.assertEqual(result["previous_generation"], parent["generation"])
        self.assertEqual(holds, {"qemu-server"})
        self.assertFalse(self.module.FREEZE_MIGRATION.exists())
        self.assertEqual(self.module.read_json(self.module.LAST_FREEZE_MIGRATION)["state"],
                         "COMPLETE")

    def test_schema2_migration_replays_every_durable_crash_boundary(self):
        for boundary in ("receipt", "migrating", "selection", "active", "complete", "unlink"):
            with self.subTest(boundary=boundary):
                # Each subcase needs an independent durable state directory.
                self.tearDown()
                self.setUp()
                self._exercise_schema2_migration_crash(boundary)

    def test_schema2_migration_rechecks_baseline_after_selection_effect(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        records = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        current = [dict(row) for row in records]
        self.module.atomic_json(self.module.FREEZE, {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest),
            "targets": ["pve-sharedlvmthin", "qemu-server"], "baseline": records,
            "owned": ["pve-sharedlvmthin", "qemu-server"], "preexisting": [],
        })
        holds = {"pve-sharedlvmthin", "qemu-server"}

        def release(packages):
            holds.difference_update(packages)
            current[1]["version"] = "foreign-drift"

        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records",
                                  side_effect=lambda _targets: [dict(row) for row in current]), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "release_holds", side_effect=release):
            with self.assertRaisesRegex(RuntimeError, "baseline changed during"):
                self.module.migrate_freeze_schema(manifest, self.core)
        self.assertEqual(self.module.read_json(self.module.FREEZE)["phase"], "MIGRATING")
        self.assertTrue(self.module.FREEZE_MIGRATION.exists())
        self.assertFalse(self.module.LAST_FREEZE_MIGRATION.exists())

    def test_active_replay_preserves_pending_evidence_on_integrity_failure(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        records = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        successor = {
            "schema": 3, "phase": "ACTIVE", "generation": "b" * 32,
            "previous_generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest),
            "baseline_targets": ["pve-sharedlvmthin", "qemu-server"],
            "hold_targets": ["qemu-server"], "baseline": records,
            "owned": ["qemu-server"], "preexisting": [],
        }
        receipt = {
            "schema": 1, "state": "PREPARED", "txid": "c" * 32,
            "parent_generation": "a" * 32, "parent_sha256": "d" * 64,
            "manifest_sha256": self.core.digest(manifest),
            "observed_holds": ["pve-sharedlvmthin", "qemu-server"],
            "release_owned": ["pve-sharedlvmthin"], "successor": successor,
            "successor_sha256": self.core.digest(successor),
        }
        self.module.atomic_json(self.module.FREEZE, successor)
        self.module.atomic_json(self.module.FREEZE_MIGRATION, receipt)
        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records",
                                  return_value=[dict(records[0]),
                                                dict(records[1], version="foreign-drift")]), \
                mock.patch.object(self.module, "held_packages", return_value={"qemu-server"}):
            with self.assertRaisesRegex(RuntimeError, "evidence was preserved"):
                self.module.migrate_freeze_schema(manifest, self.core)
        self.assertTrue(self.module.FREEZE_MIGRATION.exists())
        self.assertFalse(self.module.LAST_FREEZE_MIGRATION.exists())

    def test_schema2_reselect_requires_explicit_migration(self):
        manifest = self.manifest()
        self.module.atomic_json(self.module.FREEZE, {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest), "baseline": self.records(),
            "targets": ["libpve-storage-perl", "qemu-server"],
            "owned": ["libpve-storage-perl"], "preexisting": ["qemu-server"],
        })
        holds = {"qemu-server"}

        def mark(operation, packages):
            self.assertEqual(operation, "hold")
            holds.update(packages)

        with mock.patch.object(
                self.module, "installed_packages",
                return_value=["qemu-server", "libpve-storage-perl"]), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "package_state_records", return_value=self.records()), \
                mock.patch.object(self.module, "apt_mark", side_effect=mark):
            with self.assertRaisesRegex(RuntimeError, "explicit migrate-freeze-schema"):
                self.module.enter_freeze(manifest, self.core)
        self.assertEqual(holds, {"qemu-server"})

    def test_policy_change_refuses_open_post_gate(self):
        self.module.atomic_json(self.module.POST_GATE, {"schema": 1, "actions": []})
        args = mock.Mock(mode="freeze")
        with mock.patch.object(self.module, "require_root"):
            with self.assertRaisesRegex(RuntimeError, "another update transaction"):
                self.module.command_select(args)

    def test_authorization_is_bound_to_proposal_digest_and_payloads(self):
        self.module.atomic_json(self.module.POLICY, {"schema": 1, "mode": "MANUAL_OVERRIDE"})
        proposal = {"schema": 1, "plan_digest": "a" * 64,
                    "payloads": [{"path": "/x", "sha256": "b" * 64, "size": 1}]}
        self.module.atomic_json(self.module.PROPOSAL, proposal)
        records = self.records()
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "manifest_and_lib",
                                  return_value=(self.manifest(), self.core)), \
                mock.patch.object(self.module, "installed_packages",
                                  return_value=[row["package"] for row in records]), \
                mock.patch.object(self.module, "package_state_records", return_value=records), \
                mock.patch.object(self.module.os, "uname",
                                  return_value=mock.Mock(nodename="pve-test")), \
                mock.patch.object(self.module.pathlib.Path, "read_text",
                                  return_value="boot-test"):
            self.module.command_authorize(mock.Mock())
        authorization = self.module.read_json(self.module.AUTH)
        self.assertEqual(authorization["schema"], 2)
        self.assertEqual(authorization["plan_digest"], proposal["plan_digest"])
        self.assertEqual(authorization["payloads"], proposal["payloads"])
        self.assertEqual(authorization["context"]["node"], "pve-test")
        self.assertEqual(authorization["context"]["watched_state"], records)
        self.assertRegex(authorization["authorization_id"], r"^[0-9a-f]{32}$")

    def test_authorize_handler_does_not_recursively_acquire_policy_lock(self):
        source = SCRIPT.read_text(encoding="utf-8")
        start = source.index("def command_authorize(")
        end = source.index("\ndef package_version(", start)
        handler = source[start:end]
        self.assertNotIn("with policy_lock", handler)
        main = source[source.index("def main():"):]
        self.assertIn('else policy_lock(exclusive=args.command != "status")', main)
        self.assertIn("internal_postcheck_status", main)
        self.assertIn("contextlib.nullcontext() if internal_postcheck_status", main)

    def test_recover_never_infers_zero_effect_from_unchanged_versions(self):
        self.module.atomic_json(self.module.POST_GATE, {
            "schema": 1,
            "state": "DPKG_DISPATCHED_RESULT_UNKNOWN",
            "plan_digest": "a" * 64,
            "actions": [{"package": "qemu-server", "old_version": "1",
                         "new_version": "1", "action": "**CONFIGURE**"}],
        })
        with mock.patch.object(self.module, "require_root"):
            with self.assertRaisesRegex(RuntimeError, "cannot be proven"):
                self.module.command_recover(mock.Mock())
        self.assertTrue(self.module.POST_GATE.exists())
        self.assertFalse(self.module.LAST_POST_GATE.exists())

    def test_status_accepts_only_exact_expected_post_gate_for_internal_postcheck(self):
        receipt = {"schema": 1, "plan_digest": "a" * 64, "actions": []}
        self.module.atomic_json(self.module.POST_GATE, receipt)
        args = mock.Mock(expected_post_gate="a" * 64)
        observed = {"id": "qualified", "status": "RETEST_REQUIRED"}
        library = mock.Mock()
        library.observed_tuple.return_value = observed
        common = (
            mock.patch.object(self.module, "manifest_and_lib", return_value=(self.manifest(), library)),
            mock.patch.object(self.module.pathlib.Path, "read_text", return_value="dual"),
            mock.patch.object(self.module, "package_version", return_value="1"),
            mock.patch.object(self.module, "run", return_value=mock.Mock(returncode=0, stdout="15")),
        )
        with common[0], common[1], common[2], common[3], redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.command_status(args), 0)
        args.expected_post_gate = "b" * 64
        common = (
            mock.patch.object(self.module, "manifest_and_lib", return_value=(self.manifest(), library)),
            mock.patch.object(self.module.pathlib.Path, "read_text", return_value="dual"),
            mock.patch.object(self.module, "package_version", return_value="1"),
            mock.patch.object(self.module, "run", return_value=mock.Mock(returncode=0, stdout="15")),
        )
        with common[0], common[1], common[2], common[3], redirect_stdout(io.StringIO()):
            self.assertEqual(self.module.command_status(args), 1)

    def test_status_names_every_missing_freeze_hold(self):
        self.module.atomic_json(self.module.POLICY, {"schema": 1, "mode": "FREEZE"})
        self.module.atomic_json(self.module.FREEZE, {"schema": 2, "phase": "ACTIVE"})
        output = io.StringIO()
        with mock.patch.object(self.module, "manifest_and_lib", return_value=(self.manifest(), self.core)), \
                mock.patch.object(self.module, "freeze_integrity", return_value={
                    "result": "FAIL_MISSING_HOLD", "reasons": [],
                    "missing_holds": ["qemu-server"]}), \
                mock.patch.object(self.module.pathlib.Path, "read_text", return_value="dual"), \
                mock.patch.object(self.module, "run", return_value=mock.Mock(returncode=1, stdout="")), \
                redirect_stdout(output):
            rc = self.module.command_status(mock.Mock())
        self.assertEqual(rc, 1)
        self.assertIn("FREEZE_INTEGRITY=FAIL_MISSING_HOLD", output.getvalue())
        self.assertIn("MISSING_REQUIRED_HOLDS=qemu-server", output.getvalue())

    def test_reselect_never_masks_package_baseline_drift(self):
        manifest = self.manifest()
        baseline = self.records()
        self.module.atomic_json(self.module.FREEZE, {
            "schema": 2, "phase": "ACTIVE", "generation": "b" * 32,
            "manifest_sha256": self.core.digest(manifest), "baseline": baseline,
            "targets": ["libpve-storage-perl", "qemu-server"],
            "owned": ["libpve-storage-perl", "qemu-server"], "preexisting": [],
        })
        drifted = [dict(row, version="2") if row["package"] == "qemu-server" else row
                   for row in baseline]
        with mock.patch.object(self.module, "installed_packages",
                               return_value=["libpve-storage-perl", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records", return_value=drifted), \
                mock.patch.object(self.module, "held_packages",
                                  return_value={"libpve-storage-perl", "qemu-server"}), \
                mock.patch.object(self.module, "release_holds") as release:
            with self.assertRaisesRegex(RuntimeError, "intact ACTIVE baseline"):
                self.module.migrate_freeze_schema(manifest, self.core)
        release.assert_not_called()

    def test_exact_package_settlement_rotates_only_plugin_baseline(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        old = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        new = [dict(row, version="2") if row["package"] == "pve-sharedlvmthin"
               else dict(row) for row in old]
        ledger = {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest), "targets":
            ["pve-sharedlvmthin", "qemu-server"], "baseline": old,
            "owned": ["pve-sharedlvmthin", "qemu-server"], "preexisting": [],
        }
        self.module.atomic_json(self.module.FREEZE, ledger)
        candidate_manifest = Path(self.temporary.name) / "candidate.json"
        candidate_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        installed_version = {"pve-sharedlvmthin": "1",
                             "pve-sharedlvmthin-thick": None}
        holds = {"pve-sharedlvmthin", "qemu-server"}
        current_records = {"value": old}

        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records",
                                  side_effect=lambda _targets: current_records["value"]), \
                mock.patch.object(self.module, "held_packages", side_effect=lambda: set(holds)), \
                mock.patch.object(self.module, "package_version",
                                  side_effect=lambda name: installed_version.get(name)), \
                mock.patch.object(self.module, "apt_mark") as mark:
            receipt = self.module.prepare_freeze_package(
                "pve-sharedlvmthin", "1", "pve-sharedlvmthin", "2", "all",
                "b" * 64, str(candidate_manifest), manifest, self.core)
            installed_version["pve-sharedlvmthin"] = "2"
            current_records["value"] = new
            settled = self.module.settle_freeze_package(
                "pve-sharedlvmthin", "2", "b" * 64, receipt["txid"],
                manifest, self.core)

        self.assertEqual(settled["phase"], "ACTIVE")
        self.assertEqual(settled["schema"], 3)
        self.assertEqual(settled["baseline_targets"],
                         ["pve-sharedlvmthin", "qemu-server"])
        self.assertEqual(settled["hold_targets"], ["qemu-server"])
        self.assertEqual(settled["baseline"], new)
        self.assertEqual(settled["previous_generation"], "a" * 32)
        self.assertFalse(self.module.PACKAGE_TRANSITION.exists())
        self.assertEqual(self.module.read_json(
            self.module.LAST_PACKAGE_TRANSITION)["state"], "COMPLETE")
        self.assertEqual(mark.call_args_list, [mock.call("hold", []),
                                               mock.call("unhold", ["pve-sharedlvmthin"])])

    def test_preinst_verifier_binds_prepared_receipt_to_artifact_and_manifest(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        baseline = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        ledger = {
            "schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
            "manifest_sha256": self.core.digest(manifest),
            "targets": ["pve-sharedlvmthin", "qemu-server"],
            "baseline": baseline,
            "owned": ["pve-sharedlvmthin", "qemu-server"], "preexisting": [],
        }
        self.module.atomic_json(self.module.POLICY, {"schema": 1, "mode": "FREEZE"})
        self.module.atomic_json(self.module.FREEZE, ledger)
        candidate = Path(self.temporary.name) / "candidate.json"
        candidate.write_text(json.dumps(manifest), encoding="utf-8")
        versions = {"pve-sharedlvmthin": "1", "pve-sharedlvmthin-thick": None}
        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records", return_value=baseline), \
                mock.patch.object(self.module, "held_packages",
                                  return_value={"pve-sharedlvmthin", "qemu-server"}), \
                mock.patch.object(self.module, "package_version",
                                  side_effect=lambda name: versions.get(name)):
            self.module.prepare_freeze_package(
                "pve-sharedlvmthin", "1", "pve-sharedlvmthin", "2", "all",
                "b" * 64, str(candidate), manifest, self.core, "c" * 64,
            )
        args = mock.Mock(
            source_package="pve-sharedlvmthin", source_version="1",
            target_package="pve-sharedlvmthin", target_version="2",
            target_architecture="all", artifact_sha256="c" * 64,
            candidate_manifest=str(candidate),
        )
        output = io.StringIO()
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "manifest_and_lib",
                                  return_value=(manifest, self.core)), \
                redirect_stdout(output):
            self.assertEqual(self.module.command_verify_prepared_freeze_package(args), 0)
        self.assertIn("FREEZE_PREPARED=PASS", output.getvalue())
        args.artifact_sha256 = "d" * 64
        with mock.patch.object(self.module, "require_root"), \
                mock.patch.object(self.module, "manifest_and_lib",
                                  return_value=(manifest, self.core)):
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                self.module.command_verify_prepared_freeze_package(args)

    def test_preinst_verifier_is_not_applicable_without_freeze(self):
        args = mock.Mock()
        with mock.patch.object(self.module, "require_root"):
            self.assertEqual(self.module.command_verify_prepared_freeze_package(args), 3)

    def test_package_settlement_refuses_foreign_frozen_drift(self):
        manifest = {"watched_packages": ["pve-sharedlvmthin", "qemu-server"],
                    "watched_package_patterns": []}
        old = [
            {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
             "version": "1", "architecture": "all", "status": "installed"},
            {"package": "qemu-server", "binary": "qemu-server",
             "version": "1", "architecture": "amd64", "status": "installed"},
        ]
        ledger = {"schema": 2, "phase": "ACTIVE", "generation": "c" * 32,
                  "manifest_sha256": self.core.digest(manifest),
                  "targets": ["pve-sharedlvmthin", "qemu-server"],
                  "baseline": old, "owned": ["pve-sharedlvmthin", "qemu-server"],
                  "preexisting": []}
        self.module.atomic_json(self.module.FREEZE, ledger)
        candidate_manifest = Path(self.temporary.name) / "candidate.json"
        candidate_manifest.write_text(json.dumps(manifest), encoding="utf-8")
        versions = {"pve-sharedlvmthin": "1", "pve-sharedlvmthin-thick": None}
        current = {"value": old}
        with mock.patch.object(self.module, "installed_packages",
                               return_value=["pve-sharedlvmthin", "qemu-server"]), \
                mock.patch.object(self.module, "package_state_records",
                                  side_effect=lambda _targets: current["value"]), \
                mock.patch.object(self.module, "held_packages",
                                  return_value={"pve-sharedlvmthin", "qemu-server"}), \
                mock.patch.object(self.module, "package_version",
                                  side_effect=lambda name: versions.get(name)):
            receipt = self.module.prepare_freeze_package(
                "pve-sharedlvmthin", "1", "pve-sharedlvmthin", "2", "all",
                "d" * 64, str(candidate_manifest), manifest, self.core)
            versions["pve-sharedlvmthin"] = "2"
            current["value"] = [dict(row, version="2") for row in old]
            with self.assertRaisesRegex(RuntimeError, "outside the exact plugin"):
                self.module.settle_freeze_package(
                    "pve-sharedlvmthin", "2", "d" * 64, receipt["txid"],
                    manifest, self.core)

    def test_policy_lock_refuses_concurrent_policy_operation(self):
        with self.module.policy_lock(exclusive=True):
            with self.assertRaisesRegex(RuntimeError, "another update-policy operation"):
                with self.module.policy_lock(exclusive=True):
                    self.fail("contended lock must not be entered")

    def test_empty_or_inconsistent_ledger_never_passes(self):
        manifest = self.manifest()
        for ledger in (
                {"schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
                 "manifest_sha256": self.core.digest(manifest), "targets": [],
                 "owned": [], "preexisting": [], "baseline": []},
                {"schema": 2, "phase": "ACTIVE", "generation": "a" * 32,
                 "manifest_sha256": self.core.digest(manifest),
                 "targets": ["qemu-server"], "owned": ["qemu-server"],
                 "preexisting": ["qemu-server"], "baseline": self.records()},
        ):
            with mock.patch.object(self.module, "installed_packages",
                                   return_value=["libpve-storage-perl", "qemu-server"]), \
                    mock.patch.object(self.module, "package_state_records",
                                      return_value=self.records()), \
                    mock.patch.object(self.module, "held_packages", return_value=set()):
                result = self.module.freeze_integrity(ledger, manifest, self.core)
            self.assertTrue(result["result"].startswith("FAIL"))

    def test_pending_post_gate_makes_status_unsettled(self):
        self.module.atomic_json(self.module.POLICY, {"schema": 1, "mode": "MANUAL_OVERRIDE"})
        self.module.atomic_json(self.module.POST_GATE, {"schema": 1, "actions": []})
        output = io.StringIO()
        with mock.patch.object(self.module, "manifest_and_lib", return_value=(self.manifest(), self.core)), \
                mock.patch.object(self.module.pathlib.Path, "read_text", return_value="dual"), \
                mock.patch.object(self.module, "run", return_value=mock.Mock(returncode=1, stdout="")), \
                redirect_stdout(output):
            rc = self.module.command_status(mock.Mock())
        self.assertEqual(rc, 1)
        self.assertIn("POST_GATE_REQUIRED=YES", output.getvalue())


if __name__ == "__main__":
    unittest.main()
