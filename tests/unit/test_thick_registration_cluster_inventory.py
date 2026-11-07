import copy
import importlib.util
import pathlib
import unittest
import time
import threading
import os
import sys
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace

PATH = pathlib.Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-cluster-inventory.py'
SPEC = importlib.util.spec_from_file_location('registration_cluster_inventory', PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def evidence():
    candidate = {'name': 'test-vg', 'vg_uuid': 'vg-uuid', 'pv_uuid': 'pv-uuid',
                 'wwid': '3600000000000001', 'size_bytes': 1000, 'free_bytes': 900,
                 'pv_count': 1, 'healthy_paths': 1, 'empty_vg': True, 'canonical_mapper_present': True}
    members = {'node-a': '192.0.2.1', 'node-b': '192.0.2.2'}
    observations = {n: {'result': 'OBSERVED', 'candidates': [copy.deepcopy(candidate)]} for n in members}
    return members, observations, [{'storage': 'local', 'type': 'dir'}]


class ClusterInventoryTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe selector implementation')
    def test_run_json_bounds_both_pipes_and_timeout(self):
        child = lambda source: [sys.executable, '-c', source]
        self.assertEqual(MOD.run_json(child('import sys; sys.stderr.write("diagnostic"); print("{\\"ok\\":true}")'), timeout=5), {'ok': True})
        for source in (
            'import sys; sys.stdout.write("x" * (1024 * 1024 + 1)); sys.stdout.flush()',
            'import sys; sys.stderr.write("x" * (64 * 1024 + 1)); sys.stderr.flush(); print("{}")',
            'import time; time.sleep(3)',
            'import sys; print("{}"); sys.exit(7)',
        ):
            with self.subTest(source=source), self.assertRaises(MOD.InventoryError):
                MOD.run_json(child(source), timeout=0.5 if 'sleep' in source else 5)

    @unittest.skipUnless(os.name == 'posix', 'POSIX pipe selector implementation')
    def test_run_json_drains_concurrent_pipes_and_exact_limits(self):
        source = ('import sys,threading; '
                  't=threading.Thread(target=lambda: sys.stderr.write("e" * (64 * 1024))); '
                  't.start(); sys.stdout.write("\\\"" + "a" * (1024 * 1024 - 2) + "\\\""); '
                  'sys.stdout.flush(); t.join()')
        value = MOD.run_json([sys.executable, '-c', source], timeout=10)
        self.assertEqual(len(value), MOD.MAX_STDOUT_BYTES - 2)

    def test_node_fanout_is_bounded_and_each_node_is_read_once(self):
        members = {f'node-{i}': f'192.0.2.{i + 1}' for i in range(9)}
        active = 0
        peak = 0
        calls = []
        lock = threading.Lock()
        def runner(args, timeout):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                calls.append(args)
            time.sleep(0.02)
            with lock:
                active -= 1
            self.assertEqual(timeout, 60)
            return {'result': 'OBSERVED', 'candidates': []}
        result = MOD.collect_node_observations(members, 'node-0', 'print(1)', runner=runner)
        self.assertEqual(set(result), set(members))
        self.assertEqual(len(calls), len(members))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(sum(args[0] == '/usr/bin/python3' for args in calls), 1)
        self.assertTrue(all('StrictHostKeyChecking=yes' in args for args in calls if args[0] == '/usr/bin/ssh'))
        self.assertTrue(all('ControlMaster=no' in args and 'ControlPath=none' in args
                            for args in calls if args[0] == '/usr/bin/ssh'))

    def test_failed_node_fanout_never_returns_partial_success(self):
        members = {'node-a': '192.0.2.1', 'node-b': '192.0.2.2'}
        def runner(args, timeout):
            if args[0] == '/usr/bin/ssh':
                raise MOD.InventoryError('node unavailable')
            return {'result': 'OBSERVED', 'candidates': []}
        with self.assertRaises(MOD.InventoryError):
            MOD.collect_node_observations(members, 'node-a', 'print(1)', runner=runner)

    def test_failure_behind_slow_first_node_cancels_queued_reads(self):
        members = {f'node-{i:02d}': f'192.0.2.{i + 1}' for i in range(20)}
        first_started, failed, release = threading.Event(), threading.Event(), threading.Event()
        calls, errors = [], []
        lock = threading.Lock()
        def runner(args, timeout):
            with lock:
                calls.append(args)
            if args[0] == '/usr/bin/python3':
                first_started.set()
                release.wait(2)
            elif 'root@192.0.2.2' in args:
                self.assertTrue(first_started.wait(2))
                failed.set()
                raise MOD.InventoryError('peer unavailable')
            else:
                release.wait(2)
            return {'result': 'OBSERVED', 'candidates': []}
        def invoke():
            try:
                MOD.collect_node_observations(members, 'node-00', 'print(1)', runner=runner)
            except MOD.InventoryError as exc:
                errors.append(exc)
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(failed.wait(2))
            time.sleep(0.05)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertLessEqual(len(calls), 4, 'queued nodes must not start new reads after failure')

    def test_evidence_rejects_changed_report_or_unquorate_status(self):
        members, observations, storage = evidence()
        report = MOD.correlate(members, observations, storage)
        status = [{'type': 'cluster', 'quorate': 1, 'nodes': 2},
                  *[{'type': 'node', 'name': n, 'ip': a, 'online': 1} for n, a in members.items()]]
        raw = {'status_before': status, 'status_after': copy.deepcopy(status),
               'members_before': members, 'members_after': members,
               'config_before': 'a' * 64, 'config_after': 'a' * 64,
               'peer_pairs': {('node-a', 'node-b'), ('node-b', 'node-a')},
               'observations': observations, 'storage': storage, 'started_monotonic': time.monotonic(),
               'started_wall_utc': report['observed_at']}
        MOD.validate_evidence(report, raw)
        modified = copy.deepcopy(report)
        modified['candidates'][0]['free_bytes'] += 1
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(modified, raw)
        modified = copy.deepcopy(report)
        modified['storage_definitions'] = []
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(modified, raw)
        missing_pair = copy.deepcopy(raw)
        missing_pair['peer_pairs'].remove(('node-a', 'node-b'))
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(report, missing_pair)
        stale = copy.deepcopy(raw)
        stale['started_monotonic'] = time.monotonic() - 301
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(report, stale)
        modified = copy.deepcopy(report)
        modified['observed_at'] = 'different'
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(modified, raw)
        invalid_timestamp = copy.deepcopy(raw)
        invalid_timestamp['started_wall_utc'] = 'not-a-time'
        modified['observed_at'] = 'not-a-time'
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(modified, invalid_timestamp)
        raw['status_after'][0]['quorate'] = 0
        with self.assertRaises(MOD.InventoryError): MOD.validate_evidence(report, raw)

    def test_freshness_boundaries(self):
        MOD.require_fresh_duration(0)
        MOD.require_fresh_duration(299.99)
        for elapsed in (-1, 300, float('nan'), None):
            with self.subTest(elapsed=elapsed), self.assertRaises(MOD.InventoryError):
                MOD.require_fresh_duration(elapsed)

    def test_consistent_empty_vg_is_displayed_as_candidate(self):
        members, observations, storage = evidence()
        result = MOD.correlate(members, observations, storage)
        self.assertEqual(result['nodes'], ['node-a', 'node-b'])
        self.assertTrue(result['candidates'][0]['eligible_for_new_vg_registration'])
        self.assertEqual(result['candidates'][0]['paths_by_node'], {'node-a': 1, 'node-b': 1})
        self.assertIn('not fencing', result['notice'])

    def test_identity_mismatch_or_missing_path_is_not_eligible(self):
        for field, value in [('wwid', 'other'), ('pv_uuid', 'other'),
                             ('size_bytes', 2000), ('healthy_paths', 0)]:
            with self.subTest(field=field):
                members, observations, storage = evidence()
                observations['node-b']['candidates'][0][field] = value
                result = MOD.correlate(members, observations, storage)
                self.assertEqual(result['candidates'], [])
                self.assertIn('healthy_paths_unverified' if field == 'healthy_paths' else 'identity_mismatch', result['unverified_reasons'])

    def test_vg_missing_from_one_node_is_reported_as_unverified(self):
        members, observations, storage = evidence()
        observations['node-b']['candidates'] = []
        result = MOD.correlate(members, observations, storage)
        self.assertEqual(result['candidates'], [])
        self.assertEqual(result['unverified_reasons'], ['vg_not_visible_on_every_node'])

    def test_incomplete_nodes_and_unresolved_storage_fail_closed(self):
        members, observations, storage = evidence()
        del observations['node-b']
        with self.assertRaises(MOD.InventoryError): MOD.correlate(members, observations, storage)
        members, observations, storage = evidence()
        storage.append({'storage': 'other', 'type': 'lvm'})
        with self.assertRaises(MOD.InventoryError): MOD.correlate(members, observations, storage)

    def test_existing_alias_and_nonempty_vg_are_not_automatically_eligible(self):
        members, observations, storage = evidence()
        storage.append({'storage': 'existing', 'type': 'sharedlvmthin', 'slt-vgname': 'test-vg'})
        self.assertFalse(MOD.correlate(members, observations, storage)['candidates'][0]['eligible_for_new_vg_registration'])
        storage.pop()
        observations['node-a']['candidates'][0]['empty_vg'] = False
        self.assertFalse(MOD.correlate(members, observations, storage)['candidates'][0]['eligible_for_new_vg_registration'])

    def test_existing_case_variant_identity_is_not_eligible(self):
        for config_key, candidate_key in [('slt-expected-vg-uuid', 'vg_uuid'), ('slt-expected-wwid', 'wwid')]:
            with self.subTest(config_key=config_key):
                members, observations, storage = evidence()
                for row in observations.values():
                    row['candidates'][0][candidate_key] = 'abcd1234'
                storage.append({'storage': 'existing', 'type': 'sharedlvmthin',
                                'slt-vgname': 'different-vg', config_key: 'ABCD1234'})
                candidate = MOD.correlate(members, observations, storage)['candidates'][0]
                self.assertFalse(candidate['eligible_for_new_vg_registration'])
                self.assertEqual(candidate['existing_aliases'][0]['id'], 'existing')

    def test_existing_pv_identity_is_not_eligible(self):
        members, observations, storage = evidence()
        pv_uuid = observations['node-a']['candidates'][0]['pv_uuid']
        storage.append({'storage': 'existing', 'type': 'sharedlvmthin',
                        'slt-vgname': 'different-vg', 'slt-expected-pv-uuid': pv_uuid})
        candidate = MOD.correlate(members, observations, storage)['candidates'][0]
        self.assertFalse(candidate['eligible_for_new_vg_registration'])
        self.assertEqual(candidate['existing_aliases'][0]['id'], 'existing')

    def test_conflicting_candidate_even_if_not_seen_on_peer_is_refused(self):
        for field in ('vg_uuid', 'pv_uuid', 'wwid'):
            with self.subTest(field=field):
                members, observations, storage = evidence()
                original = observations['node-a']['candidates'][0]
                other = dict(original, name='other-vg', vg_uuid='other-vg-uuid',
                             pv_uuid='other-pv-uuid', wwid='other-wwid')
                other[field] = original[field].swapcase() if field != 'pv_uuid' else original[field]
                observations['node-a']['candidates'].append(other)
                with self.assertRaises(MOD.InventoryError): MOD.correlate(members, observations, storage)

    def test_quorum_and_offline_nodes_refused(self):
        status = [{'type': 'cluster', 'quorate': 1, 'nodes': 2},
                  {'type': 'node', 'name': 'node-a', 'ip': '192.0.2.1', 'online': 1},
                  {'type': 'node', 'name': 'node-b', 'ip': '192.0.2.2', 'online': 1}]
        self.assertEqual(len(MOD.members_from_status(status)), 2)
        status[2]['online'] = 0
        with self.assertRaises(MOD.InventoryError): MOD.members_from_status(status)
        status[2]['online'] = 1
        status[0]['quorate'] = 0
        with self.assertRaises(MOD.InventoryError): MOD.members_from_status(status)

    def test_every_peer_direction_is_probed_with_strict_host_keys(self):
        members = {'node-a': '192.0.2.1', 'node-b': '192.0.2.2', 'node-c': '192.0.2.3'}
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return SimpleNamespace(returncode=0)
        MOD.verify_peer_matrix(members, 'node-a', runner=runner)
        self.assertEqual(len(calls), 6)
        self.assertTrue(all('StrictHostKeyChecking=yes' in ' '.join(args) for args in calls))
        self.assertTrue(all('ssh_known_hosts' in ' '.join(args) for args in calls))
        for args in calls:
            # Remote-to-remote probes contain a second SSH command as a shell string.
            for command in (args, shlex.split(args[-1]) if args[-1].startswith('/usr/bin/ssh ') else []):
                if command:
                    self.assertIn('ControlMaster=no', command)
                    self.assertIn('ControlPath=none', command)
        def failed(args, **kwargs): return SimpleNamespace(returncode=255)
        with self.assertRaises(MOD.InventoryError): MOD.verify_peer_matrix(members, 'node-a', runner=failed)
        self.assertEqual(MOD.verify_peer_matrix({'node-a': '192.0.2.1'}, 'node-a', runner=runner), frozenset())
        with self.assertRaises(MOD.InventoryError): MOD.verify_peer_matrix({}, 'node-a', runner=runner)
        def unexpected(args, **kwargs): raise TypeError('unexpected runner error')
        with self.assertRaises(MOD.InventoryError): MOD.verify_peer_matrix(members, 'node-a', runner=unexpected)

    @unittest.skipUnless(shutil.which('ssh'), 'OpenSSH client unavailable')
    def test_ssh_options_override_multiplexing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = pathlib.Path(tmp) / 'config'
            config.write_text('Host *\n  ControlMaster auto\n  ControlPath /tmp/ssh-control-%r-%h-%p\n', encoding='ascii')
            baseline = subprocess.run([shutil.which('ssh'), '-G', '-F', str(config), 'root@192.0.2.2'],
                                      capture_output=True, text=True, check=True)
            baseline_options = dict(line.split(' ', 1) for line in baseline.stdout.splitlines() if ' ' in line)
            self.assertEqual(baseline_options.get('controlmaster'), 'auto')
            self.assertTrue(baseline_options.get('controlpath'))
            args = MOD.ssh_command('node-b', '192.0.2.2')
            result = subprocess.run([shutil.which('ssh'), '-G', '-F', str(config)] + args[1:],
                                    capture_output=True, text=True, check=True)
            options = dict(line.split(' ', 1) for line in result.stdout.splitlines() if ' ' in line)
            self.assertEqual(options.get('controlmaster'), 'false')
            self.assertEqual(options.get('controlpath', 'none'), 'none')

    def test_peer_failure_cancels_queued_matrix_reads(self):
        members = {f'node-{i:02d}': f'192.0.2.{i + 1}' for i in range(8)}
        first_started, failed, release = threading.Event(), threading.Event(), threading.Event()
        calls, errors = [], []
        lock = threading.Lock()
        def runner(args, **kwargs):
            with lock:
                calls.append(args)
            if 'root@192.0.2.2' in args:
                first_started.set()
                release.wait(2)
            elif 'root@192.0.2.3' in args:
                self.assertTrue(first_started.wait(2))
                failed.set()
                return SimpleNamespace(returncode=255)
            else:
                release.wait(2)
            return SimpleNamespace(returncode=0)
        def invoke():
            try:
                MOD.verify_peer_matrix(members, 'node-00', runner=runner)
            except MOD.InventoryError as exc:
                errors.append(exc)
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(failed.wait(2))
            time.sleep(0.05)
        finally:
            release.set()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertLessEqual(len(calls), 4)

    def test_changed_members_or_storage_config_invalidate_preview(self):
        MOD.require_stable_snapshot({'a': '192.0.2.1'}, {'a': '192.0.2.1'}, 'hash-a', 'hash-a')
        with self.assertRaises(MOD.InventoryError):
            MOD.require_stable_snapshot({'a': '192.0.2.1'}, {'a': '192.0.2.2'}, 'hash-a', 'hash-a')
        with self.assertRaises(MOD.InventoryError):
            MOD.require_stable_snapshot({'a': '192.0.2.1'}, {'a': '192.0.2.1'}, 'hash-a', 'hash-b')


if __name__ == '__main__':
    unittest.main()
