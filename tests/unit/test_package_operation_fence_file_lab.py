import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/package-operation-fence-file-lab.py"
SPEC = importlib.util.spec_from_file_location("package_operation_fence_lab", PATH)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


class Crash(Exception):
    pass


class PackageOperationFenceFileLabTests(unittest.TestCase):
    def fixture(self, maintenance=True):
        temp = tempfile.TemporaryDirectory()
        root = Path(temp.name) / "maintenance"
        root.mkdir(mode=0o700)
        (root / "package-fences").mkdir(mode=0o700)
        request = {
            "attempt_id": "a" * 32,
            "node": "node-a",
            "boot_id": "11111111-1111-4111-8111-111111111111",
            "package": "pve-sharedlvmthin",
            "flavor": "dual",
            "version": "0.9.0~test1",
            "artifact_sha256": "b" * 64,
        }
        manifest = {
            "schema": "slt-package-maintenance/v1", "tx": "c" * 32,
            "generation": 7, "phase": "PREPARE_READY",
        }
        if maintenance:
            active = root / "active.json"
            active.write_bytes(LAB.canonical(manifest))
            active.chmod(0o600)
        return temp, root, LAB.PackageOperationFenceLab(root), request, manifest

    def test_fence_survives_process_boundary_and_blocks_release(self):
        temp, _root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        self.assertEqual(store.admit(request)["classification"],
                         "PACKAGE_IN_PROGRESS")
        successor = LAB.PackageOperationFenceLab(store.root)
        with self.assertRaisesRegex(LAB.Refusal, "release refused"):
            successor.release_admission()

    def test_durable_fence_crash_replays_exactly(self):
        temp, _root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(Crash):
            store.admit(request, fault=lambda point: (_ for _ in ()).throw(
                Crash(point)) if point == "after_fence_durable" else None)
        result = store.admit(request)
        self.assertEqual(result["publication"], "VERIFIED_REPLAY")
        self.assertFalse(result["release_authorized"])

    def test_staged_durable_fence_also_blocks_release(self):
        temp, _root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(Crash):
            store.admit(request, fault=lambda point: (_ for _ in ()).throw(
                Crash(point)) if point == "after_fence_file_fsync" else None)
        with self.assertRaisesRegex(LAB.Refusal, "release refused"):
            store.release_admission()
        self.assertEqual(store.admit(request)["publication"], "CREATED")

    def test_stored_then_error_is_unknown_but_second_attempt_conflicts(self):
        temp, _root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(Crash):
            store.admit(request, fault=lambda point: (_ for _ in ()).throw(
                Crash(point)) if point == "after_fence_link" else None)
        other = dict(request)
        other["attempt_id"] = "d" * 32
        with self.assertRaisesRegex(LAB.Refusal, "conflicts"):
            store.admit(other)

    def test_missing_or_different_fence_refuses_postinst(self):
        temp, root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        with self.assertRaisesRegex(LAB.Refusal, "required record is absent"):
            store.record_configured(request)
        store.admit(request)
        other = dict(request)
        other["artifact_sha256"] = "e" * 64
        with self.assertRaisesRegex(LAB.Refusal, "predecessor differs"):
            store.record_configured(other)
        (root / "active.json").unlink()
        with self.assertRaisesRegex(LAB.Refusal, "transition is not exact"):
            store.record_configured(request)

    def test_only_exact_prepare_to_config_transition_is_accepted(self):
        temp, _root, store, request, manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        store.cooperating_transition(manifest["tx"], manifest["generation"])
        result = store.record_configured(request)
        self.assertEqual(result["classification"],
                         "CONFIG_RECORDED_RETAINED")
        self.assertFalse(result["release_authorized"])
        with self.assertRaisesRegex(LAB.Refusal, "release refused"):
            store.release_admission()

    def test_transition_and_config_crash_reconcile_without_release(self):
        temp, _root, store, request, manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        with self.assertRaises(Crash):
            store.cooperating_transition(
                manifest["tx"], manifest["generation"],
                fault=lambda point: (_ for _ in ()).throw(Crash(point))
                if point == "after_active_transition_directory_fsync" else None)
        replay = store.cooperating_transition(manifest["tx"],
                                              manifest["generation"])
        self.assertEqual(replay["publication"], "VERIFIED_REPLAY")
        with self.assertRaises(Crash):
            store.record_configured(
                request, fault=lambda point: (_ for _ in ()).throw(Crash(point))
                if point == "after_configured_directory_fsync" else None)
        with self.assertRaisesRegex(LAB.Refusal, "release refused"):
            store.release_admission()
        self.assertEqual(store.record_configured(request)["publication"],
                         "VERIFIED_REPLAY")

    def test_transition_replay_retries_durability_not_replace(self):
        temp, root, store, request, manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        real = store._durable_active
        calls = 0

        def fail_once(root_fd):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected active directory fsync failure")
            return real(root_fd)

        with mock.patch.object(store, "_durable_active", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "fsync failure"):
                store.cooperating_transition(manifest["tx"],
                                             manifest["generation"])
            self.assertEqual(store.cooperating_transition(
                manifest["tx"], manifest["generation"])["publication"],
                "VERIFIED_REPLAY")
        self.assertEqual(calls, 2)
        self.assertEqual(LAB.decode((root / "active.json").read_bytes())["phase"],
                         "CONFIG_COMMITTED")

        with mock.patch.object(store, "_durable_active",
                               side_effect=OSError("still not durable")):
            with self.assertRaisesRegex(OSError, "still not durable"):
                store.cooperating_transition(manifest["tx"],
                                             manifest["generation"])

    def test_transition_never_overwrites_foreign_hold_after_staging(self):
        temp, root, store, request, manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        foreign = dict(manifest)
        foreign["tx"] = "f" * 32

        def replace(point):
            if point == "after_transition_directory_fsync":
                staged = root / ".foreign-active"
                staged.write_bytes(LAB.canonical(foreign))
                staged.chmod(0o600)
                os.replace(staged, root / "active.json")

        with self.assertRaisesRegex(LAB.Refusal,
                                    "source changed before transition effect"):
            store.cooperating_transition(manifest["tx"],
                                         manifest["generation"], fault=replace)
        self.assertEqual((root / "active.json").read_bytes(),
                         LAB.canonical(foreign))

    def test_configured_result_refuses_late_hold_injection(self):
        temp, root, store, request, manifest = self.fixture(maintenance=False)
        self.addCleanup(temp.cleanup)
        store.admit(request)

        def inject(point):
            if point == "after_configured_directory_fsync":
                active = root / "active.json"
                active.write_bytes(LAB.canonical(manifest))
                active.chmod(0o600)

        with self.assertRaisesRegex(LAB.Refusal,
                                    "hold changed while recording"):
            store.record_configured(request, fault=inject)
        with self.assertRaisesRegex(LAB.Refusal, "release refused"):
            store.release_admission()

    def test_lock_inode_replacement_refuses_success(self):
        temp, root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)

        def replace(point):
            if point == "after_fence_directory_fsync":
                lock = root / ".maintenance.lock"
                lock.unlink()
                lock.write_bytes(b"")
                lock.chmod(0o600)

        with self.assertRaisesRegex(LAB.Refusal,
                                    "lock namespace changed"):
            store.admit(request, fault=replace)

    def test_wrong_transition_identity_and_generation_refuse(self):
        temp, _root, store, request, manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        with self.assertRaisesRegex(LAB.Refusal, "identity differs"):
            store.cooperating_transition("f" * 32, manifest["generation"])
        with self.assertRaisesRegex(LAB.Refusal, "identity differs"):
            store.cooperating_transition(manifest["tx"], 8)

    def test_ordinary_attempt_blocks_cooperating_hold_creation(self):
        temp, _root, store, request, manifest = self.fixture(maintenance=False)
        self.addCleanup(temp.cleanup)
        store.admit(request)
        with self.assertRaisesRegex(LAB.Refusal,
                                    "cannot create a hold"):
            store.cooperating_transition(manifest["tx"], manifest["generation"])
        result = store.record_configured(request)
        self.assertEqual(result["classification"],
                         "CONFIG_RECORDED_RETAINED")

    def test_two_package_attempts_and_profile_switch_serialize(self):
        temp, _root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        thick = dict(request)
        thick.update(attempt_id="9" * 32,
                     package="pve-sharedlvmthin-thick", flavor="thick-only")
        with self.assertRaisesRegex(LAB.Refusal, "conflicts"):
            store.admit(thick)

    def test_reboot_or_pid_absence_does_not_clear_fence(self):
        temp, _root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        store.admit(request)
        rebooted = dict(request)
        rebooted["boot_id"] = "22222222-2222-4222-8222-222222222222"
        with self.assertRaisesRegex(LAB.Refusal, "predecessor differs"):
            store.record_configured(rebooted)
        with self.assertRaisesRegex(LAB.Refusal, "release refused"):
            store.release_admission()

    def test_unsafe_fence_is_never_absent(self):
        temp, root, store, request, _manifest = self.fixture()
        self.addCleanup(temp.cleanup)
        fence = root / "package-fences" / LAB.FENCE_NAME
        os.mkfifo(fence, 0o600)
        with self.assertRaisesRegex(LAB.Refusal, "inode is unsafe"):
            store.admit(request)


if __name__ == "__main__":
    unittest.main()
