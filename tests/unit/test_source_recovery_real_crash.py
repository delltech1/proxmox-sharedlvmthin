"""Real temp-file source-recovery crash prefixes; no host package changes.

The policy flock, JSON create/fsync and unlink/fsync are real. Package/hold
and dpkg-audit probes are synthetic. This tests process death, not power loss,
and does not acquire the host dpkg locks or prove cluster/storage fencing.
"""
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest


RUNTIME = Path(__file__).resolve().parents[2] / "usr/libexec/pve-sharedlvmthin"
POLICY = RUNTIME / "sharedlvmthin-update-policy"
TXID = "a" * 32


DRIVER = r'''
import importlib.machinery,importlib.util,json,os,pathlib,signal,sys,types
loader=importlib.machinery.SourceFileLoader('policy',sys.argv[1])
spec=importlib.util.spec_from_loader(loader.name,loader)
m=importlib.util.module_from_spec(spec); loader.exec_module(m)
root=pathlib.Path(sys.argv[2]); cut=sys.argv[3]
m.STATE=root; m.LOCK=root/'policy.lock'; m.FREEZE=root/'freeze-owned.json'
m.POST_GATE=root/'post-gate-required.json'; m.FREEZE_MIGRATION=root/'freeze-schema-migration.json'
m.RUNTIME_QUALIFICATION=root/'runtime-qualification-pending.json'
m.RUNTIME_RELEASE=root/'runtime-release.json'; m.PACKAGE_TRANSITION=root/'package-transition.json'
m.PACKAGE_RECOVERY_ARCHIVE=root/'package-transition-recovery'
manifest={'watched_packages':['pve-sharedlvmthin'],'watched_package_patterns':[]}
row={'package':'pve-sharedlvmthin','binary':'pve-sharedlvmthin','version':'1',
     'architecture':'all','status':'installed'}
m.installed_packages=lambda: ['pve-sharedlvmthin']
m.package_state_records=lambda names: [dict(row)] if 'pve-sharedlvmthin' in names else []
m.held_packages=lambda: {'pve-sharedlvmthin'}
m.package_version=lambda name: '1' if name == 'pve-sharedlvmthin' else None
def run(argv,**kwargs):
 if argv != ['dpkg','--audit']: raise AssertionError('unexpected package probe')
 return types.SimpleNamespace(returncode=0,stdout='',stderr='')
m.run=run
create=m.create_json; unlink=m.unlink_durable
def kill(): os.kill(os.getpid(),signal.SIGKILL)
def create_cut(path,value):
 create(path,value)
 if cut=='archive' and path.parent==m.PACKAGE_RECOVERY_ARCHIVE: kill()
def unlink_cut(path):
 unlink(path)
 if (cut=='release' and path==m.RUNTIME_RELEASE) or (cut=='transition' and path==m.PACKAGE_TRANSITION): kill()
m.create_json=create_cut; m.unlink_durable=unlink_cut
with m.policy_lock():
 result=m.recover_prepared_freeze_source('a'*32,manifest,m.load_library())
print(json.dumps(result,sort_keys=True))
'''


@unittest.skipUnless(sys.platform.startswith("linux") and os.geteuid() == 0,
                     "Linux root durable state-file contract")
class SourceRecoveryRealCrashTests(unittest.TestCase):
    def test_each_durable_prefix_survives_process_kill_and_replays(self):
        loader = importlib.machinery.SourceFileLoader("real_source_policy", str(POLICY))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        policy = importlib.util.module_from_spec(spec)
        loader.exec_module(policy)
        core = policy.load_library()
        manifest = {"watched_packages": ["pve-sharedlvmthin"], "watched_package_patterns": []}
        row = {"package": "pve-sharedlvmthin", "binary": "pve-sharedlvmthin",
               "version": "1", "architecture": "all", "status": "installed"}
        freeze = {"schema": 2, "phase": "ACTIVE", "generation": "b" * 32,
                  "manifest_sha256": core.digest(manifest), "targets": ["pve-sharedlvmthin"],
                  "baseline": [row], "owned": ["pve-sharedlvmthin"], "preexisting": []}
        receipt = {"schema": 2, "state": "PREPARED", "txid": TXID,
                   "hostname": os.uname().nodename,
                   "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                   "parent_generation": freeze["generation"], "parent_ledger_sha256": core.digest(freeze),
                   "source_package": "pve-sharedlvmthin", "source_version": "1",
                   "target_package": "pve-sharedlvmthin", "target_version": "2",
                   "target_architecture": "all", "candidate_sha256": "c" * 64,
                   "candidate_artifact_sha256": "d" * 64, "candidate_manifest_sha256": "e" * 64}
        release = {"schema": 1, "qualified": "QUALIFIED", "fixture": "old release"}
        for cut in ("archive", "release", "transition"):
            with self.subTest(cut=cut), tempfile.TemporaryDirectory(prefix="slt-source-crash-") as directory:
                root = Path(directory)
                policy.create_json(root / "freeze-owned.json", freeze)
                policy.create_json(root / "package-transition.json", receipt)
                policy.create_json(root / "runtime-release.json", release)
                def run(phase):
                    return subprocess.run([sys.executable, "-B", "-c", DRIVER, str(POLICY), str(root), phase],
                                          env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
                                          capture_output=True, text=True, timeout=20)
                killed = run(cut)
                self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr)
                archive = root / "package-transition-recovery" / (TXID + ".json")
                self.assertTrue(archive.is_file())
                archived = policy.read_json(archive)
                self.assertEqual(archived["runtime_release_sha256"], core.digest(release))
                self.assertEqual((root / "runtime-release.json").exists(), cut == "archive")
                self.assertEqual((root / "package-transition.json").exists(), cut != "transition")
                self.assertFalse((root / "runtime-qualification-pending.json").exists())
                result = run("retry")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), archived)
                self.assertFalse((root / "runtime-release.json").exists())
                self.assertFalse((root / "package-transition.json").exists())
                self.assertEqual(policy.read_json(archive), archived)
                before = (archive.read_bytes(), archive.stat().st_mtime_ns)
                repeated = run("retry")
                self.assertEqual(repeated.returncode, 0, repeated.stderr)
                self.assertEqual(json.loads(repeated.stdout), archived)
                self.assertEqual((archive.read_bytes(), archive.stat().st_mtime_ns), before)


if __name__ == "__main__":
    unittest.main()
