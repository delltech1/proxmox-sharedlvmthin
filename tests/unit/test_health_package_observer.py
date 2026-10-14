"""Package health evidence must bind the exact displayed snapshot."""
import ast
import importlib
import os
from pathlib import Path
import platform
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

HEALTH = Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-health-json'


@unittest.skipUnless(os.name == 'posix', 'Linux boot identity')
class HealthPackageObserverTests(unittest.TestCase):
    def invoke(self, output=None, rc=0, changed_objects=False, changed_references=False,
               changed_scope=False, timeout=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'sharedlvmthin-recovery-check').write_text('# no probes\n')
            namespace = {'os': os, 'Path': Path, 'platform': platform,
                         'subprocess': subprocess, 'importlib': importlib,
                         '__file__': str(root / 'health-json')}
            tree = ast.parse(HEALTH.read_text())
            nodes = [item for item in tree.body if isinstance(item, ast.FunctionDef)
                     and item.name == 'active_package_health_observer']
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(HEALTH), 'exec'), namespace)
            namespace['health_object_identity'] = lambda inventory: inventory
            namespace['lvm_volume_inventory'] = lambda vg: 'changed' if changed_objects else 'original'
            namespace['pve_volume_reference_index'] = lambda **kw: 'changed' if changed_references else 'refs'
            context = {'scopes': [['scope', 'vm-901-disk-0']], 'fds': [42]}
            fence = SimpleNamespace(read_sealed_context=lambda *args: context)
            def unchanged(before, after):
                if before != after:
                    raise RuntimeError('scope changed')
            collector = SimpleNamespace(collect_snapshot=lambda *args: {'scopes':
                (('other' if changed_scope else 'scope', 'vm-901-disk-0'),)},
                require_unchanged=unchanged)
            evidence = output if output is not None else (
                'STORAGE_ID=scope\nSTATE=PACKAGE_PRESERVATION_READY\n'
                'PACKAGE_PRESERVATION_READY=YES\nSAFE_FOR_MUTATION=NO\n')
            with patch.dict('sys.modules', {'sharedlvmthin_package_executor_fence': fence,
                    'sharedlvmthin_package_preservation': collector}), \
                 patch.dict(os.environ, {'SLT_PACKAGE_FENCE_FD': '43'}), \
                 patch.object(subprocess, 'run', side_effect=(subprocess.TimeoutExpired('probe', 300)
                    if timeout else None), return_value=SimpleNamespace(returncode=rc, stdout=evidence)):
                return namespace['active_package_health_observer']('scope', 'vg', 'original', 'refs')

    def test_valid_proof_is_volume_scoped(self):
        observer = self.invoke()
        self.assertTrue(observer('scope', 'vm-901-disk-0'))
        self.assertFalse(observer('scope', 'vm-902-disk-0'))

    def test_failed_incomplete_or_mutating_evidence_is_refused(self):
        for kwargs in ({'rc': 2}, {'output': ''}, {'output': 'SAFE_FOR_MUTATION=YES\n'},
                       {'output': 'STORAGE_ID=scope\nSTORAGE_ID=scope\n'},
                       {'changed_objects': True}, {'changed_references': True},
                       {'changed_scope': True}):
            with self.subTest(kwargs=kwargs), self.assertRaises((ValueError, RuntimeError)):
                self.invoke(**kwargs)

    def test_timeout_does_not_yield_a_callback(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.invoke(timeout=True)
