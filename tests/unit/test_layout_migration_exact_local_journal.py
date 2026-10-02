import copy
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from test_layout_migration_exact_restore_model import fixture, M, Backend

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("exact_local_files", ROOT / "experiments/thick-generations/layout-migration-exact-local-journal.py")
J = importlib.util.module_from_spec(SPEC)
with mock.patch.dict(sys.modules, {} if os.name == "posix" else {"fcntl": types.ModuleType("fcntl")}):
    SPEC.loader.exec_module(J)


@unittest.skipUnless(os.name == "posix", "descriptor/fsync/flock tests require POSIX")
class FilesystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "journal"
        self.plan = fixture(); self.tx = self.plan["tx"]
        self.journal = J.Journal.for_test(self.plan, self.root, create=True)
        self.addCleanup(self.journal.close)

    def open(self):
        value = J.Journal.for_test(self.plan, self.root)
        self.addCleanup(value.close); return value

    def path(self, key): return self.root / self.tx / self.journal._name(self.tx, key)

    def test_create_read_reconcile_and_no_effect_surface(self):
        ack = self.journal.create_once(self.tx, "baseline", {"exact": True})
        self.assertEqual(ack, {"durable": True, "sha256": J.digest({"exact": True})})
        fresh = self.open()
        self.assertEqual(fresh.read(self.tx, "baseline"), {"exact": True})
        result = fresh.reconcile(self.tx, "baseline", {"exact": True})
        self.assertFalse(result["effect_retry_allowed"])
        self.assertEqual(result["authority"], "NONE")
        with self.assertRaises(J.Refusal): fresh.reconcile(self.tx, "baseline", {"exact": False})
        with self.assertRaises(FileExistsError): fresh.create_once(self.tx, "baseline", {"exact": True})
        self.assertFalse(hasattr(fresh, "dispatch_typed"))
        self.assertFalse(hasattr(fresh, "verify_release"))

    def test_direct_journal_accepts_complete_bounded_protocol_without_subclass(self):
        effect = 'pve01:qmeventd.service:UNMASK'
        keys = (effect + ':grant', 'flow:started', 'flow:held', 'flow:release', 'flow:complete',
                'barrier:11:OBSERVED_COMPLETE', 'archive:pve03:done', 'receipt:' + effect,
                'transport:' + effect + ':intent', 'transport:' + effect + ':done')
        for key in keys:
            value = {'authority': 'NONE', 'key': key}
            self.journal.create_once(self.tx, key, value)
            self.assertEqual(self.open().read(self.tx, key), value)
        with self.assertRaises(FileExistsError): self.journal.create_once(self.tx, keys[0], {})

    def test_every_record_publication_fault_is_retained_and_not_retried(self):
        for point in ("file-created", "file-written", "file-fsync", "directory-fsync", "parent-fsync", "root-parent-record-fsync", "read-file-fsync", "before-ack"):
            with self.subTest(point=point):
                root = Path(self.tmp.name) / point
                journal = J.Journal.for_test(self.plan, root, create=True)
                def fail(event):
                    if event == point: raise OSError("injected publication fault")
                # read-file-fsync during identity validation precedes the
                # effect attempt; cover the actual record readback separately.
                calls = [0]
                def fault(event):
                    if event == "read-file-fsync":
                        calls[0] += 1
                        if point == event and calls[0] == 1: return
                    fail(event)
                journal.fault = fault
                with self.assertRaises(OSError): journal.create_once(self.tx, "baseline", {"value": 1})
                journal.fault = None
                with self.assertRaises(J.Refusal): journal.create_once(self.tx, "baseline", {"value": 1})
                journal.close()
                with J.Journal.for_test(self.plan, root) as fresh:
                    if point == "file-created":
                        with self.assertRaises(J.Refusal): fresh.read(self.tx, "baseline")
                    else:
                        self.assertEqual(fresh.reconcile(self.tx, "baseline", {"value": 1})["state"], "DURABLE_RECORD_OBSERVED")

    def test_symlink_hardlink_fifo_and_mode_tamper_refuse(self):
        path = self.path("baseline")
        for kind in ("symlink", "hardlink", "fifo", "mode"):
            with self.subTest(kind=kind):
                if kind == "symlink": path.symlink_to("/dev/null")
                elif kind == "fifo": os.mkfifo(path, 0o600)
                else:
                    path.write_bytes(b"{}\n"); path.chmod(0o600)
                    if kind == "hardlink": os.link(path, Path(self.tmp.name) / "second-link")
                    else: path.chmod(0o644)
                with self.assertRaises((J.Refusal, OSError)): self.journal.read(self.tx, "baseline")
                path.unlink()
                if kind == "hardlink": (Path(self.tmp.name) / "second-link").unlink()

    def test_partial_noncanonical_duplicate_and_wrong_identity_records_refuse(self):
        path = self.path("baseline")
        for raw in (b"{", b'{"x":1,"x":2}\n', b'{ "x":1}\n', b'{}\n'):
            path.write_bytes(raw); path.chmod(0o600)
            with self.assertRaises((J.Refusal, ValueError)): self.journal.read(self.tx, "baseline")
            path.unlink()

    def test_lock_and_directory_replacement_or_mode_drift_refuse(self):
        lock = self.root / self.tx / ".journal.lock"
        lock.rename(self.root / self.tx / ("f" * 64 + ".json"))
        lock.write_bytes(b""); lock.chmod(0o600)
        with self.assertRaisesRegex(J.Refusal, "lock namespace"): self.journal.read(self.tx, "baseline")

    def test_directory_replacement_is_detected_on_pinned_handle(self):
        moved = Path(self.tmp.name) / "moved"
        self.root.rename(moved); self.root.mkdir(mode=0o700)
        with self.assertRaisesRegex(J.Refusal, "directory namespace"): self.journal.read(self.tx, "baseline")

    def test_competing_writer_lock_refuses_without_record(self):
        other = self.open()
        J.fcntl.flock(self.journal.lock, J.fcntl.LOCK_EX | J.fcntl.LOCK_NB)
        try:
            with self.assertRaises(BlockingIOError): other.create_once(self.tx, "baseline", {})
        finally: J.fcntl.flock(self.journal.lock, J.fcntl.LOCK_UN)
        self.assertFalse(self.path("baseline").exists())

    def test_tx_key_size_and_plan_swaps_cannot_escape(self):
        for key in ("../outside", "pve01:pve-guests.service:STOP:intent", "shell:rm -rf /"):
            with self.assertRaises(J.Refusal): self.journal.create_once(self.tx, key, {})
        with self.assertRaises(J.Refusal): self.journal.create_once("f" * 32, "baseline", {})
        with self.assertRaises(J.Refusal): self.journal.create_once(self.tx, "baseline", "x" * J.LIMIT)
        changed = copy.deepcopy(self.plan); changed["workload_sha256"] = "f" * 64
        with self.assertRaises(J.Refusal): J.Journal.for_test(changed, self.root)

    def test_fresh_enrollment_never_adopts_existing_transaction(self):
        with self.assertRaises(FileExistsError): J.Journal.for_test(self.plan, self.root, create=True)

    def test_symlink_root_and_directory_mode_drift_refuse(self):
        linked = Path(self.tmp.name) / "linked"
        linked.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError): J.Journal.for_test(self.plan, linked)
        self.root.chmod(0o755)
        with self.assertRaisesRegex(J.Refusal, "directory namespace"):
            self.journal.read(self.tx, "baseline")

    def test_real_file_backend_composes_with_model_and_historical_replay(self):
        backend = Backend(self.plan)
        backend.create_once = self.journal.create_once
        backend.read = self.journal.read
        M.Coordinator(self.plan, backend).baseline(); backend.held()
        self.assertEqual(M.Coordinator(self.plan, backend).restore()["authority"], "NONE")
        count = len(backend.effects)
        fresh = self.open(); backend.read = fresh.read; backend.create_once = fresh.create_once
        M.Coordinator(self.plan, backend).restore()
        self.assertEqual(len(backend.effects), count)


class PureTests(unittest.TestCase):
    def test_protocol_keys_are_bound_to_plan_cohort_san_role_and_fixed_services(self):
        journal = object.__new__(J.Journal); journal.plan = fixture(); journal.tx = journal.plan['tx']
        accepted = ('pve01:qmeventd.service:UNMASK:grant', 'receipt:pve04:pve-guests.service:UNMASK',
                    'flow:release', 'barrier:11:ATTEMPTED', 'archive:pve03:intent',
                    'transport:pve04:pvedaemon.service:START:done')
        for key in accepted: self.assertRegex(journal._name(journal.tx, key), r'^[a-f0-9]{64}\.json$')
        for key in ('PVE01:qmeventd.service:UNMASK:grant', 'archive:pve04:done', 'archive:pve05:done',
                    'barrier:12:ATTEMPTED', 'flow:resume', 'receipt:pve01:foreign.service:START',
                    'node-baseline:pve05', 'transport:pve01:pvedaemon.service:STOP:intent'):
            with self.subTest(key=key), self.assertRaises(J.Refusal): journal._name(journal.tx, key)

    def test_closed_decode_rejects_duplicate_nonfinite_and_noncanonical(self):
        for raw in (b'{"x":1,"x":2}\n', b'{"x":NaN}\n', b'{ "x":1}\n'):
            with self.assertRaises(J.Refusal): J.decode(raw)
        self.assertEqual(J.decode(b'{"authority":"NONE"}\n'), {"authority": "NONE"})


if __name__ == "__main__": unittest.main()
