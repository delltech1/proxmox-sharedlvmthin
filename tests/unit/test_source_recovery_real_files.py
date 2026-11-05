"""Source recovery with real temporary files; package probes are fixtures."""
import copy
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin'


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0,
                     'Linux root file/directory fsync contract')
class SourceRecoveryRealFilesTests(unittest.TestCase):
    def test_replay_after_each_real_durable_write_or_unlink(self):
        m = load('real_source_recovery', ROOT / 'sharedlvmthin-update-policy')
        core = load('real_source_core', ROOT / 'sharedlvmthin_update_policy.py')
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        source = 'pve-sharedlvmthin'
        record = {'phase': 'ACTIVE', 'generation': 'a' * 32}
        receipt = {'schema': 2, 'state': 'PREPARED', 'txid': 'b' * 32,
                   'hostname': os.uname().nodename, 'boot_id': boot,
                   'parent_generation': record['generation'], 'parent_ledger_sha256': core.digest(record),
                   'source_package': source, 'target_package': source, 'source_version': '1',
                   'target_version': '2', 'target_architecture': 'all',
                   'candidate_sha256': 'c' * 64, 'candidate_artifact_sha256': 'd' * 64,
                   'candidate_manifest_sha256': 'e' * 64}
        for cut in ('archive', 'release', 'transition'):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory(prefix='slt-replay-', dir='/root') as directory:
                root = Path(directory)
                archive_dir = root / 'archive'
                archive_dir.mkdir(mode=0o700)
                paths = {'FREEZE': root / 'freeze.json', 'PACKAGE_TRANSITION': root / 'transition.json',
                         'RUNTIME_RELEASE': root / 'release.json', 'POST_GATE': root / 'post.json',
                         'RUNTIME_QUALIFICATION': root / 'pending.json', 'FREEZE_MIGRATION': root / 'migration.json',
                         'PACKAGE_RECOVERY_ARCHIVE': archive_dir}
                with mock.patch.multiple(m, **paths), \
                        mock.patch.object(m, 'freeze_integrity', return_value={'result': 'PASS'}), \
                        mock.patch.object(m, 'freeze_baseline_targets', return_value=[source]), \
                        mock.patch.object(m, 'package_state_records', return_value=[{'package': source, 'version': '1'}]), \
                        mock.patch.object(m, 'held_packages', return_value={source}), \
                        mock.patch.object(m, 'package_version', return_value='1'), \
                        mock.patch.object(m, 'run', return_value=types.SimpleNamespace(returncode=0, stdout='', stderr='')):
                    m.create_json(paths['FREEZE'], record)
                    m.create_json(paths['PACKAGE_TRANSITION'], receipt)
                    m.create_json(paths['RUNTIME_RELEASE'], {'qualified': 'QUALIFIED'})
                    real_create, real_unlink = m.create_json, m.unlink_durable
                    def create(path, value):
                        real_create(path, value)
                        if cut == 'archive':
                            raise OSError('injected lost acknowledgement')
                    def unlink(path):
                        real_unlink(path)
                        if (cut == 'release' and path == paths['RUNTIME_RELEASE']) or \
                                (cut == 'transition' and path == paths['PACKAGE_TRANSITION']):
                            raise OSError('injected lost acknowledgement')
                    with mock.patch.object(m, 'create_json', side_effect=create), \
                            mock.patch.object(m, 'unlink_durable', side_effect=unlink):
                        with self.assertRaisesRegex(OSError, 'lost acknowledgement'):
                            m.recover_prepared_freeze_source(receipt['txid'], {}, core)
                    result = m.recover_prepared_freeze_source(receipt['txid'], {}, core)
                    self.assertEqual(result['state'], 'SOURCE_REVALIDATED_AFTER_FAILED_TRANSACTION')
                    self.assertTrue(result['runtime_release_invalidated'])
                    self.assertFalse(paths['RUNTIME_RELEASE'].exists())
                    self.assertFalse(paths['PACKAGE_TRANSITION'].exists())
                    self.assertEqual(m.recover_prepared_freeze_source(receipt['txid'], {}, core), result)
                    self.assertEqual(m.read_json(archive_dir / (receipt['txid'] + '.json')), result)


if __name__ == '__main__':
    unittest.main()
