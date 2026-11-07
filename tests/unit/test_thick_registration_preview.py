import importlib.machinery
import importlib.util
import copy
import time
import pathlib
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from io import StringIO

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HELPER = load('preview_helper', ROOT / 'usr/sbin/sharedlvmthin-register-preview')
PLANNER = load('preview_planner', ROOT / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-plan.py')
INVENTORY = load('preview_inventory', ROOT / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-cluster-inventory.py')


class PreviewTests(unittest.TestCase):
    def inventory(self):
        return {'result': 'OBSERVED', 'nodes': ['node-a', 'node-b'], 'storage_definitions': [],
                'candidates': [{'name': 'labvg', 'eligible_for_new_vg_registration': True,
                                'vg_uuid': 'ABCDEF-abcd-1234-ABCD-5678-aaaa-123456',
                                'pv_uuid': '123456-abcd-1234-ABCD-5678-aaaa-ABCDEF',
                                'wwid': '3600000000000001', 'size_bytes': 1000000,
                                'free_bytes': 900000, 'paths_by_node': {'node-a': 1, 'node-b': 2},
                                'transport_by_node': {'node-a': 'iscsi', 'node-b': 'iscsi'},
                                'empty_on_all_nodes': True, 'canonical_mapper_on_all_nodes': True,
                                'existing_aliases': []}]}

    def collected(self, inventory):
        c = inventory['candidates'][0]
        row = {key: c[key] for key in ('name', 'vg_uuid', 'pv_uuid', 'wwid', 'size_bytes')}
        row.update(pv_count=1, empty_vg=True, canonical_mapper_present=True)
        nodes = inventory['nodes']
        return {'members_before': {n: n for n in nodes}, 'members_after': {n: n for n in nodes},
                'config_before': 'digest', 'config_after': 'digest',
                'peer_pairs': {(a, b) for a in nodes for b in nodes if a != b},
                'observations': {n: {'result': 'OBSERVED', 'candidates': [{**row, 'healthy_paths': c['paths_by_node'][n]}]} for n in nodes}}

    def partial(self):
        report = self.inventory()
        c = report['candidates'][0]
        definition = {'id': 'present-eager', 'type': 'sharedlvmthin', 'vg_name': 'labvg',
                      'vg_uuid': c['vg_uuid'], 'pv_uuid': c['pv_uuid'], 'wwid': c['wwid'],
                      'allocation_mode': 'thick-generations'}
        report['storage_definitions'] = [definition]
        c['existing_aliases'] = [copy.deepcopy(definition)]
        c['eligible_for_new_vg_registration'] = False
        raw = self.collected(report)
        raw['pve_parsed_config'] = [{'storage': 'present-eager', 'type': 'sharedlvmthin',
            'shared': 1, 'nodes': {'node-a': 1, 'node-b': 1},
            'content': {'images': 1, 'rootdir': 1}, 'slt-vgname': 'labvg',
            'slt-allocation-mode': 'thick-generations', 'slt-vg-layout': 'isolated',
            'slt-expected-vg-uuid': c['vg_uuid'], 'slt-expected-pv-uuid': c['pv_uuid'],
            'slt-expected-wwid': c['wwid'], 'slt-vg-reserve-percent': 0,
            'slt-expected-min-paths': 1, 'slt-lock-timeout': 42}]
        return report, raw

    def test_resume_uses_complete_parsed_policy_and_preserves_defaults(self):
        report, raw = self.partial()
        result = HELPER.build_plan(report, 'labvg', None, 'missing-lazy', 0,
                                   PLANNER, raw, resume_existing='present-eager')
        self.assertEqual(result['authority'], 'NONE')
        self.assertEqual(result['missing_mode'], 'lazy')
        self.assertEqual(result['api_parameters']['slt-lock-timeout'], 42)
        self.assertNotIn('slt-lock-yield-ms', result['api_parameters'])
        self.assertIn('--slt-allocation-mode thick-generations-lazy', result['cli'])

    def test_resume_refuses_missing_parser_alias_conflicts_and_nonempty_peer(self):
        for change in ('parser', 'scope', 'alias', 'nonempty', 'mode', 'unknown', 'policy'):
            report, raw = self.partial()
            if change == 'parser': del raw['pve_parsed_config']
            elif change == 'scope': raw['pve_parsed_config'][0]['nodes'] = {'node-a': 1}
            elif change == 'alias': report['candidates'][0]['existing_aliases'].append({'id': 'other'})
            elif change == 'nonempty': raw['observations']['node-b']['candidates'][0]['empty_vg'] = False
            elif change == 'mode': raw['pve_parsed_config'][0]['slt-allocation-mode'] = 'thin'
            elif change == 'unknown': raw['pve_parsed_config'][0]['slt-unreviewed'] = 'x'
            else: raw['pve_parsed_config'][0]['slt-expected-min-paths'] = 2
            with self.subTest(change=change), self.assertRaises((ValueError, PLANNER.Refusal)):
                HELPER.build_plan(report, 'labvg', None, 'missing-lazy', 0,
                                  PLANNER, raw, resume_existing='present-eager')

    def test_resume_cli_uses_two_identical_observations_without_writes(self):
        report, raw = self.partial()
        inventory = SimpleNamespace(collect_evidence=lambda **kwargs: (copy.deepcopy(report), copy.deepcopy(raw)),
                                    validate_evidence=lambda *_: None)
        out = StringIO()
        with patch.object(HELPER.os, 'geteuid', return_value=0, create=True), \
             patch.object(HELPER.sys, 'argv', ['helper', '--vg', 'labvg',
                          '--resume-existing', 'present-eager', '--missing-id', 'missing-lazy']), \
             patch.object(HELPER, 'load', side_effect=[inventory, PLANNER]), \
             patch.object(HELPER.sys, 'stdout', out):
            self.assertEqual(HELPER.main(), 0)
        self.assertIn('PREVIEW ONLY', out.getvalue())
        self.assertEqual(out.getvalue().count('pvesm add sharedlvmthin'), 1)

    def test_live_collector_attaches_one_partial_preview_from_validated_evidence(self):
        source, raw = self.partial()
        members = {'node-a': '192.0.2.1', 'node-b': '192.0.2.2'}
        status = [{'type': 'cluster', 'quorate': 1, 'nodes': 2},
                  *[{'type': 'node', 'name': n, 'ip': a, 'online': 1} for n, a in members.items()]]
        for obs in raw['observations'].values():
            obs['candidates'][0].update(free_bytes=900000, transport='iscsi')
        storage = [{k: v for k, v in raw['pve_parsed_config'][0].items()
                    if k not in ('nodes', 'content')}]
        report = INVENTORY.correlate(members, raw['observations'], storage)
        raw.update(members_before=members, members_after=members, status_before=status,
                   status_after=copy.deepcopy(status), config_before='a'*64, config_after='a'*64,
                   storage=storage, started_monotonic=time.monotonic(), started_wall_utc=report['observed_at'])
        result = INVENTORY.add_resume_previews(report, raw, PLANNER, HELPER)
        preview = result['candidates'][0]['resume_preview']
        self.assertEqual(preview['existing_id'], 'present-eager')
        self.assertEqual(preview['missing_mode'], 'lazy')
        self.assertEqual(preview['argv'][:4], ['pvesm', 'add', 'sharedlvmthin', preview['placeholder']])
        self.assertIn('--slt-lock-timeout', preview['argv'])

    def test_collector_never_previews_conflicting_parsed_config(self):
        report, raw = self.partial()
        raw['pve_parsed_config'][0]['slt-expected-vg-uuid'] = 'FEDCBA-abcd-1234-ABCD-5678-aaaa-123456'
        with self.assertRaises(ValueError):
            HELPER.build_plan(report, 'labvg', None, 'missing-lazy', 0,
                              PLANNER, raw, resume_existing='present-eager')

    def test_build_plan_preserves_identity_and_observed_minimum_paths(self):
        inventory = self.inventory()
        result = HELPER.build_plan(inventory, 'labvg', 'eager', 'lazy', 5, PLANNER, self.collected(inventory))
        self.assertEqual(result['result'], 'PREVIEW_ONLY')
        for item in result['definitions']:
            self.assertEqual(item['api_parameters']['slt-expected-min-paths'], 1)
            self.assertEqual(item['api_parameters']['slt-expected-wwid'], '3600000000000001')
            self.assertNotIn('slt-burst-headroom-gib', item['api_parameters'])

    def test_missing_or_ineligible_candidate_is_not_planned(self):
        inventory = self.inventory()
        inventory['candidates'][0]['eligible_for_new_vg_registration'] = False
        with self.assertRaises(ValueError):
            HELPER.build_plan(inventory, 'labvg', 'eager', 'lazy', 5, PLANNER, self.collected(inventory))

    def test_selection_refresh_checks_identity_and_membership(self):
        before = self.inventory()
        HELPER.require_unchanged_selection(before, copy.deepcopy(before), 'labvg')
        for field in ('vg_uuid', 'pv_uuid', 'wwid', 'canonical_mapper_on_all_nodes'):
            with self.subTest(field=field):
                after = copy.deepcopy(before)
                after['candidates'][0][field] = False if field == 'canonical_mapper_on_all_nodes' else 'changed'
                with self.assertRaises(ValueError): HELPER.require_unchanged_selection(before, after, 'labvg')
        after = copy.deepcopy(before)
        after['nodes'].append('node-c')
        with self.assertRaises(ValueError): HELPER.require_unchanged_selection(before, after, 'labvg')

    def test_unproven_empty_or_mapper_is_not_planned(self):
        for field in ('empty_on_all_nodes', 'canonical_mapper_on_all_nodes'):
            with self.subTest(field=field):
                inventory = self.inventory()
                inventory['candidates'][0][field] = False
                with self.assertRaises(ValueError): HELPER.build_plan(inventory, 'labvg', 'eager', 'lazy', 0, PLANNER, self.collected(inventory))

    def test_missing_or_inconsistent_raw_evidence_refused(self):
        inventory = self.inventory()
        for change in ('coverage', 'node_identity', 'pv_count', 'config'):
            with self.subTest(change=change):
                raw = self.collected(inventory)
                if change == 'coverage': raw['peer_pairs'].pop()
                elif change == 'node_identity': raw['observations']['node-b']['candidates'][0]['pv_uuid'] = 'different'
                elif change == 'pv_count': raw['observations']['node-b']['candidates'][0]['pv_count'] = 2
                else: raw['config_after'] = 'different'
                with self.assertRaises(ValueError): HELPER.build_plan(inventory, 'labvg', 'eager', 'lazy', 0, PLANNER, raw)

    def test_main_refreshes_and_refuses_changed_identity(self):
        before = self.inventory()
        after = copy.deepcopy(before)
        after['candidates'][0]['wwid'] = '3600000000000002'
        observations = iter([before, after])
        calls = []
        def collect():
            calls.append(1)
            return next(observations)
        inventory = SimpleNamespace(collect_evidence=lambda: (collect(), self.collected(before)),
                                    validate_evidence=lambda report, evidence: None)
        output, error = StringIO(), StringIO()
        with patch.object(HELPER.os, 'geteuid', return_value=0, create=True), \
             patch.object(HELPER.sys, 'argv', ['helper', '--vg', 'labvg']), \
             patch.object(HELPER, 'load', side_effect=[inventory, PLANNER]), \
             patch.object(HELPER.sys, 'stdout', output), patch.object(HELPER.sys, 'stderr', error):
            self.assertEqual(HELPER.main(), 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn('pvesm add', output.getvalue())
        self.assertIn('changed', error.getvalue())

    def test_main_emits_preview_through_real_evidence_validator(self):
        source = self.inventory()
        raw = self.collected(source)
        members = {'node-a': '192.0.2.1', 'node-b': '192.0.2.2'}
        status = [{'type': 'cluster', 'quorate': 1, 'nodes': 2},
                  *[{'type': 'node', 'name': n, 'ip': a, 'online': 1} for n, a in members.items()]]
        for obs in raw['observations'].values():
            obs['candidates'][0].update(free_bytes=900000, transport='iscsi')
        report = INVENTORY.correlate(members, raw['observations'], [])
        raw.update(members_before=members, members_after=members, status_before=status,
                   status_after=copy.deepcopy(status), config_before='a'*64, config_after='a'*64,
                   storage=[], started_monotonic=time.monotonic(), started_wall_utc=report['observed_at'])
        inventory = SimpleNamespace(collect_evidence=lambda: (report, raw), validate_evidence=INVENTORY.validate_evidence)
        output = StringIO()
        with patch.object(HELPER.os, 'geteuid', return_value=0, create=True), \
             patch.object(HELPER.sys, 'argv', ['helper', '--vg', 'labvg']), \
             patch.object(HELPER, 'load', side_effect=[inventory, PLANNER]), \
             patch.object(HELPER.sys, 'stdout', output):
            self.assertEqual(HELPER.main(), 0)
        self.assertIn('PREVIEW ONLY', output.getvalue())
        self.assertIn('--slt-vg-reserve-percent 0', output.getvalue())
        self.assertIn('Observation and command generation do not guarantee VG identity or emptiness at execution time.', output.getvalue())
        self.assertIn('Before each command, recheck the VG identity and state on every node, including whether it is empty and existing storage definitions.', output.getvalue())
        self.assertIn('Eager and Lazy registrations are not atomic; the first can succeed even if the second fails.', output.getvalue())
        self.assertIn('inspect it; do not retry blindly.', output.getvalue())
        self.assertEqual(output.getvalue().count('pvesm add sharedlvmthin'), 2)


if __name__ == '__main__':
    unittest.main()
