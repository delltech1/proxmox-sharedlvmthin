"""Candidate policy must load its adjacent fence module under python -I."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


RUNTIME = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"


class IsolatedFenceImportTests(unittest.TestCase):
    def test_missing_adjacent_helper_never_falls_back_to_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "sharedlvmthin-candidate-update-policy"
            shutil.copyfile(RUNTIME / "sharedlvmthin-update-policy", policy)
            foreign = root / "foreign"
            foreign.mkdir()
            (foreign / "sharedlvmthin_package_executor_fence.py").write_text(
                "raise AssertionError('foreign helper loaded')\n")
            driver = r'''
import importlib.machinery, importlib.util, sys, types
if sys.platform == 'win32': sys.modules['fcntl'] = types.ModuleType('fcntl')
loader=importlib.machinery.SourceFileLoader('candidate',sys.argv[1])
spec=importlib.util.spec_from_loader(loader.name,loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
try: m.load_package_executor_fence()
except RuntimeError as exc: print(str(exc))
else: raise AssertionError('missing helper was accepted')
'''
            result = subprocess.run([sys.executable, "-I", "-c", driver, str(policy)],
                                    env=dict(os.environ, PYTHONPATH=str(foreign)),
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("adjacent package fence helper is missing or unsafe", result.stdout)

    def test_real_isolated_python_loads_adjacent_helper_and_forwards_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            policy = root / "sharedlvmthin-candidate-update-policy"
            shutil.copyfile(RUNTIME / "sharedlvmthin-update-policy", policy)
            (root / "sharedlvmthin_package_executor_fence.py").write_text(
                "def read_sealed_context(fd, boot):\n"
                " assert fd == 7 and boot == 'fake-boot'\n"
                " return {'fds': [8, 9]}\n")
            driver = r'''
import importlib.machinery, importlib.util, json, os, sys, types
if sys.platform == 'win32': sys.modules['fcntl'] = types.ModuleType('fcntl')
loader = importlib.machinery.SourceFileLoader('candidate', sys.argv[1])
spec = importlib.util.spec_from_loader(loader.name, loader)
m = importlib.util.module_from_spec(spec); loader.exec_module(m)
m.pathlib.Path.read_text = lambda self, *a, **kw: 'fake-boot\n'
def capture(argv, **kw):
 print(json.dumps({'fds': kw['pass_fds'], 'transport': kw['env'].get('SLT_PACKAGE_FENCE_FD')}))
 return types.SimpleNamespace(returncode=0)
m.subprocess.run = capture
os.environ['SLT_PACKAGE_FENCE_FD'] = '7'
m.run(['unused'])
'''
            env = dict(os.environ, PYTHONPATH=str(root / "untrusted"))
            result = subprocess.run([sys.executable, "-I", "-c", driver, str(policy)],
                                    env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {"fds": [8, 9, 7], "transport": "7"})

    @unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                         "Linux root sealed memfd/flock contract")
    def test_isolated_python_forwards_actual_sealed_context(self):
        import importlib.util
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            locks = root / "locks"
            locks.mkdir()
            policy = root / "sharedlvmthin-candidate-update-policy"
            shutil.copyfile(RUNTIME / "sharedlvmthin-update-policy", policy)
            fence_path = root / "sharedlvmthin_package_executor_fence.py"
            fence_path.write_text((RUNTIME / fence_path.name).read_text().replace(
                'directory="/run/lock"', 'directory=' + repr(str(locks))))
            spec = importlib.util.spec_from_file_location("isolated_fence", fence_path)
            fence = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fence)
            scope = [("test", "vm-100-disk-0")]
            boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
            driver = r'''
import importlib.machinery, importlib.util, json, os, sys
loader=importlib.machinery.SourceFileLoader('candidate',sys.argv[1])
spec=importlib.util.spec_from_loader(loader.name,loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
child="""import importlib.util,os,pathlib,sys
spec=importlib.util.spec_from_file_location('child_fence',sys.argv[1])
f=importlib.util.module_from_spec(spec); spec.loader.exec_module(f)
fd=int(os.environ['SLT_PACKAGE_FENCE_FD'])
boot=pathlib.Path('/proc/sys/kernel/random/boot_id').read_text().strip()
ctx=f.read_sealed_context(fd,boot)
assert len(ctx['fds']) == 2
print('INHERITED=YES')
"""
result=m.run([sys.executable,'-I','-c',child,sys.argv[2]])
print(result.stdout.strip()); sys.exit(result.returncode)
'''
            with fence.hold_executors(scope, str(locks)) as descriptors:
                fd = fence.create_sealed_context(scope, descriptors, boot)
                try:
                    result = subprocess.run([sys.executable, "-I", "-c", driver, str(policy), str(fence_path)],
                        env=dict(os.environ, SLT_PACKAGE_FENCE_FD=str(fd)),
                        pass_fds=(*descriptors, fd), capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "INHERITED=YES")
                finally:
                    os.close(fd)


if __name__ == "__main__":
    unittest.main()
