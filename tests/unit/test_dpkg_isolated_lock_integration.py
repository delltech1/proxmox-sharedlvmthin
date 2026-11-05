"""Opt-in disposable Linux dpkg locking experiment; never use the real db.

Requires a disposable host: dpkg reads host configuration and executes a dummy
maintainer script without chroot. A conservative config preflight allows only
two known directives, and every dpkg data/log/root path is temporary.
"""
import os
import json
import importlib.util
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest import mock


@unittest.skipUnless(sys.platform.startswith("linux") and getattr(os, "geteuid", lambda: -1)() == 0
                     and os.environ.get("SLT_RUN_ISOLATED_DPKG_TEST") == "1",
                     "opt-in disposable Linux root dpkg experiment")
class IsolatedDpkgLockTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        self.fcntl = fcntl
        self.dpkg = shutil.which("dpkg")
        self.deb = shutil.which("dpkg-deb")
        if not self.dpkg or not self.deb:
            self.skipTest("dpkg and dpkg-deb required")
        cfg = Path("/etc/dpkg/dpkg.cfg")
        cfgdir = Path("/etc/dpkg/dpkg.cfg.d")
        configs = ([cfg] if cfg.exists() else []) + (sorted(cfgdir.iterdir()) if cfgdir.exists() else [])
        for path in configs:
            if path.is_file():
                for line in path.read_text().splitlines():
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Exactly the default log directive is safe here because
                    # the later command-line --log selects our temporary file.
                    if line != "no-debsig" and line.split() != ["log", "/var/log/dpkg.log"]:
                        self.skipTest("unrecognized dpkg config may run hooks; use a clean disposable host")
        self.tmp = tempfile.TemporaryDirectory(prefix="slt-dpkg-test-")
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / "root"
        self.db = self.root / "var/lib/dpkg"
        for name in ("updates", "info", "triggers"):
            (self.db / name).mkdir(parents=True, exist_ok=True)
        (self.db / "status").write_text("")
        self.env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
        package = self.base / "package"
        (package / "DEBIAN").mkdir(parents=True)
        (package / "usr/share/slt-dpkg-lock-test").mkdir(parents=True)
        (package / "DEBIAN/control").write_text(
            "Package: slt-dpkg-lock-test\nVersion: 1\nArchitecture: all\n"
            "Maintainer: Test <" + "test" + chr(64) + "example.invalid>\n"
            "Description: disposable lock experiment\n")
        (package / "usr/share/slt-dpkg-lock-test/payload").write_text("dummy\n")
        marker, release = self.base / "ready", self.base / "release"
        observer = self.base / "observe_fd.py"
        observer.write_text('''import errno, fcntl, json, os
result = {"environment_value": os.environ.get("SLT_EXPERIMENT_FD"),
          "sentinel_present": "SLT_UNTRUSTED_SENTINEL" in os.environ,
          "same_inode": False, "owns_lock": False, "error": None}
try:
    fd = int(result["environment_value"])
    actual, expected = os.fstat(fd), os.stat(os.environ["SLT_EXPERIMENT_LOCK"])
    result["same_inode"] = (actual.st_dev, actual.st_ino) == (expected.st_dev, expected.st_ino)
    if result["same_inode"]:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result["owns_lock"] = True
except (ValueError, TypeError, OSError) as error:
    result["error"] = type(error).__name__ + ":" + str(getattr(error, "errno", ""))
with open(os.environ["SLT_EXPERIMENT_RESULT"], "w") as out:
    json.dump(result, out)
''')
        preinst = package / "DEBIAN/preinst"
        preinst.write_text("#!/bin/sh\nset -eu\n"
                           "if [ -n \"${SLT_EXPERIMENT_FD:-}\" ]; then "
                           + shlex.quote(sys.executable) + " " + shlex.quote(str(observer)) + "; fi\n"
                           ": > " + shlex.quote(str(marker)) + "\n"
                           "while [ ! -f " + shlex.quote(str(release)) + " ]; do sleep 0.05; done\n")
        preinst.chmod(0o755)
        self.package = self.base / "test.deb"
        subprocess.run([self.deb, "--build", str(package), str(self.package)], env=self.env,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.argv = [self.dpkg, "--root=" + str(self.root), "--admindir=" + str(self.db),
                     "--log=" + str(self.base / "dpkg.log"), "--no-triggers", "--no-debsig",
                     "--force-script-chrootless", "--unpack", str(self.package)]
        self.assertIn(self.base.resolve(), self.root.resolve().parents)
        self.assertIn(self.root.resolve(), self.db.resolve().parents)

    def test_parent_lock_blocks_even_when_descriptor_is_inherited(self):
        lock = self.db / "lock"
        with lock.open("a+") as held:
            self.fcntl.lockf(held, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)
            result = subprocess.run(self.argv, env=self.env, pass_fds=(held.fileno(),),
                                    capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lock", result.stderr.lower())
            self.assertFalse((self.base / "ready").exists())
            self.assertFalse((self.root / "usr/share/slt-dpkg-lock-test/payload").exists())

    def test_dpkg_retains_database_lock_during_maintainer_script(self):
        proc = subprocess.Popen(self.argv, env=self.env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        try:
            deadline = time.monotonic() + 20
            while not (self.base / "ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue((self.base / "ready").exists(), "dummy preinst did not start")
            self.assertFalse((self.base / "fd-result.json").exists(), "unexpected observer environment")
            competitor = subprocess.run(self.argv, env=self.env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(competitor.returncode, 0)
            self.assertIn("lock", competitor.stderr.lower())
            (self.base / "release").touch()
            stdout, stderr = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, (stdout + stderr).decode(errors="replace"))
            self.assertEqual((self.root / "usr/share/slt-dpkg-lock-test/payload").read_text(), "dummy\n")
            self.assertIn("slt-dpkg-lock-test", (self.base / "dpkg.log").read_text())
        finally:
            if proc.poll() is None:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate(timeout=5)

    def test_preinst_receives_environment_and_descriptor_observation(self):
        self.observe_descriptor_transport(pass_descriptor=True)

    def test_preinst_cannot_claim_an_unpassed_descriptor(self):
        self.observe_descriptor_transport(pass_descriptor=False)

    def test_clean_env_wrapper_descriptor_observation(self):
        self.observe_descriptor_transport(pass_descriptor=True, wrapped=True)

    def test_clean_env_wrapper_unpassed_descriptor(self):
        self.observe_descriptor_transport(pass_descriptor=False, wrapped=True)

    def observe_descriptor_transport(self, *, pass_descriptor, wrapped=False):
        executor_lock = self.base / "executor.lock"
        with executor_lock.open("a+") as held:
            self.fcntl.flock(held, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)
            env = dict(self.env, SLT_EXPERIMENT_FD=str(held.fileno()),
                       SLT_EXPERIMENT_LOCK=str(executor_lock),
                       SLT_EXPERIMENT_RESULT=str(self.base / "fd-result.json"),
                       SLT_UNTRUSTED_SENTINEL="must-not-cross-clean-env")
            argv = self.argv
            if wrapped:
                bash = shutil.which("bash")
                if not bash or not Path("/usr/bin/env").is_file():
                    self.skipTest("bash and /usr/bin/env required for wrapper experiment")
                wrapper = self.base / "clean-env-wrapper.sh"
                # Models the env -i / bash exec boundary only, not the full
                # BASTRIX gate or its real fence-context verification.
                wrapper.write_text('#!/bin/bash\nset -eu\nexec /usr/bin/env -i '
                    'PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C '
                    'SLT_EXPERIMENT_FD="$SLT_EXPERIMENT_FD" '
                    'SLT_EXPERIMENT_LOCK="$SLT_EXPERIMENT_LOCK" '
                    'SLT_EXPERIMENT_RESULT="$SLT_EXPERIMENT_RESULT" "$@"\n')
                argv = [bash, str(wrapper), *self.argv]
            proc = subprocess.Popen(argv, env=env,
                                    pass_fds=(held.fileno(),) if pass_descriptor else (),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    start_new_session=True)
            try:
                deadline = time.monotonic() + 20
                while not (self.base / "ready").exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue((self.base / "ready").exists(), "dummy preinst did not report readiness")
                observed = json.loads((self.base / "fd-result.json").read_text())
                self.assertEqual(observed["environment_value"], str(held.fileno()))
                self.assertEqual(observed["sentinel_present"], not wrapped)
                self.assertFalse(observed["owns_lock"] and not observed["same_inode"])
                if not pass_descriptor:
                    self.assertFalse(observed["same_inode"], observed)
                    self.assertFalse(observed["owns_lock"], observed)
                with executor_lock.open("a+") as competitor:
                    with self.assertRaises(BlockingIOError):
                        self.fcntl.flock(competitor, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)
                rival = subprocess.run(self.argv, env=self.env, capture_output=True, text=True, timeout=20)
                self.assertNotEqual(rival.returncode, 0)
                self.assertIn("lock", rival.stderr.lower())
                print("DPKG_PREINST_DESCRIPTOR_OBSERVATION=" + json.dumps(
                    {"descriptor_passed": pass_descriptor, "clean_env_wrapper": wrapped,
                     **observed}, sort_keys=True), flush=True)
                (self.base / "release").touch()
                stdout, stderr = proc.communicate(timeout=20)
                self.assertEqual(proc.returncode, 0, (stdout + stderr).decode(errors="replace"))
            finally:
                if proc.poll() is None:
                    import signal
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.communicate(timeout=5)

    def test_profile_executor_fence_reaches_real_dpkg_preinst(self):
        self.observe_profile_executor(with_context=True)

    def test_profile_executor_without_context_stays_absent(self):
        self.observe_profile_executor(with_context=False)

    def observe_profile_executor(self, *, with_context):
        """Real helper and dpkg, but test adapter replaces the production argv.

        This checks descriptor transport only. It does not test the production
        argv, real package scripts, transaction identity or the BASTRIX gate.
        """
        import signal
        import test_profile_replacement as fixture
        fixture.ProfileReplacementTests.setUp(self)
        helper = self.m
        helper.read_boot_id = lambda: Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        # Restore real dpkg-deb field parsing, overridden by the profile fixture.
        helper.candidate_field = fixture.load_module().candidate_field
        runtime = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"
        spec = importlib.util.spec_from_file_location("profile_integration_fence",
                    runtime / "sharedlvmthin_package_executor_fence.py")
        fence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fence)
        locks = self.base / "locks"
        locks.mkdir()
        scope = [("test", "vm-100-disk-0")]
        result_path = self.base / "profile-observation.json"
        observer = self.base / "profile-observer.py"
        observer.write_text("import importlib.util,json,os,pathlib\n"
            "value=os.environ.get('SLT_PACKAGE_FENCE_FD')\n"
            "result={'transport':value}\n"
            "if value is not None:\n"
            " spec=importlib.util.spec_from_file_location('f'," + repr(str(runtime / "sharedlvmthin_package_executor_fence.py")) + ")\n"
            " f=importlib.util.module_from_spec(spec); spec.loader.exec_module(f)\n"
            " boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()\n"
            " result['context']=f.read_sealed_context(int(value),boot," + repr(str(locks)) + ")\n"
            "pathlib.Path(" + repr(str(result_path)) + ").write_text(json.dumps(result))\n")
        package_dir = self.base / "package"
        (package_dir / "DEBIAN/control").write_text(
            "Package: pve-sharedlvmthin\nVersion: 1\nArchitecture: all\n"
            "Maintainer: Test <" + "test" + chr(64) + "example.invalid>\n"
            "Description: dummy isolated executor experiment\n")
        preinst = package_dir / "DEBIAN/preinst"
        preinst.write_text("#!/bin/sh\nset -eu\n" + shlex.quote(sys.executable) + " "
                           + shlex.quote(str(observer)) + "\n: > " + shlex.quote(str(self.base / "ready"))
                           + "\nwhile [ ! -f " + shlex.quote(str(self.base / "release"))
                           + " ]; do sleep 0.05; done\n")
        subprocess.run([self.deb, "--build", str(package_dir), str(self.package)], env=self.env,
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.package.chmod(0o400)
        args = types.SimpleNamespace(candidate=str(self.package), sha256=helper.sha256(self.package),
                    source_package="pve-sharedlvmthin-thick", source_version="1",
                    target_package="pve-sharedlvmthin", target_version="1")
        real_execve = os.execve
        real_read_context = fence.read_sealed_context
        def read_context(fd, boot):
            return real_read_context(fd, boot, str(locks))
        def adapted_execve(path, argv, env):
            if path != "/usr/bin/dpkg" or argv[:3] != ["/usr/bin/dpkg", "--force-hold", "-i"] \
                    or argv[3] != str(helper.ROOT / "candidate.deb"):
                os._exit(124)
            # Deliberately substitute *all* actual dpkg args with the fixed,
            # isolated root/db/log args built by setUp; no host database access.
            real_execve(self.dpkg, [*self.argv[:-1], argv[3]], env)
        with fence.hold_executors(scope, str(locks)) as descriptors:
            context_fd = fence.create_sealed_context(scope, descriptors, helper.read_boot_id())
            driver = None
            try:
                with mock.patch.dict(sys.modules, {"sharedlvmthin_package_executor_fence": fence}), \
                        mock.patch.object(fence, "read_sealed_context", side_effect=read_context), \
                        mock.patch.dict(os.environ, ({"SLT_PACKAGE_FENCE_FD": str(context_fd)}
                                                     if with_context else {}), clear=True), \
                        mock.patch.object(os, "execve", side_effect=adapted_execve):
                    driver = os.fork()
                    if driver == 0:
                        try:
                            os.setsid()
                            helper.execute(args)
                            os._exit(0)
                        except BaseException as exc:
                            (self.base / "driver-error").write_text(repr(exc))
                            os._exit(1)
                    deadline = time.monotonic() + 20
                    while not (self.base / "ready").exists() and time.monotonic() < deadline:
                        if (self.base / "driver-error").exists():
                            self.fail((self.base / "driver-error").read_text())
                        time.sleep(0.02)
                    self.assertTrue((self.base / "ready").exists(), "dummy preinst did not run")
                    observed = json.loads(result_path.read_text())
                    if with_context:
                        self.assertEqual(observed["transport"], str(context_fd))
                        self.assertEqual(observed["context"]["fds"], list(descriptors))
                    else:
                        self.assertIsNone(observed["transport"])
                    with self.assertRaises(BlockingIOError):
                        with fence.hold_executors(scope, str(locks)):
                            self.fail("executor locks released")
                    competing = subprocess.run(self.argv, env=self.env, capture_output=True, text=True, timeout=20)
                    self.assertNotEqual(competing.returncode, 0)
                    self.assertIn("lock", competing.stderr.lower())
                    (self.base / "release").touch()
                    deadline = time.monotonic() + 20
                    while True:
                        waited, status = os.waitpid(driver, os.WNOHANG)
                        if waited:
                            driver = None
                            self.assertEqual(os.waitstatus_to_exitcode(status), 0)
                            break
                        self.assertLess(time.monotonic(), deadline, "profile executor did not finish")
                        time.sleep(0.02)
                    self.assertEqual((self.root / "usr/share/slt-dpkg-lock-test/payload").read_text(), "dummy\n")
            finally:
                if driver is not None:
                    try:
                        os.killpg(driver, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    os.waitpid(driver, 0)
                os.close(context_fd)


if __name__ == "__main__":
    unittest.main()
