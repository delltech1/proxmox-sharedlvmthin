import importlib.util
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
import uuid
import shutil
import json

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/layout-migration-candidate-render.py"
SPEC = importlib.util.spec_from_file_location("candidate_render", PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


def archive(entries):
    result = io.BytesIO()
    with tarfile.open(fileobj=result, mode="w") as tf:
        for name, kind, raw, mode in entries:
            ti = tarfile.TarInfo(name)
            ti.type, ti.mode, ti.size = kind, mode, len(raw)
            tf.addfile(ti, io.BytesIO(raw) if kind == tarfile.REGTYPE else None)
    return result.getvalue()


def good():
    return [("./" + p, tarfile.REGTYPE, b"test", 0o644) for p in (M.PLUGIN, M.ARTIFACT, M.FLAVOR)]


class CandidateRender(unittest.TestCase):
    def test_select_only_modules_and_identity(self):
        rows = good() + [("./usr/sbin/should-not-run", tarfile.REGTYPE, b"bad", 0o755)]
        self.assertEqual(set(M.select_payload(archive(rows))), {M.PLUGIN, M.ARTIFACT, M.FLAVOR})

    def test_traversal_absolute_duplicate_and_link_rejected(self):
        for name, kind in [("./../escape", tarfile.REGTYPE), ("/escape", tarfile.REGTYPE),
                           ("./usr//a", tarfile.REGTYPE), ("./link", tarfile.SYMTYPE),
                           ("./hard", tarfile.LNKTYPE), ("./device", tarfile.CHRTYPE),
                           ("./" + M.PLUGIN, tarfile.REGTYPE)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                M.select_payload(archive(good() + [(name, kind, b"", 0o644)]))

    def test_writable_or_setuid_archive_rejected(self):
        for mode in (0o666, 0o4777, 0o2755):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                M.select_payload(archive(good() + [("./unsafe", tarfile.REGTYPE, b"", mode)]))

    def test_missing_candidate_module_rejected(self):
        with self.assertRaises(ValueError):
            M.select_payload(archive(good()[1:]))

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "Linux root inode test")
    def test_reads_refuse_symlink_and_writable_inode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input"
            path.write_bytes(b"pinned")
            path.chmod(0o600)
            self.assertEqual(M.read_regular(path), b"pinned")
            link = Path(tmp) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                M.read_regular(link)
            path.chmod(0o666)
            with self.assertRaises(ValueError):
                M.read_regular(path)

    @unittest.skipUnless(Path("/usr/bin/perl").exists(), "Linux Perl startup")
    def test_startup_refuses_missing_controls_and_perl_injection(self):
        helper = PATH.with_suffix(".pl")
        for env in ({}, {"PERL_HASH_SEED": "1", "PERL_PERTURB_KEYS": "0"},
                    {**M.ENV, "PERL5LIB": "/tmp"}, {**M.ENV, "PERL5OPT": ""}):
            with self.subTest(env=env):
                r = subprocess.run(["/usr/bin/perl", "-T", str(helper)], env=env, capture_output=True)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn(b"deterministic fresh interpreter", r.stderr)

    def test_create_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "once"
            M.create(path, b"first")
            with self.assertRaises(FileExistsError):
                M.create(path, b"second")
            self.assertEqual(path.read_bytes(), b"first")

    def test_runtime_never_invokes_cluster_writer(self):
        text = PATH.with_suffix(".pl").read_text()
        self.assertNotIn("PVE::Storage::write_config", text)
        self.assertEqual(text.count("my $cfg = PVE::Storage::config();"), 1)
        self.assertNotIn("cfs_write_file", text)
        self.assertNotIn("write_file('/etc/pve", text)
        self.assertLess(text.index("require PVE::Storage;"), text.index("PVE::Storage::config()"))

    @unittest.skipUnless(Path("/usr/bin/perl").exists() and hasattr(os, "geteuid") and os.geteuid() == 0,
                         "Linux root Perl fixture")
    def test_helper_rejects_warning_semantic_registry_and_reference_drift(self):
        fixture = r'''
package PVE::Storage::Custom::SharedLvmThinPlugin;
package PVE::Storage::Plugin;
our $scenario = 'SCENARIO'; our $writes = 0; our $changed = 0;
sub lookup_types { return ['sharedlvmthin'] }
sub lookup { return $changed ? 'Foreign' : 'PVE::Storage::Custom::SharedLvmThinPlugin' }
sub parse_config {
    my ($class, $file, $raw) = @_;
    my %ids = map { $_ => { type => 'sharedlvmthin' } }
        qw(slt-scale-thick slt-scale-thin slt-tg-thick slt-tg-thin);
    if ($raw eq 'target') { $_->{'slt-vg-layout'} = 'mixed' for values %ids }
    $ids{'slt-tg-thick'}->{foreign} = 1 if $scenario eq 'semantic' && $raw eq 'target';
    return {ids => \%ids, digest => 'a' x 40};
}
sub write_config {
    $writes++;
    warn 'parser warning' if $scenario eq 'warning';
    $changed = 1 if $scenario eq 'registry';
    if ($scenario eq 'reference') {
        no warnings 'redefine';
        *lookup_types = sub { return ['sharedlvmthin'] };
    }
    $INC{'Injected.pm'} = __FILE__ if $scenario eq 'modules';
    return $scenario eq 'unstable' && $writes > 1 ? 'different' : 'target';
}
1;
'''
        for scenario in ('valid', 'warning', 'semantic', 'registry', 'reference', 'modules', 'unstable'):
            with self.subTest(scenario=scenario):
                root = Path('/run') / ('slt-candidate-render-' + uuid.uuid4().hex)
                root.mkdir(mode=0o700)
                try:
                    lib = root / 'payload/usr/share/perl5/PVE'
                    (lib / 'Storage/Custom').mkdir(parents=True, mode=0o700)
                    (lib / 'Storage/Custom/SharedLvmThinPlugin.pm').write_text(fixture.replace('SCENARIO', scenario))
                    (lib / 'Storage.pm').write_text(r'''package PVE::Storage;
require PVE::Storage::Custom::SharedLvmThinPlugin;
sub config {
    open my $fh, '<', '/etc/pve/storage.cfg' or die $!;
    local $/; my $raw = <$fh>; close $fh;
    return PVE::Storage::Plugin->parse_config('storage.cfg', $raw);
}
1;
''')
                    (root / 'baseline.cfg').write_bytes(Path('/etc/pve/storage.cfg').read_bytes())
                    result = subprocess.run(['/usr/bin/perl', '-T', str(PATH.with_suffix('.pl')), str(root)],
                                            env=M.ENV, capture_output=True)
                    if scenario == 'valid':
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(json.loads(result.stdout)['authorization'], 'NONE')
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(b'REFUSED', result.stderr)
                finally:
                    self.assertEqual(root.parent, Path('/run'))
                    self.assertTrue(root.name.startswith('slt-candidate-render-'))
                    shutil.rmtree(root)


if __name__ == "__main__":
    unittest.main()
