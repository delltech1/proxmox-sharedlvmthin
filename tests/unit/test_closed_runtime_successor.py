"""Closed-to-closed package settlement; in-memory durability fault injection."""
import copy
import types
import unittest
from unittest import mock

import test_freeze_unqualified_settlement as existing


class ClosedRuntimeSuccessorTests(unittest.TestCase):
    row = staticmethod(existing.ClosedSettlementTests.row)
    write = existing.ClosedSettlementTests.write
    create = existing.ClosedSettlementTests.create
    mark = existing.ClosedSettlementTests.mark
    prepare = existing.ClosedSettlementTests.prepare
    install = existing.ClosedSettlementTests.install
    replacement = existing.ClosedSettlementTests.replacement
    run_command = existing.ClosedSettlementTests.run_command
    unlisted = existing.ClosedSettlementTests.unlisted
    command = existing.ClosedSettlementTests.command
    assert_closed = existing.ClosedSettlementTests.assert_closed

    def setUp(self):
        existing.ClosedSettlementTests.setUp(self)
        self.command()
        self.old = copy.deepcopy(self.store)
        self.old_args = copy.deepcopy(self.args)
        receipt = self.m.prepare_freeze_package(self.target, '1', self.target, '2',
            'all', 'c' * 64, 'candidate.json', self.manifest, self.core)
        self.records[0] = self.row(self.target, '2', 'all')
        self.holds.discard(self.target)
        self.args = types.SimpleNamespace(package=self.target, version='2', sha256='c' * 64,
            txid=receipt['txid'], replacement_txid=None)
        self.stack.enter_context(mock.patch.object(self.m, 'exact_hex_identity', return_value='c' * 64))
        self.stack.enter_context(mock.patch.object(self.m, 'exact_runtime_build_id', return_value='e' * 64))
        self.stack.enter_context(mock.patch.object(self.m, 'command_qualify_runtime',
            side_effect=AssertionError('closed settlement cannot qualify')))
        self.stack.enter_context(mock.patch.object(self.m, 'command_finalize_runtime',
            side_effect=AssertionError('closed settlement cannot finalize')))

    def test_exact_successor_pins_predecessor_and_rebases_without_runtime_open(self):
        output = self.command()
        latch = self.store[self.m.RUNTIME_QUALIFICATION]
        self.assertEqual(latch['txid'], self.args.txid)
        self.assertEqual(latch['artifact_sha256'], 'c' * 64)
        self.assertEqual(latch['runtime_build_id'], 'e' * 64)
        archive = self.m.STATE / 'closed-runtime-history' / (self.old_args.txid + '.json')
        self.assertEqual(latch['closed_predecessor'], {
            'qualification_sha256': self.core.digest(self.old[self.m.RUNTIME_QUALIFICATION]),
            'release_sha256': self.core.digest(self.old[self.m.RUNTIME_RELEASE]),
            'package_settlement_sha256': self.core.digest(self.old[self.m.LAST_PACKAGE_TRANSITION]),
            'freeze_ledger_sha256': self.core.digest(self.old[self.m.FREEZE]),
            'txid': self.old_args.txid,
            'preimage_sha256': self.core.digest(self.store[archive]),
        })
        self.assertEqual(latch['previous_runtime_release_sha256'],
                         self.core.digest(self.old[self.m.RUNTIME_RELEASE]))
        self.assertEqual(self.store[self.m.LAST_PACKAGE_TRANSITION]['txid'], self.args.txid)
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertNotIn('EXECUTE_PASS', output)
        self.assert_closed()
        before = copy.deepcopy(self.store)
        self.effects.clear()
        self.command()
        self.assertEqual(self.store, before)
        self.assertEqual(self.effects, [])

    def test_every_persisted_write_and_unlink_prefix_is_closed_and_replayable(self):
        initial, holds = copy.deepcopy(self.store), set(self.holds)
        events = []
        def write(path, value):
            events.append(('write', path)); self.write(path, value)
        def unlink(path):
            events.append(('unlink', path)); self.store.pop(path, None)
        with mock.patch.object(self.m, 'atomic_json', side_effect=write), \
                mock.patch.object(self.m, 'create_json', side_effect=write), \
                mock.patch.object(self.m, 'unlink_durable', side_effect=unlink):
            self.command()
        self.assertEqual(events[0], ('write', self.m.STATE / 'closed-runtime-history' / (self.old_args.txid + '.json')))
        self.assertEqual(events[1], ('write', self.m.RUNTIME_QUALIFICATION))
        for cut in range(1, len(events) + 1):
            with self.subTest(cut=cut, event=events[cut - 1]):
                self.store, self.holds = copy.deepcopy(initial), set(holds)
                count = [0]
                def after():
                    self.assert_closed()
                    count[0] += 1
                    if count[0] == cut:
                        raise OSError('lost ACK')
                def write_fault(path, value):
                    self.write(path, value); after()
                def unlink_fault(path):
                    self.store.pop(path, None); after()
                with mock.patch.object(self.m, 'atomic_json', side_effect=write_fault), \
                        mock.patch.object(self.m, 'create_json', side_effect=write_fault), \
                        mock.patch.object(self.m, 'unlink_durable', side_effect=unlink_fault):
                    with self.assertRaisesRegex(OSError, 'lost ACK'):
                        self.command()
                self.command()
                self.assert_closed()
                successor = copy.deepcopy(self.store)
                self.effects.clear()
                self.command()
                self.assertEqual(self.store, successor)
                self.assertEqual(self.effects, [])

    def test_bad_predecessor_or_next_edge_refused_without_writes(self):
        initial = copy.deepcopy(self.store)
        cases = [
            (self.m.LAST_PACKAGE_TRANSITION, 'state', 'PREPARED'),
            (self.m.LAST_PACKAGE_TRANSITION, 'txid', 'f' * 32),
            (self.m.LAST_PACKAGE_TRANSITION, 'successor_ledger_sha256', 'f' * 64),
            (self.m.FREEZE, 'generation', 'f' * 32),
            (self.m.RUNTIME_RELEASE, 'qualified', 'QUALIFIED'),
            (self.m.RUNTIME_QUALIFICATION, 'plugin_version', 'foreign'),
            (self.m.PACKAGE_TRANSITION, 'parent_ledger_sha256', 'f' * 64),
            (self.m.PACKAGE_TRANSITION, 'parent_generation', 'f' * 32),
            (self.m.PACKAGE_TRANSITION, 'source_version', 'foreign'),
            (self.m.PACKAGE_TRANSITION, 'source_package', self.source),
            (self.m.PACKAGE_TRANSITION, 'boot_id', 'different-boot'),
        ]
        for path, key, value in cases:
            with self.subTest(path=path, key=key):
                self.store = copy.deepcopy(initial)
                self.store[path][key] = value
                before = copy.deepcopy(self.store)
                with self.assertRaises(RuntimeError):
                    self.command()
                self.assertEqual(self.store, before)

    def test_predecessor_cas_drift_during_package_proof_is_not_overwritten(self):
        original = self.m.replacement_proof
        def drift(*args):
            result = original(*args)
            self.store[self.m.RUNTIME_RELEASE]['verdict'] = 'foreign'
            return result
        with mock.patch.object(self.m, 'replacement_proof', side_effect=drift):
            with self.assertRaises(RuntimeError):
                self.command()
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION], self.old[self.m.RUNTIME_QUALIFICATION])
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]['verdict'], 'foreign')
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)

    def test_stale_predecessor_and_fork_cannot_replace_completed_successor(self):
        fork = copy.deepcopy(self.store[self.m.PACKAGE_TRANSITION])
        self.command()
        completed = copy.deepcopy(self.store)
        fork['txid'] = 'f' * 32
        self.store[self.m.PACKAGE_TRANSITION] = fork
        before = copy.deepcopy(self.store)
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.store, before)
        self.store = copy.deepcopy(completed)
        self.args = self.old_args
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.store, completed)
        self.store[self.m.RUNTIME_QUALIFICATION] = copy.deepcopy(self.old[self.m.RUNTIME_QUALIFICATION])
        before = copy.deepcopy(self.store)
        with self.assertRaises(RuntimeError):
            self.command()
        self.assertEqual(self.store, before)

    def test_third_closed_edge_has_bounded_lineage_and_exact_replay(self):
        self.command()
        second_latch = copy.deepcopy(self.store[self.m.RUNTIME_QUALIFICATION])
        receipt = self.m.prepare_freeze_package(self.target, '2', self.target, '3',
            'all', 'f' * 64, 'candidate.json', self.manifest, self.core)
        self.records[0] = self.row(self.target, '3', 'all')
        self.holds.discard(self.target)
        self.args = types.SimpleNamespace(package=self.target, version='3', sha256='f' * 64,
            txid=receipt['txid'], replacement_txid=None)
        self.m.exact_hex_identity.return_value = 'f' * 64
        self.command()
        latch = self.store[self.m.RUNTIME_QUALIFICATION]
        self.assertEqual(latch['closed_predecessor']['qualification_sha256'], self.core.digest(second_latch))
        self.assertEqual(len(latch['closed_predecessor']), 6)
        self.assertTrue(all(isinstance(value, str) for value in latch['closed_predecessor'].values()))
        before = copy.deepcopy(self.store)
        self.command()
        self.assertEqual(self.store, before)
        self.assert_closed()

    def test_artifact_drift_after_closed_handoff_keeps_new_latch_and_pending_package(self):
        write = self.write
        def handoff(path, value):
            write(path, value)
            if path == self.m.RUNTIME_QUALIFICATION:
                self.m.exact_hex_identity.return_value = 'f' * 64
        with mock.patch.object(self.m, 'atomic_json', side_effect=handoff):
            with self.assertRaisesRegex(RuntimeError, 'artifact differs'):
                self.command()
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION]['txid'], self.args.txid)
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assert_closed()

    def test_preimage_archive_fork_refuses_and_replay_requires_exact_preimage(self):
        archive = self.m.STATE / 'closed-runtime-history' / (self.old_args.txid + '.json')
        self.store[archive] = {'foreign': 'archive'}
        before = copy.deepcopy(self.store)
        with self.assertRaisesRegex(RuntimeError, 'archive fork'):
            self.command()
        self.assertEqual(self.store, before)
        self.store.pop(archive)
        self.command()
        self.store[archive]['qualification']['reason'] = 'foreign'
        before = copy.deepcopy(self.store)
        with self.assertRaisesRegex(RuntimeError, 'preimage changed'):
            self.command()
        self.assertEqual(self.store, before)


if __name__ == '__main__':
    unittest.main()
