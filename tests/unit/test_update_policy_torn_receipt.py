"""A killed receipt writer must leave an unreadable record fail-closed."""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import unittest


POLICY = Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy'


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0,
                     'Linux root state-file contract')
class TornReceiptTests(unittest.TestCase):
    def test_killed_writer_leaves_record_that_cannot_be_read_as_absent(self):
        loader = importlib.machinery.SourceFileLoader('torn_receipt_policy', str(POLICY))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        policy = importlib.util.module_from_spec(spec)
        loader.exec_module(policy)
        script = r'''
import importlib.machinery,importlib.util,pathlib,sys,time
l=importlib.machinery.SourceFileLoader('policy',sys.argv[1]); s=importlib.util.spec_from_loader(l.name,l)
m=importlib.util.module_from_spec(s); l.exec_module(m)
def partial(value,handle,**kwargs):
    handle.write('{"state":'); handle.flush(); print('READY',flush=True); time.sleep(60)
m.json.dump=partial
m.create_json(pathlib.Path(sys.argv[2]),{'state':'PREPARED'})
'''
        with tempfile.TemporaryDirectory(prefix='slt-receipt-', dir='/root') as directory:
            receipt = Path(directory) / 'receipt.json'
            child = subprocess.Popen([sys.executable, '-c', script, str(POLICY), str(receipt)],
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                ready, _, _ = select.select([child.stdout], [], [], 10)
                self.assertTrue(ready, 'receipt writer did not reach partial write')
                self.assertEqual(child.stdout.readline(), b'READY\n')
                child.kill()
                child.wait(timeout=5)
                self.assertTrue(receipt.exists())
                with self.assertRaises(json.JSONDecodeError):
                    policy.read_json(receipt, default=None)
                self.assertTrue(receipt.exists(), 'failed read must not clear evidence')
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


if __name__ == '__main__':
    unittest.main()
