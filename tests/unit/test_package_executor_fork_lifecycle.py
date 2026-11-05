"""Linux fork lifecycle of the local package executor fence; temp files only."""
import importlib.util
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


RUNTIME = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"
FENCE = RUNTIME / "sharedlvmthin_package_executor_fence.py"


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                     "Linux root flock contract")
class ForkFenceLifecycleTests(unittest.TestCase):
    def test_exec_child_keeps_sealed_fence_after_parent_sigkill(self):
        spec = importlib.util.spec_from_file_location('fork_exec_test_fence', FENCE)
        fence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fence)
        scope = [('test', 'vm-100-disk-0')]
        with tempfile.TemporaryDirectory() as directory:
            script = r'''
import importlib.util, os, pathlib, sys
spec=importlib.util.spec_from_file_location('fence',sys.argv[1])
f=importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
scope=[('test','vm-100-disk-0')]
boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
with f.hold_executors(scope,sys.argv[2]) as descriptors:
    context=f.create_sealed_context(scope,descriptors,boot)
    pid=os.fork()
    if pid==0:
        for fd in (*descriptors,context): os.set_inheritable(fd,True)
        child="""import importlib.util,os,pathlib,sys
s=importlib.util.spec_from_file_location('fence',sys.argv[1]); f=importlib.util.module_from_spec(s); s.loader.exec_module(f)
boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
ctx=f.read_sealed_context(int(sys.argv[3]),boot,sys.argv[2]); assert len(ctx['fds'])==2
os.write(1,b'READY\\n'); os.read(0,1)
"""
        os.execv(sys.executable,[sys.executable,'-I','-c',child,sys.argv[1],sys.argv[2],str(context)])
    while True: __import__('signal').pause()
'''
            parent = subprocess.Popen([sys.executable, '-c', script, str(FENCE), directory],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True)
            completed = False
            try:
                import select
                ready, _, _ = select.select([parent.stdout], [], [], 10)
                self.assertTrue(ready, 'exec child did not signal readiness')
                self.assertEqual(parent.stdout.readline(), b'READY\n')
                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)
                with self.assertRaises(BlockingIOError):
                    with fence.hold_executors(scope, directory):
                        self.fail('exec child lost the fence')
                parent.stdin.write(b'x')
                parent.stdin.flush()
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with fence.hold_executors(scope, directory):
                            break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail('fence remained held after exec child exited')
                        time.sleep(0.02)
                parent.communicate(timeout=5)
                completed = True
            finally:
                if not completed:
                    try:
                        os.killpg(parent.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    parent.communicate(timeout=5)

    def test_child_fence_survives_parent_sigkill_until_last_close(self):
        spec = importlib.util.spec_from_file_location("fork_test_fence", FENCE)
        fence = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(fence)
        scope = [("test", "vm-100-disk-0")]
        with tempfile.TemporaryDirectory() as directory:
            script = r'''
import importlib.util, os, sys
spec = importlib.util.spec_from_file_location("fence", sys.argv[1])
fence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fence)
scope = [("test", "vm-100-disk-0")]
with fence.hold_executors(scope, sys.argv[2]) as descriptors:
    pid = os.fork()
    if pid == 0:
        try:
            fence.verify_inherited_executors(scope, descriptors, sys.argv[2])
            os.write(1, ("READY " + str(os.getpid()) + "\n").encode())
            os.read(0, 1)
        finally:
            os._exit(0)
    while True:
        signal = __import__("signal")
        signal.pause()
'''
            parent = subprocess.Popen([sys.executable, "-c", script, str(FENCE), directory],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, start_new_session=True)
            completed = False
            try:
                # select prevents a broken child from hanging the test forever.
                import select
                ready, _, _ = select.select([parent.stdout], [], [], 10)
                self.assertTrue(ready, "forked executor did not signal readiness")
                line = parent.stdout.readline().decode().strip().split()
                self.assertEqual(line[:1], ["READY"], line)
                self.assertEqual(len(line), 2)
                self.assertGreater(int(line[1]), 1)
                os.kill(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)
                self.assertEqual(parent.returncode, -signal.SIGKILL)
                with self.assertRaises(BlockingIOError):
                    with fence.hold_executors(scope, directory):
                        self.fail("child-held fence became available")
                parent.stdin.write(b"x")
                parent.stdin.flush()
                # Child exits immediately after receiving the byte. Lock
                # release follows descriptor closure and can be scheduled later.
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with fence.hold_executors(scope, directory):
                            break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail("fence remained held after child exit")
                        time.sleep(0.02)
                parent.communicate(timeout=5)
                completed = True
            finally:
                if not completed:
                    try:
                        os.killpg(parent.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    parent.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
