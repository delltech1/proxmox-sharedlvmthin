"""No sockets/SSH/hosts: all transports and bootstrap filesystem calls mocked."""
import base64
import copy
import importlib.util
from pathlib import Path
import stat
import struct
import types
import unittest
from contextlib import ExitStack
from unittest import mock

import test_layout_migration_exact_restore_model as fixture

SOURCE = Path(__file__).resolve().parents[2] / 'experiments/thick-generations/layout-migration-exact-transport.py'
spec = importlib.util.spec_from_file_location('exact_transport_test', SOURCE)
T = importlib.util.module_from_spec(spec)
spec.loader.exec_module(T)


class Files:
    identity_path = str(T.ROOT / 'identity_ed25519')
    known_hosts_path = str(T.ROOT / 'known_hosts')
    def __init__(self, plan, pins):
        self.plan, self.pins, self.drift = plan, pins, False
    def check(self):
        T.need(not self.drift, 'bootstrap identity drift')
    def load(self):
        return copy.deepcopy((self.plan, self.pins))


class Journal:
    def __init__(self): self.records = {}
    def read(self, tx, key): return copy.deepcopy(self.records.get((tx, key)))
    def create_once(self, tx, key, value):
        T.need((tx, key) not in self.records, 'create-only conflict')
        self.records[(tx, key)] = copy.deepcopy(value)
        return {'durable': True, 'sha256': T.M.digest(value)}


class ExactTransportTests(unittest.TestCase):
    def setUp(self):
        self.plan = fixture.fixture()
        for row, node in zip(self.plan['participants'], T.NODES):
            row['node'] = node
        self.records = fixture.Backend(self.plan).records
        self.pins = {'schema': 'slt-exact-transport-pins/v1', 'authority': 'NONE',
                     'plan_sha256': T.M.digest(self.plan), 'helper_path': T.HELPER, 'participants': []}
        for index, participant in enumerate(self.plan['participants'], 1):
            raw = struct.pack('>I', 11) + b'ssh-ed25519' + struct.pack('>I', 32) + bytes([index]) * 32
            key = 'ssh-ed25519 ' + base64.b64encode(raw).decode()
            self.pins['participants'].append({
                'node': participant['node'], 'host': '192.0.2.' + str(index), 'boot_id': participant['boot_id'],
                'ssh_host_key': key, 'ssh_host_fingerprint': T.ssh_key(key), 'helper_sha256': 'f' * 64,
                'source_package': {'name': 'pve-sharedlvmthin', 'version': '1', 'artifact_sha256': 'a' * 64,
                                   'runtime_build_id': 'c' * 64, 'sources_sha256': 'd' * 64}})
        self.files = Files(self.plan, self.pins)
        self.calls, self.nonces = [], 0
        self.response_change = lambda result: result
        self.wire_change = lambda raw: raw
        self.failure = None
        self.now = 200
        # Any accidental use of the real process entrypoint fails before exec.
        self.process = mock.patch.object(T.subprocess, 'Popen', side_effect=AssertionError('real process forbidden'))
        self.process.start(); self.addCleanup(self.process.stop)

    def nonce(self):
        self.nonces += 1
        return format(self.nonces, '064x')

    def create(self, **kwargs):
        kwargs.setdefault('receipt_journal', Journal())
        return T.Transport(files=self.files, runner=self.exchange, require_root=lambda: None,
                           clock=lambda: self.now, nonce=self.nonce, **kwargs)

    def request(self, node='PVE04', operation='UNMASK', unit='pvedaemon.service'):
        return {'tx': self.plan['tx'], 'plan_sha256': T.M.digest(self.plan), 'node': node,
                'boot_id': next(r['boot_id'] for r in self.plan['participants'] if r['node'] == node),
                'unit': unit, 'operation': operation, 'baseline_sha256': 'a' * 64, 'release_sha256': 'b' * 64}

    def exchange(self, argv, raw, timeout):
        self.calls.append((argv, raw, timeout))
        if self.failure:
            raise self.failure
        request = T.strict(raw)
        node = request['node']
        row = self.pins['participants'][T.NODES.index(node)]
        if request['kind'] == 'READ_OBSERVATION':
            payload = copy.deepcopy(self.records[node])
        else:
            effect = request['payload']
            payload = {'schema': 'slt-local-restore-receipt/v1', 'authority': 'NONE',
                       'state': 'LOCAL_EFFECT_CONFIRMED', 'request_sha256': T.M.digest(effect),
                       'intent_sha256': 'a' * 64, 'effect_retry_allowed': False,
                       'started_at': 190, 'finished_at': 199,
                       'after': {'active': effect['operation'] == 'START', 'masked': False,
                                 'substate': 'running' if effect['operation'] == 'START' else 'dead',
                                 'source': {'fixture': 'unit-source-pin'}},
                       'command': {'exit_code': 0, 'stdout_sha256': 'b' * 64, 'stderr_sha256': 'c' * 64}}
        response = {key: request[key] for key in ('authority', 'kind', 'tx', 'plan_sha256', 'node', 'boot_id', 'challenge')}
        response.update(schema='slt-exact-transport-response/v1', request_sha256=T.M.digest(request),
                        helper_path=T.HELPER, helper_sha256=row['helper_sha256'],
                        source_package=copy.deepcopy(row['source_package']), payload=payload)
        return 0, self.wire_change(T.canonical(self.response_change(response))), b''

    def test_collect_exact_four_including_control_and_unique_challenges(self):
        result = self.create().collect()
        self.assertEqual(result['authority'], 'NONE')
        self.assertEqual(tuple(result['records']), T.NODES)
        self.assertEqual(len({T.strict(raw)['challenge'] for _, raw, _ in self.calls}), 4)
        self.assertTrue(all(T.strict(raw)['kind'] == 'READ_OBSERVATION' for _, raw, _ in self.calls))

    def test_fixed_subsystem_pinned_keys_no_shell_or_generic_command(self):
        self.create().observe('PVE04')
        argv, _, timeout = self.calls[0]
        self.assertEqual(argv[:4], ['/usr/bin/ssh', '-F', '/dev/null', '-T'])
        self.assertEqual(argv[-4:], ['-s', '--', '192.0.2.4', T.SUBSYSTEM])
        for option in ('StrictHostKeyChecking=yes', 'HostKeyAlias=PVE04', 'ProxyCommand=none',
                       'IdentityAgent=none', 'ConnectionAttempts=1', 'ControlMaster=no',
                       'UserKnownHostsFile=' + self.files.known_hosts_path):
            self.assertIn(option, argv)
        self.assertNotIn(T.HELPER, argv, 'helper path is not a remote shell command')
        self.assertLessEqual(timeout, T.DEADLINE)

    def test_partial_alias_duplicate_or_mixed_helper_cohort_refused_without_process(self):
        original = copy.deepcopy(self.pins)
        for kind in ('partial', 'alias', 'key', 'fingerprint', 'helper', 'source', 'boot', 'order'):
            self.pins.clear(); self.pins.update(copy.deepcopy(original))
            row = self.pins['participants'][3]
            if kind == 'partial': self.pins['participants'].pop()
            elif kind == 'alias': row['host'] = 'pve04.example.invalid'
            elif kind == 'key': row.update(ssh_host_key=original['participants'][0]['ssh_host_key'],
                                          ssh_host_fingerprint=original['participants'][0]['ssh_host_fingerprint'])
            elif kind == 'fingerprint': row['ssh_host_fingerprint'] = 'SHA256:foreign'
            elif kind == 'helper': row['helper_sha256'] = '1' * 64
            elif kind == 'source': row['source_package']['sources_sha256'] = '1' * 64
            elif kind == 'boot': row['boot_id'] = original['participants'][0]['boot_id']
            elif kind == 'order': self.pins['participants'].reverse()
            with self.subTest(kind=kind), self.assertRaises(T.Refusal): self.create()
        self.assertEqual(self.calls, [])

    def test_host_key_mismatch_exit_is_fatal_without_retry(self):
        transport = self.create()
        transport.runner = mock.Mock(return_value=(255, b'', b'Host key verification failed.'))
        with self.assertRaises(T.Refusal): transport.observe('PVE01')
        with self.assertRaises(T.Refusal): transport.observe('PVE01')
        self.assertEqual(transport.runner.call_count, 1)

    def test_response_node_boot_nonce_request_helper_and_package_are_exact(self):
        for field, value in (('node', 'pve01'), ('boot_id', 'foreign'), ('challenge', 'f' * 64),
                             ('request_sha256', 'f' * 64), ('helper_path', '/bin/sh'),
                             ('helper_sha256', '0' * 64), ('authority', 'ALLOW'),
                             ('source_package', {})):
            self.response_change = lambda response, f=field, v=value: dict(response, **{f: v})
            with self.subTest(field=field), self.assertRaises(T.Refusal): self.create().observe('PVE01')

    def test_noncanonical_duplicate_unknown_trailing_and_oversize_outputs_refused(self):
        transforms = (lambda raw: raw + b'\n', lambda raw: b' ' + raw,
                      lambda raw: raw.replace(b'"authority":"NONE"', b'"authority":"NONE","authority":"NONE"', 1),
                      lambda _: b'x' * (T.MAX_WIRE + 1), lambda _: b'{"value":NaN}\n')
        for transform in transforms:
            self.wire_change = transform
            with self.subTest(transform=transform), self.assertRaises(T.Refusal): self.create().observe('PVE01')
        self.wire_change = lambda raw: raw
        self.response_change = lambda response: dict(response, extra='unknown')
        with self.assertRaises(T.Refusal): self.create().observe('PVE01')

    def test_partial_cohort_never_returns_success(self):
        def change(response):
            if response['node'] == 'PVE04': response['payload']['membership'].pop()
            return response
        self.response_change = change
        transport = self.create()
        with self.assertRaises(T.Refusal): transport.collect()
        self.assertTrue(transport.poisoned)
        self.assertEqual(len(self.calls), 4)

    def test_default_and_refused_dispatch_guard_never_contact_peer(self):
        for guard in (None, lambda *_: False):
            with self.subTest(guard=guard), self.assertRaises(T.Refusal):
                self.create(dispatch_guard=guard).dispatch_typed(self.request())
        self.assertEqual(self.calls, [])

    def test_exact_one_effect_control_node_and_replay_refusal(self):
        transport = self.create(dispatch_guard=lambda *_: True)
        request = self.request()
        self.assertEqual(transport.dispatch_typed(request), {'request_sha256': T.M.digest(request), 'result': 'CONFIRMED'})
        with self.assertRaises(T.Refusal): transport.dispatch_typed(request)
        envelope = T.strict(self.calls[0][1])
        self.assertEqual(envelope['payload'], request)
        self.assertEqual(envelope['authority'], 'NONE')
        self.assertEqual(len(self.calls), 1)

    def test_timeout_uncertain_dispatch_poisons_all_nodes_without_retry(self):
        self.failure = TimeoutError('deadline')
        transport = self.create(dispatch_guard=lambda *_: True)
        with self.assertRaises(TimeoutError): transport.dispatch_typed(self.request())
        with self.assertRaises(T.Refusal): transport.dispatch_typed(self.request('PVE01'))
        with self.assertRaises(T.Refusal): transport.observe('PVE04')
        self.assertEqual(len(self.calls), 1)

    def test_full_receipt_is_persisted_before_return_and_fresh_process_never_retries(self):
        journal = Journal()
        transport = self.create(dispatch_guard=lambda *_: True, receipt_journal=journal)
        request = self.request()
        receipt = transport.dispatch_restore_once(request)
        self.assertEqual(receipt['state'], 'LOCAL_EFFECT_CONFIRMED')
        self.assertEqual(journal.read(self.plan['tx'], 'receipt:' + T.effect_key(request)), receipt)
        snapshot = copy.deepcopy(journal.records)
        transport = self.create(dispatch_guard=lambda *_: True, receipt_journal=journal)
        self.now = self.plan['expires_at'] + 1
        result = transport.inspect_restore_attempt(request)
        self.assertEqual(result['receipt'], receipt)
        self.assertFalse(result['effect_retry_allowed'])
        self.assertEqual(transport.inspect_restore_attempt(request, challenge='f' * 64)['state'], 'UNKNOWN')
        self.assertEqual(journal.records, snapshot)
        self.now = 200
        with self.assertRaises(T.Refusal): transport.dispatch_restore_once(request)
        self.assertEqual(len(self.calls), 1)

    def test_attempt_and_full_receipt_lost_ack_are_retained_without_redispatch(self):
        for suffix in (':intent', 'receipt:', ':done'):
            with self.subTest(suffix=suffix):
                journal = Journal(); create = journal.create_once
                def lost(tx, key, value):
                    ack = create(tx, key, value)
                    if key.startswith(suffix) or key.endswith(suffix): raise OSError('ACK lost')
                    return ack
                journal.create_once = lost
                transport = self.create(dispatch_guard=lambda *_: True, receipt_journal=journal)
                before = len(self.calls)
                with self.assertRaises(OSError): transport.dispatch_restore_once(self.request())
                snapshot = copy.deepcopy(journal.records)
                result = transport.inspect_restore_attempt(self.request())
                self.assertIn(result['state'], ('UNKNOWN', 'COMPLETED_HISTORICAL'))
                self.assertFalse(result['effect_retry_allowed'])
                fresh = self.create(dispatch_guard=lambda *_: True, receipt_journal=journal)
                with self.assertRaises(T.Refusal): fresh.dispatch_restore_once(self.request())
                self.assertEqual(len(self.calls) - before, 0 if suffix == ':intent' else 1)
                self.assertEqual(journal.records, snapshot)

    def test_restore_requires_explicit_durable_journal_before_contact(self):
        with self.assertRaisesRegex(T.Refusal, 'journal absent'):
            self.create(dispatch_guard=lambda *_: True, receipt_journal=None).dispatch_restore_once(self.request())
        self.assertEqual(self.calls, [])

    def test_replayed_response_to_new_challenge_is_refused(self):
        old = []
        def wire(raw):
            if not old: old.append(raw)
            return old[0]
        self.wire_change = wire
        transport = self.create()
        transport.observe('PVE01')
        with self.assertRaises(T.Refusal): transport.observe('PVE01')

    def test_nonce_reuse_refused_before_second_network_call(self):
        transport = self.create(); transport.nonce = lambda: 'a' * 64
        transport.observe('PVE01')
        with self.assertRaises(T.Refusal): transport.observe('PVE04')
        self.assertEqual(len(self.calls), 1)

    def test_shell_args_unknown_units_and_preserved_service_start_refused(self):
        for changes in ({'argv': ['sh', '-c', 'anything']}, {'operation': 'STOP'},
                        {'unit': 'pve-guests.service', 'operation': 'START'},
                        {'unit': 'corosync.service'}, {'node': 'PVE04;anything'}):
            with self.subTest(changes=changes), self.assertRaises(T.Refusal):
                self.create(dispatch_guard=lambda *_: True).dispatch_typed(dict(self.request(), **changes))
        self.assertEqual(self.calls, [])

    def test_bad_effect_outcome_timing_command_or_postcondition_poison(self):
        for field, value in (('state', 'UNKNOWN_RETAIN'), ('effect_retry_allowed', True),
                             ('finished_at', 201), ('command', {'exit_code': 1}), ('extra', 'unknown')):
            def change(response, f=field, v=value):
                response['payload'][f] = v
                return response
            self.response_change = change
            transport = self.create(dispatch_guard=lambda *_: True)
            with self.subTest(field=field), self.assertRaises(T.Refusal): transport.dispatch_typed(self.request())
            self.assertTrue(transport.poisoned)

    def test_root_bootstrap_drift_plan_expiry_and_fork_refuse(self):
        transport = self.create()
        self.files.drift = True
        with self.assertRaises(T.Refusal): transport.observe('PVE01')
        self.files.drift = False
        self.now = 1801
        with self.assertRaises(T.Refusal): transport.observe('PVE01')
        self.now = 200
        with mock.patch.object(T.os, 'getpid', return_value=-1):
            with self.assertRaises(T.Refusal): transport.observe('PVE01')
        self.assertEqual(self.calls, [])

    def test_fixed_files_reject_owner_mode_links_before_reading_content(self):
        safe_dir = types.SimpleNamespace(st_uid=0, st_mode=stat.S_IFDIR | 0o700)
        safe_file = dict(st_uid=0, st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_size=20)
        for changed in ({'st_uid': 1000}, {'st_mode': stat.S_IFREG | 0o644},
                        {'st_nlink': 2}, {'st_mode': stat.S_IFLNK | 0o600}):
            entries = [safe_dir] * (len(T.ROOT.parts)) + [types.SimpleNamespace(**(safe_file | changed))]
            with self.subTest(changed=changed), mock.patch.object(T, 'root_required'), \
                    mock.patch.object(T.os, 'open', return_value=9), mock.patch.object(T.os, 'close'), \
                    mock.patch.object(T.os, 'fstat', side_effect=entries), mock.patch.object(T.os, 'read') as read, \
                    mock.patch.object(T.os, 'O_DIRECTORY', 0, create=True), \
                    mock.patch.object(T.os, 'O_NOFOLLOW', 0, create=True), \
                    mock.patch.object(T.os, 'O_CLOEXEC', 0, create=True), \
                    mock.patch.object(T.os, 'O_NONBLOCK', 0, create=True):
                with self.assertRaises(T.Refusal): T.FixedFiles.read('pins.json', 100)
                read.assert_not_called()

    def fake_process_run(self, scenario):
        class Pipe:
            def __init__(self, fd): self.fd, self.closed = fd, False
            def fileno(self): return self.fd
            def close(self): self.closed = True
        class Selector:
            def __init__(self): self.items = {}
            def register(self, pipe, events, label):
                self.items[pipe.fd] = types.SimpleNamespace(fd=pipe.fd, fileobj=pipe, data=label)
            def unregister(self, pipe): self.items.pop(pipe.fd)
            def select(self, _): return [(key, 1) for key in list(self.items.values())]
            def get_map(self): return self.items
            def close(self): pass
        process = mock.Mock(stdin=Pipe(10), stdout=Pipe(11), stderr=Pipe(12))
        process.poll.return_value = None
        process.wait.return_value = 0
        reads = {11: [b'ok', b''], 12: [b'']}
        if scenario == 'stdout': reads[11] = [b'x' * 65536] * 5
        if scenario == 'stderr': reads[12] = [b'x' * (T.MAX_ERROR + 1)]
        times = iter([0, 0, 21] if scenario == 'timeout' else [0] * 50)
        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(T, 'root_required'))
            popen = stack.enter_context(mock.patch.object(T.subprocess, 'Popen', return_value=process))
            kill = stack.enter_context(mock.patch.object(T.os, 'killpg', create=True))
            stack.enter_context(mock.patch.object(T.signal, 'SIGKILL', 9, create=True))
            stack.enter_context(mock.patch.object(T.os, 'set_blocking', create=True))
            stack.enter_context(mock.patch.object(T.os, 'write', side_effect=lambda _, value: len(value)))
            stack.enter_context(mock.patch.object(T.os, 'read', side_effect=lambda fd, _: reads[fd].pop(0)))
            stack.enter_context(mock.patch.object(T.selectors, 'DefaultSelector', Selector))
            stack.enter_context(mock.patch.object(T.time, 'monotonic', side_effect=lambda: next(times)))
            if scenario == 'success':
                self.assertEqual(T.bounded_process(T.ssh_argv('PVE01', '192.0.2.1'), b'{}\n', 20), (0, b'ok', b''))
            else:
                with self.assertRaises(T.Refusal):
                    T.bounded_process(T.ssh_argv('PVE01', '192.0.2.1'), b'{}\n', 20)
                kill.assert_called_once()
            self.assertFalse(popen.call_args.kwargs['shell'])
            self.assertEqual(popen.call_args.kwargs['env'], T.ENV)
            self.assertTrue(process.stdin.closed and process.stdout.closed and process.stderr.closed)

    def test_bounded_process_streams_timeout_and_fixed_environment_without_real_process(self):
        for scenario in ('success', 'stdout', 'stderr', 'timeout'):
            with self.subTest(scenario=scenario): self.fake_process_run(scenario)

    def test_process_runner_refuses_generic_argv_before_popen(self):
        with mock.patch.object(T, 'root_required'):
            for argv in (['/bin/sh', '-c', 'anything'],
                         T.ssh_argv('PVE01', '192.0.2.1') + ['extra'],
                         ['/usr/bin/ssh', 'some-alias', T.SUBSYSTEM]):
                with self.subTest(argv=argv), self.assertRaises(T.Refusal):
                    T.bounded_process(argv, b'{}\n', 20)


if __name__ == '__main__':
    unittest.main()
