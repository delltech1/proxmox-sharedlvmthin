import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[2] / 'experiments/thick-generations/pve-native-task-observer.py'
SPEC = importlib.util.spec_from_file_location('native_observer', PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)
UPID = 'UPID:fixture:00000001:00000002:00000003:resize:999901:root@pam:'


class NativeTaskObserverTests(unittest.TestCase):
    def setUp(self):
        self.identity = M.binding(UPID, 'fixture', 'resize', '999901', 'root@pam')
        self.status = dict(self.identity, status='stopped', exitstatus='OK')
        self.log = [{'n': 1, 't': 'fixture log'}]

    def observe(self, first=None, log=None, last=None, refusal=None):
        with patch.object(M, 'fetch', side_effect=[first or self.status, self.log if log is None else log, last or self.status]) as fetch:
            result = M.observe(self.identity, refusal)
            self.assertTrue(all(call.args[0].startswith('/nodes/fixture/tasks/' + UPID + '/') for call in fetch.call_args_list))
            return result

    def test_ok(self):
        self.assertEqual(self.observe()[0]['result'], 'PASS')

    def test_dispatch_noise_and_exact_hash(self):
        import hashlib,json
        raw=('  Logical volume fixture changed.\n'+json.dumps(UPID)+'\n').encode()
        result=M.extract_dispatch_upid(raw,'fixture','resize','999901','root@pam')
        self.assertEqual(result['identity'],self.identity)
        self.assertEqual(result['raw_sha256'],hashlib.sha256(raw).hexdigest())

    def test_dispatch_ambiguity_refused(self):
        import json
        line=(json.dumps(UPID)+'\n').encode()
        for raw in (line+line,line+b'{}\n',line+b'null\n',line[:-3],
                    line.replace(b'999901',b'999902'),line+b'"broken UPID\n'):
            with self.subTest(raw=raw),self.assertRaises(ValueError):
                M.extract_dispatch_upid(raw,'fixture','resize','999901','root@pam')

    def test_exact_refusal_not_ok(self):
        self.status['exitstatus'] = 'shrinking disks is not supported'
        self.assertEqual(self.observe()[0]['result'], 'FAIL')
        self.assertEqual(self.observe(refusal=self.status['exitstatus'])[0]['result'], 'EXPECTED_REFUSAL')

    def test_each_identity_field(self):
        for key in self.identity:
            with self.subTest(key=key):
                changed = dict(self.status); changed[key] = 'wrong'
                with self.assertRaises(ValueError): self.observe(first=changed)

    def test_binding_mismatch(self):
        with self.assertRaises(ValueError): M.binding(UPID, 'other', 'resize', '999901', 'root@pam')

    def test_running_unknown(self):
        self.assertEqual(self.observe(first=dict(self.status, status='running'))[0]['result'], 'UNKNOWN')

    def test_missing_unknown_terminal(self):
        for change in ({'status': 'unknown'}, {'exitstatus': None}, {'exitstatus': ''}):
            with self.assertRaises(ValueError): self.observe(first=dict(self.status, **change))

    def test_changed_terminal(self):
        with self.assertRaises(ValueError): self.observe(last=dict(self.status, exitstatus='ERROR'))

    def test_missing_or_duplicate_log(self):
        for log in ([], [{'n': 1, 't': 'a'}, {'n': 1, 't': 'b'}], [{'t': 'a'}]):
            with self.assertRaises(ValueError): self.observe(log=log)

    def test_only_read_argv_no_shell(self):
        with patch.object(M.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '{}')) as run:
            M.fetch('/nodes/fixture/tasks/' + UPID + '/status')
            self.assertEqual(run.call_args.args[0][:2], ['/usr/bin/pvesh', 'get'])
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_cli_transport_success_not_task_success(self):
        with patch.object(M.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'null')):
            with self.assertRaises(ValueError): M.observe(self.identity)
        with patch.object(M.subprocess, 'run', side_effect=subprocess.TimeoutExpired('fixture', 30)):
            with self.assertRaises(subprocess.TimeoutExpired): M.observe(self.identity)


if __name__ == '__main__':
    unittest.main()
