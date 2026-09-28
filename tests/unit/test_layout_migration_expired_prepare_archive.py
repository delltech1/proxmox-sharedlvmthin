import copy
import fcntl
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M = load("expired_prepare_test", ROOT / "experiments/thick-generations/layout-migration-expired-prepare-archive.py")
H = load("expired_prepare_fixture", ROOT / "tests/unit/test_layout_migration_v2_v1_adapter.py")


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.root = self.parent / "maintenance"
        self.saved_fixed_root = M.FIXED_ROOT
        M.FIXED_ROOT = self.root
        self.addCleanup(setattr, M, "FIXED_ROOT", self.saved_fixed_root)
        self.cas = self.parent / "storage-config-cas"
        for path in (self.root, self.cas): path.mkdir(mode=0o700)
        for name in ("attempts", "sidecars", "receipts"): (self.root / name).mkdir(mode=0o700)
        self.locks = (self.parent / "dpkg-lock", self.parent / "dpkg-frontend-lock")
        for path in (*self.locks, self.root / ".maintenance.lock", self.cas / ".cas.lock"): self.write(path, b"")
        fixture = H.Tests().fixture()
        self.manifest = H.Tests().project(fixture)["manifest"]
        self.raw = M.canonical(self.manifest)
        self.write(self.root / "active.json", self.raw)
        self.now = self.manifest["expires_at"] + 1
        self.facts = {"node": "pve01", "boot_id": self.manifest["nodes"][0]["boot_id"],
            "cluster_name": self.manifest["cluster_name"], "corosync_conf_sha256": self.manifest["corosync_conf_sha256"],
            "storage_cfg_sha256": self.manifest["baseline_storage_cfg_sha256"], "blocking_processes": [],
            "payload_verified": True,
            "package": {**{key: self.manifest["candidate"][key] for key in ("package", "version", "flavor", "artifact_sha256")},
                        "config_version": "0.9.0~rc5.12~tg33", "dpkg_state": "unpacked"}}
        self.receipt = {"schema": "slt-package-maintenance-receipt/v1", "tx": self.manifest["tx"],
            "generation": self.manifest["generation"], "phase": "PREINST_ACCEPTED", "node": "pve01",
            "boot_id": self.facts["boot_id"], "manifest_sha256": M.digest(self.raw),
            "storage_cfg_sha256": self.facts["storage_cfg_sha256"], "recorded_at": self.manifest["issued_at"],
            **{key: self.manifest["candidate"][key] for key in ("package", "version", "flavor", "artifact_sha256")}}
        self.receipt_path = self.root / "receipts" / (self.manifest["tx"] + ".json")
        self.write(self.receipt_path, M.canonical(self.receipt) + b"\n")
        stem = f"{self.manifest['tx']}-{self.manifest['generation']}-pve01-create-prepare-" + "e" * 32
        self.write(self.root / "attempts" / (stem + "-intent.json"), b'{"preserved":"intent"}')
        self.write(self.root / "attempts" / (stem + "-complete.json"), b'{"preserved":"complete"}')
        self.write(self.root / "sidecars" / (stem + ".json"), b'{"preserved":"sidecar"}')
        self.request = {"schema": "slt-expired-prepare-archive/v1", "operation_id": "f" * 32,
            "node": self.facts["node"], "boot_id": self.facts["boot_id"], "manifest_sha256": M.digest(self.raw),
            "storage_cfg_sha256": self.facts["storage_cfg_sha256"], "expected_package": copy.deepcopy(self.facts["package"]),
            "receipt_sha256": {self.receipt_path.name: M.digest(self.receipt_path.read_bytes())},
            "evidence_tree_sha256": M.snapshot(self.root, os.geteuid())["tree_sha256"]}

    def write(self, path, raw):
        path.write_bytes(raw); path.chmod(0o600)

    def lab(self):
        return M.FileLab(self.root, self.cas, self.locks, uid=os.geteuid())

    def archive(self, **kwargs):
        return self.lab().archive(self.request, self.raw, lambda: copy.deepcopy(self.facts), clock=lambda: self.now, **kwargs)

    def refresh(self):
        self.request["evidence_tree_sha256"] = M.snapshot(self.root, os.geteuid())["tree_sha256"]

    def test_exact_unpacked_candidate_archives_whole_tree_once(self):
        before = M.snapshot(self.root, os.geteuid())
        output = self.archive()
        archived = Path(output["archive_path"])
        self.assertFalse(self.root.exists())
        self.assertEqual(M.snapshot(archived, os.geteuid()), before)
        self.assertEqual((archived / "active.json").read_bytes(), self.raw)
        self.assertEqual((archived / "receipts" / self.receipt_path.name).read_bytes(), M.canonical(self.receipt) + b"\n")
        self.assertTrue(output["parent_synced"])
        self.assertFalse(output["runtime_authorized"])
        with self.assertRaises(M.Refusal): self.archive()

    def test_expiry_is_strict_and_manifest_is_byte_exact(self):
        for now in (self.manifest["issued_at"], self.manifest["expires_at"], True):
            self.now = now
            with self.assertRaises(M.Refusal): self.archive()
            self.assertTrue(self.root.exists())
        self.now = self.manifest["expires_at"] + 1
        self.write(self.root / "active.json", self.raw + b"\n")
        self.refresh()
        with self.assertRaises(M.Refusal): self.archive()

    def test_wrong_package_boot_storage_or_live_process_refuses(self):
        before = copy.deepcopy(self.facts)
        for mutation in (
                lambda f: f.update(boot_id="foreign"),
                lambda f: f.update(storage_cfg_sha256="0" * 64),
                lambda f: f.update(blocking_processes=[{"pid": 42}]),
                lambda f: f["package"].update(dpkg_state="half-configured"),
                lambda f: f["package"].update(artifact_sha256="0" * 64),
                lambda f: f["package"].update(config_version="other")):
            self.facts = copy.deepcopy(before); mutation(self.facts)
            with self.assertRaises(M.Refusal): self.archive()
            self.assertTrue(self.root.exists())

    def test_configured_candidate_or_deferred_receipt_refuses_even_when_authorized(self):
        self.facts["package"]["dpkg_state"] = "installed"
        self.request["expected_package"] = copy.deepcopy(self.facts["package"])
        with self.assertRaises(M.Refusal): self.archive()
        self.facts["package"]["dpkg_state"] = "unpacked"
        self.request["expected_package"] = copy.deepcopy(self.facts["package"])
        self.receipt["phase"] = "PACKAGE_CONFIGURED_DEFERRED"
        self.write(self.receipt_path, M.canonical(self.receipt))
        self.request["receipt_sha256"][self.receipt_path.name] = M.digest(self.receipt_path.read_bytes())
        self.refresh()
        with self.assertRaises(M.Refusal): self.archive()

    def test_missing_foreign_or_changed_preinst_refuses(self):
        self.write(self.receipt_path, self.receipt_path.read_bytes() + b"\n")
        self.refresh()
        with self.assertRaises(M.Refusal): self.archive()

    def prepare_zero_receipt_restart(self):
        predecessor_request = copy.deepcopy(self.request)
        predecessor = self.parent / (
            "maintenance.expired-" + self.manifest["tx"] + "-" + self.request["operation_id"])
        self.root.rename(predecessor)
        self.root.mkdir(mode=0o700)
        for name in ("attempts", "sidecars", "receipts"):
            (self.root / name).mkdir(mode=0o700)
        self.write(self.root / ".maintenance.lock", b"")
        current = copy.deepcopy(self.manifest)
        current["tx"] = "a" * 32
        current["issued_at"] = self.manifest["expires_at"] + 1
        current["expires_at"] = current["issued_at"] + 100
        self.manifest = current
        self.raw = M.canonical(current)
        self.write(self.root / "active.json", self.raw)
        self.now = current["expires_at"] + 1
        self.request = {
            "schema": "slt-expired-prepare-archive/v2", "operation_id": "d" * 32,
            "node": self.facts["node"], "boot_id": self.facts["boot_id"],
            "manifest_sha256": M.digest(self.raw),
            "storage_cfg_sha256": self.facts["storage_cfg_sha256"],
            "expected_package": copy.deepcopy(self.facts["package"]),
            "receipt_sha256": {},
            "preexisting_unpacked_proof": {"abort_request": predecessor_request},
            "evidence_tree_sha256": M.snapshot(self.root, os.geteuid())["tree_sha256"],
        }
        return predecessor

    def test_zero_receipt_restart_requires_exact_archived_predecessor(self):
        predecessor = self.prepare_zero_receipt_restart()
        output = self.archive()
        self.assertEqual(output["classification"], "EXPIRED_PREPARE_ARCHIVED")
        self.assertTrue(predecessor.exists())

    def test_zero_receipt_restart_refuses_missing_or_changed_predecessor(self):
        predecessor = self.prepare_zero_receipt_restart()
        old_receipt = predecessor / "receipts" / (self.request["preexisting_unpacked_proof"]["abort_request"]["receipt_sha256"].keys().__iter__().__next__())
        old_receipt.write_bytes(old_receipt.read_bytes() + b" ")
        with self.assertRaises(M.Refusal):
            self.archive()
        self.assertTrue(self.root.exists())
        self.request["receipt_sha256"] = {}
        with self.assertRaises(M.Refusal): self.archive()

    def test_cas_intent_outcome_staged_and_unsafe_objects_refuse(self):
        for name in ("INTENT.json", "OUTCOME.json", ".INTENT.json.tmp"):
            path = self.cas / name; self.write(path, b"evidence")
            with self.assertRaises(M.Refusal): self.archive()
            path.unlink()  # Disposable fixture only.
        (self.cas / "foreign").symlink_to(self.receipt_path)
        with self.assertRaises(M.Refusal): self.archive()

    def test_symlink_hardlink_fifo_permissions_and_root_extra_refuse(self):
        path = self.root / "unsafe"
        path.symlink_to(self.receipt_path)
        with self.assertRaises(M.Refusal): self.archive()
        path.unlink()
        os.link(self.receipt_path, path)
        with self.assertRaises(M.Refusal): self.archive()
        path.unlink()
        os.mkfifo(path, 0o600)
        with self.assertRaises(M.Refusal): self.archive()
        path.unlink()
        self.receipt_path.chmod(0o666)
        with self.assertRaises(M.Refusal): self.archive()
        self.receipt_path.chmod(0o600)
        path.mkdir(mode=0o700); self.refresh()
        with self.assertRaises(M.Refusal): self.archive()

    def test_lock_contention_and_replacement_refuse(self):
        fd = os.open(self.root / ".maintenance.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(M.Refusal): self.archive()
        finally: os.close(fd)
        def replace(point):
            if point == "before_rename":
                self.locks[0].rename(self.parent / "old-dpkg-lock")
                self.write(self.locks[0], b"")
        with self.assertRaises(M.Refusal): self.archive(fault=replace)
        self.assertTrue(self.root.exists())

    def test_last_boundary_evidence_or_cas_race_refuses_before_rename(self):
        def mutate(point):
            if point == "before_rename": self.write(self.root / "active.json", self.raw + b" ")
        with self.assertRaises(M.Refusal): self.archive(fault=mutate)
        self.assertTrue(self.root.exists())
        self.write(self.root / "active.json", self.raw); self.refresh()
        def cas(point):
            if point == "before_rename": self.write(self.cas / "INTENT.json", b"uncertain")
        with self.assertRaises(M.Refusal): self.archive(fault=cas)
        self.assertTrue(self.root.exists())

    def test_final_facts_change_prevents_rename(self):
        calls = []
        def collect():
            calls.append(1)
            value = copy.deepcopy(self.facts)
            if len(calls) > 1: value["blocking_processes"] = [{"pid": 1}]
            return value
        with self.assertRaises(M.Refusal):
            self.lab().archive(self.request, self.raw, collect, clock=lambda: self.now)
        self.assertTrue(self.root.exists())

    def test_destination_collision_never_overwrites(self):
        destination = self.parent / ("maintenance.expired-" + self.manifest["tx"] + "-" + self.request["operation_id"])
        destination.mkdir(mode=0o700)
        self.write(destination / "foreign", b"must remain")
        with self.assertRaises(M.Refusal): self.archive()
        self.assertTrue(self.root.exists())
        self.assertEqual((destination / "foreign").read_bytes(), b"must remain")

    def test_postrename_fault_retains_complete_recoverable_tree(self):
        before = M.snapshot(self.root, os.geteuid())
        def crash(point):
            if point == "after_rename": raise OSError("fsync not reached")
        with self.assertRaisesRegex(M.Refusal, "outcome uncertain"):
            self.archive(fault=crash)
        self.assertFalse(self.root.exists())
        archives = list(self.parent.glob("maintenance.expired-*"))
        self.assertEqual(len(archives), 1)
        self.assertEqual(M.snapshot(archives[0], os.geteuid()), before)

    def absent_cas(self):
        # Remove only this disposable fixture's known empty CAS namespace.
        (self.cas / ".cas.lock").unlink()
        self.cas.rmdir()

    def test_absent_cas_root_is_created_pinned_and_locked(self):
        self.absent_cas()
        output = self.archive()
        self.assertEqual(output["classification"], "EXPIRED_PREPARE_ARCHIVED")
        self.assertEqual(self.cas.stat().st_mode & 0o777, 0o700)
        self.assertEqual(sorted(path.name for path in self.cas.iterdir()), [".cas.lock"])
        self.assertEqual((self.cas / ".cas.lock").stat().st_mode & 0o777, 0o600)

    def test_concurrent_cas_directory_creator_refuses_before_archive(self):
        self.absent_cas()
        def create(point):
            if point == "before_cas_root_create": self.cas.mkdir(mode=0o700)
        with self.assertRaisesRegex(M.Refusal, "concurrent CAS root creator"):
            self.archive(fault=create)
        self.assertTrue(self.root.exists())
        self.assertEqual((self.root / "active.json").read_bytes(), self.raw)

    def test_new_cas_root_replacement_or_concurrent_reservation_refuses(self):
        self.absent_cas()
        def replace(point):
            if point == "after_cas_root_create":
                self.cas.rename(self.parent / "original-cas")
                self.cas.mkdir(mode=0o700)
        with self.assertRaisesRegex(M.Refusal, "CAS root changed"):
            self.archive(fault=replace)
        self.assertTrue(self.root.exists())
        self.cas.rmdir()
        def reserve(point):
            if point == "after_cas_root_create": self.write(self.cas / "INTENT.json", b"reservation")
        with self.assertRaises(M.Refusal): self.archive(fault=reserve)
        self.assertTrue(self.root.exists())
        self.assertEqual((self.cas / "INTENT.json").read_bytes(), b"reservation")

    def test_existing_cas_symlink_file_and_unsafe_directory_refuse(self):
        self.absent_cas()
        self.cas.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(M.Refusal): self.archive()
        self.cas.unlink()
        self.write(self.cas, b"not a directory")
        with self.assertRaises(M.Refusal): self.archive()
        self.cas.unlink()
        self.cas.mkdir(mode=0o777); self.cas.chmod(0o777)
        with self.assertRaises(M.Refusal): self.archive()
        self.assertTrue(self.root.exists())


if __name__ == "__main__":
    unittest.main()
