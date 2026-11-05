"""A sealed transport is not proof of an incomplete executor fence."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "Linux root executor contract")
class ExecutorScopeIntegrityTests(unittest.TestCase):
    def test_sealed_context_cannot_omit_or_substitute_scope_locks(self):
        path = (Path(__file__).resolve().parents[2] /
                "usr/libexec/pve-sharedlvmthin/sharedlvmthin_package_executor_fence.py")
        spec = importlib.util.spec_from_file_location("scope_fence", path)
        fence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fence)
        scopes = [("a", "vm-100-disk-0"), ("b", "vm-101-disk-0")]
        with tempfile.TemporaryDirectory() as directory:
            with fence.hold_executors(scopes, directory) as descriptors:
                valid = fence.create_sealed_context(scopes, descriptors, "boot")
                incomplete = fence.create_sealed_context(scopes, descriptors[:-1], "boot")
                # The same lock pathname opened again is a different OFD. Its
                # existence and a sealed context do not prove lock ownership.
                replacement = os.open(
                    Path(directory) / "pve-sharedlvmthin-thick-transition-b-vm-101-disk-0.lock",
                    os.O_RDWR)
                substituted = fence.create_sealed_context(
                    scopes, (*descriptors[:-1], replacement), "boot")
                try:
                    self.assertEqual(fence.read_sealed_context(valid, "boot", directory)["fds"],
                                     list(descriptors))
                    with self.assertRaisesRegex(ValueError, "incomplete or ambiguous"):
                        fence.read_sealed_context(incomplete, "boot", directory)
                    with self.assertRaises(BlockingIOError):
                        fence.read_sealed_context(substituted, "boot", directory)
                finally:
                    for fd in (valid, incomplete, substituted, replacement):
                        os.close(fd)


if __name__ == "__main__":
    unittest.main()
