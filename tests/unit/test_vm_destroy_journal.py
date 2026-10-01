import importlib.machinery
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-vm-destroy"
loader = importlib.machinery.SourceFileLoader("destroy_journal", str(HELPER))
spec = importlib.util.spec_from_loader(loader.name, loader)
mod = importlib.util.module_from_spec(spec)
loader.exec_module(mod)


class DestroyJournalModelTests(unittest.TestCase):
    def test_canonical_digest_stable(self):
        self.assertEqual(mod.digest({"a": 1, "b": 2}), mod.digest({"b": 2, "a": 1}))

    def test_nonfinite_values_refused(self):
        for value in (float("nan"), float("inf")):
            with self.assertRaises(mod.Refusal):
                mod.canonical({"value": value})

    def test_duplicate_json_refused(self):
        with self.assertRaises(mod.Refusal):
            mod.strict_json(b'{"stage":"PREPARED","stage":"COMPLETE"}')

    def test_transition_matrix(self):
        for current in (None, *mod.STAGES, mod.UNKNOWN):
            for target in (*mod.STAGES, mod.UNKNOWN, "OTHER"):
                allowed = ((current is None and target == "PREPARED") or
                           (current in mod.STAGES[:-1] and
                            target in (mod.UNKNOWN, mod.STAGES[mod.STAGES.index(current) + 1])))
                with self.subTest(current=current, target=target):
                    if allowed:
                        mod.next_stage(current, target)
                    else:
                        with self.assertRaises(mod.Refusal):
                            mod.next_stage(current, target)

    def test_no_pve_or_storage_executor(self):
        source = HELPER.read_text(encoding="utf-8")
        for token in ("import subprocess", "os.system(", "os.exec", "pvesh ", "qm destroy"):
            self.assertNotIn(token, source)
        self.assertIn('choices=["observe"]', source)


@unittest.skipUnless(os.name == "posix", "requires real POSIX dirfd/flock/fsync semantics")
class DestroyJournalFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "request"
        self.root.mkdir(mode=0o700)
        self.journal = mod.Journal(self.root, expected_uid=os.geteuid())
        self.context = {"node": "lab-a", "boot_id": "old-boot", "vmid": 123,
                        "config_sha256": "a" * 64, "storage_cfg_sha256": "b" * 64}
        self.last = None

    def append(self, stage, **kwargs):
        arguments = dict(request_id="a" * 32, context=self.context,
                         evidence={"test": True}, expected_previous=self.last)
        arguments.update(kwargs)
        result = self.journal.append(stage, **arguments)
        self.last = result
        return result

    def test_full_lifecycle_and_read_only_observe(self):
        self.assertEqual(self.journal.observe()["stage"], "EMPTY")
        for stage in mod.STAGES:
            self.append(stage)
            before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.root.iterdir()}
            observed = self.journal.observe()
            self.assertEqual(observed["stage"], stage)
            self.assertEqual(observed["last_sha256"], self.last)
            self.assertEqual(observed["authority"], "NONE")
            self.assertEqual(before, {p.name: (p.read_bytes(), p.stat().st_mtime_ns)
                                      for p in self.root.iterdir()})

    def test_replay_skip_and_stale_cas_do_not_write(self):
        self.append("PREPARED")
        for stage, extra in (("PREPARED", {}), ("COMPLETE", {}),
                             ("DISPATCHED", {"expected_previous": "0" * 64})):
            with self.assertRaises(mod.Refusal):
                self.append(stage, **extra)
        self.assertEqual(len(list(self.root.iterdir())), 1)

    def test_context_and_request_identity_pinned(self):
        self.append("PREPARED")
        for extra in ({"request_id": "b" * 32}, {"context": {**self.context, "boot_id": "new-boot"}}):
            with self.assertRaises(mod.Refusal):
                self.append("DISPATCHED", **extra)

    def test_unknown_is_terminal(self):
        self.append("PREPARED")
        self.append(mod.UNKNOWN)
        with self.assertRaises(mod.Refusal):
            self.append("DISPATCHED")

    def test_complete_is_terminal(self):
        for stage in mod.STAGES:
            self.append(stage)
        with self.assertRaises(mod.Refusal):
            self.append(mod.UNKNOWN)

    def test_symlink_record_refused(self):
        outside = Path(self.temp.name) / "outside"
        outside.write_text("{}")
        (self.root / "000001-PREPARED.json").symlink_to(outside)
        with self.assertRaises((mod.Refusal, OSError)):
            self.journal.observe()

    def test_symlink_directory_refused(self):
        link = Path(self.temp.name) / "link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises((mod.Refusal, OSError)):
            mod.Journal(link, expected_uid=os.geteuid()).observe()

    def test_hardlink_record_refused(self):
        self.append("PREPARED")
        os.link(next(self.root.iterdir()), Path(self.temp.name) / "alias")
        with self.assertRaises(mod.Refusal):
            self.journal.observe()

    def test_bad_directory_mode_refused(self):
        self.root.chmod(0o755)
        with self.assertRaises(mod.Refusal):
            self.journal.observe()

    def test_bad_record_mode_refused(self):
        self.append("PREPARED")
        next(self.root.iterdir()).chmod(0o644)
        with self.assertRaises(mod.Refusal):
            self.journal.observe()

    def test_torn_create_remains_blocker(self):
        def fault(point):
            if point == "after_create":
                raise RuntimeError("crash")
        self.journal.fault = fault
        with self.assertRaises(RuntimeError):
            self.append("PREPARED")
        self.journal.fault = lambda _: None
        self.assertEqual(len(list(self.root.iterdir())), 1)
        with self.assertRaises(mod.Refusal):
            self.append("PREPARED")

    def test_durable_publication_crash_is_observable_not_replayed(self):
        for point in ("after_file_fsync", "after_directory_fsync"):
            with self.subTest(point=point):
                other = self.root / point
                other.mkdir(mode=0o700)
                journal = mod.Journal(other, expected_uid=os.geteuid(),
                                      fault=lambda at: (_ for _ in ()).throw(RuntimeError("crash"))
                                      if at == point else None)
                args = dict(request_id="a" * 32, context=self.context, evidence={}, expected_previous=None)
                with self.assertRaises(RuntimeError):
                    journal.append("PREPARED", **args)
                self.assertEqual(journal.observe()["stage"], "PREPARED")
                with self.assertRaises(mod.Refusal):
                    journal.append("PREPARED", **args)

    def test_concurrent_lock_refuses_immediately(self):
        import fcntl
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(mod.Refusal):
                self.append("PREPARED")
        finally:
            os.close(fd)

    def test_hash_chain_tamper_refused(self):
        self.append("PREPARED")
        self.append("DISPATCHED")
        path = self.root / "000002-DISPATCHED.json"
        row = mod.strict_json(path.read_bytes())
        row["previous_sha256"] = "0" * 64
        path.write_bytes(mod.canonical(row) + b"\n")
        with self.assertRaises(mod.Refusal):
            self.journal.observe()


if __name__ == "__main__":
    unittest.main()
