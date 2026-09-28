import importlib
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock

EXP = Path(__file__).resolve().parents[2] / "experiments/thick-generations"
sys.path.insert(0, str(EXP))
LAB = importlib.import_module("prelive_exact_journal_qualification")


class ExactJournalQualificationTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory(prefix="slt-qual-unit-")
        patcher = mock.patch.object(LAB.FILES, "PARENT", temp.name)
        patcher.start()
        backend = LAB.FILES.create_fresh_file_backend("9" * 32)
        expected = [b"one", b"two", b"three"]
        for index, raw in enumerate(expected, 1):
            backend.persist_exact_file(f"exact-event-{index:06d}.json", raw)
        root = backend.artifact_path(); persisted = backend.qualification_evidence()
        backend.close_preserving_artifacts()
        return temp, patcher, root, expected, persisted

    def test_scope_has_no_process_storage_or_cleanup_surface(self):
        source = (EXP / "prelive_exact_journal_qualification.py").read_text()
        for forbidden in ("os.fork", "import subprocess", "dmsetup", " lvm",
                          "pvesm",
                          "unlink", "rmdir", "remove(", "kill("):
            self.assertNotIn(forbidden, source)
        self.assertIn("power_loss_durability_proven\": False", source)
        self.assertIn("artifacts_preserved\": True", source)

    def test_event_builder_produces_exact_closed_chain(self):
        owner = {"pid": 10, "starttime": 20,
                 "boot_id": "12345678-1234-1234-1234-123456789abc"}
        executable = {"path": "/usr/bin/true", "sha256": "a" * 64,
                      "dev": 1, "inode": 2}
        events = LAB._events(owner, executable)
        self.assertEqual(len(events), 3)
        self.assertTrue(all(type(value) is bytes for value in events))

    def test_independent_verifier_binds_original_root(self):
        temp, patcher, root, expected, persisted = self.fixture()
        try:
            displaced = root + ".old"; os.rename(root, displaced)
            shutil.copytree(displaced, root)
            with self.assertRaises(LAB.JOURNAL.Refusal):
                LAB._independent_verify(root, expected, persisted)
        finally:
            patcher.stop(); temp.cleanup()

    def test_fifo_record_refuses_without_blocking(self):
        temp, patcher, root, expected, persisted = self.fixture()
        try:
            record = Path(root) / "exact-event-000001.json"
            record.unlink(); os.mkfifo(record, 0o600)
            with self.assertRaises(LAB.JOURNAL.Refusal):
                LAB._independent_verify(root, expected, persisted)
        finally:
            patcher.stop(); temp.cleanup()

    def test_record_replacement_after_read_is_detected(self):
        temp, patcher, root, expected, persisted = self.fixture()
        try:
            record = Path(root) / "exact-event-000001.json"
            real_read = LAB.os.read; fired = []
            def read(fd, size):
                data = real_read(fd, size)
                if data and not fired:
                    fired.append(True); record.unlink(); record.write_bytes(expected[0])
                    record.chmod(0o600)
                return data
            with mock.patch.object(LAB.os, "read", side_effect=read):
                with self.assertRaises(LAB.JOURNAL.Refusal):
                    LAB._independent_verify(root, expected, persisted)
        finally:
            patcher.stop(); temp.cleanup()

    def test_mode_drift_during_read_is_detected(self):
        temp, patcher, root, expected, persisted = self.fixture()
        try:
            record = Path(root) / "exact-event-000001.json"
            real_read = LAB.os.read; fired = []
            def read(fd, size):
                data = real_read(fd, size)
                if data and not fired:
                    fired.append(True); record.chmod(0o644)
                return data
            with mock.patch.object(LAB.os, "read", side_effect=read):
                with self.assertRaises(LAB.JOURNAL.Refusal):
                    LAB._independent_verify(root, expected, persisted)
        finally:
            patcher.stop(); temp.cleanup()

    def test_root_mode_drift_during_read_is_detected(self):
        temp, patcher, root, expected, persisted = self.fixture()
        try:
            real_read = LAB.os.read; fired = []
            def read(fd, size):
                data = real_read(fd, size)
                if data and not fired:
                    fired.append(True); Path(root).chmod(0o755)
                return data
            with mock.patch.object(LAB.os, "read", side_effect=read):
                with self.assertRaises(LAB.JOURNAL.Refusal):
                    LAB._independent_verify(root, expected, persisted)
        finally:
            patcher.stop(); temp.cleanup()


if __name__ == "__main__": unittest.main()
