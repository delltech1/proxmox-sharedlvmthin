import importlib.machinery
import importlib.util
import io
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
                allowed = ((current == target == "FINALIZING") or
                           (current is None and target == "PREPARED") or
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
        self.assertIn('choices=["observe", "_append", "_observe-durable"]', source)
        wrapper = (ROOT / "usr/sbin/sharedlvmthin").read_text(encoding="utf-8")
        self.assertNotIn('"_append"', wrapper)
        self.assertNotIn('"_observe-durable"', wrapper)

    def test_private_input_exact_canonical_schema(self):
        valid = payload()
        self.assertEqual(mod.read_append_payload(io.BytesIO(mod.canonical(valid) + b"\n"), "a" * 32), valid)
        for raw in (b'', mod.canonical(valid), b' ' + mod.canonical(valid) + b'\n',
                    b'{"stage":"PREPARED","stage":"COMPLETE"}\n', b'x' * (mod.LIMIT + 1)):
            with self.subTest(raw=raw[:80]), self.assertRaises(mod.Refusal):
                mod.read_append_payload(io.BytesIO(raw), "a" * 32)
        for change in ({"authority": "MUTATE"}, {"unexpected": 1}, {"schema": "other"},
                       {"request_id": "b" * 32}, {"context": []}, {"evidence": None},
                       {"expected_previous": "0" * 64}, {"stage": "DISPATCHED"},
                       {"expected_previous": True}, {"stage": "UNKNOWN"}):
            with self.subTest(change=change), self.assertRaises(mod.Refusal):
                mod.read_append_payload(io.BytesIO(mod.canonical({**valid, **change}) + b"\n"), "a" * 32)


def payload(stage="PREPARED", previous=None):
    return {"schema": mod.APPEND_SCHEMA, "authority": "NONE", "request_id": "a" * 32,
            "stage": stage, "expected_previous": previous,
            "context": {"immutable": "exact"}, "evidence": {"stage": stage}}


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

    def test_context_types_are_exact_not_python_equality(self):
        self.append("PREPARED", context={"value": 1})
        for value in (True, 1.0):
            with self.assertRaisesRegex(mod.Refusal, "context changed"):
                self.append("DISPATCHED", context={"value": value})
        self.assertEqual(len(list(self.root.iterdir())), 1)

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

    def test_finalizing_self_transition_bounded_and_context_pinned(self):
        for stage in mod.STAGES[:-1]:
            self.append(stage)
        with self.assertRaises(mod.Refusal):
            self.append("FINALIZING", context={"different": True})
        with self.assertRaises(mod.Refusal):
            self.append("FINALIZING", expected_previous="f" * 64)
        for number in range(4, mod.MAX_RECORDS - 1):
            self.append("FINALIZING", evidence={"effect": number})
        self.append("COMPLETE")
        self.assertEqual(len(self.journal.observe()["records"]), mod.MAX_RECORDS)
        with self.assertRaisesRegex(mod.Refusal, "budget exhausted"):
            self.append("FINALIZING")

    def test_durable_observation_settles_fsync_prefix_without_new_record(self):
        self.append("PREPARED")
        before = {p.name: p.read_bytes() for p in self.root.iterdir()}
        observed = self.journal.observe(durable=True)
        self.assertIs(observed["durable"], True)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.root.iterdir()})
        self.assertNotIn("durable", self.journal.observe())


@unittest.skipUnless(os.name == "posix", "requires real POSIX dirfd/flock/fsync semantics")
class DestroyJournalPrivateAppendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name) / "base"
        self.base.mkdir(mode=0o700)
        self.root = self.base / ("a" * 32)

    def append(self, value=None, **kwargs):
        return mod.append_request(value or payload(), base=self.base,
                                  expected_uid=os.geteuid(), **kwargs)

    def test_create_then_exact_cas_and_acknowledgement(self):
        first = self.append()
        self.assertEqual(first["request_sha256"], mod.digest(payload()))
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        second = self.append(payload("DISPATCHED", first["last_sha256"]))
        observed = mod.Journal(self.root, expected_uid=os.geteuid()).observe()
        self.assertEqual(observed["last_sha256"], second["last_sha256"])
        with self.assertRaises(mod.Refusal):
            self.append(payload("STORAGE_ABSENT", first["last_sha256"]))
        self.assertEqual(len(list(self.root.iterdir())), 2)

    def test_existing_empty_directory_never_adopted(self):
        self.root.mkdir(mode=0o700)
        with self.assertRaises(FileExistsError):
            self.append()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_mkdir_crash_prefixes_remain_recoverable_not_replayed(self):
        for point in ("after_request_mkdir", "after_request_parent_fsync"):
            with self.subTest(point=point):
                private_base = self.base / point
                private_base.mkdir(mode=0o700)
                def fault(at):
                    if at == point:
                        raise RuntimeError("crash")
                with self.assertRaises(RuntimeError):
                    mod.append_request(payload(), base=private_base, expected_uid=os.geteuid(), fault=fault)
                request = private_base / ("a" * 32)
                self.assertTrue(request.is_dir())
                self.assertEqual(list(request.iterdir()), [])
                with self.assertRaises(FileExistsError):
                    mod.append_request(payload(), base=private_base, expected_uid=os.geteuid())

    def test_durable_lost_ack_cannot_repeat_prepared(self):
        def fault(at):
            if at == "after_directory_fsync":
                raise RuntimeError("lost ack")
        with self.assertRaises(RuntimeError):
            self.append(fault=fault)
        self.assertEqual(mod.Journal(self.root, expected_uid=os.geteuid()).observe()["stage"], "PREPARED")
        with self.assertRaises(FileExistsError):
            self.append()

    def test_invalid_payload_has_no_namespace_effect(self):
        with self.assertRaises(mod.Refusal):
            self.append({**payload(), "authority": "MUTATE"})
        self.assertEqual(list(self.base.iterdir()), [])

    def test_symlink_or_unsafe_existing_request_refused(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir(mode=0o700)
        self.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaises((mod.Refusal, OSError)):
            self.append(payload("DISPATCHED", "a" * 64))
        self.assertEqual(list(outside.iterdir()), [])

    def test_unsafe_base_refused_before_mkdir(self):
        self.base.chmod(0o777)
        with self.assertRaises(mod.Refusal):
            self.append()
        self.assertFalse(self.root.exists())

    def test_namespace_replacement_after_mkdir_refused(self):
        moved = self.base.with_name("moved")
        def fault(at):
            if at == "after_request_mkdir":
                self.base.rename(moved)
                self.base.mkdir(mode=0o700)
        with self.assertRaisesRegex(mod.Refusal, "namespace changed"):
            self.append(fault=fault)
        self.assertEqual(list(self.base.iterdir()), [])
        self.assertEqual(list((moved / ("a" * 32)).iterdir()), [])

    def test_two_process_create_only_race_has_one_winner(self):
        # Real process/flock competition; neither loser retries or adopts.
        start_read, start_write = os.pipe()
        result_read, result_write = os.pipe()
        children = []
        for _ in range(2):
            pid = os.fork()
            if pid == 0:
                os.close(start_write)
                os.close(result_read)
                os.read(start_read, 1)
                try:
                    self.append()
                    outcome = b"Y"
                except (mod.Refusal, OSError):
                    outcome = b"N"
                os.write(result_write, outcome)
                os._exit(0)
            children.append(pid)
        os.close(start_read)
        os.close(result_write)
        os.write(start_write, b"xx")
        os.close(start_write)
        result = b""
        while len(result) < 2:
            chunk = os.read(result_read, 2 - len(result))
            if not chunk:
                break
            result += chunk
        os.close(result_read)
        for pid in children:
            self.assertEqual(os.waitpid(pid, 0)[1], 0)
        self.assertEqual(sorted(result), sorted(b"YN"))
        self.assertEqual(len(list(self.root.iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
