"""Only synthetic socket observations; no VM or storage effects."""
import importlib.util
from pathlib import Path
import socket
import threading
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'experiments/thick-generations/release-functional-console.py'
SPEC = importlib.util.spec_from_file_location('functional_console', SOURCE)
MODULE = importlib.util.module_from_spec(SPEC); SPEC.loader.exec_module(MODULE)


class FakeConnection:
    def __init__(self, chunks):
        self.chunks = iter(chunks); self.sent = []
    def settimeout(self, value):
        pass
    def sendall(self, payload):
        self.sent.append(payload)
    def recv(self, size):
        item = next(self.chunks, b'')
        if isinstance(item, Exception):
            raise item
        return item


class ConsoleTests(unittest.TestCase):
    def test_readiness_never_sends_firmware_input(self):
        connection = FakeConnection([b'\xffGRUB banner\nSLT_READONLY_GUEST_', b'PROTOCOL=1\r\n'])
        self.assertEqual(MODULE.wait_ready(connection)['status'], 'READY_OBSERVED')
        self.assertEqual(connection.sent, [])

    def test_missing_ready_is_unknown_with_zero_dispatch(self):
        for chunks in ([socket.timeout()], [b''], [b'prefix SLT_READONLY_GUEST_PROTOCOL=1\n', b'']):
            connection = FakeConnection(chunks)
            with self.assertRaises(MODULE.Unknown):
                MODULE.wait_ready(connection)
            self.assertEqual(connection.sent, [])

    def test_ready_then_exact_one_request(self):
        connection = FakeConnection([b'SLT_READONLY_GUEST_PROTOCOL=1\n', b'RAM c alpha\n'])
        MODULE.wait_ready(connection)
        MODULE.exchange(connection, 'GET_RAM', [], 'c')
        self.assertEqual(connection.sent, [b'GET_RAM c\n'])

    def test_split_and_stale_frames(self):
        connection = FakeConnection([b'RAM stale wrong\nRAM c al', b'pha\r\n'])
        result = MODULE.exchange(connection, 'GET_RAM', [], 'c')
        self.assertEqual(result['lines'], ['RAM c alpha'])
        self.assertEqual(connection.sent, [b'GET_RAM c\n'])

    def test_firmware_graphics_not_protocol_evidence(self):
        result = MODULE.exchange(FakeConnection([b'\xe2\x94\x80 firmware\nRAM challenge alpha\n']), 'GET_RAM', [], 'challenge')
        self.assertEqual(result['lines'], ['RAM challenge alpha'])
        with self.assertRaises(MODULE.Unknown):
            MODULE.exchange(FakeConnection([b'RAM challenge \xff\n']), 'GET_RAM', [], 'challenge')

    def test_mutation_only_ram_never_retries(self):
        for chunks in ([socket.timeout()], [b''], [b'ERROR c ARGUMENTS\n']):
            connection = FakeConnection(chunks)
            with self.assertRaises(MODULE.Unknown):
                MODULE.exchange(connection, 'SET_RAM', ['alpha'], 'c')
            self.assertEqual(connection.sent, [b'SET_RAM c alpha\n'])

    def test_exact_verify_binding(self):
        args = ['vda', '4096', 'a'*64]
        self.assertTrue(MODULE.response('VERIFY', args, 'c', 'VERIFY_OK c ' + ' '.join(args)))
        for line in ('VERIFY_OK c vdb 4096 ' + 'a'*64, 'VERIFY_OK c vda 4096 ' + 'b'*64):
            with self.assertRaises(MODULE.Unknown):
                MODULE.response('VERIFY', args, 'c', line)

    def test_sysinfo_requires_header(self):
        with self.assertRaises(MODULE.Unknown):
            MODULE.exchange(FakeConnection([b'SYSINFO_END c\n']), 'SYSINFO', [], 'c')
        connection = FakeConnection([b'SYSINFO c BOOT 11111111-2222-3333-4444-555555555555 EFI YES\nSYSINFO_END c\n'])
        self.assertEqual(MODULE.exchange(connection, 'SYSINFO', [], 'c')['status'], 'OBSERVED')

    def test_input_paths_alignment_and_injection(self):
        for args in (['../sda', '4096', 'a'*64], ['sda1', '4096', 'a'*64], ['vda', '4097', 'a'*64]):
            with self.assertRaises(ValueError):
                MODULE.request('VERIFY', args, 'c')
        with self.assertRaises(ValueError):
            MODULE.request('SET_RAM', ['$(id)'], 'c')
        self.assertIn(b'2199023251456', MODULE.request('VERIFY', ['vda', '2199023251456', 'a'*64], 'c'))

    def test_no_false_success_on_truncation_and_limits(self):
        for chunks, limit in (([b'RAM c alpha'], 262144), ([b'x'*20], 10)):
            with self.assertRaises(MODULE.Unknown):
                MODULE.exchange(FakeConnection(chunks), 'GET_RAM', [], 'c', max_bytes=limit)

    def test_malformed_inventory_cannot_pass(self):
        header = b'SYSINFO c BOOT 11111111-2222-3333-4444-555555555555 EFI NO\n'
        for record in (b'BLOCK c 766461 NaN 2f646576 73\n', b'NIC c badhex 73 73 73\n'):
            with self.assertRaises(MODULE.Unknown):
                MODULE.exchange(FakeConnection([header + record + b'SYSINFO_END c\n']), 'SYSINFO', [], 'c')
        result = MODULE.exchange(FakeConnection([header + b'BLOCK c 766461 524288 2f646576 \nSYSINFO_END c\n']), 'SYSINFO', [], 'c')
        self.assertEqual(result['status'], 'OBSERVED')

    @unittest.skipUnless(hasattr(socket, 'socketpair'), 'socketpair unavailable')
    def test_real_stream_one_request(self):
        client, server = socket.socketpair()
        received = []
        def guest():
            with server:
                received.append(server.recv(4096))
                server.sendall(b'RAM c alpha\n')
        thread = threading.Thread(target=guest); thread.start()
        try:
            with client:
                result = MODULE.exchange(client, 'GET_RAM', [], 'c', seconds=2)
            self.assertEqual(result['lines'], ['RAM c alpha'])
        finally:
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(received, [b'GET_RAM c\n'])


if __name__ == '__main__':
    unittest.main()
