"""Real profile finalizer + metadata-only recovery with mocked filesystem I/O.

These are state-machine/fault tests, not native inode or fsync qualification.
"""
import contextlib
import copy
import io
import types
import unittest
from unittest import mock

import test_freeze_unqualified_settlement as existing


class ClosedProfileEvidenceTests(unittest.TestCase):
    row = staticmethod(existing.ClosedSettlementTests.row)
    write = existing.ClosedSettlementTests.write
    create = existing.ClosedSettlementTests.create
    mark = existing.ClosedSettlementTests.mark
    prepare = existing.ClosedSettlementTests.prepare
    install = existing.ClosedSettlementTests.install
    replacement = existing.ClosedSettlementTests.replacement
    unlisted = existing.ClosedSettlementTests.unlisted
    command = existing.ClosedSettlementTests.command
    assert_closed = existing.ClosedSettlementTests.assert_closed

    def run_command(self, argv, **kwargs):
        if argv[:2] == ['dpkg', '--compare-versions']:
            return types.SimpleNamespace(returncode=0 if int(argv[2]) >= int(argv[4]) else 1,
                                         stdout='', stderr='')
        return existing.ClosedSettlementTests.run_command(self, argv, **kwargs)

    def setUp(self):
        existing.ClosedSettlementTests.setUp(self)
        self.original_prepared = copy.deepcopy(self.store[self.m.PACKAGE_TRANSITION])
        self.replacement_txid = self.args.replacement_txid
        self.candidate = self.helper.ROOT / 'candidate.deb'
        self.store[self.candidate] = 'exact candidate bytes'
        self.events = []
        self.fault = None
        self.stack.enter_context(mock.patch.object(self.helper, 'Locked', side_effect=contextlib.nullcontext))
        self.stack.enter_context(mock.patch.object(self.helper, 'read_boot_id', return_value='test-boot'))
        self.stack.enter_context(mock.patch.object(self.helper, 'executor_is_alive', return_value=False))
        self.stack.enter_context(mock.patch.object(self.helper, 'read_record', side_effect=self.read_record))
        self.stack.enter_context(mock.patch.object(self.helper, 'write_create', side_effect=self.create_profile))
        self.stack.enter_context(mock.patch.object(self.helper, 'safe_regular', side_effect=self.safe_regular))
        self.stack.enter_context(mock.patch.object(self.helper, 'validate_candidate', side_effect=self.validate_candidate))
        self.stack.enter_context(mock.patch.object(self.helper, 'installed_exact', side_effect=self.installed_exact))
        self.stack.enter_context(mock.patch.object(self.helper, 'fsync_dir', side_effect=lambda p: self.event('fsync', p)))
        self.stack.enter_context(mock.patch.object(self.m.os.path, 'lexists', side_effect=lambda p: self.m.pathlib.Path(p) in self.store))
        self.stack.enter_context(mock.patch.object(self.m.pathlib.Path, 'unlink', lambda path: self.unlink(path)))
        self.stack.enter_context(mock.patch.object(self.m.os, 'replace', side_effect=self.replace_file))

    def event(self, kind, path):
        self.events.append((kind, path))
        if self.fault == len(self.events):
            raise OSError('persisted effect lost ACK')

    def read_record(self, path):
        if path not in self.store:
            raise FileNotFoundError(str(path))
        return copy.deepcopy(self.store[path])

    def safe_regular(self, path, mode=None):
        if path not in self.store:
            raise FileNotFoundError(str(path))

    def validate_candidate(self, record):
        if self.store.get(self.candidate) != 'exact candidate bytes' or record['candidate_sha256'] != 'b' * 64:
            raise self.helper.Refusal('candidate identity changed')

    def installed_exact(self, record):
        if self.m.package_version(record['target_package']) != record['target_version']:
            raise self.helper.Refusal('target package is not exactly installed')

    def create_profile(self, path, value):
        self.create(path, value)
        self.event('create', path)

    def unlink(self, path):
        self.store.pop(path)
        self.event('unlink', path)

    def replace_file(self, source, target):
        self.assertNotIn(target, self.store)
        self.store[target] = self.store.pop(source)
        self.event('replace', target)

    def close(self, *, superseded=False):
        with contextlib.redirect_stdout(io.StringIO()):
            self.real_profile_closure(self.store[self.m.LAST_PACKAGE_TRANSITION]['txid'],
                                      self.replacement_txid, superseded=superseded)

    def frozen_package_state(self):
        return {path: copy.deepcopy(self.store.get(path)) for path in
                (self.m.RUNTIME_QUALIFICATION, self.m.RUNTIME_RELEASE, self.m.LAST_PACKAGE_TRANSITION,
                 self.m.PACKAGE_TRANSITION, self.m.FREEZE, self.m.POLICY)}

    def make_superseded(self):
        # Reproduce legacy installed states: two successful closed same-profile
        # upgrades left an older cross-profile INTENT/DONE/candidate behind.
        self.command()
        for old, new, digest in (('1', '2', 'c' * 64), ('2', '3', 'f' * 64)):
            receipt = self.m.prepare_freeze_package(self.target, old, self.target, new,
                'all', digest, 'candidate.json', self.manifest, self.core)
            self.records[0] = self.row(self.target, new, 'all')
            self.holds.discard(self.target)
            self.stack.enter_context(mock.patch.object(self.m, 'exact_hex_identity', return_value=digest))
            self.args = types.SimpleNamespace(package=self.target, version=new, sha256=digest,
                txid=receipt['txid'], replacement_txid=None)
            self.command()
        self.next_prepared = self.m.prepare_freeze_package(self.target, '3', self.source, '3',
            'all', 'a' * 64, 'candidate.json', self.manifest, self.core)

    def test_ordinary_settlement_auto_closes_exact_profile_proof(self):
        self.profile_closure.side_effect = self.real_profile_closure
        self.command()
        archive = self.helper.ARCHIVE / (self.replacement_txid + '.json')
        self.assertEqual(self.store[archive]['settlement'], 'DPKG_EXIT_ZERO')
        self.assertNotIn(self.helper.DONE, self.store)
        self.assertNotIn(self.helper.INTENT, self.store)
        self.assertNotIn(self.candidate, self.store)
        self.assert_closed()
        before = copy.deepcopy(self.store)
        self.command()
        self.assertEqual(self.store, before)

    def test_carrier_settlement_uses_the_same_exact_profile_closure(self):
        self.profile_closure.side_effect = self.real_profile_closure
        identity = {'schema': 'sharedlvmthin-recovery-executor/v1',
                    'carrier_sha256': 'e' * 64, 'helper_sha256': 'f' * 64, 'core_sha256': 'a' * 64}
        with mock.patch.object(self.m, 'recovery_executor_identity', return_value=identity):
            self.command()
            self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION]['recovery_executor'], identity)
            self.assertEqual(self.store[self.helper.ARCHIVE / (self.replacement_txid + '.json')]['settlement'],
                             'DPKG_EXIT_ZERO')
            before = copy.deepcopy(self.store)
            self.command()
            self.assertEqual(self.store, before)
        self.assert_closed()

    def test_exact_closure_preserves_a_new_prepared_transition(self):
        self.command()
        self.m.prepare_freeze_package(self.target, '1', self.source, '1',
            'all', 'a' * 64, 'candidate.json', self.manifest, self.core)
        before = self.frozen_package_state()
        self.close()
        self.assertEqual(self.frozen_package_state(), before)
        self.assert_closed()

    def test_normal_finalizer_all_persisted_cleanup_prefixes_replay(self):
        self.command()
        self.assert_all_cleanup_prefixes_replay(False)

    def assert_all_cleanup_prefixes_replay(self, superseded):
        initial = copy.deepcopy(self.store)
        frozen = self.frozen_package_state()
        self.events.clear()
        self.close(superseded=superseded)
        count = len(self.events)
        self.assertGreaterEqual(count, 8)
        for cut in range(1, count + 1):
            with self.subTest(cut=cut, superseded=superseded):
                self.store = copy.deepcopy(initial)
                self.events.clear()
                self.fault = cut
                with self.assertRaisesRegex(OSError, 'lost ACK'):
                    self.close(superseded=superseded)
                self.assertEqual(self.frozen_package_state(), frozen)
                self.assert_closed()
                self.fault = None
                self.close(superseded=superseded)
                before = copy.deepcopy(self.store)
                self.close(superseded=superseded)
                self.assertEqual(self.store, before)

    def test_superseded_two_upgrades_and_next_profile_prepared_are_metadata_only(self):
        self.make_superseded()
        before = self.frozen_package_state()
        self.close(superseded=True)
        archive = self.helper.ARCHIVE / (self.replacement_txid + '.json')
        record = self.store[archive]
        self.assertEqual(record['settlement'], 'SUPERSEDED_DPKG_EXIT_ZERO_RUNTIME_CLOSED')
        self.assertEqual(record['metadata_cleanup_proof']['authority'], 'NONE')
        self.assertEqual(record['metadata_cleanup_proof']['ancestry'], 'NOT_CLAIMED')
        self.assertEqual(record['metadata_cleanup_proof']['next_prepared_txid'], self.next_prepared['txid'])
        self.assertEqual(self.frozen_package_state(), before)
        with self.assertRaises(self.helper.Refusal):
            self.helper.validate_complete(record)
        with self.assertRaises(self.helper.Refusal):
            self.m.replacement_proof(self.original_prepared, self.replacement_txid, self.core)
        self.assert_closed()

    def test_superseded_all_persisted_cleanup_prefixes_replay(self):
        self.make_superseded()
        self.assert_all_cleanup_prefixes_replay(True)

    def test_superseded_downgrade_foreign_profile_active_executor_and_fake_done_refuse(self):
        self.make_superseded()
        initial = copy.deepcopy(self.store)
        cases = ('downgrade', 'profile', 'executor', 'failed', 'missing-intent', 'foreign-intent', 'candidate')
        for case in cases:
            with self.subTest(case=case):
                self.store = copy.deepcopy(initial)
                self.helper.executor_is_alive.return_value = case == 'executor'
                if case == 'downgrade':
                    for path in (self.helper.INTENT, self.helper.DONE):
                        self.store[path]['source_version'] = self.store[path]['target_version'] = '4'
                elif case == 'profile':
                    for path in (self.helper.INTENT, self.helper.DONE):
                        record = self.store[path]
                        record['source_package'], record['target_package'] = record['target_package'], record['source_package']
                        record['source_flavor'], record['target_flavor'] = record['target_flavor'], record['source_flavor']
                elif case == 'failed':
                    self.store[self.helper.DONE]['dpkg_exit'] = 1
                elif case == 'missing-intent':
                    self.store.pop(self.helper.INTENT)
                elif case == 'foreign-intent':
                    self.store[self.helper.INTENT]['txid'] = 'f' * 32
                elif case == 'candidate':
                    self.store[self.candidate] = 'foreign candidate'
                before = copy.deepcopy(self.store)
                with self.assertRaises(RuntimeError):
                    self.close(superseded=True)
                self.assertEqual(self.store, before)

    def test_superseded_current_payload_failure_and_new_prepared_drift_refuse(self):
        self.make_superseded()
        before = copy.deepcopy(self.store)
        with mock.patch.object(self.m, 'exact_hex_identity', return_value='e' * 64):
            with self.assertRaisesRegex(RuntimeError, 'package proof changed'):
                self.close(superseded=True)
        self.assertEqual(self.store, before)
        self.store[self.m.PACKAGE_TRANSITION]['source_version'] = 'foreign'
        before = copy.deepcopy(self.store)
        with self.assertRaises(RuntimeError):
            self.close(superseded=True)
        self.assertEqual(self.store, before)

    def test_explicit_carrier_cleanup_binds_executing_code_before_state_change(self):
        self.make_superseded()
        args = types.SimpleNamespace(
            txid=self.store[self.m.LAST_PACKAGE_TRANSITION]['txid'],
            replacement_txid=self.replacement_txid,
            superseded=True,
            package=self.target,
            recovery_code_deb='/approved/recovery.deb',
            recovery_code_sha256='f' * 64,
        )
        before = copy.deepcopy(self.store)
        with mock.patch.object(self.m, 'recovery_executor_identity',
                               side_effect=RuntimeError('carrier mismatch')) as identity:
            with self.assertRaisesRegex(RuntimeError, 'carrier mismatch'):
                self.m.command_finalize_closed_profile_evidence(args)
        identity.assert_called_once_with(args)
        self.assertEqual(self.store, before)

        self.profile_closure.side_effect = self.real_profile_closure
        with mock.patch.object(self.m, 'recovery_executor_identity',
                               return_value={'carrier_sha256': 'f' * 64}) as identity:
            self.m.command_finalize_closed_profile_evidence(args)
        identity.assert_called_once_with(args)
        archive = self.helper.ARCHIVE / (self.replacement_txid + '.json')
        self.assertEqual(self.store[archive]['settlement'],
                         'SUPERSEDED_DPKG_EXIT_ZERO_RUNTIME_CLOSED')
        self.assert_closed()


if __name__ == '__main__':
    unittest.main()
