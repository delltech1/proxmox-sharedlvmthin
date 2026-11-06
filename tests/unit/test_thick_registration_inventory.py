import copy
import importlib.util
import pathlib
import subprocess
import unittest
from unittest.mock import patch

PATH = pathlib.Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-local-inventory.py'
SPEC = importlib.util.spec_from_file_location('registration_local_inventory', PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


def fixtures():
    return {
        '/sbin/vgs': {'report': [{'vg': [{'vg_name': 'vg', 'vg_uuid': 'u', 'vg_size': '1000.00', 'vg_free': '900.00', 'pv_count': '1'}]}]},
        '/sbin/pvs': {'report': [{'pv': [{'pv_name': '/dev/dm-1', 'pv_uuid': 'p', 'vg_name': 'vg', 'vg_uuid': 'u'}]}]},
        '/sbin/lvs': {'report': [{'lv': []}]},
        '/usr/bin/lsblk': {'blockdevices': [{'name': 'sdb', 'tran': 'iscsi'}, {'name': 'sdc', 'tran': 'iscsi'}]},
        '/sbin/multipathd': {'maps': [{'name': '3600000000000001', 'uuid': '3600000000000001', 'path_groups': [{'paths': [
            {'dm_st': 'active', 'chk_st': 'ready', 'dev_st': 'running', 'dev_t': '8:16', 'dev': 'sdb'},
            {'dm_st': 'failed', 'chk_st': 'faulty', 'dev_st': 'running', 'dev_t': '8:32', 'dev': 'sdc'}]}]}]},
    }


def read_fixture(data):
    def run(args): return copy.deepcopy(data[args[0]])
    def realpath(path): return '/dev/dm-1' if path.startswith('/dev/mapper/') else path
    return MOD.collect(run=run, realpath=realpath, isblock=lambda path: True,
                       mapper_uuid=lambda path: 'mpath-3600000000000001')


class LocalInventoryTests(unittest.TestCase):
    def test_reports_single_pv_and_actual_healthy_paths(self):
        entry = read_fixture(fixtures())['candidates'][0]
        self.assertEqual((entry['healthy_paths'], entry['total_paths']), (1, 2))
        self.assertEqual((entry['size_bytes'], entry['free_bytes']), (1000, 900))
        self.assertEqual(entry['transport'], 'iscsi')
        self.assertTrue(entry['empty_vg'])

    def test_lvm_inventory_does_not_trust_stale_hints(self):
        data = fixtures()
        seen = []
        def run(args):
            if args[0] in ('/sbin/vgs', '/sbin/pvs', '/sbin/lvs'):
                seen.append(args)
                if '--nohints' not in args:
                    result = copy.deepcopy(data[args[0]])
                    next(iter(result['report'][0].values())).clear()
                    return result
            return copy.deepcopy(data[args[0]])
        entry = MOD.collect(run=run,
                            realpath=lambda p: '/dev/dm-1' if p.startswith('/dev/mapper/') else p,
                            isblock=lambda p: True,
                            mapper_uuid=lambda p: 'mpath-3600000000000001')['candidates']
        self.assertEqual(len(entry), 1)
        self.assertEqual(len(seen), 3)

    def test_nonempty_vg_is_not_reported_empty(self):
        data = fixtures()
        data['/sbin/lvs']['report'][0]['lv'] = [{'vg_name': 'vg', 'lv_name': 'unrelated'}]
        entry = read_fixture(data)['candidates'][0]
        self.assertFalse(entry['empty_vg'])
        self.assertEqual(entry['existing_lv_count'], 1)

    def test_multipv_and_unmatched_mapper_are_not_candidates(self):
        data = fixtures()
        data['/sbin/vgs']['report'][0]['vg'][0]['pv_count'] = '2'
        self.assertEqual(read_fixture(data)['candidates'], [])
        data = fixtures()
        data['/sbin/pvs']['report'][0]['pv'][0]['pv_name'] = '/dev/dm-99'
        self.assertEqual(read_fixture(data)['candidates'], [])

    def test_incomplete_reports_refuse_instead_of_returning_empty_success(self):
        for key in ('/sbin/vgs', '/sbin/pvs', '/sbin/lvs', '/usr/bin/lsblk', '/sbin/multipathd'):
            with self.subTest(command=key):
                data = fixtures()
                data[key] = {}
                with self.assertRaises(MOD.InventoryError): read_fixture(data)

    def test_lvm_probe_failure_or_timeout_is_not_reported_as_empty_inventory(self):
        command = ['/sbin/vgs', '--readonly', '--nohints', '--reportformat', 'json']
        for error in (subprocess.CalledProcessError(2, command),
                      subprocess.TimeoutExpired(command, 30)):
            with self.subTest(error=type(error).__name__), patch.object(MOD.subprocess, 'run', side_effect=error):
                with self.assertRaises(MOD.InventoryError):
                    MOD.run_json(command)

    def test_invalid_numeric_and_path_rows_refuse(self):
        data = fixtures()
        data['/sbin/vgs']['report'][0]['vg'][0]['vg_size'] = '-1'
        with self.assertRaises(MOD.InventoryError): read_fixture(data)
        data = fixtures()
        data['/sbin/multipathd']['maps'][0]['path_groups'][0]['paths'] = [None]
        with self.assertRaises(MOD.InventoryError): read_fixture(data)
        data = fixtures()
        paths = data['/sbin/multipathd']['maps'][0]['path_groups'][0]['paths']
        paths[1]['dev_t'] = paths[0]['dev_t']
        with self.assertRaises(MOD.InventoryError): read_fixture(data)

    def test_fc_is_not_claimed_as_distinct_from_fcoe(self):
        data = fixtures()
        for row in data['/usr/bin/lsblk']['blockdevices']:
            row['tran'] = 'fc'
        self.assertEqual(read_fixture(data)['candidates'][0]['transport'], 'fc-or-fcoe')

    def test_fcoe_transport_is_recognized_without_claiming_fabric_proof(self):
        for transports in (('fcoe', 'fcoe'), ('fc', 'fcoe')):
            with self.subTest(transports=transports):
                data = fixtures()
                for row, transport in zip(data['/usr/bin/lsblk']['blockdevices'], transports):
                    row['tran'] = transport
                self.assertEqual(read_fixture(data)['candidates'][0]['transport'], 'fc-or-fcoe')

        data = fixtures()
        data['/usr/bin/lsblk']['blockdevices'][0]['tran'] = 'fcoe'
        self.assertEqual(read_fixture(data)['candidates'][0]['transport'], 'mixed-or-unknown')

    def test_shared_sas_hint_and_mixed_transport(self):
        data = fixtures()
        for row in data['/usr/bin/lsblk']['blockdevices']:
            row['tran'] = 'sas'
        self.assertEqual(read_fixture(data)['candidates'][0]['transport'], 'sas')
        data['/usr/bin/lsblk']['blockdevices'][1]['tran'] = 'iscsi'
        self.assertEqual(read_fixture(data)['candidates'][0]['transport'], 'mixed-or-unknown')

    def test_friendly_alias_without_canonical_name_is_visible_but_not_canonical(self):
        data = fixtures()
        data['/sbin/multipathd']['maps'][0]['name'] = 'mpatha'
        def run(args): return copy.deepcopy(data[args[0]])
        def realpath(path): return '/dev/dm-1' if path in ('/dev/mapper/mpatha', '/dev/dm-1') else path
        entry = MOD.collect(run=run, realpath=realpath, isblock=lambda path: path.endswith('mpatha'),
                            mapper_uuid=lambda path: 'mpath-3600000000000001')['candidates'][0]
        self.assertEqual(entry['mapper'], '/dev/mapper/mpatha')
        self.assertFalse(entry['canonical_mapper_present'])


if __name__ == '__main__':
    unittest.main()
