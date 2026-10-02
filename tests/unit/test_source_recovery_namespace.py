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


if __name__ == '__main__':
    unittest.main()
