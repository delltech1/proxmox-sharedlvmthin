"""Exercise the real bounded control parser; subprocess output is a fixture.

No dpkg process, installation, extraction or live host path is used.
"""
import io
import sys
import tarfile
import types
import unittest
from unittest import mock

import test_freeze_package_settlement as fixture_module


class RecoveryCarrierArchiveTests(unittest.TestCase):
    def setUp(self):
        imports = {} if sys.platform != 'win32' else {'fcntl': types.ModuleType('fcntl')}
        with mock.patch.dict(sys.modules, imports):
            self.m = fixture_module.load('carrier_archive_test',
                fixture_module.ROOT / 'usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy')
        self.members = [
            ('control', b'Package: pve-sharedlvmthin-thick\nVersion: 99\nArchitecture: all\n'),
            ('sharedlvmthin-candidate-update-policy', b'helper fixture'),
            ('sharedlvmthin_update_policy.py', b'core fixture'),
        ]

    def archive(self, members=None, extra=None):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            root = tarfile.TarInfo('./')
            root.type = tarfile.DIRTYPE
            archive.addfile(root)
            for name, value in self.members if members is None else members:
                info = tarfile.TarInfo('./' + name)
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            if extra is not None:
                archive.addfile(extra, io.BytesIO(b'x' * extra.size) if extra.isfile() else None)
        return output.getvalue()

    def inspect(self, raw, *, rc=0, errors=b''):
        def process(argv, **kwargs):
            self.assertEqual(argv, ['/usr/bin/dpkg-deb', '--ctrl-tarfile', '/fixture/carrier.deb'])
            self.assertEqual(kwargs['timeout'], 30)
            self.assertEqual(kwargs['env'], {'PATH': self.m.SAFE_PATH, 'LC_ALL': 'C', 'LANG': 'C'})
            self.assertEqual(kwargs['stdin'], self.m.subprocess.DEVNULL)
            self.assertTrue(callable(kwargs['preexec_fn']))
            kwargs['stdout'].write(raw)
            kwargs['stderr'].write(errors)
            return types.SimpleNamespace(returncode=rc)
        imports = {} if sys.platform != 'win32' else {'resource': types.SimpleNamespace()}
        with mock.patch.dict(sys.modules, imports), \
                mock.patch.object(self.m.subprocess, 'run', side_effect=process):
            return self.m.recovery_carrier_members('/fixture/carrier.deb')

    def test_exact_members_are_read_without_filesystem_extraction(self):
        self.assertEqual(self.inspect(self.archive()), dict(self.members))

    def test_missing_and_duplicate_code_members_are_refused(self):
        for members in (self.members[:-1], self.members + [self.members[1]]):
            with self.subTest(members=members), self.assertRaises(RuntimeError):
                self.inspect(self.archive(members))

    def test_symlink_hardlink_absolute_parent_and_nested_members_refused(self):
        for kind, name in ((tarfile.SYMTYPE, './alias'), (tarfile.LNKTYPE, './alias'),
                           (tarfile.REGTYPE, '/outside'), (tarfile.REGTYPE, '../outside'),
                           (tarfile.REGTYPE, './nested/file')):
            entry = tarfile.TarInfo(name)
            entry.type = kind
            entry.linkname = '/outside'
            with self.subTest(kind=kind, name=name), self.assertRaisesRegex(RuntimeError, 'unsafe'):
                self.inspect(self.archive(extra=entry))

    def test_per_member_and_member_count_limits(self):
        large = tarfile.TarInfo('./oversized')
        large.size = 4 * 1024 * 1024 + 1
        with self.assertRaisesRegex(RuntimeError, 'unsafe'):
            self.inspect(self.archive(extra=large))
        with self.assertRaisesRegex(RuntimeError, 'member limit'):
            self.inspect(self.archive(self.members + [(f'file{index}', b'') for index in range(128)]))

    def test_process_error_or_any_diagnostic_refuses_archive(self):
        for rc, errors in ((1, b''), (0, b'warning')):
            with self.subTest(rc=rc, errors=errors), self.assertRaisesRegex(RuntimeError, 'inspection failed'):
                self.inspect(self.archive(), rc=rc, errors=errors)

    def test_full_control_archive_byte_limit(self):
        archive = self.archive()
        with self.assertRaisesRegex(RuntimeError, 'archive byte limit'):
            self.inspect(archive + b'\0' * (8 * 1024 * 1024 + 1 - len(archive)))


if __name__ == '__main__':
    unittest.main()
