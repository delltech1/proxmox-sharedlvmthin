import fcntl
import importlib.util
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/local-hold-release-file-lab.py"
SPEC = importlib.util.spec_from_file_location("local_hold_release_lab", PATH)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class Crash(Exception):
    pass


class LocalHoldReleaseFileLabTests(unittest.TestCase):
    def fixture(self):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "maintenance"
        root.mkdir(mode=0o700)
        for name in ("archive", "release-attempts", "release-certificates"):
            (root / name).mkdir(mode=0o700)
        manifest = b'{"phase":"CONFIG_COMMITTED","schema":1}\n'
        certificate = b'{"phase":"RELEASE_COMMITTED","schema":1}\n'
        active = root / "active.json"
        active.write_bytes(manifest)
        active.chmod(0o600)
        tx = "a" * 32
        generation = 7
        decision = root / "release-certificates" / f"{tx}-{generation}.json"
        decision.write_bytes(certificate)
        decision.chmod(0o600)
        info = active.stat()
        request = {
            "tx": tx, "generation": generation, "commit_id": "b" * 32,
            "node": "pve01",
            "boot_id": "11111111-1111-4111-8111-111111111111",
            "authorization_sha256": "1" * 64,
            "all_certified_plan_sha256": "2" * 64,
            "certificate_sha256": LAB.digest(certificate),
            "manifest_sha256": LAB.digest(manifest),
            "valid_until": time.time() + 60,
            "source_identity": LAB._identity(info, manifest),
            "certificate_identity": LAB._identity(decision.stat(), certificate),
        }
        return temp, root, LAB.LocalHoldReleaseLab(root), request, manifest, certificate

    def test_exact_release_archives_once_and_never_claims_cluster(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        result = store.release(request, manifest, certificate)
        self.assertEqual(result["classification"], "LOCAL_HOLD_ARCHIVED")
        self.assertFalse(result["cluster_released"])
        self.assertFalse((root / "active.json").exists())
        archive = root / "archive" / (
            f"{request['tx']}-{request['generation']}-{request['commit_id']}.json")
        self.assertEqual(archive.read_bytes(), manifest)
        self.assertEqual(archive.stat().st_ino,
                         request["source_identity"]["ino"])
        receipt = root / "release-attempts" / (
            f"{request['tx']}-{request['generation']}-receipt.json")
        self.assertTrue(receipt.is_file())
        replay = store.release(request, manifest, certificate)
        self.assertEqual(replay, result)

    def test_expiry_before_effect_performs_zero_rename(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        request["valid_until"] = time.time() - 1
        with self.assertRaisesRegex(LAB.Refusal, "expired before attempt"):
            store.release(request, manifest, certificate)
        self.assertTrue((root / "active.json").exists())
        self.assertEqual(list((root / "archive").iterdir()), [])

    def test_lock_replacement_before_rename_refuses(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        def replace_lock(point):
            if point == "before_rename":
                (root / ".maintenance.lock").unlink()
                replacement = root / ".maintenance.lock"
                replacement.write_bytes(b"")
                replacement.chmod(0o600)
        with self.assertRaisesRegex(LAB.Refusal, "lock namespace"):
            store.release(request, manifest, certificate, fault=replace_lock)
        self.assertEqual((root / "active.json").read_bytes(), manifest)
        self.assertEqual(list((root / "archive").iterdir()), [])

    def test_every_crash_boundary_replays_safely(self):
        points = [
            "after_attempt_file_fsync", "after_attempt_link",
            "after_attempt_directory_fsync", "before_rename", "after_rename",
            "after_active_directory_fsync", "after_archive_directory_fsync",
            "before_receipt", "after_receipt_file_fsync",
            "after_receipt_link", "after_receipt_directory_fsync",
        ]
        post_effect = set(points[4:])
        for point in points:
            with self.subTest(point=point):
                temp, root, store, request, manifest, certificate = self.fixture()
                try:
                    def fail(current):
                        if current == point:
                            raise Crash(point)
                    with self.assertRaisesRegex(Crash, point):
                        store.release(request, manifest, certificate, fault=fail)
                    if point in post_effect:
                        self.assertFalse((root / "active.json").exists())
                        request["valid_until"] = time.time() - 1
                    else:
                        self.assertTrue((root / "active.json").exists())
                    result = store.release(request, manifest, certificate)
                    self.assertEqual(result["classification"],
                                     "LOCAL_HOLD_ARCHIVED")
                    self.assertFalse(result["cluster_released"])
                finally:
                    temp.cleanup()

    def test_rename_stored_then_error_is_reconciled_without_retry(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        real = LAB._rename_noreplace
        calls = 0
        def stored_then_error(*args):
            nonlocal calls
            calls += 1
            real(*args)
            raise OSError("injected ambiguous rename")
        with mock.patch.object(LAB, "_rename_noreplace",
                               side_effect=stored_then_error):
            result = store.release(request, manifest, certificate)
        self.assertEqual(calls, 1)
        self.assertEqual(result["rename_outcome"],
                         "EXACT_ARCHIVE_CONFIRMED")
        self.assertFalse(result["cluster_released"])

    def test_lock_wait_is_bounded_and_expiry_wins(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        lock = root / ".maintenance.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(LAB.Refusal, "timed out"):
            store.release(request, manifest, certificate,
                          lock_timeout_sec=0.05)
        request["valid_until"] = time.time() - 1
        fcntl.flock(fd, fcntl.LOCK_UN)
        with self.assertRaisesRegex(LAB.Refusal, "expired"):
            store.release(request, manifest, certificate)
        self.assertTrue((root / "active.json").exists())

    def test_foreign_archive_new_inode_and_unsafe_objects_refuse(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        archive = root / "archive" / (
            f"{request['tx']}-{request['generation']}-{request['commit_id']}.json")
        archive.write_bytes(b"foreign\n")
        archive.chmod(0o600)
        with self.assertRaisesRegex(LAB.Refusal, "conflicting or unknown"):
            store.release(request, manifest, certificate)
        archive.unlink()
        active = root / "active.json"
        active.unlink()
        active.write_bytes(manifest)
        active.chmod(0o600)
        with self.assertRaisesRegex(LAB.Refusal, "inode differs"):
            store.release(request, manifest, certificate)
        active.unlink()
        os.mkfifo(active, 0o600)
        started = time.monotonic()
        with self.assertRaisesRegex(LAB.Refusal, "inode is unsafe"):
            store.release(request, manifest, certificate)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_two_link_attempt_crash_is_reconciled(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        attempts = root / "release-attempts"
        txgen = f"{request['tx']}-{request['generation']}.json"
        # Normal exception cleanup removes the model's temp; manufacture the
        # exact hard-link crash shape from a durable attempt instead.
        with self.assertRaises(Crash):
            store.release(request, manifest, certificate,
                          fault=lambda point: (_ for _ in ()).throw(Crash())
                          if point == "before_rename" else None)
        os.link(attempts / txgen, attempts / f".{txgen}.tmp")
        self.assertEqual((attempts / txgen).stat().st_nlink, 2)
        result = store.release(request, manifest, certificate)
        self.assertEqual(result["classification"], "LOCAL_HOLD_ARCHIVED")

    def test_certificate_inode_replacement_and_resurrected_hold_refuse(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        decision = root / "release-certificates" / (
            f"{request['tx']}-{request['generation']}.json")
        decision.unlink()
        decision.write_bytes(certificate)
        decision.chmod(0o600)
        with self.assertRaisesRegex(LAB.Refusal, "certificate bytes or inode"):
            store.release(request, manifest, certificate)
        temp.cleanup()
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        store.release(request, manifest, certificate)
        archive = root / "archive" / (
            f"{request['tx']}-{request['generation']}-{request['commit_id']}.json")
        os.rename(archive, root / "active.json")
        with self.assertRaisesRegex(LAB.Refusal,
                                    "receipt exists while active"):
            store.release(request, manifest, certificate)

    def test_staged_attempt_is_fsynced_before_replay_publication(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        real_fsync = LAB.os.fsync
        failed = False

        def fail_first_regular(fd):
            nonlocal failed
            if not failed and os.path.isfile(f"/proc/self/fd/{fd}"):
                failed = True
                raise OSError("injected file fsync failure")
            return real_fsync(fd)

        with mock.patch.object(LAB.os, "fsync", side_effect=fail_first_regular):
            with self.assertRaisesRegex(OSError, "injected file fsync"):
                store.release(request, manifest, certificate)
        txgen = f"{request['tx']}-{request['generation']}.json"
        staged = root / "release-attempts" / f".{txgen}.tmp"
        self.assertTrue(staged.is_file())
        staged_inode = staged.stat().st_ino
        synced = False
        renamed = LAB._rename_noreplace

        def observe_fsync(fd):
            nonlocal synced
            if os.fstat(fd).st_ino == staged_inode:
                synced = True
            return real_fsync(fd)

        def require_synced(*args):
            self.assertTrue(synced)
            return renamed(*args)

        with mock.patch.object(LAB.os, "fsync", side_effect=observe_fsync), \
                mock.patch.object(LAB, "_rename_noreplace",
                                  side_effect=require_synced):
            store.release(request, manifest, certificate)

    def test_child_namespace_replacement_refuses_before_rename(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)

        def replace_archive(point):
            if point == "before_rename":
                os.rename(root / "archive", root / "archive-old")
                (root / "archive").mkdir(mode=0o700)

        with self.assertRaisesRegex(LAB.Refusal,
                                    "release child directory changed"):
            store.release(request, manifest, certificate, fault=replace_archive)
        self.assertTrue((root / "active.json").exists())
        self.assertEqual(list((root / "archive-old").iterdir()), [])
        self.assertEqual(list((root / "archive").iterdir()), [])

    def test_expiry_during_final_hook_performs_zero_rename(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        request["valid_until"] = time.time() + 0.03

        def expire(point):
            if point == "before_rename":
                time.sleep(0.05)

        with self.assertRaisesRegex(LAB.Refusal,
                                    "expired immediately before rename"):
            store.release(request, manifest, certificate, fault=expire)
        self.assertTrue((root / "active.json").exists())
        self.assertEqual(list((root / "archive").iterdir()), [])

    def test_two_link_receipt_is_reconciled_without_second_rename(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        expected = store.release(request, manifest, certificate)
        attempts = root / "release-attempts"
        receipt_name = (f"{request['tx']}-{request['generation']}"
                        "-receipt.json")
        os.link(attempts / receipt_name, attempts / f".{receipt_name}.tmp")
        self.assertEqual((attempts / receipt_name).stat().st_nlink, 2)
        with mock.patch.object(
                LAB, "_rename_noreplace",
                side_effect=AssertionError("must not rename twice")):
            replay = store.release(request, manifest, certificate)
        self.assertEqual(replay, expected)
        self.assertFalse((attempts / f".{receipt_name}.tmp").exists())
        self.assertEqual((attempts / receipt_name).stat().st_nlink, 1)

    def test_prelink_attempt_replay_preserves_original_execution_identity(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(Crash):
            store.release(
                request, manifest, certificate,
                fault=lambda point: (_ for _ in ()).throw(Crash())
                if point == "after_attempt_file_fsync" else None)
        with mock.patch.object(LAB, "_process_starttime",
                               side_effect=AssertionError("must preserve staged")), \
                mock.patch.object(LAB.os, "getpid", return_value=999999):
            result = store.release(request, manifest, certificate)
        self.assertEqual(result["classification"], "LOCAL_HOLD_ARCHIVED")

    def test_prelink_receipt_replay_preserves_original_finished_at(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(Crash):
            store.release(
                request, manifest, certificate,
                fault=lambda point: (_ for _ in ()).throw(Crash())
                if point == "after_receipt_file_fsync" else None)
        attempts = root / "release-attempts"
        receipt_name = (f"{request['tx']}-{request['generation']}"
                        "-receipt.json")
        staged = attempts / f".{receipt_name}.tmp"
        original = LAB._decode(staged.read_bytes())
        later = max(time.time() + 2, original["finished_at"] + 2)
        with mock.patch.object(LAB.time, "time", return_value=later), \
                mock.patch.object(
                    LAB, "_rename_noreplace",
                    side_effect=AssertionError("must not rename twice")):
            result = store.release(request, manifest, certificate)
        self.assertEqual(result["finished_at"], original["finished_at"])
        self.assertFalse(staged.exists())

    def test_active_replacement_at_last_hook_performs_zero_rename(self):
        temp, root, store, request, manifest, certificate = self.fixture()
        self.addCleanup(temp.cleanup)

        def replace_active(point):
            if point == "before_rename":
                os.rename(root / "active.json", root / "displaced.json")
                (root / "active.json").write_bytes(manifest)
                (root / "active.json").chmod(0o600)

        with self.assertRaisesRegex(
                LAB.Refusal, "active manifest changed immediately before rename"):
            store.release(request, manifest, certificate, fault=replace_active)
        self.assertTrue((root / "active.json").exists())
        self.assertEqual(list((root / "archive").iterdir()), [])

    def test_receipt_callback_drift_cannot_return_success(self):
        for drift in ("active", "archive"):
            with self.subTest(drift=drift):
                temp, root, store, request, manifest, certificate = self.fixture()
                try:
                    archive_name = (f"{request['tx']}-{request['generation']}-"
                                    f"{request['commit_id']}.json")

                    def mutate(point):
                        if point != "after_receipt_file_fsync":
                            return
                        if drift == "active":
                            (root / "active.json").write_bytes(manifest)
                            (root / "active.json").chmod(0o600)
                        else:
                            archive = root / "archive" / archive_name
                            archive.unlink()
                            archive.write_bytes(manifest)
                            archive.chmod(0o600)

                    with self.assertRaisesRegex(
                            LAB.Refusal, "archived hold changed before receipt"):
                        store.release(request, manifest, certificate, fault=mutate)
                finally:
                    temp.cleanup()


if __name__ == "__main__":
    unittest.main()
