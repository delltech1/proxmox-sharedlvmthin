import importlib.util
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import json
import unittest


@unittest.skipUnless(os.name == "posix" and getattr(os, "geteuid", lambda: -1)() == 0,
                     "Linux root executor contract")
class PackageExecutorFenceTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_package_executor_fence.py"
        spec = importlib.util.spec_from_file_location("executor_fence", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)

    def test_busy_executor_refuses_and_release_is_exact(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = [("lazy", "vm-100-disk-0")]
            with self.module.hold_executors(identity, directory) as descriptors:
                self.assertEqual(len(descriptors), 2)
                self.assertTrue(self.module.verify_inherited_executors(identity, descriptors, directory))
                with self.assertRaises(BlockingIOError):
                    with self.module.hold_executors(identity, directory):
                        self.fail("busy executor entered")
            with self.module.hold_executors(identity, directory):
                pass

    def test_sealed_transport_binds_boot_scope_and_real_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = [("lazy", "vm-100-disk-0")]
            with self.module.hold_executors(identity, directory) as descriptors:
                fd = self.module.create_sealed_context(identity, descriptors, "test-boot")
                try:
                    context = self.module.read_sealed_context(fd, "test-boot", directory)
                    self.assertEqual(context["fds"], list(descriptors))
                    with self.assertRaises(OSError):
                        os.pwrite(fd, b"x", 0)
                    with self.assertRaises(ValueError):
                        self.module.read_sealed_context(fd, "other-boot", directory)
                finally:
                    os.close(fd)

    def test_open_but_unheld_descriptors_are_not_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = [("lazy", "vm-100-disk-0")]
            with self.module.hold_executors(identity, directory):
                pass
            descriptors = [os.open(Path(directory) / f"pve-sharedlvmthin-{family}-lazy-vm-100-disk-0.lock", os.O_RDWR)
                           for family in ("lazy", "thick-transition")]
            try:
                with self.assertRaisesRegex(ValueError, "not held"):
                    self.module.verify_inherited_executors(identity, descriptors, directory)
            finally:
                for fd in descriptors:
                    os.close(fd)

    def test_other_open_description_is_not_an_inherited_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = [("lazy", "vm-100-disk-0")]
            with self.module.hold_executors(identity, directory):
                descriptors = [os.open(Path(directory) / f"pve-sharedlvmthin-{family}-lazy-vm-100-disk-0.lock", os.O_RDWR)
                               for family in ("lazy", "thick-transition")]
                try:
                    with self.assertRaises(BlockingIOError):
                        self.module.verify_inherited_executors(identity, descriptors, directory)
                finally:
                    for fd in descriptors:
                        os.close(fd)

    def test_aliases_are_distinct_and_duplicates_deduplicated(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.module.hold_executors([
                ("lazy", "vm-100-disk-0"), ("eager", "vm-100-disk-0"),
                ("lazy", "vm-100-disk-0")], directory) as descriptors:
                self.assertEqual(len(descriptors), 4)

    def test_inherited_child_keeps_fence_after_wrapper_closes_descriptors(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = [("lazy", "vm-100-disk-0")]
            child = None
            try:
                with self.module.hold_executors(identity, directory) as descriptors:
                    child = subprocess.Popen(
                        [sys.executable, "-c",
                         "import os,sys,json,importlib.util; "
                         "s=importlib.util.spec_from_file_location('f',sys.argv[1]); "
                         "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
                         "m.verify_inherited_executors([('lazy','vm-100-disk-0')],json.loads(sys.argv[3]),sys.argv[2]); "
                         "os.write(1,b'READY\\n'); os.read(0,1)",
                         self.module.__file__, directory, json.dumps(descriptors)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        pass_fds=descriptors)
                    self.assertEqual(child.stdout.readline(), b"READY\n")
                with self.assertRaises(BlockingIOError):
                    with self.module.hold_executors(identity, directory):
                        self.fail("child-persistent fence was released")
                child.communicate(b"x", timeout=5)
                self.assertEqual(child.returncode, 0)
                with self.module.hold_executors(identity, directory):
                    pass
            finally:
                if child is not None and child.poll() is None:
                    child.kill()
                    child.communicate(timeout=5)

    def test_unsafe_existing_lock_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            name = Path(directory) / "pve-sharedlvmthin-lazy-lazy-vm-100-disk-0.lock"
            name.symlink_to(Path(directory) / "other")
            with self.assertRaises(OSError):
                with self.module.hold_executors([("lazy", "vm-100-disk-0")], directory):
                    self.fail("symlink accepted")

    def test_invalid_scope_refuses_without_files(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                with self.module.hold_executors([("../bad", "vm-100-disk-0")], directory):
                    self.fail("bad identity accepted")
            self.assertEqual(list(Path(directory).iterdir()), [])
