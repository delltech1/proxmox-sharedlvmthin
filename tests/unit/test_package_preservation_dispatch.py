"""Public executable descriptor transport in a disposable installed layout."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

RUNTIME = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"


@unittest.skipUnless(os.name == "posix", "Linux executor imports fcntl")
class PolicyDispatchBoundaryTests(unittest.TestCase):
    def test_policy_dispatch_rejects_other_commands_and_extra_arguments(self):
        for arguments in (["freeze"], ["postcheck", "--force"], [], ["requalify-boot", "extra"],
                          ["postcheck", "--previous-boot-id", "11111111-2222-3333-4444-555555555555"],
                          ["requalify-boot", "--previous-boot-id", "bad"],
                          ["requalify-boot", "--previous-boot-id", "11111111-2222-3333-4444-555555555555", "--force"],
                          ["requalify-boot", "--previous-boot-id", "11111111-2222-3333-4444-555555555555", "--previous-boot-id", "11111111-2222-3333-4444-555555555555"]):
            result = subprocess.run(
                [sys.executable, str(RUNTIME / "sharedlvmthin-package-preservation"),
                 "--update-policy", *arguments], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("only exact postcheck or requalify-boot", result.stderr)

    def test_post_reboot_rejects_incomplete_or_duplicate_identity(self):
        for arguments in ([], ["--expect-host", "node", "--expect-host", "node"],
                          ["--expect-host"], ["--settle-runtime", "--settle-runtime"],
                          ["--force"]):
            result = subprocess.run([sys.executable,
                str(RUNTIME / "sharedlvmthin-package-preservation"), "--post-reboot", *arguments],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("post-reboot", result.stderr)


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "Linux root sealed-descriptor transport")
class PublicPackageDispatchTests(unittest.TestCase):
    def test_empty_scope_dispatch_and_invalid_inheritance_never_falls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            helper = runtime / "sharedlvmthin-package-preservation"
            helper.write_bytes((RUNTIME / helper.name).read_bytes())
            for name in ("sharedlvmthin_package_executor_fence.py",):
                (runtime / name).write_bytes((RUNTIME / name).read_bytes())
            (runtime / "sharedlvmthin-recovery-check").write_text("# no probes\n")
            (runtime / "sharedlvmthin_package_preservation.py").write_text(
                "def collect_snapshot(checker,node,boot): return {'scopes':()}\n"
                "def require_unchanged(a,b): assert a==b\n")
            marker = runtime / "invoked"
            gate = runtime / "sharedlvmthin-package-profile-gate"
            gate.write_text("#!/bin/bash\nexit 0\n")
            target = runtime / "sharedlvmthin-update-policy"
            target.write_text("#!/usr/bin/python3\nimport pathlib,sys,os\n"
                "assert 'SLT_PACKAGE_FENCE_FD' not in os.environ\n"
                "pathlib.Path(" + repr(str(marker)) + ").write_text(sys.argv[1])\n")
            target.chmod(0o755)
            for action in ("postcheck", "requalify-boot"):
                result = subprocess.run([sys.executable, str(helper), "--update-policy", action],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(marker.read_text(), action)
            exact_args = ["requalify-boot", "--previous-boot-id", "11111111-2222-3333-4444-555555555555"]
            target.write_text("#!/usr/bin/python3\nimport pathlib,sys,json\n"
                "pathlib.Path(" + repr(str(marker)) + ").write_text(json.dumps(sys.argv[1:]))\n"
                "raise SystemExit(17)\n")
            result = subprocess.run([sys.executable, str(helper), "--update-policy", *exact_args],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 17, result.stderr)
            self.assertEqual(json.loads(marker.read_text()), exact_args)
            marker.unlink()
            for inherited in ("", "bogus", "0", "999999"):
                result = subprocess.run([sys.executable, str(helper), "--update-policy", "postcheck"],
                    env=dict(os.environ, SLT_PACKAGE_FENCE_FD=inherited), capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(marker.exists())

    def test_installed_layout_sanitized_child_retains_complete_scope_after_wrapper_death(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, locks = root / "runtime", root / "locks"
            runtime.mkdir(); locks.mkdir()
            helper = runtime / "sharedlvmthin-package-preservation"
            helper.write_bytes((RUNTIME / helper.name).read_bytes())
            (runtime / "sharedlvmthin_package_executor_fence.py").write_text(
                (RUNTIME / "sharedlvmthin_package_executor_fence.py").read_text().replace(
                    'directory="/run/lock"', 'directory=' + repr(str(locks))))
            (runtime / "sharedlvmthin-recovery-check").write_text("# disposable checker carrier; no probes\n")
            scope = [("scope-eager", "vm-901-disk-0"), ("scope-lazy", "vm-901-disk-0"),
                     ("other-lazy", "vm-902-disk-0")]
            (runtime / "sharedlvmthin_package_preservation.py").write_text(
                "def collect_snapshot(checker,node,boot):\n"
                " return {'scopes': " + repr(tuple(sorted(scope))) + ", 'node':node,'boot':boot}\n"
                "def require_unchanged(a,b):\n assert a==b\n")
            child = runtime / "validate-child.py"
            policy = runtime / "sharedlvmthin-update-policy"
            policy.write_text("#!/usr/bin/python3\nimport os,sys,pathlib\n"
                "from sharedlvmthin_package_executor_fence import read_sealed_context\n"
                "boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()\n"
                "read_sealed_context(int(os.environ['SLT_PACKAGE_FENCE_FD']),boot)\n"
                "assert sys.argv[1:] in (['postcheck'],['requalify-boot'])\n"
                "pathlib.Path(" + repr(str(root / "nested-")) + "+sys.argv[1]).write_text('PASS')\n")
            policy.chmod(0o755)
            child.write_text(
                "import os,time,pathlib,subprocess\n"
                "from sharedlvmthin_package_executor_fence import read_sealed_context\n"
                "boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()\n"
                "context=read_sealed_context(int(os.environ['SLT_PACKAGE_FENCE_FD']),boot)\n"
                "assert context['scopes']==" + repr([list(row) for row in sorted(scope)]) + "\n"
                "assert 'UNTRUSTED_UPGRADE_OVERRIDE' not in os.environ\n"
                "for action in ('postcheck','requalify-boot'):\n"
                " subprocess.run(['/usr/bin/python3'," + repr(str(helper)) + ", '--update-policy',action],"
                "check=True,pass_fds=(*context['fds'],int(os.environ['SLT_PACKAGE_FENCE_FD'])))\n"
                "pathlib.Path(" + repr(str(root / "ready")) + ").write_text(str(os.getpid()))\n"
                "while not pathlib.Path(" + repr(str(root / "release")) + ").exists(): time.sleep(.02)\n"
                "read_sealed_context(int(os.environ['SLT_PACKAGE_FENCE_FD']),boot)\n"
                "pathlib.Path(" + repr(str(root / "passed")) + ").write_text('PASS')\n")
            gate = runtime / "sharedlvmthin-package-profile-gate"
            gate.write_text("#!/bin/bash\nset -eu\nexec /usr/bin/env -i PATH=/usr/bin:/bin "
                            'SLT_PACKAGE_FENCE_FD="$SLT_PACKAGE_FENCE_FD" /usr/bin/python3 ' +
                            repr(str(child)) + "\n")
            wrapper = subprocess.Popen([sys.executable, str(helper), "--execute"],
                                       env=dict(os.environ, UNTRUSTED_UPGRADE_OVERRIDE="1"),
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True)
            try:
                deadline = time.monotonic() + 8
                while not (root / "ready").exists() and wrapper.poll() is None and time.monotonic() < deadline:
                    time.sleep(.02)
                failure = "public executable did not deliver valid scope"
                if not (root / "ready").exists() and wrapper.poll() is not None:
                    output, error = wrapper.communicate(timeout=5)
                    failure += ": " + (output + error).decode(errors="replace")
                self.assertTrue((root / "ready").exists(), failure)
                for action in ("postcheck", "requalify-boot"):
                    self.assertEqual((root / ("nested-" + action)).read_text(), "PASS")
                os.kill(wrapper.pid, signal.SIGKILL)
                wrapper.wait(timeout=5)
                check = subprocess.run([sys.executable, "-c",
                                        "from sharedlvmthin_package_executor_fence import hold_executors; "
                                        "import sys; "
                                        "ctx=hold_executors(" + repr(scope) + "); "
                                        "ctx.__enter__()"], cwd=runtime,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                self.assertNotEqual(check.returncode, 0)
                self.assertIn("BlockingIOError", check.stderr)
                (root / "release").write_text("exact release")
                deadline = time.monotonic() + 8
                while not (root / "passed").exists() and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual((root / "passed").read_text(), "PASS")
            finally:
                try: os.killpg(wrapper.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                wrapper.communicate(timeout=5)
