"""Private production entrypoint tests. Native tests relocate only fixed paths
into a root-private temporary fixture and use an inert dispatcher stub. They
never import PVE or mutate a VM; source/profile semantics are tested separately.
"""
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
PREFIX = 'usr/libexec/pve-sharedlvmthin/'
LAUNCHER = ROOT / (PREFIX + 'sharedlvmthin-vm-destroy-dispatch')
MODULE = ROOT / 'usr/share/perl5/PVE/SharedLvmThinVMDestroyDispatcher.pm'
BOOTSTRAP = ROOT / (PREFIX + 'sharedlvmthin-vm-destroy-state-bootstrap')
loader = importlib.machinery.SourceFileLoader('destroy_bootstrap_tests', str(BOOTSTRAP))
spec = importlib.util.spec_from_loader(loader.name, loader)
bootstrap = importlib.util.module_from_spec(spec)
loader.exec_module(bootstrap)
TXID = 'a' * 32


class ProductionWiringTests(unittest.TestCase):
    def test_one_installed_implementation_and_no_public_execute(self):
        self.assertTrue(MODULE.is_file())
        self.assertFalse((ROOT / 'experiments/thick-generations/lib/PVE/SharedLvmThinVMDestroyDispatcher.pm').exists())
        compatibility = (ROOT / 'experiments/thick-generations/guarded-vm-destroy-dispatch.pl').read_text()
        self.assertIn("exec '/usr/bin/perl', '-T', '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-dispatch'", compatibility)
        wrapper = (ROOT / 'usr/sbin/sharedlvmthin').read_text()
        self.assertNotIn('vm-destroy-execute)', wrapper)
        self.assertNotIn('vm-destroy-complete)', wrapper)
        self.assertIn('/usr/bin/python3 -I /usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy observe', wrapper)

    def test_launcher_and_bootstrap_are_common_executable_payload(self):
        build = (ROOT / 'scripts/build.sh').read_text()
        postinst = (ROOT / 'DEBIAN/postinst').read_text()
        excludes = (ROOT / 'packaging/thick-only/excluded-paths.txt').read_text().splitlines()
        for path in (LAUNCHER, BOOTSTRAP):
            relative = path.relative_to(ROOT).as_posix()
            self.assertIn('"$STAGE/' + relative + '"', build)
            self.assertIn('/' + relative, postinst)
            self.assertNotIn(relative, excludes)
        self.assertIn('/usr/bin/python3 -I /usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-state-bootstrap', postinst)

    def test_all_private_python_ipc_is_isolated_and_environment_cleaned(self):
        for path in (MODULE,
                     ROOT / 'usr/share/perl5/PVE/SharedLvmThinVMDestroyJournal.pm',
                     ROOT / 'usr/share/perl5/PVE/SharedLvmThinVMDestroyRecoveryDescriptor.pm'):
            source = path.read_text()
            self.assertIn("['/usr/bin/python3', '-I',", source)
            self.assertNotIn("['/usr/bin/python3', $HELPER", source)
            self.assertIn('local %ENV = (PATH =>', source)
        for name in ('sharedlvmthin-vm-destroy', 'sharedlvmthin-vm-destroy-recovery'):
            self.assertEqual((ROOT / (PREFIX + name)).read_text().splitlines()[0], '#!/usr/bin/python3 -I')

    def test_bootstrap_has_no_cli_path_override_and_refuses_nonroot(self):
        with mock.patch.object(bootstrap.os, 'geteuid', return_value=1000, create=True):
            with self.assertRaisesRegex(RuntimeError, 'root required'):
                bootstrap.main()
        with mock.patch.object(bootstrap.os, 'geteuid', return_value=0, create=True), \
             mock.patch.object(bootstrap.os, 'getuid', return_value=0, create=True), \
             mock.patch.object(bootstrap.sys, 'argv', ['bootstrap', '--base', '/tmp']):
            with self.assertRaisesRegex(RuntimeError, 'no arguments'):
                bootstrap.main()


@unittest.skipUnless(os.name == 'posix', 'requires real POSIX dirfd/fsync')
class BootstrapFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / 'state'

    def run_bootstrap(self):
        bootstrap.bootstrap(self.base, expected_uid=os.geteuid())

    def test_create_0700_parent_fsync_and_idempotent_preservation(self):
        fsync = os.fsync
        calls = []
        def record(fd):
            calls.append((os.fstat(fd).st_dev, os.fstat(fd).st_ino))
            fsync(fd)
        with mock.patch.object(bootstrap.os, 'fsync', side_effect=record):
            self.run_bootstrap()
        self.assertGreaterEqual(len(calls), 6)
        for leaf in bootstrap.LEAVES:
            path = self.base / leaf
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)
            (path / 'existing-receipt').write_bytes(b'preserve')
        before = [(p, p.stat().st_ino, p.read_bytes()) for p in self.base.rglob('existing-receipt')]
        self.run_bootstrap()
        for path, inode, content in before:
            self.assertEqual((path.stat().st_ino, path.read_bytes()), (inode, content))

    def test_unsafe_existing_leaf_is_not_repaired(self):
        self.base.mkdir(mode=0o700)
        leaf = self.base / bootstrap.LEAVES[0]
        leaf.mkdir(mode=0o755)
        with self.assertRaisesRegex(RuntimeError, 'no repair'):
            self.run_bootstrap()
        self.assertEqual(leaf.stat().st_mode & 0o777, 0o755)

    def test_symlink_parent_and_leaf_refused(self):
        foreign = Path(self.temp.name) / 'foreign'
        foreign.mkdir(mode=0o700)
        self.base.symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(OSError):
            self.run_bootstrap()
        self.assertEqual(list(foreign.iterdir()), [])
        self.base.unlink()
        self.base.mkdir(mode=0o700)
        (self.base / bootstrap.LEAVES[0]).symlink_to(foreign, target_is_directory=True)
        with self.assertRaises(OSError):
            self.run_bootstrap()
        self.assertEqual(list(foreign.iterdir()), [])

    def test_writable_parent_refused_without_leaf_creation(self):
        self.base.mkdir(mode=0o777)
        self.base.chmod(0o777)
        with self.assertRaisesRegex(RuntimeError, 'unsafe ancestor'):
            self.run_bootstrap()
        self.assertEqual(list(self.base.iterdir()), [])

    def test_parent_fsync_failure_propagates_without_false_success(self):
        with mock.patch.object(bootstrap.os, 'fsync', side_effect=OSError('injected fsync')):
            with self.assertRaisesRegex(OSError, 'injected fsync'):
                self.run_bootstrap()


@unittest.skipUnless(os.name == 'posix' and hasattr(os, 'geteuid') and os.geteuid() == 0
                     and Path('/root').is_dir() and shutil.which('perl'),
                     'requires root-private Linux Perl fixture; never modifies installed paths')
class InstalledLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='slt-launcher-test-', dir='/root')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.launcher = self.base / 'sharedlvmthin-vm-destroy-dispatch'
        self.module = self.base / 'SharedLvmThinVMDestroyDispatcher.pm'
        self.called = self.base / 'called'
        source = LAUNCHER.read_text().replace(
            '/usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy-dispatch', str(self.launcher)).replace(
            '/usr/share/perl5/PVE/SharedLvmThinVMDestroyDispatcher.pm', str(self.module))
        self.launcher.write_text(source)
        self.launcher.chmod(0o755)
        self.stub = '''package PVE::SharedLvmThinVMDestroyDispatcher;
use strict; use warnings;
sub new { bless {}, shift }
sub observe { open(my $fh, '>', 'CALLPATH') or die $!; print $fh "called"; close $fh;
    return { authority => 'NONE', status => 'FIXTURE_ONLY' }; }
sub execute { die "execute stub must never be invoked" }
1;
'''.replace('CALLPATH', str(self.called))
        self.module.write_text(self.stub)
        self.module.chmod(0o644)

    def invoke(self, *args, path=None, env=None):
        return subprocess.run(['/usr/bin/perl', '-T', str(path or self.launcher), *args],
                              env=env, capture_output=True, text=True, timeout=10)

    def observe(self, **kwargs):
        return self.invoke('observe', '--txid', TXID, **kwargs)

    def test_clean_and_hostile_environment_use_only_checked_module(self):
        injected = self.base / 'injected'
        injected.mkdir()
        (injected / 'JSON').mkdir()
        (injected / 'JSON/PP.pm').write_text('die "HOSTILE_JSON_LOADED";')
        env = dict(os.environ, PERL5LIB=str(injected), PERL5OPT='-MDoesNotExist',
                   PYTHONPATH=str(injected), PYTHONHOME=str(injected), PATH=str(injected))
        result = self.observe(env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('FIXTURE_ONLY', result.stdout)
        self.assertTrue(self.called.exists())

    def test_original_launcher_symlink_spelling_refused(self):
        alias = self.base / 'alias'
        alias.symlink_to(self.launcher)
        result = self.observe(path=alias)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixed installed launcher path', result.stderr)
        self.assertFalse(self.called.exists())

    def test_module_symlink_and_hardlink_refused(self):
        original = self.base / 'original'
        self.module.rename(original)
        self.module.symlink_to(original)
        self.assertNotEqual(self.observe().returncode, 0)
        self.module.unlink()
        os.link(original, self.module)
        self.assertNotEqual(self.observe().returncode, 0)
        self.assertFalse(self.called.exists())

    def test_hardlinked_launcher_refused(self):
        os.link(self.launcher, self.base / 'other-launcher')
        self.assertNotEqual(self.observe().returncode, 0)
        self.assertFalse(self.called.exists())

    def test_writable_ancestor_refused(self):
        self.base.chmod(0o777)
        self.assertNotEqual(self.observe().returncode, 0)
        self.assertFalse(self.called.exists())

    def test_dispatcher_change_during_require_refused(self):
        self.module.write_text(self.stub.replace('1;\n',
            "open(my $out, '>>', __FILE__) or die $!; print $out qq(\\n# drift\\n); close $out;\n1;\n"))
        result = self.observe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('dispatcher changed during load', result.stderr)
        self.assertFalse(self.called.exists())

    def test_launcher_change_during_require_refused(self):
        self.module.write_text(self.stub.replace('1;\n',
            "open(my $out, '>>', '" + str(self.launcher) + "') or die $!; print $out qq(\\n# drift\\n); close $out;\n1;\n"))
        result = self.observe()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('launcher changed during load', result.stderr)
        self.assertFalse(self.called.exists())

    def test_unknown_duplicate_and_mutating_recovery_arguments_refused(self):
        for args in (('observe', '--txid', TXID, '--base', '/tmp'),
                     ('observe', '--txid', TXID, '--txid', TXID),
                     ('observe', '--txid', TXID + '\n'),
                     ('complete-finalizing-absent', '--txid', TXID),
                     ('resume', '--txid', TXID)):
            with self.subTest(args=args):
                self.assertNotEqual(self.invoke(*args).returncode, 0)
                self.assertFalse(self.called.exists())


if __name__ == '__main__':
    unittest.main()
