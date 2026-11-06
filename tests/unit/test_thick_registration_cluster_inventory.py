import copy
import importlib.util
import pathlib
import unittest
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
        def failed(args, **kwargs): return SimpleNamespace(returncode=255)
        with self.assertRaises(MOD.InventoryError): MOD.verify_peer_matrix(members, 'node-a', runner=failed)

    def test_changed_members_or_storage_config_invalidate_preview(self):
        MOD.require_stable_snapshot({'a': '192.0.2.1'}, {'a': '192.0.2.1'}, 'hash-a', 'hash-a')
        with self.assertRaises(MOD.InventoryError):
            MOD.require_stable_snapshot({'a': '192.0.2.1'}, {'a': '192.0.2.2'}, 'hash-a', 'hash-a')
        with self.assertRaises(MOD.InventoryError):
            MOD.require_stable_snapshot({'a': '192.0.2.1'}, {'a': '192.0.2.1'}, 'hash-a', 'hash-b')


if __name__ == '__main__':
    unittest.main()
