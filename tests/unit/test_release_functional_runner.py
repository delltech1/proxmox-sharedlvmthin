"""Only isolated Python child fixtures; never executes storage adapters."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

RUNNER = Path(__file__).resolve().parents[2] / 'experiments/thick-generations/release-functional-runner.py'


class FunctionalRunnerTests(unittest.TestCase):
    def test_report_never_promotes_partial_coverage(self):
        spec = importlib.util.spec_from_file_location('functional_runner', RUNNER)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        plan = {'identity': {'profile': 'fixture'}, 'mandatory_coverage': ['snapshot', 'migration'],
                'steps': [{'id': 'one', 'coverage': ['snapshot']},
                          {'id': 'two', 'coverage': ['snapshot', 'migration']}]}
        for state in ('NOT_RUN', 'FAIL', 'UNKNOWN'):
            report = module.result_report(plan, 'a'*64, {'one': 'PASS', 'two': state})
            self.assertNotEqual(report['status'], 'PASS')
            self.assertEqual(report['coverage'], {'snapshot': state, 'migration': state})
            self.assertEqual(report['plan_sha256'], 'a'*64)
        report = module.result_report(plan, 'a'*64, {'one': 'PASS', 'two': 'PASS'})
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['coverage']['migration'], 'PASS')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.plan = self.root / 'plan.json'
        self.run = self.root / 'run'

    def prepare(self, command='pass', verify="print('VERIFIED')", timeout=5, deadline=None):
        step = dict(id='one', argv=[sys.executable, '-c', command],
                    verify_argv=[sys.executable, '-c', verify], success_marker='VERIFIED',
                    poll_seconds=timeout, command_deadline_seconds=deadline, coverage=['fixture'])
        plan = dict(schema=1, identity=dict(profile='dual', artifact_sha256='a'*64,
                    platform_sha256='b'*64, package='fixture', version='1', platform='isolated'),
                    disposable_ids=[999901], mandatory_coverage=['fixture'], steps=[step])
        self.plan.write_text(json.dumps(plan))
        return hashlib.sha256(self.plan.read_bytes()).hexdigest()

    def invoke(self, *args):
        return subprocess.run([sys.executable, str(RUNNER), str(self.plan), *args],
                              capture_output=True, text=True, timeout=15)

    def execute(self, digest):
        return self.invoke('--execute', '--plan-sha256', digest, '--run-dir', str(self.run))

    def summary(self):
        return json.loads(next(self.run.glob('*-summary.json')).read_text())

    def test_default_inert_and_hash_mismatch(self):
        marker = self.root / 'must-not-exist'
        self.prepare(f'from pathlib import Path; Path({str(marker)!r}).touch()')
        self.assertEqual(self.invoke().returncode, 0)
        self.assertFalse(marker.exists())
        self.assertNotEqual(self.execute('0'*64).returncode, 0)
        self.assertFalse(self.run.exists())

    def test_duplicate_and_nonfinite_json_refused(self):
        for value in ('{"schema":1,"schema":1}', '{"schema":NaN}', '{"schema":Infinity}'):
            self.plan.write_text(value)
            self.assertNotEqual(self.invoke().returncode, 0)
        self.prepare()
        self.plan.write_text(self.plan.read_text().replace('"poll_seconds": 5', '"poll_seconds": 1e999'))
        self.assertNotEqual(self.invoke().returncode, 0)

    def test_invalid_deadline_and_legacy_field_refused(self):
        for value in (0, -1, True, float('inf')):
            self.prepare(deadline=value)
            self.assertNotEqual(self.invoke().returncode, 0)
        self.prepare()
        plan = json.loads(self.plan.read_text()); plan['steps'][0]['observation_seconds'] = 1
        self.plan.write_text(json.dumps(plan))
        self.assertNotEqual(self.invoke().returncode, 0)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
    def test_slow_child_polling_is_not_failure(self):
        for exitcode in (0, 7):
            self.run = self.root / ('slow-' + str(exitcode))
            digest = self.prepare(f'import time; time.sleep(.2); raise SystemExit({exitcode})', timeout=.02)
            result = self.execute(digest)
            self.assertEqual(result.returncode == 0, exitcode == 0)
            self.assertEqual(self.summary()['status'], 'PASS' if exitcode == 0 else 'FAIL')
            self.assertGreaterEqual(len(list(self.run.glob('*-STILL_RUNNING.json'))), 2)
            self.assertIn('STEP_STARTED: one', result.stdout)
            self.assertIn('step=one phase=command', result.stdout)
            self.assertIn('STEP_RESULT: one ' + ('PASS' if exitcode == 0 else 'FAIL'), result.stdout)
            records = [json.loads(p.read_text()) for p in self.run.glob('*-process.json')]
            self.assertEqual(sum(r['phase'] == 'command' for r in records), 1)
            self.assertEqual((self.run/'one-verify.stdout').exists(), exitcode == 0)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
    def test_interrupt_and_journal_failure_reap_without_second_dispatch(self):
        for fault in ('interrupt', 'process', 'UNKNOWN', 'identity'):
            with self.subTest(fault=fault):
                self.run = self.root / ('run-' + fault)
                done = self.root / ('done-' + fault)
                digest = self.prepare(f'import time; from pathlib import Path; time.sleep(.2); Path({str(done)!r}).touch()',
                                      timeout=.03 if fault in ('UNKNOWN', 'identity') else 5,
                                      deadline=.03 if fault == 'UNKNOWN' else None)
                wrapper = '''import importlib.util,sys
spec=importlib.util.spec_from_file_location('runner',sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fault=sys.argv[5]; fired=False
original_write=m.Journal.write
original_wait=m.subprocess.Popen.wait
original_identity=m.identity
identity_calls=0
def identity(pid):
 global identity_calls
 identity_calls+=1
 result=original_identity(pid)
 if fault=='identity' and identity_calls>=3:
  result['starttime']+=1
 return result
def write(self,kind,value):
 global fired
 if not fired and kind==fault:
  fired=True
  raise OSError('fixture journal failure')
 return original_write(self,kind,value)
def wait(self,*args,**kwargs):
 global fired
 if fault=='interrupt' and not fired:
  fired=True
  raise KeyboardInterrupt()
 return original_wait(self,*args,**kwargs)
m.Journal.write=write; m.subprocess.Popen.wait=wait; m.identity=identity
sys.exit(m.execute(m.validate(m.strict_json(m.Path(sys.argv[2]).read_bytes())),sys.argv[3],m.Path(sys.argv[4])))
'''
                result = subprocess.run([sys.executable, '-c', wrapper, str(RUNNER), str(self.plan),
                                         digest, str(self.run), fault], capture_output=True, text=True, timeout=15)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(done.exists(), fault != 'process')
                self.assertEqual(self.summary()['status'], 'UNKNOWN')
                self.assertFalse((self.run / 'one-verify.stdout').exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
    def test_success_and_no_second_worker(self):
        counter = self.root / 'counter'
        digest = self.prepare(f'from pathlib import Path; p=Path({str(counter)!r}); p.write_text(p.read_text()+"x" if p.exists() else "x")')
        self.assertEqual(self.execute(digest).returncode, 0)
        self.assertEqual(self.summary()['status'], 'PASS')
        self.assertNotEqual(self.execute(digest).returncode, 0)
        self.assertNotEqual(self.invoke('--resume', '--run-dir', str(self.run)).returncode, 0)
        self.assertEqual(counter.read_text(), 'x')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
    def test_command_failure_never_verifies(self):
        marker = self.root / 'verifier'
        digest = self.prepare('raise SystemExit(17)', f'from pathlib import Path; Path({str(marker)!r}).touch(); print("VERIFIED")')
        self.assertNotEqual(self.execute(digest).returncode, 0)
        self.assertEqual(self.summary()['status'], 'FAIL')
        self.assertFalse(marker.exists())

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
    def test_marker_requires_exact_single_line_and_zero_exit(self):
        for code in ("print('prefix VERIFIED')", "print('VERIFIED twice VERIFIED')",
                     "print('VERIFIED'); print('VERIFIED')", "print('VERIFIED'); raise SystemExit(4)"):
            with self.subTest(code=code):
                self.run = self.root / ('run' + str(len(list(self.root.glob('run*')))))
                self.assertNotEqual(self.execute(self.prepare(verify=code)).returncode, 0)
                self.assertEqual(self.summary()['status'], 'FAIL')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux process identity required')
    def test_timeout_observes_existing_child_no_verify_or_next_step(self):
        done = self.root / 'done'
        self.prepare(f'import time; from pathlib import Path; time.sleep(.2); Path({str(done)!r}).touch()', timeout=.01, deadline=.03)
        plan = json.loads(self.plan.read_text())
        second = dict(plan['steps'][0], id='two', argv=[sys.executable, '-c', 'raise SystemExit(99)'])
        plan['steps'].append(second)
        self.plan.write_text(json.dumps(plan))
        result = self.execute(hashlib.sha256(self.plan.read_bytes()).hexdigest())
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(done.exists())
        self.assertEqual(self.summary()['steps'], {'one': 'UNKNOWN', 'two': 'NOT_RUN'})
        self.assertFalse((self.run / 'one-verify.stdout').exists())
        processes = list(self.run.glob('*-process.json'))
        self.assertEqual(len(processes), 1)
        self.assertGreater(json.loads(processes[0].read_text())['identity']['starttime'], 0)


if __name__ == '__main__':
    unittest.main()
