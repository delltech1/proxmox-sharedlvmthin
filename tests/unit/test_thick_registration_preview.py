import importlib.machinery
import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


HELPER = load('preview_helper', ROOT / 'usr/sbin/sharedlvmthin-register-preview')
PLANNER = load('preview_planner', ROOT / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-plan.py')


class PreviewTests(unittest.TestCase):
    def inventory(self):
        return {'result': 'OBSERVED', 'nodes': ['node-a', 'node-b'], 'storage_definitions': [],
                'candidates': [{'name': 'labvg', 'eligible_for_new_vg_registration': True,
                                'vg_uuid': 'ABCDEF-abcd-1234-ABCD-5678-aaaa-123456',
                                'pv_uuid': '123456-abcd-1234-ABCD-5678-aaaa-ABCDEF',
                                'wwid': '3600000000000001', 'size_bytes': 1000000,
                                'free_bytes': 900000, 'paths_by_node': {'node-a': 1, 'node-b': 2}}]}

    def test_build_plan_preserves_identity_and_observed_minimum_paths(self):
        result = HELPER.build_plan(self.inventory(), 'labvg', 'eager', 'lazy', 5, PLANNER)
        self.assertEqual(result['result'], 'PREVIEW_ONLY')
        for item in result['definitions']:
            self.assertEqual(item['api_parameters']['slt-expected-min-paths'], 1)
            self.assertEqual(item['api_parameters']['slt-expected-wwid'], '3600000000000001')
            self.assertNotIn('slt-burst-headroom-gib', item['api_parameters'])

    def test_missing_or_ineligible_candidate_is_not_planned(self):
        inventory = self.inventory()
        inventory['candidates'][0]['eligible_for_new_vg_registration'] = False
        with self.assertRaises(ValueError):
            HELPER.build_plan(inventory, 'labvg', 'eager', 'lazy', 5, PLANNER)


if __name__ == '__main__':
    unittest.main()
