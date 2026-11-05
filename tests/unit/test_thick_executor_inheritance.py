"""Exercise Linux inherited flock behavior without touching any device."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

try:
    import fcntl
except ImportError:
    fcntl = None


@unittest.skipUnless(fcntl and Path('/proc/self').exists() and shutil.which('timeout'),
                     'requires Linux procfs and GNU timeout')
class ThickExecutorInheritanceTests(unittest.TestCase):
    def test_surviving_child_retains_inherited_flock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'executor.lock'
            locked = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            contender = None
            wrapper = None
            child = None
            try:
                fcntl.flock(locked, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # The subprocess receives only this explicit inherited handle.
                wrapper = subprocess.Popen(
                    ['timeout', '--foreground', '20s', '/bin/sleep', '15'],
                    pass_fds=(locked,), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                children = Path(f'/proc/{wrapper.pid}/task/{wrapper.pid}/children')
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    ids = children.read_text().split()
                    if ids:
                        child = int(ids[0])
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(child, 'timeout did not start its child')
                os.close(locked)
                locked = None
                contender = os.open(path, os.O_RDWR)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                wrapper.kill()
                wrapper.wait(timeout=3)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                os.kill(child, signal.SIGTERM)
                deadline = time.monotonic() + 3
                while True:
                    try:
                        fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            self.fail('lock did not clear after the test child exited')
                        time.sleep(0.02)
                child = None
            finally:
                if wrapper is not None and wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=3)
                if child is not None:
                    try:
                        os.kill(child, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if contender is not None:
                    os.close(contender)
                if locked is not None:
                    os.close(locked)


if __name__ == '__main__':
    unittest.main()
