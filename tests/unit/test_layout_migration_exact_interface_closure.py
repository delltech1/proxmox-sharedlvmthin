"""Compose real source-only interfaces; every external effect is simulated."""
import contextlib
import copy
import os
from pathlib import Path, PurePosixPath
import tempfile
import types
import unittest
from unittest import mock

import test_layout_migration_exact_grant_provider_model as GF
import test_layout_migration_exact_transport_server as SF
import test_layout_migration_exact_wire as WF

S, T, W = SF.S, SF.T, WF.W


class CompositionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls): GF.Tests.setUpClass()

    @classmethod
    def tearDownClass(cls): GF.Tests.tearDownClass()

    def setUp(self):
        f = GF.Tests(); f.setUp(); self.addCleanup(f.doCleanups)
        self.f, self.m = f, f.m
        m = self.m
        m.poisoned = False
        m.pending = copy.deepcopy(f.request)
        self.provider = f.provider()
        self.context = types.SimpleNamespace(plan=m.plan, row=m.pins_after['participants'][0], pins=m.pins_after,
            transport=T, wire=W, phase='AFTER', check=m.check, check_package=m.check,
            baseline=lambda: copy.deepcopy(m.baseline), release_verifier=m.verifier)
        def executor(plan, supplied, **kwargs):
            self.assertIs(supplied, m.journals['PVE01'])
            return m.E.Executor(plan, supplied, **kwargs, clock=m.now, node_reader=lambda: 'PVE01',
                boot_reader=lambda: f.request['boot_id'], process_reader=lambda: {'pid': 101, 'starttime': 1000},
                unit_reader=lambda unit: m.unit('PVE01', unit), command=lambda op, unit: m.command('PVE01', op, unit))
        self.context.local = types.SimpleNamespace(LocalJournal=lambda *_a, **_k: contextlib.nullcontext(m.journals['PVE01']),
            installed_identity=lambda: m.source_identity('PVE01'), Executor=executor)
        stack = contextlib.ExitStack(); self.addCleanup(stack.close)
        for module, field, value in ((S, 'root_required', lambda: None),
                (S, 'INSTALLED', PurePosixPath('/usr/libexec/pve-sharedlvmthin')),
                (S.socket, 'gethostname', lambda: 'PVE01'), (S.Path, 'read_text', lambda *_a, **_k: f.request['boot_id']),
                (S.time, 'time', lambda: m.time), (m.E.os, 'getuid', lambda: 0), (m.E.os, 'geteuid', lambda: 0)):
            stack.enter_context(mock.patch.object(module, field, value, create=True))
        self.calls = []
        def runner(argv, raw, timeout):
            envelope = T.strict(raw, T.MAX_REQUEST); self.calls.append(envelope)
            response = S.Engine(self.context, authorizer=self.authorize).handle(envelope)
            if getattr(self, 'lose_response', False) and envelope['kind'] == 'RESTORE_EFFECT':
                raise OSError('remote completion ACK lost')
            return 0, T.canonical(response), b''
        self.transport = T.Transport(files=SF.transport_fixture.Files(m.plan, m.pins_after),
            runner=runner, dispatch_guard=m.dispatch_guard, receipt_journal=m.journals['coordinator'],
            clock=m.now, nonce=m.nonce, require_root=lambda: None)

    def authorize(self, effect, source, before):
        return self.provider.authorize(effect, source, before, now=self.m.time)

    def test_real_provider_server_executor_transport_preserves_phase_grant_and_full_receipt(self):
        receipt = self.transport.dispatch_restore_once(self.f.request, release_evidence=self.f.raw)
        key = T.effect_key(self.f.request)
        issued = self.m.journals['PVE01'].read(self.m.plan['tx'], key + ':grant')
        intent = self.m.journals['PVE01'].read(self.m.plan['tx'], key + ':intent')
        self.assertNotEqual(issued['server_grant']['authorization_sha256'], issued['grant']['authorization_sha256'])
        self.assertEqual(intent['grant'], issued['grant'])
        self.assertEqual(receipt, self.m.read(self.m.plan['tx'], 'receipt:' + key))
        self.assertEqual(receipt, self.m.journals['PVE01'].read(self.m.plan['tx'], key + ':done'))
        self.assertEqual(len(self.m.effects), 1)

    def test_remote_lost_ack_inspects_full_receipt_without_authorizer_effect_or_write(self):
        self.lose_response = True
        with self.assertRaises(OSError):
            self.transport.dispatch_restore_once(self.f.request, release_evidence=self.f.raw)
        saved = {n: copy.deepcopy(j.journal.records) for n, j in self.m.journals.items()}
        self.authorize = mock.Mock(side_effect=AssertionError('inspection must not authorize'))
        self.context.local.Executor = mock.Mock(side_effect=AssertionError('inspection must not execute'))
        self.assertEqual(self.transport.inspect_restore_attempt(self.f.request)['state'], 'UNKNOWN')
        inspected = self.transport.inspect_restore_attempt(self.f.request, remote=True)
        self.assertEqual(inspected['state'], 'COMPLETED_HISTORICAL')
        self.assertEqual(inspected['receipt']['state'], 'LOCAL_EFFECT_CONFIRMED')
        self.assertFalse(inspected['effect_retry_allowed'])
        self.assertEqual(len(self.m.effects), 1)
        self.assertEqual([r['kind'] for r in self.calls], ['RESTORE_EFFECT', 'INSPECT_RESTORE'])
        self.assertEqual({n: j.journal.records for n, j in self.m.journals.items()}, saved)
        self.assertEqual(self.transport.inspect_restore_attempt(self.f.request, challenge='f' * 64, remote=True)['state'], 'UNKNOWN')
        self.assertEqual(len(self.calls), 2)
        payload = {'effect': self.f.request, 'wire_sha256': 'f' * 64}
        self.assertEqual(S.Engine(self.context).inspect_restore(payload)['state'], 'UNKNOWN')
        with self.assertRaises(T.Refusal):
            self.transport.dispatch_restore_once(self.f.request, release_evidence=self.f.raw)

    @unittest.skipUnless(os.name == 'posix', 'native descriptor/fsync composition requires POSIX')
    def test_direct_base_journal_composes_provider_server_executor_and_receipt_transport(self):
        originals, ports = self.m.journals, {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); root.chmod(0o700)
            try:
                for name, port in originals.items():
                    journal = self.m.J.Journal.for_test(self.m.plan, root / name, create=True)
                    ports[name] = GF.F.I.JournalPort(journal, self.m.trace, name)
                    for key, value in port.journal.records.items():
                        journal.create_once(self.m.plan['tx'], key, value)
                self.m.journals = ports
                self.provider = self.f.provider()
                self.transport.receipt_journal = ports['coordinator']
                self.test_real_provider_server_executor_transport_preserves_phase_grant_and_full_receipt()
            finally:
                self.m.journals = originals
                for port in ports.values(): port.journal.close()

    def test_extra_provider_fields_unbound_grant_and_recomputed_grant_tamper_refuse_before_effect(self):
        original = self.authorize
        for mutation in (lambda r: r.update(extra=True),
                         lambda r: r.update(issued_grant=copy.deepcopy(r['server_grant'])),
                         lambda r: r['server_grant'].update(authorization_sha256='f' * 64),
                         lambda r: r['issued_grant'].update(expires_at=r['issued_grant']['expires_at'] + 1)):
            envelope = {'schema': 'slt-exact-transport-request/v1', 'authority': 'NONE', 'kind': 'RESTORE_EFFECT',
                **{k: self.f.request[k] for k in ('tx', 'plan_sha256', 'node', 'boot_id')}, 'challenge': 'a' * 64}
            envelope['payload'] = W.pack(self.f.request, self.f.raw, envelope, self.m.pins_after)
            def changed(effect, source, before):
                result = original(effect, source, before); mutation(result); return result
            with self.subTest(mutation=mutation), self.assertRaises((S.Refusal, T.Refusal)):
                S.Engine(self.context, authorizer=changed).handle(envelope)
            self.assertEqual(self.m.effects, [])
            self.assertIsNone(self.m.journals['PVE01'].read(self.m.plan['tx'], T.effect_key(self.f.request) + ':intent'))


if __name__ == '__main__': unittest.main()
