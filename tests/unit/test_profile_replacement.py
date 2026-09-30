import importlib.machinery
import importlib.util
import os
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-profile-replacement"
VERSION = "0.9.0~rc5.31~tg52"


def load_module():
    loader = importlib.machinery.SourceFileLoader("slt_profile_replace_test", str(SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                     "profile replacement inode tests require POSIX root")
class ProfileReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.m = load_module()
        state = Path(self.temp.name) / "state"
        self.m.ROOT = state
        self.m.LOCK = state / ".lock"
        self.m.READY = state / "ready.json"
        self.m.INTENT = state / "prerm-intent.json"
        self.m.DONE = state / "dpkg-done.json"
        self.m.COMPLETE = state / "complete.json"
        self.m.ARCHIVE = state / "archive"

        def ensure_root():
            state.mkdir(mode=0o700, exist_ok=True)
            self.m.ARCHIVE.mkdir(mode=0o700, exist_ok=True)
            os.chmod(state, 0o700)
            os.chmod(self.m.ARCHIVE, 0o700)

        self.m.ensure_root = ensure_root
        self.m.read_boot_id = lambda: "11111111-1111-1111-1111-111111111111"
        self.m.candidate_field = lambda _path, field: {
            "Package": "pve-sharedlvmthin", "Version": VERSION,
            "Architecture": "all"}[field]
        self.real_exact_dpkg_process = self.m.exact_dpkg_process
        self.m.exact_dpkg_process = lambda _record: None
        self.candidate = Path(self.temp.name) / "candidate.deb"
        self.candidate.write_bytes(b"exact candidate\n")
        os.chmod(self.candidate, 0o400)
        self.digest = self.m.sha256(self.candidate)

    def args(self):
        return types.SimpleNamespace(
            candidate=str(self.candidate), sha256=self.digest,
            source_package="pve-sharedlvmthin-thick", source_version=VERSION,
            target_package="pve-sharedlvmthin", target_version=VERSION)

    def prepare(self):
        with self.m.Locked():
            return self.m.prepare(self.args(), os.getpid(), self.m.proc_start(os.getpid()))

    def consume(self):
        verify = types.SimpleNamespace(source_package="pve-sharedlvmthin-thick",
                                       source_version=VERSION, action="remove")
        with self.m.Locked():
            self.m.verify_prerm(verify)

    def fake_installed(self, target_status="ii "):
        original = self.m.subprocess.run
        self.addCleanup(setattr, self.m.subprocess, "run", original)

        def run(argv, **_kwargs):
            if argv[-1] == "pve-sharedlvmthin":
                return subprocess.CompletedProcess(argv, 0,
                                                   f"{target_status} {VERSION}", "")
            return subprocess.CompletedProcess(argv, 1, "", "not installed")
        self.m.subprocess.run = run

    def test_held_exact_target_is_installed_and_pending_removal_is_not(self):
        record = self.prepare()
        self.fake_installed("hi ")
        self.m.installed_exact(record)

        # The exact same verifier must not reinterpret a pending remove as a
        # successfully installed target merely because files still exist.
        self.fake_installed("ri ")
        with self.assertRaisesRegex(self.m.Refusal, "not exactly installed"):
            self.m.installed_exact(record)

    def write_done(self, rc=0):
        intent = self.m.read_record(self.m.INTENT)
        done = dict(intent)
        done.update(phase="DPKG_DONE", dpkg_completed=int(time.time()), dpkg_exit=rc)
        self.m.write_create(self.m.DONE, done)

    def test_prepared_record_is_one_shot_and_exactly_shaped(self):
        record = self.prepare()
        self.assertEqual(record["phase"], "READY")
        self.consume()
        self.assertFalse(self.m.READY.exists())
        self.assertEqual(self.m.read_record(self.m.INTENT)["phase"], "PRERM_INTENT")
        with self.assertRaises((self.m.Refusal, FileNotFoundError)):
            self.consume()

    def test_stale_ready_refuses_but_consumed_intent_does_not_expire(self):
        self.prepare()
        record = self.m.read_record(self.m.READY)
        record["created"] = int(time.time()) - self.m.MAX_AGE - 1
        self.m.READY.unlink()
        self.m.write_create(self.m.READY, record)
        with self.assertRaises(self.m.Refusal):
            self.consume()
        self.m.READY.unlink()
        record["created"] = int(time.time())
        self.m.write_create(self.m.READY, record)
        self.consume()
        intent = self.m.read_record(self.m.INTENT)
        intent["created"] = int(time.time()) - 1801
        self.m.INTENT.unlink()
        self.m.write_create(self.m.INTENT, intent)
        self.write_done()
        self.fake_installed()
        args = types.SimpleNamespace(txid=record["txid"],
                                     target_package="pve-sharedlvmthin",
                                     target_version=VERSION,
                                     candidate_sha256=self.digest, recovery=False)
        with self.m.Locked():
            self.m.finalize(args)
        self.assertTrue((self.m.ARCHIVE / f"{record['txid']}.json").exists())

    def test_complete_receipt_makes_cleanup_replayable(self):
        record = self.prepare()
        self.consume()
        self.write_done()
        done = self.m.read_record(self.m.DONE)
        complete = dict(done)
        complete.update(phase="COMPLETE", completed=int(time.time()),
                        settlement="DPKG_EXIT_ZERO")
        self.m.write_create(self.m.COMPLETE, complete)
        self.m.INTENT.unlink()
        self.m.DONE.unlink()
        self.fake_installed()
        args = types.SimpleNamespace(txid=record["txid"],
                                     target_package="pve-sharedlvmthin",
                                     target_version=VERSION,
                                     candidate_sha256=self.digest, recovery=True)
        with self.m.Locked():
            self.m.finalize(args)
        self.assertFalse((self.m.ROOT / "candidate.deb").exists())
        self.assertTrue((self.m.ARCHIVE / f"{record['txid']}.json").exists())
        with self.m.Locked():
            self.m.finalize(args)

    def test_boot_change_requires_explicit_recovery_settlement(self):
        record = self.prepare()
        self.consume()
        self.write_done()
        self.fake_installed()
        self.m.read_boot_id = lambda: "22222222-2222-2222-2222-222222222222"
        normal = types.SimpleNamespace(txid=record["txid"],
                                       target_package="pve-sharedlvmthin",
                                       target_version=VERSION,
                                       candidate_sha256=self.digest, recovery=False)
        with self.assertRaises(self.m.Refusal):
            with self.m.Locked():
                self.m.finalize(normal)
        recovery = types.SimpleNamespace(txid=record["txid"],
                                         target_package="pve-sharedlvmthin",
                                         target_version=VERSION,
                                         candidate_sha256=self.digest, recovery=True)
        with self.m.Locked():
            self.m.finalize(recovery)

    def test_wrong_txid_creates_no_complete_record(self):
        record = self.prepare()
        self.consume()
        self.write_done()
        self.fake_installed()
        args = types.SimpleNamespace(txid="f" * 32,
                                     target_package="pve-sharedlvmthin",
                                     target_version=VERSION,
                                     candidate_sha256=self.digest, recovery=False)
        with self.assertRaises(self.m.Refusal):
            with self.m.Locked():
                self.m.finalize(args)
        self.assertFalse(self.m.COMPLETE.exists())
        self.assertTrue(self.m.INTENT.exists())
        self.assertTrue(self.m.DONE.exists())
        self.assertNotEqual(record["txid"], args.txid)

    def test_recovery_can_settle_exact_target_without_done_after_executor_loss(self):
        record = self.prepare()
        self.consume()
        self.fake_installed()
        self.m.executor_is_alive = lambda _record: False
        args = types.SimpleNamespace(txid=record["txid"],
                                     target_package="pve-sharedlvmthin",
                                     target_version=VERSION,
                                     candidate_sha256=self.digest, recovery=True)
        with self.m.Locked():
            self.m.finalize(args)
        archived = self.m.read_record(self.m.ARCHIVE / f"{record['txid']}.json")
        self.assertEqual(archived["settlement"], "RECOVERED_EXACT_TARGET")
        self.assertNotIn("dpkg_exit", archived)

    def test_executor_unknown_is_not_misclassified_as_dead(self):
        record = self.prepare()
        original = self.m.proc_start
        self.addCleanup(setattr, self.m, "proc_start", original)
        self.m.proc_start = lambda _pid: (_ for _ in ()).throw(PermissionError(13, "denied"))
        with self.assertRaisesRegex(self.m.Refusal, "unobservable"):
            self.m.executor_is_alive(record)

    def test_executor_identity_proves_alive_reused_or_previous_boot(self):
        record = self.prepare()
        original = self.m.proc_start
        self.addCleanup(setattr, self.m, "proc_start", original)
        self.m.proc_start = lambda _pid: record["dpkg_start"]
        self.assertTrue(self.m.executor_is_alive(record))
        self.m.proc_start = lambda _pid: "different-start"
        self.assertFalse(self.m.executor_is_alive(record))

    def test_exact_dpkg_binding_refuses_sibling_executable_argv_and_reuse(self):
        record = self.prepare()
        self.m.ancestry_contains = lambda _pid, _start: False
        with self.assertRaisesRegex(self.m.Refusal, "not a live ancestor"):
            self.real_exact_dpkg_process(record)
        self.m.ancestry_contains = lambda _pid, _start: True
        self.m.proc_executable = lambda _pid: "/bin/sh"
        with self.assertRaisesRegex(self.m.Refusal, "not dpkg"):
            self.real_exact_dpkg_process(record)
        self.m.proc_executable = lambda _pid: "/usr/bin/dpkg"
        self.m.proc_cmdline = lambda _pid: [b"/usr/bin/dpkg", b"--remove",
                                             b"pve-sharedlvmthin"]
        with self.assertRaisesRegex(self.m.Refusal, "arguments changed"):
            self.real_exact_dpkg_process(record)
        self.m.proc_cmdline = lambda _pid: [b"/usr/bin/dpkg", b"--force-hold",
                                                b"-i", record["candidate"].encode()]
        self.real_exact_dpkg_process(record)
        record["boot_id"] = "22222222-2222-2222-2222-222222222222"
        self.m.proc_start = lambda _pid: (_ for _ in ()).throw(AssertionError("must not inspect old boot"))
        self.assertFalse(self.m.executor_is_alive(record))

    def test_prepare_failure_never_releases_dpkg_child(self):
        original = self.m.prepare
        self.addCleanup(setattr, self.m, "prepare", original)

        def fail_prepare(*_args):
            raise self.m.Refusal("injected prepare failure")

        self.m.prepare = fail_prepare
        with self.assertRaisesRegex(self.m.Refusal, "injected prepare failure"):
            self.m.execute(self.args())
        self.assertFalse(self.m.READY.exists())
        self.assertFalse(self.m.INTENT.exists())

    def test_live_executor_blocks_recovery_without_done(self):
        record = self.prepare()
        self.consume()
        self.fake_installed()
        self.m.executor_is_alive = lambda _record: True
        args = types.SimpleNamespace(txid=record["txid"],
                                     target_package="pve-sharedlvmthin",
                                     target_version=VERSION,
                                     candidate_sha256=self.digest, recovery=True)
        with self.assertRaisesRegex(self.m.Refusal, "still alive"):
            with self.m.Locked():
                self.m.finalize(args)
        self.assertFalse(self.m.COMPLETE.exists())
        self.assertTrue(self.m.INTENT.exists())

    def test_wrong_digest_and_cross_version_refuse_without_ready(self):
        args = self.args()
        args.sha256 = "0" * 64
        with self.assertRaises(self.m.Refusal):
            with self.m.Locked():
                self.m.prepare(args, os.getpid(), self.m.proc_start(os.getpid()))
        self.assertFalse(self.m.READY.exists())
        args = self.args()
        args.target_version = "0.9.0~rc5.30~tg51"
        with self.assertRaises(self.m.Refusal):
            with self.m.Locked():
                self.m.prepare(args, os.getpid(), self.m.proc_start(os.getpid()))


if __name__ == "__main__":
    unittest.main()
