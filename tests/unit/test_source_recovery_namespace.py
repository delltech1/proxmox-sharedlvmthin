"""Prepared-source recovery must observe only its injected JSON state reader."""
import copy
import unittest
from unittest import mock

import test_freeze_package_settlement as existing


class SourceRecoveryNamespaceTests(unittest.TestCase):
    row = staticmethod(existing.FreezePackageSettlementTests.row)
    write = existing.FreezePackageSettlementTests.write
    create = existing.FreezePackageSettlementTests.create
    mark = existing.FreezePackageSettlementTests.mark
    prepare = existing.FreezePackageSettlementTests.prepare
    run_command = existing.FreezePackageSettlementTests.run_command

    def setUp(self):
        existing.FreezePackageSettlementTests.setUp(self)
        self.receipt = self.prepare()

    def recover(self):
        return self.m.recover_prepared_freeze_source(self.receipt['txid'], self.manifest, self.core)

    def test_no_host_exists_probe_even_when_production_latch_would_exist(self):
        with mock.patch.object(self.m.pathlib.Path, 'exists',
                               side_effect=AssertionError('foreign filesystem probe')):
            result = self.recover()
            self.assertFalse(result['runtime_release_invalidated'])
            self.assertEqual(self.recover(), result)
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)

    def test_every_present_json_value_in_each_gate_refuses(self):
        initial = copy.deepcopy(self.store)
        for path in (self.m.POST_GATE, self.m.RUNTIME_QUALIFICATION, self.m.FREEZE_MIGRATION):
            for value in (None, False, {}, [], {'state': 'PACKAGE_SETTLED_RUNTIME_CLOSED'}):
                with self.subTest(path=path, value=value):
                    self.store = copy.deepcopy(initial)
                    self.store[path] = value
                    before = copy.deepcopy(self.store)
                    with self.assertRaisesRegex(RuntimeError, 'another gate is pending'):
                        self.recover()
                    self.assertEqual(self.store, before)

    def test_malformed_or_unsafe_json_reader_error_is_not_absence(self):
        reader = self.m.read_json.side_effect
        def reject(path, default=None):
            if path == self.m.RUNTIME_QUALIFICATION:
                raise RuntimeError('unsafe or malformed fixture state')
            return reader(path, default)
        before = copy.deepcopy(self.store)
        with mock.patch.object(self.m, 'read_json', side_effect=reject):
            with self.assertRaisesRegex(RuntimeError, 'unsafe or malformed'):
                self.recover()
        self.assertEqual(self.store, before)

    def test_gate_appearing_during_dpkg_audit_refuses_before_archive(self):
        def audit(argv, **kwargs):
            self.store[self.m.RUNTIME_QUALIFICATION] = {'state': 'foreign'}
            return self.run_command(argv, **kwargs)
        with mock.patch.object(self.m, 'run', side_effect=audit):
            with self.assertRaisesRegex(RuntimeError, 'CAS changed'):
                self.recover()
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertNotIn(self.m.PACKAGE_RECOVERY_ARCHIVE / (self.receipt['txid'] + '.json'), self.store)

    def test_release_invalidation_flag_uses_injected_json_presence(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        result = self.recover()
        self.assertTrue(result['runtime_release_invalidated'])
        self.assertNotIn(self.m.RUNTIME_RELEASE, self.store)

    def test_lost_ack_after_archive_and_each_unlink_replays_exactly(self):
        initial = copy.deepcopy(self.store)
        initial[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED', 'txid': 'old'}
        archive = self.m.PACKAGE_RECOVERY_ARCHIVE / (self.receipt['txid'] + '.json')
        for cut in ('archive', 'release', 'transition'):
            with self.subTest(cut=cut):
                self.store = copy.deepcopy(initial)
                def create(path, value):
                    self.create(path, value)
                    if cut == 'archive' and path == archive:
                        raise OSError('lost ACK')
                def unlink(path):
                    self.store.pop(path, None)
                    if (cut == 'release' and path == self.m.RUNTIME_RELEASE) or \
                            (cut == 'transition' and path == self.m.PACKAGE_TRANSITION):
                        raise OSError('lost ACK')
                with mock.patch.object(self.m, 'create_json', side_effect=create), \
                        mock.patch.object(self.m, 'unlink_durable', side_effect=unlink):
                    with self.assertRaisesRegex(OSError, 'lost ACK'):
                        self.recover()
                self.assertIn(archive, self.store)
                self.assertNotIn(self.m.RUNTIME_QUALIFICATION, self.store)
                result = self.recover()
                self.assertTrue(result['runtime_release_invalidated'])
                self.assertEqual(result['runtime_release_sha256'],
                                 self.core.digest(initial[self.m.RUNTIME_RELEASE]))
                self.assertNotIn(self.m.RUNTIME_RELEASE, self.store)
                self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)
                before = copy.deepcopy(self.store)
                self.assertEqual(self.recover(), result)
                self.assertEqual(self.store, before)

    def test_archive_or_release_drift_never_erases_evidence(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        archive = self.m.PACKAGE_RECOVERY_ARCHIVE / (self.receipt['txid'] + '.json')
        def stop(path, value):
            self.create(path, value)
            raise OSError('lost ACK')
        with mock.patch.object(self.m, 'create_json', side_effect=stop):
            with self.assertRaises(OSError):
                self.recover()
        original = copy.deepcopy(self.store)
        for key, value in (('txid', 'f' * 32), ('candidate_sha256', 'f' * 64),
                           ('runtime_release_invalidated', 'true'),
                           ('recovery_proof', 'foreign'), ('foreign', True)):
            with self.subTest(key=key):
                self.store = copy.deepcopy(original)
                self.store[archive][key] = value
                before = copy.deepcopy(self.store)
                with self.assertRaises(RuntimeError):
                    self.recover()
                self.assertEqual(self.store, before)
        self.store = copy.deepcopy(original)
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED', 'txid': 'foreign'}
        before = copy.deepcopy(self.store)
        with self.assertRaisesRegex(RuntimeError, 'cannot prove'):
            self.recover()
        self.assertEqual(self.store, before)

    def test_legacy_archive_only_replays_after_release_is_absent(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        archive = self.m.PACKAGE_RECOVERY_ARCHIVE / (self.receipt['txid'] + '.json')
        def stop(path, value):
            self.create(path, value)
            raise OSError('lost ACK')
        with mock.patch.object(self.m, 'create_json', side_effect=stop):
            with self.assertRaises(OSError):
                self.recover()
        del self.store[archive]['runtime_release_sha256']
        before = copy.deepcopy(self.store)
        with self.assertRaisesRegex(RuntimeError, 'cannot prove'):
            self.recover()
        self.assertEqual(self.store, before)
        del self.store[self.m.RUNTIME_RELEASE]
        self.assertTrue(self.recover()['runtime_release_invalidated'])
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)

    def test_completed_replay_revalidates_freeze_and_refuses_new_release(self):
        self.recover()
        completed = copy.deepcopy(self.store)
        self.records[0]['version'] = 'foreign'
        with self.assertRaises(RuntimeError):
            self.recover()
        self.assertEqual(self.store, completed)
        self.records[0]['version'] = '1'
        self.holds.remove(self.source)
        with self.assertRaisesRegex(RuntimeError, 'intact|baseline'):
            self.recover()
        self.assertEqual(self.store, completed)
        self.holds.add(self.source)
        self.store[self.m.FREEZE]['generation'] = 'f' * 32
        changed = copy.deepcopy(self.store)
        with self.assertRaisesRegex(RuntimeError, 'ledger changed'):
            self.recover()
        self.assertEqual(self.store, changed)
        self.store = copy.deepcopy(completed)
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        before = copy.deepcopy(self.store)
        with self.assertRaisesRegex(RuntimeError, 'cannot prove'):
            self.recover()
        self.assertEqual(self.store, before)

    def test_archive_change_during_reproof_refuses_before_release_unlink(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        archive = self.m.PACKAGE_RECOVERY_ARCHIVE / (self.receipt['txid'] + '.json')
        def stop(path, value):
            self.create(path, value)
            raise OSError('lost ACK')
        with mock.patch.object(self.m, 'create_json', side_effect=stop):
            with self.assertRaises(OSError):
                self.recover()
        def audit(argv, **kwargs):
            self.store[archive]['recovery_proof'] = 'foreign'
            return self.run_command(argv, **kwargs)
        with mock.patch.object(self.m, 'run', side_effect=audit):
            with self.assertRaisesRegex(RuntimeError, 'CAS changed'):
                self.recover()
        self.assertIn(self.m.RUNTIME_RELEASE, self.store)
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)

    def test_malformed_archive_values_are_not_treated_as_absent(self):
        archive = self.m.PACKAGE_RECOVERY_ARCHIVE / (self.receipt['txid'] + '.json')
        initial = copy.deepcopy(self.store)
        for value in (None, False, {}, [], {'state': 'foreign'}):
            with self.subTest(value=value):
                self.store = copy.deepcopy(initial)
                self.store[archive] = value
                before = copy.deepcopy(self.store)
                with self.assertRaises(RuntimeError):
                    self.recover()
                self.assertEqual(self.store, before)

    def test_new_gate_after_release_unlink_preserves_transition(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        def unlink(path):
            self.store.pop(path, None)
            if path == self.m.RUNTIME_RELEASE:
                self.store[self.m.RUNTIME_QUALIFICATION] = {'state': 'foreign'}
        with mock.patch.object(self.m, 'unlink_durable', side_effect=unlink):
            with self.assertRaisesRegex(RuntimeError, 'CAS changed'):
                self.recover()
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertIn(self.m.RUNTIME_QUALIFICATION, self.store)

    def test_source_baseline_drift_after_release_unlink_preserves_transition(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        def unlink(path):
            self.store.pop(path, None)
            if path == self.m.RUNTIME_RELEASE:
                self.holds.remove(self.source)
        with mock.patch.object(self.m, 'unlink_durable', side_effect=unlink):
            with self.assertRaisesRegex(RuntimeError, 'CAS changed'):
                self.recover()
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertNotIn(self.m.RUNTIME_RELEASE, self.store)

    def test_release_change_after_archive_creation_never_unlinks_it(self):
        self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED'}
        def create(path, value):
            self.create(path, value)
            self.store[self.m.RUNTIME_RELEASE] = {'qualified': 'QUALIFIED', 'foreign': True}
        with mock.patch.object(self.m, 'create_json', side_effect=create):
            with self.assertRaisesRegex(RuntimeError, 'CAS changed'):
                self.recover()
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertTrue(self.store[self.m.RUNTIME_RELEASE]['foreign'])


if __name__ == '__main__':
    unittest.main()
