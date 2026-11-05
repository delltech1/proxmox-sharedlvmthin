"""Exercise actual dpkg-deb archive parsing using synthetic disposable DEBs."""
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

import test_freeze_package_settlement as fixture


@unittest.skipUnless(os.name == 'posix' and shutil.which('ar') and shutil.which('dpkg-deb'),
                     'real Debian archive tools required')
class RealCarrierArchiveTests(unittest.TestCase):
    def setUp(self):
        self.m = fixture.load('carrier_real_archive', fixture.ROOT /
            'usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = {'control': b'Package: test\nVersion: 1\nArchitecture: all\n',
                      'sharedlvmthin-candidate-update-policy': b'policy',
                      'sharedlvmthin_update_policy.py': b'core',
                      'sharedlvmthin_package_executor_fence.py': b'fence'}

    def make_deb(self, *, duplicate=False, link=False, truncate=False):
        (self.root / 'debian-binary').write_bytes(b'2.0\n')
        with tarfile.open(self.root / 'control.tar.gz', 'w:gz') as archive:
            for name, data in self.files.items():
                info = tarfile.TarInfo('./' + name)
                if link and name.endswith('fence.py'):
                    info.type = tarfile.SYMTYPE
                    info.linkname = 'control'
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            if duplicate:
                info = tarfile.TarInfo('./sharedlvmthin_package_executor_fence.py')
                info.size = 1
                archive.addfile(info, io.BytesIO(b'x'))
        if truncate:
            path = self.root / 'control.tar.gz'
            data = path.read_bytes()
            path.write_bytes(data[:len(data) // 2])
        with tarfile.open(self.root / 'data.tar.gz', 'w:gz'):
            pass
        subprocess.run(['ar', 'rc', 'candidate.deb', 'debian-binary', 'control.tar.gz', 'data.tar.gz'],
                       cwd=self.root, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        return self.root / 'candidate.deb'

    def test_real_deb_accepts_matching_regular_members(self):
        self.assertEqual(self.m.recovery_carrier_members(self.make_deb()), self.files)

    def test_real_deb_rejects_duplicate_symlink_and_truncation(self):
        for variant in ('duplicate', 'link', 'truncate'):
            with self.subTest(variant=variant), self.assertRaises((RuntimeError, tarfile.TarError, EOFError, OSError)):
                self.m.recovery_carrier_members(self.make_deb(**{variant: True}))


if __name__ == '__main__':
    unittest.main()
