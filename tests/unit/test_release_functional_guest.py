"""Function-only shell fixtures; never invokes guest main on host /proc/cmdline."""
from pathlib import Path
import os
import select
import shutil
import subprocess
import unittest

SOURCE = Path(__file__).resolve().parents[2] / 'experiments/thick-generations/release-functional-guest.sh'


class GuestProtocolTests(unittest.TestCase):
    @unittest.skipUnless(os.name == 'posix', 'isolated PTY requires POSIX')
    def test_partial_request_timeout_never_mutates_ram(self):
        busybox = shutil.which('busybox')
        if busybox is None:
            self.skipTest('stock BusyBox unavailable')
        import pty
        import tty
        master, slave = pty.openpty(); tty.setraw(slave)
        functions = SOURCE.read_text().split('# MAIN_GUEST_GUARD:')[0]
        child = subprocess.Popen([busybox, 'sh', '-c', functions + '\nperiodic_beacon=YES\nprotocol\n'],
                                 stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        try:
            self.assertTrue(select.select([master], [], [], 4)[0])
            self.assertIn(b'SLT_READONLY_GUEST_PROTOCOL=1', os.read(master, 4096))
            os.write(master, b'SET_RAM c al')
            self.assertTrue(select.select([master], [], [], 5)[0])
            output = os.read(master, 4096)
            self.assertIn(b'SLT_READONLY_GUEST_PROTOCOL=1', output)
            os.write(master, b'pha\nGET_RAM inspect\n')
            for _ in range(8):
                if select.select([master], [], [], 1)[0]:
                    output += os.read(master, 4096)
                if b'RAM inspect UNSET' in output:
                    break
            self.assertIn(b'RAM inspect UNSET', output)
            self.assertNotIn(b'RAM_SET c', output)
        finally:
            child.terminate(); child.wait(timeout=3); os.close(master)

    @unittest.skipUnless(os.name == 'posix', 'isolated PTY requires POSIX')
    def test_receive_only_beacon_then_one_ram_request(self):
        busybox = shutil.which('busybox')
        if busybox is None:
            self.skipTest('stock BusyBox unavailable')
        import pty
        functions = SOURCE.read_text().split('# MAIN_GUEST_GUARD:')[0]
        # Function-only fixture. Never executes main/host cmdline or devices.
        script = functions + '\nperiodic_beacon=YES\nprotocol\n'
        master, slave = pty.openpty()
        child = subprocess.Popen([busybox, 'sh', '-c', script], stdin=slave, stdout=slave, stderr=slave)
        os.close(slave)
        try:
            self.assertTrue(select.select([master], [], [], 4)[0])
            self.assertIn(b'SLT_READONLY_GUEST_PROTOCOL=1', os.read(master, 4096))
            os.write(master, b'SET_RAM c alpha\nGET_RAM d\n')
            output = b''
            for _ in range(10):
                if select.select([master], [], [], 1)[0]:
                    output += os.read(master, 4096)
                if b'RAM d alpha' in output:
                    break
            self.assertIn(b'RAM_SET c alpha', output)
            self.assertIn(b'RAM d alpha', output)
        finally:
            # Terminates only this isolated fixture, never a storage worker.
            child.terminate(); child.wait(timeout=3); os.close(master)

    def test_host_guard_and_no_mutation_surface(self):
        text = SOURCE.read_text()
        self.assertTrue(text.startswith('#!/bin/busybox sh\n'))
        self.assertIn("# shellcheck disable=SC3045\n            if ! IFS=' ' read -r -t 1", text)
        self.assertIn('[ "$argument" != slt_disposable_guest=YES ]', text)
        self.assertIn('[ "$approved" -eq 1 ]', text)
        self.assertNotIn('eval ', text)
        self.assertNotIn('of=', text)
        self.assertNotIn('mkfs', text)
        self.assertNotIn('dmsetup', text)
        self.assertIn('set -o pipefail || exit 2', text)

    def run_protocol(self, requests):
        shell = shutil.which('sh')
        if shell is None:
            self.skipTest('POSIX shell unavailable; no host guest entrypoint execution')
        functions = SOURCE.read_text().split('# MAIN_GUEST_GUARD:')[0]
        # Read-only SYSINFO/VERIFY replaced: these tests never inspect host devices.
        script = functions + '\nsysinfo() { printf "FIXTURE_SYSINFO %s\\n" "$1"; }\nverify_read() { disk_ok "$2" && offset_ok "$3" && hash_ok "$4"; }\nprotocol\n'
        return subprocess.run([shell, '-c', script], input=requests, text=True,
                              capture_output=True, timeout=5, check=True).stdout

    def test_ram_challenge_and_state(self):
        output = self.run_protocol('GET_RAM c1\nSET_RAM c2 alpha\nGET_RAM c3\nSET_RAM c4 beta\nGET_RAM c5\n')
        self.assertEqual(output, 'RAM c1 UNSET\nRAM_SET c2 alpha\nRAM c3 alpha\nRAM_SET c4 beta\nRAM c5 beta\n')

    def test_invalid_tokens_and_arguments(self):
        output = self.run_protocol('GET_RAM c extra\nSET_RAM c $(id)\nGET_RAM ../bad\nBOGUS c\nGET_RAM c\n')
        self.assertNotIn('RAM_SET', output)
        self.assertIn('RAM c UNSET', output)
        self.assertEqual(output.count('ERROR'), 4)

    def test_verify_path_and_offset_refusal(self):
        sha = 'a'*64
        for disk, offset in (('../sda', '4096'), ('sda1','4096'), ('vda','-1'), ('vda','4097'), ('vda','0004096')):
            with self.subTest(disk=disk,offset=offset):
                self.assertIn('VERIFY_REFUSED', self.run_protocol(f'VERIFY c {disk} {offset} {sha}\n'))

    def test_tib_scale_offset_is_not_rejected_by_parser(self):
        sha = 'a'*64
        output = self.run_protocol(f'VERIFY c vda 2199023251456 {sha}\nGET_RAM end\n')
        self.assertEqual(output, 'RAM end UNSET\n')


if __name__ == '__main__':
    unittest.main()
