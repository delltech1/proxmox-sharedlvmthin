"""Raw wire + real verifier/executor tests. No host or real process effects."""
import base64
import contextlib
import copy
import hashlib
import importlib.util
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

import test_layout_migration_exact_transport_server as SF
import test_layout_migration_exact_integration_model as IF
import test_layout_migration_exact_release_validator as VF

S, T, E = SF.S, SF.T, SF.local_fixture.E
spec = importlib.util.spec_from_file_location('exact_wire_tests', SF.SOURCE.parent / 'layout-migration-exact-wire.py')
W = importlib.util.module_from_spec(spec); spec.loader.exec_module(W)


class RawWireTests(unittest.TestCase):
    def setUp(self):
        self.f = IF.uppercase_fixtures(); self.addCleanup(self.f.doCleanups)
        with mock.patch.object(IF, 'SOURCE_COHORT_SHA256', 'd' * 64, create=True):
            self.pins = IF.pins(self.f.plan, after=True)
        self.envelope = {'schema': 'slt-exact-transport-request/v1', 'authority': 'NONE', 'kind': 'RESTORE_EFFECT',
            **{k: self.f.request[k] for k in ('tx', 'plan_sha256', 'node', 'boot_id')},
            'challenge': '9' * 64, 'payload': {}}
        self.raw = VF.V.canonical(self.f.e)
        self.payload = W.pack(self.f.request, self.raw, self.envelope, self.pins)

    def test_exact_full_raw_roundtrip_multichunk_and_actual_validator(self):
        effect, raw = W.unpack(self.payload, self.envelope, self.pins)
        self.assertEqual((effect, raw), (self.f.request, self.raw))
        self.assertGreater(len(self.payload['release_evidence']['chunks']), 1)
        verifier = VF.V.Validator.for_offline_test(self.f.sources, self.f.hashes)
        grant = verifier.evaluate(raw, effect, self.f.identity, VF.Tests.before(self.f), now=self.f.now)
        self.assertEqual(grant['allowed_effect'], 'UNMASK')

    def test_chunk_reorder_omission_duplicate_corruption_size_and_encoding_refuse(self):
        mutations = [lambda w: w['chunks'].reverse(), lambda w: w['chunks'].pop(),
            lambda w: w['chunks'].append(copy.deepcopy(w['chunks'][0])),
            lambda w: w['chunks'][0].update(index=True), lambda w: w['chunks'][0].update(sha256='0' * 64),
            lambda w: w['chunks'][0].update(base64='A' * (4 * ((W.CHUNK + 2) // 3) + 1)),
            lambda w: w['chunks'][0].update(base64='!!!!'), lambda w: w.update(raw_sha256='0' * 64),
            lambda w: w.update(byte_count=W.MAX_RAW + 1), lambda w: w.update(byte_count=True)]
        for mutation in mutations:
            value = copy.deepcopy(self.payload); mutation(value['release_evidence'])
            with self.subTest(mutation=mutation), self.assertRaises(W.Refusal): W.unpack(value, self.envelope, self.pins)

    def test_wrong_challenge_source_pin_effect_cohort_or_plan_cannot_rebind(self):
        for field in ('challenge', 'sources_sha256', 'pins_sha256', 'effect_sha256', 'cohort_id', 'plan_sha256'):
            value = copy.deepcopy(self.payload); value['release_evidence'][field] = '0' * 64
            with self.subTest(field=field), self.assertRaises(W.Refusal): W.unpack(value, self.envelope, self.pins)
        pins = copy.deepcopy(self.pins); pins['participants'][3]['source_package']['sources_sha256'] = '0' * 64
        with self.assertRaises(W.Refusal): W.unpack(self.payload, self.envelope, pins)

    def test_partial_cohort_alias_boot_package_and_reduced_labels_refuse(self):
        changes = [lambda e: e['current'].pop('PVE04'), lambda e: e['archives'].pop('PVE03'),
            lambda e: e['current']['PVE04']['observation'].update(node='pve04'),
            lambda e: e['current']['PVE04']['observation'].update(boot_id='foreign'),
            lambda e: e['current']['PVE04']['observation'].update(package_sha256='0' * 64),
            lambda e: e['current']['PVE04'].update(cohort_id='0' * 32)]
        for change in changes:
            value = copy.deepcopy(self.f.e); change(value)
            with self.subTest(change=change), self.assertRaises(W.Refusal):
                W.pack(self.f.request, W.canonical(value), self.envelope, self.pins)
        with self.assertRaises(W.Refusal): W.pack(self.f.request, b'{"verdict":"PASS"}', self.envelope, self.pins)

    def test_raw_must_be_exact_canonical_bounded_document(self):
        for raw in (self.raw + b'\n', b'{"a":1,"a":1}', b'{"a":NaN}', b'{}{}', b' ' + self.raw,
                    b'x' * (W.MAX_RAW + 1)):
            with self.subTest(raw=raw[:20]), self.assertRaises(W.Refusal): W.strict(raw)

    def context(self):
        f = self.f
        self.unit = copy.deepcopy(VF.Tests.before(f)); self.effects, self.records = [], {}
        def create(tx, key, value):
            self.assertNotIn(key, self.records); self.records[key] = copy.deepcopy(value)
            return {'durable': True, 'sha256': T.M.digest(value)}
        journal = types.SimpleNamespace(read=lambda tx, key: copy.deepcopy(self.records.get(key)), create_once=create)
        def command(operation, unit):
            self.assertEqual((operation, unit), ('UNMASK', f.request['unit']))
            self.effects.append((operation, unit)); self.unit['masked'] = False
            self.unit['source']['/etc/systemd/system/' + unit] = None
            self.unit['source']['effective:' + unit] = copy.deepcopy(f.e['baseline_sources']['PVE01']['effective:' + unit])
            return {'exit_code': 0, 'stdout_sha256': 'a' * 64, 'stderr_sha256': 'b' * 64}
        def executor(plan, received, **kwargs):
            self.assertIs(received, journal)
            return E.Executor(plan, journal, **kwargs, unit_reader=lambda _: copy.deepcopy(self.unit), command=command,
                clock=lambda: f.now, node_reader=lambda: 'PVE01', boot_reader=lambda: f.request['boot_id'],
                process_reader=lambda: {'pid': 100, 'starttime': 100})
        self.c = types.SimpleNamespace(plan=f.plan, row=self.pins['participants'][0], pins=self.pins, transport=T,
            wire=W, phase='AFTER', check=mock.Mock(), check_package=mock.Mock(),
            baseline=lambda: copy.deepcopy(f.e['baseline']),
            release_verifier=lambda: VF.V.Validator.for_offline_test(f.sources, f.hashes),
            local=types.SimpleNamespace(LocalJournal=lambda *_a, **_k: contextlib.nullcontext(journal),
                                       Executor=executor, installed_identity=lambda: copy.deepcopy(f.identity)))
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.object(S, 'INSTALLED', PurePosixPath('/usr/libexec/pve-sharedlvmthin')))
        for module, field, value in ((S, 'root_required', lambda: None), (S.socket, 'gethostname', lambda: 'PVE01'),
            (S.Path, 'read_text', lambda *_a, **_k: f.request['boot_id']), (S.time, 'time', lambda: f.now)):
            self.stack.enter_context(mock.patch.object(module, field, value))
        self.stack.enter_context(mock.patch.object(E.os, 'getuid', return_value=0, create=True))
        self.stack.enter_context(mock.patch.object(E.os, 'geteuid', return_value=0, create=True))
        self.stack.enter_context(mock.patch.object(S.subprocess, 'run', side_effect=AssertionError('live process forbidden')))
        self.envelope['payload'] = copy.deepcopy(self.payload)
        def grant(effect, source, before):
            recomputed = self.c.release_verifier().evaluate(self.raw, effect, source, before, now=f.now)
            return SF.provider_result(recomputed, effect, source, before, self.raw)
        return grant

    def test_server_real_recomposition_before_intent_effect_and_response(self):
        grant = self.context()
        result = S.Engine(self.c, authorizer=grant).handle(self.envelope)
        self.assertEqual(result['payload']['state'], 'LOCAL_EFFECT_CONFIRMED')
        self.assertEqual(len(self.effects), 1); self.assertEqual(len(self.records), 2)
        self.assertEqual(result['request_sha256'], T.M.digest(self.envelope))
        intent = next(value for key, value in self.records.items() if key.endswith(':intent'))
        self.assertEqual(intent['executor_identity']['transport']['wire_sha256'], T.M.digest(self.envelope))
        self.assertEqual(intent['executor_identity']['transport']['sources_sha256'],
                         self.pins['participants'][0]['source_package']['sources_sha256'])
        with self.assertRaisesRegex(S.Refusal, 'inspection-only'):
            S.Engine(self.c, authorizer=grant).handle(self.envelope)
        self.assertEqual(len(self.effects), 1)

    def test_new_challenge_after_restore_lost_ack_cannot_rebind_durable_attempt(self):
        grant = self.context()
        original = self.c.check_package
        self.c.check_package = mock.Mock(side_effect=[None, OSError('response lost after completion')])
        with self.assertRaises(OSError): S.Engine(self.c, authorizer=grant).handle(self.envelope)
        self.c.check_package = original
        self.envelope['challenge'] = '2' * 64
        self.envelope['payload'] = W.pack(self.f.request, self.raw, self.envelope, self.pins)
        with self.assertRaisesRegex(E.Refusal, 'prior attempt identity differs'):
            S.Engine(self.c, authorizer=grant).handle(self.envelope)
        self.assertEqual(len(self.effects), 1); self.assertEqual(len(self.records), 2)

    def test_caller_valid_chunks_cannot_bypass_real_certificate_recomputation(self):
        grant = self.context()
        bad = copy.deepcopy(self.f.e); bad['certified_inputs']['records'][0]['payload']['dpkg_verify_clean'] = False
        self.envelope['payload'] = W.pack(self.f.request, W.canonical(bad), self.envelope, self.pins)
        with self.assertRaises(Exception) as raised: S.Engine(self.c, authorizer=grant).handle(self.envelope)
        self.assertEqual(type(raised.exception).__name__, 'Refusal')
        self.assertEqual(self.effects, []); self.assertEqual(self.records, {})

    def test_no_independent_grant_rawless_or_foreign_grant_never_dispatches(self):
        grant = self.context()
        with self.assertRaises(S.Refusal): S.Engine(self.c).handle(self.envelope)
        rawless = dict(self.envelope, payload=self.f.request)
        with self.assertRaises(W.Refusal): S.Engine(self.c, authorizer=grant).handle(rawless)
        with self.assertRaises(S.Refusal): S.Engine(self.c, authorizer=lambda *_: {'verdict': 'PASS'}).handle(self.envelope)
        self.assertEqual(self.effects, []); self.assertEqual(self.records, {})

    def test_transport_emits_raw_chunks_with_own_nonce_and_accepts_only_recomputed_receipt(self):
        grant = self.context(); calls = []
        files = SF.transport_fixture.Files(self.f.plan, self.pins)
        def runner(argv, raw, timeout):
            calls.append(raw); request = T.strict(raw, T.MAX_REQUEST)
            self.assertEqual(argv, T.ssh_argv('PVE01', self.pins['participants'][0]['host']))
            return 0, S.canonical(S.Engine(self.c, authorizer=grant).handle(request)), b''
        transport = T.Transport(files=files, runner=runner, dispatch_guard=lambda *_: True,
            receipt_journal=SF.transport_fixture.Journal(),
            clock=lambda: self.f.now, nonce=lambda: '6' * 64, require_root=lambda: None)
        result = transport.dispatch_typed(self.f.request, release_evidence=self.raw)
        self.assertEqual(result['result'], 'CONFIRMED')
        self.assertEqual(T.strict(calls[0], T.MAX_REQUEST)['payload']['release_evidence']['challenge'], '6' * 64)
        with self.assertRaises(T.Refusal): transport.dispatch_typed(self.f.request, release_evidence=self.raw)
        self.assertEqual(len(calls), 1)


class EntryWireTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(mock.patch.dict(sys.modules, {} if os.name == 'posix' else {'fcntl': types.ModuleType('fcntl')}))
        import test_layout_migration_barrier_backend as BF
        spec = importlib.util.spec_from_file_location('wire_node_runner', SF.SOURCE.parent / S.NODE_RUNNER)
        self.r = importlib.util.module_from_spec(spec); spec.loader.exec_module(self.r)
        f = BF.Tests(); f.setUp(); self.addCleanup(f.doCleanups); self.f = f
        plan = SF.local_fixture.fixture()
        plan.update(tx=f.plan['tx'], storage_cfg_sha256=f.plan['storage_cfg_sha256'], workload_sha256=f.plan['workload_snapshot_sha256'],
                    candidate_sha256=f.plan['candidate_sha256'], issued_at=900, expires_at=1100)
        for row, original in zip(plan['participants'], f.plan['participants']):
            row['node'] = original['node']; row['boot_id'] = original['boot_id']
            row['after_package_sha256'] = plan['candidate_sha256'] if row['role'] == 'SAN' else row['before_package_sha256']
        self.c = types.SimpleNamespace(plan=plan, row={'node': 'pve01', 'boot_id': plan['participants'][0]['boot_id'],
            'helper_sha256': 'a' * 64, 'source_package': {'sources_sha256': 'b' * 64}}, pins={'test': 'fixed'}, transport=T,
            phase='BEFORE', workload=f.workload, check_package=mock.Mock(), node_runner=self.r,
            local=types.SimpleNamespace(C=types.SimpleNamespace(legacy_plan=lambda _: copy.deepcopy(f.plan))))
        self.attempts = {}; self.reserved = set()
        def reserve(envelope, grant):
            key = self.r.L.request_nonce(envelope['payload'])
            if key in self.reserved: raise FileExistsError('wire attempt exists')
            self.reserved.add(key); return 'c' * 64
        self.c.reserve_entry = mock.Mock(side_effect=reserve)
        def request(agent, value):
            key = self.r.L.request_nonce(value)
            if key in self.attempts: raise FileExistsError('node attempt exists')
            self.attempts[key] = 'RESERVED'
            result = agent.request(value); self.attempts[key] = result; return result
        self.stack.enter_context(mock.patch.object(self.r.L, 'DurableNodeAgent',
            side_effect=lambda agent: types.SimpleNamespace(request=lambda value: request(agent, value))))
        self.stack.enter_context(mock.patch.object(self.r.L, 'inspect', return_value={
            'state': 'COMPLETED_HISTORICAL', 'records': [], 'retry_authorized': False, 'release_authorized': False}))
        self.stack.enter_context(mock.patch.object(self.r.C, 'collect', side_effect=lambda *_: copy.deepcopy(f.states['pve01'])))
        self.stack.enter_context(mock.patch.object(self.r, 'execute', side_effect=lambda argv: f.execute('pve01', argv)))
        self.stack.enter_context(mock.patch.object(S, 'root_required'))
        self.stack.enter_context(mock.patch.object(S.socket, 'gethostname', return_value='pve01'))
        self.stack.enter_context(mock.patch.object(S.Path, 'read_text', return_value=self.c.row['boot_id']))
        self.stack.enter_context(mock.patch.object(S.time, 'time', return_value=1000))
        self.stack.enter_context(mock.patch.object(S.subprocess, 'run', side_effect=AssertionError('live command forbidden')))

    def envelope(self, operation):
        return {'schema': 'slt-exact-transport-request/v1', 'authority': 'NONE', 'kind': operation,
            'tx': self.c.plan['tx'], 'plan_sha256': T.M.digest(self.c.plan), 'node': 'pve01',
            'boot_id': self.c.row['boot_id'], 'challenge': 'd' * 64,
            'payload': {'tx': self.f.plan['tx'], 'plan_sha256': self.r.B.MODEL.frozen_hash(self.f.plan),
                        'node': 'pve01', 'operation': operation}}

    def grant(self, envelope, pins):
        return {'schema': 'slt-exact-entry-grant/v1', 'authority': 'NONE', 'wire_sha256': T.M.digest(envelope),
            'pins_sha256': T.M.digest(pins), 'sources_sha256': 'b' * 64, 'issued_at': 999, 'expires_at': 1020,
            'operation': envelope['kind']}

    def test_entry_then_drain_use_real_node_agent_closed_commands_and_durable_identity(self):
        for operation in ('ENTRY_BLOCK', 'SERVICE_DRAIN'):
            request = self.envelope(operation)
            result = S.Engine(self.c, entry_authorizer=self.grant).handle(request)['payload']
            self.assertEqual(result['state'], 'OBSERVED_COMPLETE')
            self.assertEqual(result['attempt_nonce'], self.r.L.request_nonce(request['payload']))
            self.assertFalse(result['effect_retry_allowed'])
        self.assertEqual(len(self.f.commands), 4)
        self.assertTrue(all(argv in self.r.COMMANDS for _, argv in self.f.commands))

    def test_default_wrong_phase_projection_or_expired_grant_refuse_before_reservation(self):
        request = self.envelope('ENTRY_BLOCK')
        with self.assertRaises(S.Refusal): S.Engine(self.c).handle(request)
        bad = copy.deepcopy(request); bad['payload']['operation'] = 'START'
        with self.assertRaises(S.Refusal): S.Engine(self.c, entry_authorizer=self.grant).handle(bad)
        expired = lambda *args: dict(self.grant(*args), expires_at=999)
        with self.assertRaises(S.Refusal): S.Engine(self.c, entry_authorizer=expired).handle(request)
        self.c.phase = 'AFTER'
        with self.assertRaises(S.Refusal): S.Engine(self.c, entry_authorizer=self.grant).handle(request)
        self.c.reserve_entry.assert_not_called(); self.assertEqual(self.f.commands, [])

    def test_new_challenge_cannot_retry_after_ambiguous_effect(self):
        original = self.r.execute.side_effect
        def lost(argv): original(argv); raise TimeoutError('ACK lost')
        self.r.execute.side_effect = lost
        request = self.envelope('ENTRY_BLOCK')
        with self.assertRaises(TimeoutError): S.Engine(self.c, entry_authorizer=self.grant).handle(request)
        request['challenge'] = 'e' * 64
        with self.assertRaises(FileExistsError): S.Engine(self.c, entry_authorizer=self.grant).handle(request)
        self.assertEqual(len(self.f.commands), 1)

    def test_drain_without_entry_refuses_in_existing_node_semantics(self):
        with self.assertRaises(self.r.B.Refusal):
            S.Engine(self.c, entry_authorizer=self.grant).handle(self.envelope('SERVICE_DRAIN'))
        self.assertEqual(self.f.commands, [])
        self.assertEqual(len(self.reserved), 1)

    def test_wire_intent_persistence_uncertain_prevents_node_latch(self):
        self.c.reserve_entry.side_effect = OSError('fsync lost')
        with self.assertRaises(OSError): S.Engine(self.c, entry_authorizer=self.grant).handle(self.envelope('ENTRY_BLOCK'))
        self.assertEqual(self.attempts, {}); self.assertEqual(self.f.commands, [])

    def test_entry_grant_recheck_before_each_command_retains_intent_on_drift(self):
        calls = []
        def drift(envelope, pins):
            calls.append(1)
            value = self.grant(envelope, pins)
            if len(calls) == 3: value['sources_sha256'] = '0' * 64
            return value
        with self.assertRaises(S.Refusal): S.Engine(self.c, entry_authorizer=drift).handle(self.envelope('ENTRY_BLOCK'))
        self.assertEqual(len(self.f.commands), 1); self.assertEqual(len(self.reserved), 1)

    @unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0,
                         'native root descriptor/fsync test in disposable directory')
    def test_real_wire_intent_create_only_fsync_lost_ack_and_new_nonce_refuse(self):
        with tempfile.TemporaryDirectory() as name, mock.patch.object(S, 'STATE', Path(name)):
            os.chmod(name, 0o700)
            envelope = self.envelope('ENTRY_BLOCK'); grant = self.grant(envelope, self.c.pins)
            expected = S.FixedContext.reserve_entry(self.c, envelope, grant)
            path = Path(name) / 'entry-attempts' / ('attempt-' + self.r.L.request_nonce(envelope['payload'])) / 'wire-intent.json'
            raw = path.read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(S.strict(raw)['envelope'], envelope)
            envelope['challenge'] = 'f' * 64
            with self.assertRaises(FileExistsError): S.FixedContext.reserve_entry(self.c, envelope, self.grant(envelope, self.c.pins))
            self.assertEqual(path.read_bytes(), raw)
            drain = self.envelope('SERVICE_DRAIN')
            original = self.r.L._write_once
            def lost(*args): original(*args); raise OSError('post-fsync lost ACK')
            with mock.patch.object(self.r.L, '_write_once', side_effect=lost):
                with self.assertRaises(OSError): S.FixedContext.reserve_entry(self.c, drain, self.grant(drain, self.c.pins))
            with self.assertRaises(FileExistsError): S.FixedContext.reserve_entry(self.c, drain, self.grant(drain, self.c.pins))
            self.assertEqual(self.f.commands, [])

    def test_wire_intent_mkdir_fsync_write_ack_order_and_unsafe_modes(self):
        envelope = self.envelope('ENTRY_BLOCK'); grant = self.grant(envelope, self.c.pins)
        for unsafe in (False, True):
            trace = []
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(self.r.L, '_safe_root', return_value=S.STATE / 'entry-attempts'))
                stack.enter_context(mock.patch.object(S.os, 'O_DIRECTORY', 0, create=True))
                stack.enter_context(mock.patch.object(S.os, 'O_NOFOLLOW', 0, create=True))
                stack.enter_context(mock.patch.object(S.os, 'O_CLOEXEC', 0, create=True))
                stack.enter_context(mock.patch.object(S.os, 'open', side_effect=[11, 12]))
                stack.enter_context(mock.patch.object(S.os, 'mkdir', side_effect=lambda *a, **k: trace.append('mkdir')))
                stack.enter_context(mock.patch.object(S.os, 'fsync', side_effect=lambda *a: trace.append('fsync')))
                stack.enter_context(mock.patch.object(S.os, 'close'))
                stack.enter_context(mock.patch.object(S.os, 'fstat', return_value=types.SimpleNamespace(
                    st_uid=1000 if unsafe else 0, st_mode=stat.S_IFDIR | 0o700)))
                def write(fd, filename, raw):
                    self.assertEqual((fd, filename), (12, 'wire-intent.json')); trace.append('write_once')
                    record = S.strict(raw)
                    self.assertEqual(record['envelope'], envelope)
                    self.assertEqual(record['source_package'], self.c.row['source_package'])
                    return {'sha256': hashlib.sha256(raw).hexdigest(), 'file_synced': True, 'dir_synced': True, 'closed': True}
                stack.enter_context(mock.patch.object(self.r.L, '_write_once', side_effect=write))
                if unsafe:
                    with self.assertRaises(S.Refusal): S.FixedContext.reserve_entry(self.c, envelope, grant)
                    self.assertEqual(trace, ['mkdir', 'fsync'])
                else:
                    self.assertEqual(len(S.FixedContext.reserve_entry(self.c, envelope, grant)), 64)
                    self.assertEqual(trace, ['mkdir', 'fsync', 'write_once'])


class EntryClientTests(unittest.TestCase):
    def setUp(self):
        self.f = SF.transport_fixture.ExactTransportTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.request = {'tx': self.f.plan['tx'], 'plan_sha256': 'a' * 64, 'node': 'PVE04', 'operation': 'ENTRY_BLOCK'}
        self.calls = []
        self.change = lambda value: value
        self.failure = None

    def runner(self, argv, raw, timeout):
        self.calls.append(raw)
        if self.failure: raise self.failure
        envelope = T.strict(raw); request = envelope['payload']; row = self.f.pins['participants'][3]
        identity = hashlib.sha256(T.canonical(request)).hexdigest()
        response = {'request': request, 'evidence': {'fixture': 'raw node-agent evidence'}}
        records = [{'schema': 'slt-barrier-node-attempt/v1', 'request': request, 'boot_id': row['boot_id'], 'state': 'ATTEMPT_RESERVED'},
            {'schema': 'slt-barrier-node-result/v1', 'request_sha256': identity, 'state': 'OBSERVED_COMPLETE', 'response': response}]
        payload = {'schema': 'slt-exact-entry-wire-result/v1', 'authority': 'NONE', 'state': 'OBSERVED_COMPLETE',
            'request_sha256': identity, 'attempt_nonce': identity[:32], 'wire_intent_sha256': 'c' * 64,
            'attempt': {'state': 'COMPLETED_HISTORICAL', 'records': records, 'retry_authorized': False, 'release_authorized': False},
            'response': response, 'effect_retry_allowed': False}
        result = {k: envelope[k] for k in ('authority', 'kind', 'tx', 'plan_sha256', 'node', 'boot_id', 'challenge')}
        result.update(schema='slt-exact-transport-response/v1', request_sha256=T.M.digest(envelope), helper_path=T.HELPER,
                      helper_sha256=row['helper_sha256'], source_package=row['source_package'], payload=self.change(payload))
        return 0, T.canonical(result), b''

    def transport(self, admitted=True):
        return T.Transport(files=self.f.files, runner=self.runner, dispatch_guard=(lambda *_: True) if admitted else None,
            clock=lambda: self.f.now, nonce=self.f.nonce, require_root=lambda: None)

    def test_control_node_entry_one_attempt_and_no_default_authority(self):
        with self.assertRaises(T.Refusal): self.transport(False).dispatch_entry(self.request)
        transport = self.transport(); result = transport.dispatch_entry(self.request)
        self.assertEqual(result['state'], 'OBSERVED_COMPLETE')
        with self.assertRaises(T.Refusal): transport.dispatch_entry(self.request)
        self.assertEqual(len(self.calls), 1)

    def test_partial_or_forged_latch_and_timeout_poison_no_retry(self):
        def partial(v): v['attempt']['records'].pop(); return v
        def foreign(v): v['attempt']['records'][0]['boot_id'] = 'foreign'; return v
        for mutation in (partial, foreign):
            self.change = mutation; transport = self.transport()
            with self.assertRaises(T.Refusal): transport.dispatch_entry(self.request)
            with self.assertRaises(T.Refusal): transport.dispatch_entry(self.request)
        self.failure = TimeoutError('uncertain dispatch'); transport = self.transport()
        with self.assertRaises(TimeoutError): transport.dispatch_entry(self.request)
        with self.assertRaises(T.Refusal): transport.dispatch_entry(self.request)
        self.assertEqual(len(self.calls), 3)

    def test_entry_cannot_route_restore_shell_arguments_or_unknown_peer(self):
        for field, value in (('operation', 'UNMASK'), ('operation', '/bin/sh'), ('node', 'pve04'), ('argv', ['true'])):
            with self.assertRaises(T.Refusal): self.transport().dispatch_entry(dict(self.request, **{field: value}))
        self.assertEqual(self.calls, [])


if __name__ == '__main__': unittest.main()
