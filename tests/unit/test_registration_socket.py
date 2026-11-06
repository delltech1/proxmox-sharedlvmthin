import importlib.util
import json
import pathlib
import unittest
from unittest import mock

PATH = pathlib.Path(__file__).resolve().parents[2] / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin_registration_service.py'
SPEC = importlib.util.spec_from_file_location('registration_service', PATH)
MOD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MOD)


class FakeSocket:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def settimeout(self, value): pass
    def connect(self, path): pass
    def recv(self, n):
        part, self.payload = self.payload[:n], self.payload[n:]
        return part


class RegistrationSocketTests(unittest.TestCase):
    def setUp(self):
        self.unix = mock.patch.object(MOD.socket, 'AF_UNIX', 1, create=True)
        self.unix.start()

    def tearDown(self):
        self.unix.stop()

    def test_permissions_are_rechecked_and_group_membership_alone_is_not_enough(self):
        required = {'Sys.Audit', 'Datastore.Audit'}
        self.assertFalse(MOD.may_discover({'user': 'root@pam'}, {}, required))
        self.assertFalse(MOD.may_discover({'user': 'admin@pve', 'pve_ticket': 'ticket'}, {'/': {'Sys.Audit': 1}}, required))
        self.assertTrue(MOD.may_discover({'user': 'admin@pve', 'pve_ticket': 'ticket'}, {'/': {'Sys.Audit': 1, 'Datastore.Audit': 1}}, required))
        self.assertTrue(MOD.may_discover({'user': 'root@pam', 'pve_ticket': 'ticket'}, {}, required))
        self.assertFalse(MOD.may_discover({'user': 'root@pam', 'pve_ticket': 'ticket'}, None, required))

    def test_valid_response_is_cached_and_incomplete_is_refused(self):
        response = {'result': 'OBSERVED', 'observed_at': '2026-01-01T00:00:00Z', 'nodes': [], 'candidates': [], 'storage_definitions': []}
        calls = []
        def factory(*args):
            calls.append(1)
            return FakeSocket(json.dumps(response).encode())
        client = MOD.InventoryClient(connector=factory, clock=lambda: 100)
        self.assertEqual(client.observe(cache_seconds=15), response)
        self.assertEqual(client.observe(cache_seconds=15), response)
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.observe(), response)
        self.assertEqual(len(calls), 2, 'explicit discovery must collect a fresh observation')
        client = MOD.InventoryClient(connector=lambda *args: FakeSocket(b'{"result":"UNKNOWN"}'))
        with self.assertRaises(MOD.InventoryUnavailable): client.observe()

    def test_oversize_and_concurrent_requests_are_refused(self):
        client = MOD.InventoryClient(connector=lambda *args: FakeSocket(b'x' * (MOD.MAX_BYTES + 1)))
        with self.assertRaises(MOD.InventoryUnavailable): client.observe()
        self.assertTrue(client.lock.acquire(blocking=False))
        try:
            with self.assertRaises(MOD.InventoryUnavailable): client.observe()
        finally:
            client.lock.release()

    def test_known_failures_return_only_bounded_actionable_codes(self):
        for reason, code in [('peer SSH proof failed for node-a to node-b','peer_ssh_unverified'),
                             ('cluster quorum not proven','quorum_unverified'),
                             ('cluster node unavailable or ambiguous','nodes_unverified'),
                             ('unexpected private output','unavailable')]:
            with self.subTest(reason=reason):
                payload=json.dumps({'result':'UNKNOWN','reason':reason}).encode()
                client=MOD.InventoryClient(connector=lambda *args: FakeSocket(payload))
                with self.assertRaises(MOD.InventoryUnavailable) as caught: client.observe()
                self.assertEqual(caught.exception.code,code)


if __name__ == '__main__':
    unittest.main()
