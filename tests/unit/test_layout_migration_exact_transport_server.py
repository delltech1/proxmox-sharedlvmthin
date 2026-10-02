"""Unshipped server/loader tests: no SSH, package probes or live effects."""
import contextlib
import copy
import hashlib
import importlib.util
import os
import stat
from pathlib import Path
import sys
import types
import unittest
from unittest import mock

import test_layout_migration_exact_transport as transport_fixture
import test_layout_migration_exact_local_restore as local_fixture

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'experiments/thick-generations/layout-migration-exact-transport-server.py'
spec = importlib.util.spec_from_file_location('transport_server_test', SOURCE)
S = importlib.util.module_from_spec(spec); spec.loader.exec_module(S)
T = transport_fixture.T


def provider_result(grant, effect, source, before, raw):
    record = {'schema': 'slt-exact-grant-issuance-model/v1', 'authority': 'NONE', 'request': effect,
        'executor_sha256': T.M.digest(source), 'before_sha256': T.M.digest(before),
        'evidence_sha256': hashlib.sha256(raw).hexdigest(), 'source_closure_sha256': source['transport']['sources_sha256'],
        'phase_index': 0, 'phase_prefix_sha256': T.M.digest([]), 'server_grant': copy.deepcopy(grant)}
    binding = {k: v for k, v in record.items() if k not in ('schema', 'authority')}
    record['grant'] = {**grant, 'authorization_sha256': T.M.digest(binding)}
    record['record_sha256'] = T.M.digest(record)
    return {'schema': 'slt-exact-grant-provider-result/v1', 'authority': 'NONE',
            'server_grant': copy.deepcopy(grant), 'issued_grant': copy.deepcopy(record['grant']), 'issuance': record}


class TransportServerTests(unittest.TestCase):
    def setUp(self):
        self.plan = local_fixture.fixture()
        self.backend = local_fixture.Backend(self.plan)
        self.row = {'node': 'pve01', 'boot_id': self.plan['participants'][0]['boot_id'],
                    'helper_sha256': 'f' * 64, 'source_package': {'fixture': 'measured', 'sources_sha256': 'c' * 64}}
        self.collection = mock.Mock(side_effect=self.collect)
        self.context = types.SimpleNamespace(plan=self.plan, row=self.row, transport=T, phase='BEFORE',
            workload={'evidence_sha256': self.plan['workload_sha256']}, check=mock.Mock(),
            check_package=mock.Mock(), baseline=lambda: None,
            local=types.SimpleNamespace(C=types.SimpleNamespace(collect_local=self.collection),
                                       LocalJournal=mock.Mock(side_effect=AssertionError('unexpected journal'))))
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(S, 'root_required'))
        self.stack.enter_context(mock.patch.object(S.socket, 'gethostname', return_value='pve01'))
        self.stack.enter_context(mock.patch.object(S.Path, 'read_text', return_value=self.row['boot_id']))
        self.stack.enter_context(mock.patch.object(S.time, 'time', return_value=200))
        self.stack.enter_context(mock.patch.object(S.subprocess, 'run', side_effect=AssertionError('live process forbidden')))

    def collect(self, plan, workload, timeout):
        self.assertEqual(timeout, 12)
        value = copy.deepcopy(self.backend.records['pve01'])
        if self.context.phase == 'AFTER':
            value['package_sha256'] = self.plan['candidate_sha256']
            value['storage_cfg_sha256'] = self.plan['target_storage_cfg_sha256']
        return {'authority': 'NONE', 'mutation_performed': False, 'plan_sha256': T.M.digest(plan), 'observation': value}

    def request(self, kind='READ_OBSERVATION', payload=None):
        return {'schema': 'slt-exact-transport-request/v1', 'authority': 'NONE', 'kind': kind,
                'tx': self.plan['tx'], 'plan_sha256': T.M.digest(self.plan), 'node': 'pve01',
                'boot_id': self.row['boot_id'], 'challenge': 'a' * 64, 'payload': {} if payload is None else payload}

    def test_read_before_and_after_projection_preserve_original_plan_binding(self):
        for phase in ('BEFORE', 'AFTER'):
            with self.subTest(phase=phase):
                self.context.phase = phase
                request = self.request()
                result = S.Engine(self.context).handle(request)
                self.assertEqual(result['plan_sha256'], T.M.digest(self.plan))
                self.assertEqual(result['request_sha256'], T.M.digest(request))
                self.assertEqual(result['authority'], 'NONE')
                projected = self.collection.call_args.args[0]
                self.assertEqual(projected['storage_cfg_sha256'], self.plan[
                    'storage_cfg_sha256' if phase == 'BEFORE' else 'target_storage_cfg_sha256'])
        self.context.local.LocalJournal.assert_not_called()

    def test_default_effect_is_refused_before_journal_or_command(self):
        self.context.phase = 'AFTER'
        with self.assertRaisesRegex(S.Refusal, 'grant provider absent'):
            S.Engine(self.context).handle(self.request('RESTORE_EFFECT'))
        self.context.local.LocalJournal.assert_not_called()
        self.collection.assert_not_called()

    def test_request_grant_argv_path_and_env_fields_cannot_supply_authority(self):
        for field in ('grant', 'argv', 'helper_path', 'environment', 'authorize'):
            request = self.request(); request[field] = 'caller-controlled'
            with self.subTest(field=field), self.assertRaises(T.Refusal):
                S.Engine(self.context).handle(request)
        self.collection.assert_not_called()

    def test_boot_node_digest_kind_payload_and_reuse_refuse(self):
        for field, value in (('node', 'pve04'), ('boot_id', 'foreign'), ('plan_sha256', '0' * 64),
                             ('kind', 'SHELL'), ('payload', {'unit': 'anything'}), ('challenge', 'bad')):
            with self.subTest(field=field), self.assertRaises((S.Refusal, T.Refusal)):
                S.Engine(self.context).handle(dict(self.request(), **{field: value}))
        engine = S.Engine(self.context); engine.handle(self.request())
        with self.assertRaisesRegex(S.Refusal, 'one request'): engine.handle(self.request())

    def test_post_collection_package_drift_produces_no_response(self):
        self.context.check_package.side_effect = [None, S.Refusal('package changed')]
        with self.assertRaisesRegex(S.Refusal, 'package changed'):
            S.Engine(self.context).handle(self.request())

    def configure_real_executor_fixture(self):
        f = local_fixture.Tests(methodName='test_process_reuse_foreign_receipt_and_changed_completed_source_refuse')
        f.setUp(); self.addCleanup(f.doCleanups)
        self.plan = f.plan; self.context.plan = f.plan; self.context.phase = 'AFTER'
        self.context.baseline = lambda: copy.deepcopy(f.baseline)
        journal = types.SimpleNamespace(read=lambda tx, key: copy.deepcopy(f.records.get(key)), create_once=f.create)
        def factory(plan, passed_journal, **kwargs):
            self.assertIs(passed_journal, journal)
            return f.executor(journal=journal, **kwargs)
        self.context.local = types.SimpleNamespace(
            LocalJournal=mock.Mock(side_effect=lambda *_args, **_kw: contextlib.nullcontext(journal)),
            Executor=factory, installed_identity=lambda: copy.deepcopy(f.source))
        # Executor-journal tests isolate the wire/verifier seams; real chunk
        # decoding and actual v2 recomposition are covered by wire tests.
        self.context.pins = {}
        self.context.wire = types.SimpleNamespace(unpack=lambda payload, *_: (payload, b'fixture raw'))
        self.context.release_verifier = lambda: types.SimpleNamespace(
            evaluate=lambda raw, effect, source, before, **_: f.grant(effect, source, before))
        f.provider = lambda effect, source, before: provider_result(f.grant(effect, source, before),
                                                                  effect, source, before, b'fixture raw')
        return f

    def test_real_executor_requires_independent_exact_grant_and_retains_lost_ack(self):
        f = self.configure_real_executor_fixture()
        bad = lambda *_: {'authority': 'ALLOW'}
        with self.assertRaises(S.Refusal):
            S.Engine(self.context, authorizer=bad).handle(self.request('RESTORE_EFFECT', f.request))
        self.assertEqual(f.effects, [])
        f.lost_ack = True
        with self.assertRaises(OSError):
            S.Engine(self.context, authorizer=f.provider).handle(self.request('RESTORE_EFFECT', f.request))
        self.assertEqual(len(f.effects), 1)
        self.assertTrue(any(key.endswith(':intent') for key in f.records))
        with self.assertRaisesRegex(S.Refusal, 'inspection-only'):
            S.Engine(self.context, authorizer=f.provider).handle(self.request('RESTORE_EFFECT', f.request))
        self.assertEqual(len(f.effects), 1)

    def test_exact_independent_grant_routes_one_typed_effect(self):
        f = self.configure_real_executor_fixture()
        request = self.request('RESTORE_EFFECT', f.request)
        result = S.Engine(self.context, authorizer=f.provider).handle(request)
        self.assertEqual(result['payload']['state'], 'LOCAL_EFFECT_CONFIRMED')
        self.assertEqual(result['authority'], 'NONE')
        self.assertEqual(f.effects, [('UNMASK', f.unit)])
        with self.assertRaisesRegex(S.Refusal, 'inspection-only'):
            S.Engine(self.context, authorizer=f.provider).handle(request)
        self.assertEqual(len(f.effects), 1)


class SealedLoaderTests(unittest.TestCase):
    def bundle(self, changes=None):
        sources = {name: b'VALUE = "approved"\n' for name in S.FILES}
        sources.update(changes or {})
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}
        return sources, hashes, S.SealedBundle(sources, hashes, lambda: None)

    def test_compiles_immutable_approved_bytes_not_later_caller_or_disk_bytes(self):
        sources, hashes, bundle = self.bundle()
        sources[S.TRANSPORT] = b'raise AssertionError("unapproved")\n'
        with mock.patch('builtins.open', side_effect=AssertionError('disk fallback forbidden')):
            self.assertEqual(bundle.load(S.TRANSPORT).VALUE, 'approved')
        with self.assertRaises(TypeError): bundle.sources[S.TRANSPORT] = b'changed'

    def test_mixed_hash_missing_module_and_external_path_load_refuse(self):
        sources, hashes, _ = self.bundle(); hashes[S.LOCAL] = '0' * 64
        with self.assertRaises(S.Refusal): S.SealedBundle(sources, hashes, lambda: None)
        sources.pop(S.LOCAL)
        with self.assertRaises(S.Refusal): S.SealedBundle(sources, hashes, lambda: None)
        code = b'import importlib.util\nimportlib.util.spec_from_file_location("foreign", "/tmp/foreign.py")\n'
        _, _, bundle = self.bundle({S.TRANSPORT: code})
        original = importlib.util.spec_from_file_location
        with self.assertRaises(S.Refusal): bundle.load(S.TRANSPORT)
        self.assertIs(importlib.util.spec_from_file_location, original)

    @unittest.skipUnless(os.name == 'posix', 'native fixed /usr paths and fcntl/resource imports')
    def test_complete_real_bundle_imports_from_sealed_bytes_without_installation(self):
        sources = {name: (SOURCE if name == S.ENTRY else SOURCE.parent / name).read_bytes() for name in S.FILES}
        hashes = {name: hashlib.sha256(raw).hexdigest() for name, raw in sources.items()}
        bundle = S.SealedBundle(sources, hashes, lambda: None)
        local = bundle.load(S.LOCAL)
        transport = bundle.load(S.TRANSPORT)
        self.assertEqual(local.LOADED_SOURCE_SHA256, {name: hashes[name] for name in local.FILES})
        self.assertEqual(transport.HELPER, str(S.INSTALLED / S.ENTRY))
        wire = bundle.load(S.WIRE)
        verifier = bundle.load(S.VERIFIER)
        runner = bundle.load(S.NODE_RUNNER)
        self.assertEqual(wire.MAX_FRAME, S.MAX_REQUEST)
        self.assertTrue(set(verifier.FILES) <= set(bundle.sources))
        self.assertEqual(len(runner.COMMANDS), 4)
        self.assertEqual(runner.L.PRODUCTION_ROOT, Path('/var/lib/pve-sharedlvmthin/barrier-node-attempts'))

    def test_canonical_single_document_limits_duplicate_and_nan(self):
        for raw in (b'{}\n{}\n', b' {"a":1}\n', b'{"a":1,"a":1}\n', b'{"a":NaN}\n',
                    b'x' * (S.MAX_REQUEST + 1)):
            with self.subTest(raw=raw[:20]), self.assertRaises(S.Refusal): S.strict(raw, S.MAX_REQUEST)
        self.assertEqual(S.strict(b'{}\n'), {})

    def test_server_itself_executes_measured_bytes_and_rejects_bootstrap_drift(self):
        raw = b'REVISION = "approved"\n'
        pins = S.canonical({'schema': 'slt-exact-server-source-pins/v1', 'authority': 'NONE',
                            'sources': {S.ENTRY: hashlib.sha256(raw).hexdigest()}})
        with mock.patch.object(S, 'read_fixed', side_effect=[pins, raw, pins, raw]):
            self.assertEqual(S.fresh_server().REVISION, 'approved')
        with mock.patch.object(S, 'read_fixed', side_effect=[pins, raw, pins, b'changed']):
            with self.assertRaisesRegex(S.Refusal, 'changed during bootstrap'): S.fresh_server()
        with mock.patch.object(S, 'read_fixed', side_effect=[pins, b'foreign']):
            with self.assertRaisesRegex(S.Refusal, 'approval differs'): S.fresh_server()

    def test_read_one_requires_eof_and_refuses_extra_frame_without_process(self):
        selector = mock.Mock(); selector.select.return_value = [True]
        for blocks, success in (([b'{}\n', b''], True), ([b'{}\n', b'{}\n', b''], False),
                                ([b'x' * (S.MAX_REQUEST + 1)], False)):
            with self.subTest(success=success), mock.patch.object(S.selectors, 'DefaultSelector', return_value=selector), \
                    mock.patch.object(S.os, 'read', side_effect=blocks), mock.patch.object(S.time, 'monotonic', return_value=1):
                if success: self.assertEqual(S.read_one(), {})
                else:
                    with self.assertRaises(S.Refusal): S.read_one()

    def test_fixed_entry_rejects_flags_and_closes_all_standard_fds(self):
        with mock.patch.object(S, 'root_required'), mock.patch.object(S.sys, 'flags', types.SimpleNamespace(isolated=1)), \
                mock.patch.object(S.sys, 'argv', [str(S.INSTALLED / S.ENTRY), '--grant']), \
                mock.patch.object(S.signal, 'alarm', create=True), mock.patch.object(S.os, 'write', side_effect=lambda _, raw: len(raw)) as write, \
                mock.patch.object(S.os, 'close') as close, mock.patch.object(S, 'FixedContext') as context:
            self.assertEqual(S.main(), 2)
            context.assert_not_called()
            self.assertEqual(S.strict(write.call_args.args[1])['state'], 'REFUSED_OR_UNKNOWN')
            self.assertEqual([call.args[0] for call in close.call_args_list], [0, 1, 2])

    def test_server_state_refuses_owner_mode_hardlink_symlink_before_read(self):
        directory = types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
        file = dict(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_size=20)
        for change in ({'st_uid': 1000}, {'st_mode': stat.S_IFREG | 0o644},
                       {'st_nlink': 2}, {'st_mode': stat.S_IFLNK | 0o600}):
            with self.subTest(change=change), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(S, 'root_required'))
                stack.enter_context(mock.patch.object(S.os, 'open', return_value=9))
                stack.enter_context(mock.patch.object(S.os, 'close'))
                stack.enter_context(mock.patch.object(S.os, 'fstat', side_effect=
                    [directory] * len(S.STATE.parts) + [types.SimpleNamespace(**(file | change))]))
                read = stack.enter_context(mock.patch.object(S.os, 'read'))
                for option in ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'O_CLOEXEC'):
                    stack.enter_context(mock.patch.object(S.os, option, 0, create=True))
                with self.assertRaises(S.Refusal): S.read_fixed(S.STATE, 'pins.json')
                read.assert_not_called()

    def test_stdin_timeout_refuses_without_returning_partial_document(self):
        selector = mock.Mock(); selector.select.return_value = []
        with mock.patch.object(S.selectors, 'DefaultSelector', return_value=selector), \
                mock.patch.object(S.os, 'read') as read:
            with self.assertRaisesRegex(S.Refusal, 'framing deadline'): S.read_one()
            read.assert_not_called()


if __name__ == '__main__': unittest.main()
