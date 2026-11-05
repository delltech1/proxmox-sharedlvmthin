"""Profile executor transports only a validated inherited fence to dpkg."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import test_profile_replacement as fixture


class ProfileFenceInputTests(unittest.TestCase):
    def setUp(self):
        imports = {"fcntl": types.ModuleType("fcntl")} if sys.platform == "win32" else {}
        with mock.patch.dict(sys.modules, imports):
            self.m = fixture.load_module()
        self.m.read_boot_id = lambda: "boot"

    def test_absent_and_invalid_transport_are_distinct(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.m.inherited_package_fence())
        for value in ("", "no", "12345678901", "１２"):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SLT_PACKAGE_FENCE_FD": value}):
                with self.assertRaises(self.m.Refusal):
                    self.m.inherited_package_fence()

    def test_present_context_is_verified_and_failures_refuse(self):
        fence = types.SimpleNamespace(read_sealed_context=mock.Mock(return_value={"fds": [4, 5]}))
        with mock.patch.dict(sys.modules, {"sharedlvmthin_package_executor_fence": fence}), \
                mock.patch.dict(os.environ, {"SLT_PACKAGE_FENCE_FD": "7"}):
            self.assertEqual(self.m.inherited_package_fence(), ("7", {"fds": [4, 5]}))
            fence.read_sealed_context.assert_called_once_with(7, "boot")
            fence.read_sealed_context.side_effect = ValueError("wrong boot")
            with self.assertRaisesRegex(self.m.Refusal, "wrong boot"):
                self.m.inherited_package_fence()


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                     "Linux root memfd/flock and fork contract")
class ProfileReplacementFenceTests(unittest.TestCase):
    setUp = fixture.ProfileReplacementTests.setUp
    args = fixture.ProfileReplacementTests.args

    def test_real_fork_carries_valid_context_and_exact_locks_to_exec(self):
        path = fixture.ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_package_executor_fence.py"
        spec = importlib.util.spec_from_file_location("profile_fence_test", path)
        fence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fence)
        scope = [("fixture", "vm-100-disk-0")]
        locks = Path(self.temp.name) / "locks"
        locks.mkdir()
        output = Path(self.temp.name) / "exec.json"
        real_read = fence.read_sealed_context
        def read_context(fd, boot):
            return real_read(fd, boot, str(locks))
        def fake_exec(path, argv, env):
            value = {"path": path, "argv": argv, "env": env,
                     "context": read_context(int(env["SLT_PACKAGE_FENCE_FD"]), self.m.read_boot_id())}
            value["inheritable"] = [os.get_inheritable(fd) for fd in
                                    (*value["context"]["fds"], int(env["SLT_PACKAGE_FENCE_FD"]))]
            output.write_text(json.dumps(value))
            os._exit(0)
        with fence.hold_executors(scope, str(locks)) as descriptors:
            context_fd = fence.create_sealed_context(scope, descriptors, self.m.read_boot_id())
            try:
                with mock.patch.dict(sys.modules, {"sharedlvmthin_package_executor_fence": fence}), \
                        mock.patch.object(fence, "read_sealed_context", side_effect=read_context), \
                        mock.patch.dict(os.environ, {"SLT_PACKAGE_FENCE_FD": str(context_fd)}), \
                        mock.patch.object(os, "execve", side_effect=fake_exec):
                    self.m.execute(self.args())
                result = json.loads(output.read_text())
                self.assertEqual(result["path"], "/usr/bin/dpkg")
                self.assertEqual(result["env"]["SLT_PACKAGE_FENCE_FD"], str(context_fd))
                self.assertEqual(result["context"]["fds"], list(descriptors))
                self.assertTrue(all(result["inheritable"]))
                with self.assertRaises(BlockingIOError):
                    with fence.hold_executors(scope, str(locks)):
                        self.fail("executor locks released")
            finally:
                os.close(context_fd)

    def test_malformed_or_closed_context_refuses_before_fork(self):
        path = fixture.ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_package_executor_fence.py"
        spec = importlib.util.spec_from_file_location("profile_fence_invalid", path)
        fence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fence)
        with mock.patch.dict(sys.modules, {"sharedlvmthin_package_executor_fence": fence}):
            for value in ("", "bad", "0", "99999999999"):
                with self.subTest(value=value), mock.patch.dict(os.environ, {"SLT_PACKAGE_FENCE_FD": value}), \
                        mock.patch.object(os, "fork", side_effect=AssertionError("fork must not run")):
                    with self.assertRaises(self.m.Refusal):
                        self.m.execute(self.args())

    def test_no_context_keeps_legacy_sanitized_exec(self):
        output = Path(self.temp.name) / "legacy.json"
        def fake_exec(path, argv, env):
            output.write_text(json.dumps(env))
            os._exit(0)
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(os, "execve", side_effect=fake_exec):
            self.m.execute(self.args())
        self.assertEqual(json.loads(output.read_text()),
                         {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})

    def test_revalidation_failure_keeps_ready_and_never_executes_child(self):
        marker = Path(self.temp.name) / "dispatched"
        def fake_exec(*_args):
            marker.write_text("unexpected")
            os._exit(0)
        with mock.patch.object(self.m, "inherited_package_fence",
                               side_effect=[("7", {"fds": [8]}), self.m.Refusal("changed")]), \
                mock.patch.object(os, "execve", side_effect=fake_exec):
            with self.assertRaisesRegex(self.m.Refusal, "changed"):
                self.m.execute(self.args())
        self.assertFalse(marker.exists())
        self.assertTrue(self.m.READY.exists())
        self.assertEqual(self.m.read_record(self.m.READY)["phase"], "READY")
        self.assertFalse(self.m.INTENT.exists())
        self.assertFalse(self.m.DONE.exists())


if __name__ == "__main__":
    unittest.main()
