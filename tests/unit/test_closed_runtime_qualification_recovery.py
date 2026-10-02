"""Exact completed-package recovery, using only the in-memory policy store."""
import copy
from contextlib import redirect_stdout
import io
import types
import unittest
from unittest import mock

import test_freeze_package_settlement as fixture_module


class ClosedRuntimeQualificationRecoveryTests(unittest.TestCase):
    row = staticmethod(fixture_module.FreezePackageSettlementTests.row)
    write = fixture_module.FreezePackageSettlementTests.write
    create = fixture_module.FreezePackageSettlementTests.create
    mark = fixture_module.FreezePackageSettlementTests.mark
    prepare = fixture_module.FreezePackageSettlementTests.prepare
    install = fixture_module.FreezePackageSettlementTests.install

    def run_command(self, argv, **kwargs):
        if argv == ['dpkg', '--audit'] or argv == ['dpkg', '--verify', self.source]:
            return types.SimpleNamespace(returncode=0, stdout='', stderr='')
        if '--runtime-qualification' in argv:
            return types.SimpleNamespace(returncode=0, stdout='RUNTIME_QUALIFICATION_READY=YES\n', stderr='')
        raise AssertionError(f'unexpected command: {argv!r}')

    def setUp(self):
        fixture_module.FreezePackageSettlementTests.setUp(self)
        self.stack.enter_context(mock.patch.object(self.m, 'require_root'))
        self.stack.enter_context(mock.patch.object(self.m, 'manifest_and_lib',
                                                   return_value=(self.manifest, self.core)))
        self.stack.enter_context(mock.patch.object(self.m, 'exact_runtime_build_id', return_value='c' * 64))
        self.stack.enter_context(mock.patch.object(self.m, 'exact_hex_identity', return_value='b' * 64))
        self.stack.enter_context(mock.patch.object(self.m.os, 'uname', create=True,
                                  return_value=types.SimpleNamespace(nodename='test-node', release='test-kernel')))
        self.stack.enter_context(mock.patch.object(self.m, 'package_state_records',
            side_effect=lambda names: copy.deepcopy([row for row in self.records if row['package'] in names])))
        self.store[self.m.POLICY] = {'schema': 2, 'mode': 'FREEZE'}
        self.receipt = self.prepare()
        self.install()
        self.args = types.SimpleNamespace(package=self.source, version='2', sha256='b' * 64,
                                         txid=self.receipt['txid'], replacement_txid=None)
        with mock.patch.object(self.m, 'assert_listed_runtime_tuple',
                side_effect=self.m.UnlistedRuntimeTuple({'fixture': 'identity'}, self.core.digest(self.manifest))), \
                redirect_stdout(io.StringIO()):
            self.m.command_settle_unqualified_freeze_package(self.args)
        self.qualification = types.SimpleNamespace(package=self.source, version='2',
            artifact_sha256='b' * 64, plan_digest='b' * 64, transaction_id=self.receipt['txid'])
        self.tuple = {'tuple': {'id': 'exact-fixture', 'status': 'RETEST_REQUIRED'},
                      'manifest_sha256': self.core.digest(self.manifest),
                      'tuple_sha256': 'd' * 64, 'identity_sha256': 'e' * 64}

    def qualify(self):
        # Explicitly mocked classifier transition, not a claim that real
        # unlisted tuples can become qualified without new authoritative proof.
        with mock.patch.object(self.m, 'assert_listed_runtime_tuple', return_value=self.tuple), \
                redirect_stdout(io.StringIO()):
            self.m.command_qualify_runtime(self.qualification)

    def test_completed_package_recovery_keeps_runtime_latch_until_finalization(self):
        completed = copy.deepcopy(self.store[self.m.LAST_PACKAGE_TRANSITION])
        self.qualify()
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertEqual(self.store[self.m.LAST_PACKAGE_TRANSITION], completed)
        pending = self.store[self.m.RUNTIME_QUALIFICATION]
        self.assertEqual(pending['state'], 'QUALIFYING')
        self.assertEqual(pending['prepared_transition'], self.receipt)
        self.assertEqual(pending['package_settlement_sha256'], self.core.digest(completed))
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]['qualified'], 'QUALIFIED')
        self.assertNotIn(self.m.LAST_RUNTIME_FINALIZATION, self.store)

    def test_lost_ack_after_qualifying_publication_replays_exact_completed_package(self):
        fired = False
        def fault(path, value):
            nonlocal fired
            self.write(path, value)
            if path == self.m.RUNTIME_QUALIFICATION and value.get('state') == 'QUALIFYING' and not fired:
                fired = True
                raise OSError('lost qualifying ACK')
        with mock.patch.object(self.m, 'atomic_json', side_effect=fault):
            with self.assertRaisesRegex(OSError, 'lost qualifying ACK'):
                self.qualify()
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION]['state'], 'QUALIFYING')
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]['qualified'], 'UNQUALIFIED')
        self.qualify()
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]['qualified'], 'QUALIFIED')
        self.assertIn(self.m.RUNTIME_QUALIFICATION, self.store)

    def test_foreign_completed_transition_refuses_before_runtime_publication(self):
        before = copy.deepcopy(self.store)
        self.store[self.m.LAST_PACKAGE_TRANSITION]['target_version'] = 'foreign'
        with self.assertRaisesRegex(RuntimeError, 'transition changed'):
            self.qualify()
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION], before[self.m.RUNTIME_QUALIFICATION])
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]['qualified'], 'UNQUALIFIED')

    def test_changed_freeze_successor_cannot_reuse_old_completed_authorization(self):
        self.store[self.m.FREEZE]['generation'] = 'f' * 32
        with self.assertRaisesRegex(RuntimeError, 'exact intact FREEZE successor'):
            self.qualify()
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION]['state'], 'PACKAGE_SETTLED_RUNTIME_CLOSED')
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]['qualified'], 'UNQUALIFIED')


if __name__ == '__main__':
    unittest.main()
