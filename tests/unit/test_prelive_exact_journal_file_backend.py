import copy
import importlib
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock


EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
LAB = importlib.import_module("prelive_exact_journal_file_backend")


class ExactFileBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="slt-journal-unit-")
        self.backends = []
        self.parent_patch = mock.patch.object(LAB, "PARENT", self.temp.name)
        self.parent_patch.start()

    def tearDown(self):
        for backend in self.backends:
            if backend.root_fd is not None or backend.parent_fd is not None:
                try: backend.release_descriptors_preserving_state()
                except BaseException: pass
        self.parent_patch.stop(); self.temp.cleanup()

    def backend(self, nonce="a" * 32):
        backend = LAB.create_fresh_file_backend(nonce)
        self.backends.append(backend)
        return backend

    def test_exact_bytes_identity_and_preserved_close(self):
        backend = self.backend(); raw = b'{"exact":true}\n'
        ack = backend.persist_exact_file("exact-event-000001.json", raw)
        path = Path(backend.artifact_path()) / "exact-event-000001.json"
        self.assertEqual(path.read_bytes(), raw)
        self.assertEqual(ack["bytes_written"], len(raw))
        self.assertEqual(ack["mode"], 0o600) if "mode" in ack else None
        self.assertEqual(ack["record_identity"]["mode"], 0o600)
        backend.close_preserving_artifacts()
        self.assertTrue(path.exists())

    def test_duplicate_record_and_root_refuse_without_overwrite(self):
        backend = self.backend(); name = "exact-event-000001.json"
        backend.persist_exact_file(name, b"first")
        with self.assertRaises(LAB.Refusal): backend.persist_exact_file(name, b"second")
        self.assertEqual((Path(backend.artifact_path()) / name).read_bytes(), b"first")
        with self.assertRaises((FileExistsError, LAB.Refusal)):
            self.backend()

    def test_bad_names_types_and_bounds_refuse_before_creation(self):
        backend = self.backend()
        for name, raw in (("../x", b"x"), ("event-1", b"x"),
                          ("exact-event-000001.json", b""),
                          ("exact-event-000001.json", "x")):
            with self.assertRaises(LAB.Refusal): backend.persist_exact_file(name, raw)
            if backend.state == "UNKNOWN": break

    def test_short_writes_are_completed(self):
        backend = self.backend(); real_write = LAB.os.write
        with mock.patch.object(LAB.os, "write",
                               side_effect=lambda fd, data: real_write(fd, data[:2])):
            ack = backend.persist_exact_file("exact-event-000001.json", b"abcdef")
        self.assertEqual(ack["bytes_written"], 6)

    def test_zero_write_poisons_without_retry(self):
        backend = self.backend()
        with mock.patch.object(LAB.os, "write", return_value=0):
            with self.assertRaises(LAB.Refusal):
                backend.persist_exact_file("exact-event-000001.json", b"abc")
        self.assertEqual(backend.state, "UNKNOWN")

    def test_oversized_write_report_poisons(self):
        backend = self.backend()
        with mock.patch.object(LAB.os, "write", return_value=4):
            with self.assertRaises(LAB.Refusal):
                backend.persist_exact_file("exact-event-000001.json", b"abc")
        self.assertEqual(backend.state, "UNKNOWN")

    def test_file_or_directory_fsync_failure_poisons(self):
        for fail_call in (1, 2):
            backend = self.backend(("%032x" % fail_call)); calls = []
            real = LAB.os.fsync
            def fsync(fd):
                calls.append(fd)
                if len(calls) == fail_call: raise OSError("fsync")
                return real(fd)
            with mock.patch.object(LAB.os, "fsync", side_effect=fsync):
                with self.assertRaises((LAB.Refusal, OSError)):
                    backend.persist_exact_file("exact-event-000001.json", b"abc")
            self.assertEqual(backend.state, "UNKNOWN")

    def test_close_after_persist_failure_is_ambiguous(self):
        backend = self.backend(); real_close = LAB.os.close; fired = []
        def close(fd):
            if not fired:
                fired.append(True); real_close(fd); raise OSError("close")
            return real_close(fd)
        with mock.patch.object(LAB.os, "close", side_effect=close):
            with self.assertRaises(LAB.Refusal):
                backend.persist_exact_file("exact-event-000001.json", b"abc")
        self.assertEqual(backend.state, "UNKNOWN")

    def test_unknown_backend_can_release_descriptors_without_becoming_clean(self):
        backend = self.backend()
        with mock.patch.object(LAB.os, "write", return_value=0):
            with self.assertRaises(LAB.Refusal):
                backend.persist_exact_file("exact-event-000001.json", b"abc")
        evidence = backend.release_descriptors_preserving_state()
        self.assertEqual(evidence["prior_state"], "UNKNOWN")
        self.assertEqual(evidence["final_state"], "UNKNOWN")
        self.assertIsNone(backend.root_fd); self.assertIsNone(backend.parent_fd)

    def test_unknown_release_reentry_is_bounded_one_attempt_per_fd(self):
        backend = self.backend()
        with mock.patch.object(LAB.os, "write", return_value=0):
            with self.assertRaises(LAB.Refusal):
                backend.persist_exact_file("exact-event-000001.json", b"abc")
        original = (backend.root_fd, backend.parent_fd); real_close = LAB.os.close
        attempts = []
        def close(fd):
            attempts.append(fd)
            try: backend.release_descriptors_preserving_state()
            except LAB.Refusal: pass
            return real_close(fd)
        with mock.patch.object(LAB.os, "close", side_effect=close):
            evidence = backend.release_descriptors_preserving_state()
        self.assertEqual(attempts, list(original))
        self.assertEqual(evidence["final_state"], "UNKNOWN")

    def test_swallowed_reentry_during_directory_close_stays_unknown(self):
        for trigger in (1, 2):
            backend = self.backend(("%032x" % (100 + trigger)))
            real_close = LAB.os.close; calls = []
            def close(fd):
                calls.append(fd)
                if len(calls) == trigger:
                    try:
                        backend.persist_exact_file("exact-event-000001.json", b"x")
                    except LAB.Refusal:
                        pass
                return real_close(fd)
            with mock.patch.object(LAB.os, "close", side_effect=close):
                with self.assertRaises(LAB.Refusal): backend.close_preserving_artifacts()
            self.assertEqual(backend.state, "UNKNOWN")

    def test_record_replacement_during_close_is_unknown(self):
        for replacement in ("regular", "symlink", "hardlink"):
            nonce = {"regular": "b", "symlink": "c", "hardlink": "d"}[replacement] * 32
            backend = self.backend(nonce); real_close = LAB.os.close; fired = []
            root = Path(backend.artifact_path()); record = root / "exact-event-000001.json"
            other = root / "other"; other.write_bytes(b"other")
            def close(fd):
                if fd not in (backend.root_fd, backend.parent_fd) and not fired:
                    fired.append(True); real_close(fd); record.unlink()
                    if replacement == "regular": record.write_bytes(b"abc")
                    elif replacement == "symlink": record.symlink_to("other")
                    else: os.link(other, record)
                    return None
                return real_close(fd)
            with mock.patch.object(LAB.os, "close", side_effect=close):
                with self.assertRaises(LAB.Refusal):
                    backend.persist_exact_file(record.name, b"abc")
            self.assertEqual(backend.state, "UNKNOWN")

    def test_root_mode_drift_is_detected_before_record_open(self):
        backend = self.backend(); root = Path(backend.artifact_path())
        root.chmod(0o755)
        with self.assertRaises(LAB.Refusal):
            backend.persist_exact_file("exact-event-000001.json", b"abc")
        self.assertEqual(backend.state, "UNKNOWN")
        self.assertFalse((root / "exact-event-000001.json").exists())

    def test_root_name_replacement_is_detected(self):
        backend = self.backend(); root = Path(backend.artifact_path())
        displaced = root.with_name(root.name + ".displaced")
        root.rename(displaced); root.mkdir(mode=0o700)
        with self.assertRaises(LAB.Refusal):
            backend.persist_exact_file("exact-event-000001.json", b"abc")
        self.assertEqual(backend.state, "UNKNOWN")

    def test_parent_named_identity_replacement_is_detected(self):
        backend = self.backend(); real_stat = LAB.os.stat
        def drift(path, *args, **kwargs):
            value = real_stat(path, *args, **kwargs)
            if path == LAB.PARENT and "dir_fd" not in kwargs:
                values = {name: getattr(value, name) for name in dir(value)
                          if name.startswith("st_") and not callable(getattr(value, name))}
                values["st_ino"] = value.st_ino + 1
                return SimpleNamespace(**values)
            return value
        with mock.patch.object(LAB.os, "stat", side_effect=drift):
            with self.assertRaises(LAB.Refusal):
                backend.persist_exact_file("exact-event-000001.json", b"abc")
        self.assertEqual(backend.state, "UNKNOWN")

    def test_foreign_thread_poisons_without_file(self):
        backend = self.backend(); errors = []
        def foreign():
            try: backend.persist_exact_file("exact-event-000001.json", b"abc")
            except BaseException as exc: errors.append(exc)
        worker = threading.Thread(target=foreign); worker.start(); worker.join()
        self.assertEqual(len(errors), 1); self.assertEqual(backend.state, "UNKNOWN")
        self.assertFalse((Path(backend.artifact_path()) /
                          "exact-event-000001.json").exists())

    def test_noncopyable_and_no_delete_reopen_surface(self):
        backend = self.backend()
        with self.assertRaises(LAB.Refusal): copy.copy(backend)
        with self.assertRaises(LAB.Refusal): copy.deepcopy(backend)
        source = (EXP / "prelive_exact_journal_file_backend.py").read_text()
        for forbidden in ("unlink", "rmdir", "remove(", "rename(", "replace("):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
