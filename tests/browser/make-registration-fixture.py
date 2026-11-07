"""Build browser fixture through the actual read-only cluster correlator."""
import importlib.util
import json
import pathlib

root = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('registration_inventory', root / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-registration-cluster-inventory.py')
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)
candidate = {'name': 'test_vg', 'vg_uuid': 'ABCDEF-abcd-1234-ABCD-5678-aaaa-123456',
             'pv_uuid': '123456-abcd-1234-ABCD-5678-aaaa-ABCDEF',
             'wwid': '3600000000000001', 'size_bytes': 1000, 'free_bytes': 900,
             'pv_count': 1, 'healthy_paths': 1, 'empty_vg': True,
             'canonical_mapper_present': True, 'transport': 'iscsi'}
members = {'node-a': '192.0.2.1', 'node-b': '192.0.2.2'}
observations = {n: {'result': 'OBSERVED', 'candidates': [candidate]} for n in members}
storage = [{'storage': 'local', 'type': 'dir'},
           {'storage': 'archive', 'type': 'lvm', 'vgname': '.archive'}]
print(json.dumps(inventory.correlate(members, observations, storage)))
