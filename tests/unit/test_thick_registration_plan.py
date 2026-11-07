import copy
import importlib.util
import pathlib
import unittest

PATH = pathlib.Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-plan.py'
SPEC = importlib.util.spec_from_file_location('thick_registration_plan', PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def sample():
    identity = {'name': 'test_vg', 'vg_uuid': 'ABCDEF-abcd-1234-ABCD-5678-aaaa-123456',
                'pv_uuid': '123456-abcd-1234-ABCD-5678-aaaa-ABCDEF', 'pv_count': 1,
                'wwid': '3600000000000001', 'size_bytes': 1000000,
                'free_bytes': 900000, 'dedicated': True}
    peer = {k: identity[k] for k in ('vg_uuid', 'pv_uuid', 'wwid', 'size_bytes')}
    peer.update(complete=True, peer_ssh_verified=True, mapper_verified=True, healthy_paths=2)
    request = {'nodes': ['node-b', 'node-a'], 'vg': 'test_vg',
               'aliases': {'eager': 'demo-eager', 'lazy': 'demo-lazy'}}
    evidence = {'quorate': True, 'inventory_complete': True,
                'members': {'node-a': True, 'node-b': True}, 'vg': identity,
                'node_evidence': {'node-a': copy.deepcopy(peer), 'node-b': copy.deepcopy(peer)},
                'storage_definitions': []}
    return request, evidence


class RegistrationPlanTests(unittest.TestCase):
    def resume_fixture(self):
        request, evidence = sample()
        request = {'nodes': request['nodes'], 'vg': request['vg'], 'missing_id': 'demo-lazy'}
        evidence['config_snapshot_complete'] = True
        evidence['vg']['empty_on_all_nodes'] = True
        for peer in evidence['node_evidence'].values(): peer['empty_vg'] = True
        config = {'storage': 'demo-eager', 'type': 'sharedlvmthin', 'shared': 1, 'content': 'images,rootdir',
                  'nodes': 'node-a,node-b', 'slt-vgname': 'test_vg',
                  'slt-vg-layout': 'isolated', 'slt-allocation-mode': 'thick-generations',
                  'slt-expected-vg-uuid': evidence['vg']['vg_uuid'],
                  'slt-expected-pv-uuid': evidence['vg']['pv_uuid'],
                  'slt-expected-wwid': evidence['vg']['wwid'], 'slt-vg-reserve-percent': 0,
                  'slt-expected-min-paths': 2}
        evidence['storage_definitions'] = [{'id': 'demo-eager', 'type': 'sharedlvmthin',
              'vg_name': 'test_vg', 'vg_uuid': config['slt-expected-vg-uuid'],
              'pv_uuid': config['slt-expected-pv-uuid'], 'wwid': config['slt-expected-wwid'],
              'allocation_mode': 'thick-generations'}]
        return request, evidence, config

    def test_resume_empty_vg_copies_explicit_policy_and_retains_implicit_defaults(self):
        request, evidence, config = self.resume_fixture()
        config.update({'slt-vg-reserve-gib': 3, 'slt-lock-timeout': 46,
                       'slt-tg-command-deadline-sec': 45})
        result = MOD.resume_plan(request, evidence, config)
        self.assertEqual(result['authority'], 'NONE')
        self.assertEqual(result['missing_mode'], 'lazy')
        cfg = result['api_parameters']
        self.assertEqual(cfg['slt-vg-reserve-gib'], 3)
        self.assertEqual(cfg['slt-lock-timeout'], 46)
        self.assertEqual(cfg['slt-tg-command-deadline-sec'], 45)
        self.assertNotIn('slt-lock-yield-ms', cfg)
        self.assertEqual(cfg['slt-expected-min-paths'], 2)
        self.assertIn('--slt-allocation-mode thick-generations-lazy', result['cli'])

    def test_resume_refuses_each_ambiguous_or_changed_condition(self):
        cases = [('config_snapshot_complete', False), ('empty_on_all_nodes', False),
                 ('empty_vg', False), ('wwid', '3600000000000002'), ('shared', 0),
                 ('nodes', 'node-a'), ('content', 'images'), ('slt-expected-min-paths', 3), ('slt-vg-layout', 'mixed'),
                 ('slt-allocation-mode', 'thin'), ('slt-lock-timeout', True),
                 ('slt-unknown-policy', 1), ('duplicate', True)]
        for key, value in cases:
            request, evidence, config = self.resume_fixture()
            if key == 'config_snapshot_complete': evidence[key] = value
            elif key == 'empty_on_all_nodes': evidence['vg'][key] = value
            elif key == 'empty_vg': evidence['node_evidence']['node-a'][key] = value
            elif key == 'wwid': config['slt-expected-wwid'] = value
            elif key == 'duplicate': evidence['storage_definitions'].append(copy.deepcopy(evidence['storage_definitions'][0]))
            else: config[key] = value
            with self.subTest(key=key), self.assertRaises(MOD.Refusal):
                MOD.resume_plan(request, evidence, config)

    def test_minimum_path_count_obeys_plugin_option_limit(self):
        request, evidence = sample()
        for peer in evidence['node_evidence'].values():
            peer['healthy_paths'] = 64
        self.assertEqual(MOD.plan(request, evidence)['expected_min_paths'], 64)
        for peer in evidence['node_evidence'].values():
            peer['healthy_paths'] = 65
        with self.assertRaises(MOD.Refusal):
            MOD.plan(request, evidence)
        evidence['node_evidence']['node-a']['healthy_paths'] = 1
        self.assertEqual(MOD.plan(request, evidence)['expected_min_paths'], 1)

    def test_two_modes_produce_preview_only_and_same_identity(self):
        request, evidence = sample()
        output = MOD.plan(request, evidence)
        self.assertEqual(output['result'], 'PREVIEW_ONLY')
        configs = [item['api_parameters'] for item in output['definitions']]
        self.assertEqual({c['slt-allocation-mode'] for c in configs}, {'thick-generations', 'thick-generations-lazy'})
        self.assertTrue(all(c['slt-expected-vg-uuid'] == evidence['vg']['vg_uuid'] for c in configs))
        self.assertTrue(all(c['slt-vg-reserve-percent'] == 0 for c in configs))
        self.assertTrue(all('pvesm add sharedlvmthin' in item['cli'] for item in output['definitions']))

    def test_mismatched_identity_and_missing_paths_are_refused(self):
        for field, value in [('wwid', '3600000000000002'), ('vg_uuid', 'different'),
                             ('pv_uuid', 'different'), ('size_bytes', 999),
                             ('healthy_paths', 0), ('peer_ssh_verified', False), ('complete', False)]:
            with self.subTest(field=field):
                request, evidence = sample()
                evidence['node_evidence']['node-b'][field] = value
                with self.assertRaises(MOD.Refusal):
                    MOD.plan(request, evidence)

    def test_quorum_and_existing_conflicts_are_refused(self):
        for change in ('quorum', 'missing', 'duplicate', 'vg_alias', 'bad_reserve'):
            with self.subTest(change=change):
                request, evidence = sample()
                if change == 'quorum': evidence['quorate'] = False
                elif change == 'missing': del evidence['node_evidence']['node-b']
                elif change == 'duplicate': evidence['storage_definitions'].append({'id': 'demo-eager'})
                elif change == 'vg_alias': evidence['storage_definitions'].append({'id': 'other', 'vg_uuid': evidence['vg']['vg_uuid']})
                else: request['reserve_percent'] = -1
                with self.assertRaises(MOD.Refusal):
                    MOD.plan(request, evidence)

    def test_names_and_unproven_vg_refused(self):
        request, evidence = sample()
        request['aliases']['eager'] = 'x;touch /tmp/bad'
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)
        request, evidence = sample()
        evidence['vg']['dedicated'] = False
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)

    def test_zero_is_explicitly_supported_and_out_of_range_refused(self):
        request, evidence = sample()
        request['reserve_percent'] = 0
        result = MOD.plan(request, evidence)
        self.assertTrue(all(row['api_parameters']['slt-vg-reserve-percent'] == 0 for row in result['definitions']))
        request['reserve_percent'] = 5
        result = MOD.plan(request, evidence)
        self.assertTrue(all(row['api_parameters']['slt-vg-reserve-percent'] == 5 for row in result['definitions']))
        request['reserve_percent'] = 51
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)

    def test_existing_alias_requires_full_policy_review(self):
        request, evidence = sample()
        evidence['storage_definitions'] = [{
            'id': 'demo-eager', 'type': 'sharedlvmthin', 'vg_name': 'test_vg',
            'vg_uuid': evidence['vg']['vg_uuid'], 'pv_uuid': evidence['vg']['pv_uuid'],
            'wwid': evidence['vg']['wwid'], 'mode': 'eager', 'reserve_percent': 5,
            'nodes': ['node-a', 'node-b'],
        }]
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)
        evidence['storage_definitions'][0]['wwid'] = '3600000000000002'
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)

    def test_unresolved_association_is_refused(self):
        request, evidence = sample()
        evidence['storage_definitions'] = [{'id': 'unknown', 'type': 'lvm'}]
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)

    def test_existing_identity_conflicts_are_refused_case_insensitively(self):
        for field in ('vg_uuid', 'wwid'):
            with self.subTest(field=field):
                request, evidence = sample()
                evidence['storage_definitions'] = [{'id': 'other', field: evidence['vg'][field].swapcase()}]
                with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)

    def test_pv_identity_conflict_and_duplicate_definitions_refused(self):
        request, evidence = sample()
        evidence['storage_definitions'] = [{'id': 'other', 'pv_uuid': evidence['vg']['pv_uuid']}]
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)
        evidence['storage_definitions'] = [{'id': 'other'}, {'id': 'other'}]
        with self.assertRaises(MOD.Refusal): MOD.plan(request, evidence)


if __name__ == '__main__':
    unittest.main()
