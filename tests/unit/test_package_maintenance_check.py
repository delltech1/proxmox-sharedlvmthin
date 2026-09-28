import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-package-maintenance-check"
LOADER = importlib.machinery.SourceFileLoader("package_maintenance_check", str(PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
CHECK = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(CHECK)


class PackageMaintenanceCheckTests(unittest.TestCase):
    def manifest(self, phase="PREPARE_READY"):
        now = 2_000_000_000
        nodes = [
            {"name": "pve01", "boot_id": "11111111-1111-4111-8111-111111111111"},
            {"name": "pve02", "boot_id": "22222222-2222-4222-8222-222222222222"},
            {"name": "pve03", "boot_id": "33333333-3333-4333-8333-333333333333"},
        ]
        evidence = [{"node": node["name"], "challenge": str(index) * 32,
                     "evidence_sha256": "e" * 64, "barrier": True,
                     "workers_clear": True, "guard_idle_disarmed": True,
                     "old_consumers_absent": True}
                    for index, node in enumerate(nodes, 1)]
        return {
            "schema": "slt-package-maintenance/v1", "tx": "a" * 32,
            "phase": phase, "generation": 1, "issued_at": now - 10,
            "expires_at": now + 300, "cluster_name": "cluster-a",
            "corosync_conf_sha256": "c" * 64, "nodes": nodes,
            "candidate": {"package": "pve-sharedlvmthin",
                          "version": "0.9.0~rc5.13~tg34", "flavor": "dual",
                          "artifact_sha256": "a" * 64, "deb_sha256": "d" * 64},
            "baseline_storage_cfg_sha256": "b" * 64,
            "target_storage_cfg_sha256": "9" * 64,
            "allowed_effects": ["package-unpack", "package-configure-deferred"],
            "plan_sha256": "f" * 64, "node_evidence": evidence,
        }

    def validate(self, manifest, phase="PREPARE_READY", **changes):
        values = dict(
            expected_phase=phase, package="pve-sharedlvmthin",
            version="0.9.0~rc5.13~tg34", flavor="dual",
            artifact_sha256="a" * 64,
            storage_sha256="b" * 64 if phase == "PREPARE_READY" else "9" * 64,
            hostname="pve02", boot_id="22222222-2222-4222-8222-222222222222",
            cluster_name="cluster-a", corosync_sha256="c" * 64,
            now=2_000_000_000,
        )
        values.update(changes)
        return CHECK.validate_manifest(manifest, **values)

    def test_exact_prepare_and_commit_contracts_pass_without_authorizing_storage(self):
        prepare = self.validate(self.manifest())
        self.assertEqual(prepare["phase"], "PREPARE_READY")
        commit = self.validate(self.manifest("CONFIG_COMMITTED"),
                               phase="CONFIG_COMMITTED")
        self.assertEqual(commit["phase"], "CONFIG_COMMITTED")

    def test_unknown_duplicate_and_non_boolean_evidence_refuse(self):
        manifest = self.manifest()
        manifest["unknown"] = True
        with self.assertRaisesRegex(CHECK.Refusal, "fields"):
            self.validate(manifest)
        with self.assertRaisesRegex(CHECK.Refusal, "duplicate"):
            CHECK.decode_exact(b'{"schema":1,"schema":2}')
        manifest = self.manifest()
        manifest["node_evidence"][0]["barrier"] = "true"
        with self.assertRaisesRegex(CHECK.Refusal, "not positive"):
            self.validate(manifest)

    def test_stale_replay_wrong_phase_and_identity_refuse(self):
        with self.assertRaisesRegex(CHECK.Refusal, "stale"):
            self.validate(self.manifest(), now=2_000_001_000)
        with self.assertRaisesRegex(CHECK.Refusal, "phase"):
            self.validate(self.manifest(), phase="CONFIG_COMMITTED")
        with self.assertRaisesRegex(CHECK.Refusal, "boot identity"):
            self.validate(self.manifest(), boot_id="99999999-9999-4999-8999-999999999999")
        with self.assertRaisesRegex(CHECK.Refusal, "candidate"):
            self.validate(self.manifest(), artifact_sha256="9" * 64)

    def test_expiry_cannot_be_extended_past_bound(self):
        manifest = self.manifest()
        manifest["expires_at"] = manifest["issued_at"] + 1801
        with self.assertRaisesRegex(CHECK.Refusal, "overlong"):
            self.validate(manifest)

    def receipt(self, phase, recorded_at=2_000_000_000):
        return {
            "schema": "slt-package-maintenance-receipt/v1", "tx": "a" * 32,
            "generation": 1, "phase": phase, "node": "pve02",
            "boot_id": "22222222-2222-4222-8222-222222222222",
            "package": "pve-sharedlvmthin", "version": "0.9.0~rc5.13~tg34",
            "flavor": "dual", "artifact_sha256": "a" * 64,
            "manifest_sha256": "f" * 64, "storage_cfg_sha256": "b" * 64,
            "recorded_at": recorded_at,
        }

    def test_receipt_state_machine_is_idempotent_and_non_skippable(self):
        preinst = self.receipt("PREINST_ACCEPTED")
        self.assertEqual(CHECK.receipt_transition(None, preinst, "PREINST_ACCEPTED"),
                         "WRITE")
        repeated = self.receipt("PREINST_ACCEPTED", recorded_at=2_000_000_010)
        self.assertEqual(CHECK.receipt_transition(preinst, repeated, "PREINST_ACCEPTED"),
                         "IDEMPOTENT")
        configured = self.receipt("PACKAGE_CONFIGURED_DEFERRED")
        configured["manifest_sha256"] = "9" * 64
        configured["storage_cfg_sha256"] = "8" * 64
        self.assertEqual(CHECK.receipt_transition(
            preinst, configured, "PACKAGE_CONFIGURED_DEFERRED"), "WRITE")
        with self.assertRaisesRegex(CHECK.Refusal, "lacks exact PREINST"):
            CHECK.receipt_transition(None, configured,
                                     "PACKAGE_CONFIGURED_DEFERRED")

    def test_same_phase_receipt_with_changed_evidence_refuses(self):
        previous = self.receipt("PREINST_ACCEPTED")
        changed = self.receipt("PREINST_ACCEPTED", recorded_at=2_000_000_010)
        changed["boot_id"] = "99999999-9999-4999-8999-999999999999"
        with self.assertRaisesRegex(CHECK.Refusal, "different evidence"):
            CHECK.receipt_transition(previous, changed, "PREINST_ACCEPTED")

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_secure_manifest_read_and_receipt_write_execute_end_to_end(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-test-", dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                receipts = directory / "receipts"
                receipts.mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(directory)

                prepare = self.manifest("PREPARE_READY")
                prepare_bytes = (json.dumps(prepare, sort_keys=True,
                                             separators=(",", ":")) + "\n").encode()
                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(prepare_bytes)
                active.chmod(0o600)
                self.assertEqual(CHECK.secure_manifest_read(), prepare_bytes)

                result = {"tx": prepare["tx"], "generation": 1}
                common = dict(
                    package="pve-sharedlvmthin", version="0.9.0~rc5.13~tg34",
                    flavor="dual", artifact_sha256="a" * 64,
                    hostname="pve02",
                    boot_id="22222222-2222-4222-8222-222222222222",
                    now=2_000_000_000,
                )
                with CHECK.MaintenanceContext() as context:
                    self.assertEqual(context.read_manifest(), prepare_bytes)
                    context.freeze_authorization(prepare["expires_at"])
                    recorded = CHECK.secure_receipt_record(
                        context=context, manifest_bytes=prepare_bytes,
                        manifest=prepare, result=result,
                        record_phase="PREINST_ACCEPTED",
                        storage_sha256="b" * 64, **common)
                self.assertEqual(recorded, "RECORDED")
                with CHECK.MaintenanceContext() as context:
                    self.assertEqual(context.read_manifest(), prepare_bytes)
                    context.freeze_authorization(prepare["expires_at"])
                    repeated = CHECK.secure_receipt_record(
                        context=context, manifest_bytes=prepare_bytes,
                        manifest=prepare, result=result,
                        record_phase="PREINST_ACCEPTED",
                        storage_sha256="b" * 64, **common)
                self.assertEqual(repeated, "IDEMPOTENT")

                commit = self.manifest("CONFIG_COMMITTED")
                commit_bytes = (json.dumps(commit, sort_keys=True,
                                            separators=(",", ":")) + "\n").encode()
                active.write_bytes(commit_bytes)
                active.chmod(0o600)
                with CHECK.MaintenanceContext() as context:
                    self.assertEqual(context.read_manifest(), commit_bytes)
                    context.freeze_authorization(commit["expires_at"])
                    configured = CHECK.secure_receipt_record(
                        context=context, manifest_bytes=commit_bytes,
                        manifest=commit, result=result,
                        record_phase="PACKAGE_CONFIGURED_DEFERRED",
                        storage_sha256="9" * 64, **common)
                self.assertEqual(configured, "RECORDED")
                receipt = json.loads(
                    (receipts / f"{prepare['tx']}.json").read_text())
                self.assertEqual(receipt["phase"],
                                 "PACKAGE_CONFIGURED_DEFERRED")
                self.assertEqual(receipt["storage_cfg_sha256"], "9" * 64)
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_common_lock_covers_manifest_read_through_receipt(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-lock-", dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                (directory / "receipts").mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(directory)
                manifest = self.manifest("PREPARE_READY")
                encoded = (json.dumps(manifest, sort_keys=True,
                                      separators=(",", ":")) + "\n").encode()
                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(encoded)
                active.chmod(0o600)
                with CHECK.MaintenanceContext() as first:
                    self.assertEqual(first.read_manifest(), encoded)
                    first.freeze_authorization(manifest["expires_at"])
                    with self.assertRaisesRegex(CHECK.Refusal, "timed out"):
                        with CHECK.MaintenanceContext(timeout_sec=0.03):
                            self.fail("second context acquired the live lock")
                    result = CHECK.secure_receipt_record(
                        context=first, manifest_bytes=encoded,
                        manifest=manifest,
                        result={"tx": manifest["tx"], "generation": 1},
                        record_phase="PREINST_ACCEPTED",
                        package="pve-sharedlvmthin",
                        version="0.9.0~rc5.13~tg34", flavor="dual",
                        artifact_sha256="a" * 64,
                        storage_sha256="b" * 64, hostname="pve02",
                        boot_id="22222222-2222-4222-8222-222222222222",
                        now=2_000_000_000)
                self.assertEqual(result, "RECORDED")
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_active_inode_replacement_under_live_context_refuses_receipt(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-swap-", dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                receipts = directory / "receipts"
                receipts.mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(directory)
                manifest = self.manifest("PREPARE_READY")
                encoded = (json.dumps(manifest, sort_keys=True,
                                      separators=(",", ":")) + "\n").encode()
                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(encoded)
                active.chmod(0o600)
                with CHECK.MaintenanceContext() as context:
                    self.assertEqual(context.read_manifest(), encoded)
                    context.freeze_authorization(manifest["expires_at"])
                    os.rename(active, directory / "displaced.json")
                    active.write_bytes(encoded)
                    active.chmod(0o600)
                    with self.assertRaisesRegex(
                            CHECK.Refusal, "active maintenance manifest changed"):
                        CHECK.secure_receipt_record(
                            context=context, manifest_bytes=encoded,
                            manifest=manifest,
                            result={"tx": manifest["tx"], "generation": 1},
                            record_phase="PREINST_ACCEPTED",
                            package="pve-sharedlvmthin",
                            version="0.9.0~rc5.13~tg34", flavor="dual",
                            artifact_sha256="a" * 64,
                            storage_sha256="b" * 64, hostname="pve02",
                            boot_id="22222222-2222-4222-8222-222222222222",
                            now=2_000_000_000)
                self.assertEqual(list(receipts.iterdir()), [])
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_idempotent_receipt_replay_fsyncs_file_and_directory(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-replay-", dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                receipts = directory / "receipts"
                receipts.mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(directory)
                manifest = self.manifest("PREPARE_READY")
                encoded = (json.dumps(manifest, sort_keys=True,
                                      separators=(",", ":")) + "\n").encode()
                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(encoded)
                active.chmod(0o600)
                common = dict(
                    manifest_bytes=encoded, manifest=manifest,
                    result={"tx": manifest["tx"], "generation": 1},
                    record_phase="PREINST_ACCEPTED",
                    package="pve-sharedlvmthin",
                    version="0.9.0~rc5.13~tg34", flavor="dual",
                    artifact_sha256="a" * 64,
                    storage_sha256="b" * 64, hostname="pve02",
                    boot_id="22222222-2222-4222-8222-222222222222",
                    now=2_000_000_000)
                with CHECK.MaintenanceContext() as context:
                    context.read_manifest()
                    context.freeze_authorization(manifest["expires_at"])
                    self.assertEqual(CHECK.secure_receipt_record(
                        context=context, **common), "RECORDED")
                receipt = receipts / f"{manifest['tx']}.json"
                receipt_inode = receipt.stat().st_ino
                receipts_inode = receipts.stat().st_ino
                synced = set()
                real_fsync = CHECK.os.fsync

                def observe(descriptor):
                    inode = os.fstat(descriptor).st_ino
                    if inode in (receipt_inode, receipts_inode):
                        synced.add(inode)
                    return real_fsync(descriptor)

                with mock.patch.object(CHECK.os, "fsync", side_effect=observe):
                    with CHECK.MaintenanceContext() as context:
                        context.read_manifest()
                        context.freeze_authorization(manifest["expires_at"])
                        self.assertEqual(CHECK.secure_receipt_record(
                            context=context, **common), "IDEMPOTENT")
                self.assertEqual(synced, {receipt_inode, receipts_inode})
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_fifo_manifest_and_receipt_refuse_without_blocking_or_lock_leak(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            for fifo_kind in ("manifest", "receipt"):
                with self.subTest(fifo_kind=fifo_kind), tempfile.TemporaryDirectory(
                        prefix="slt-package-maintenance-fifo-",
                        dir="/var/lib") as temp:
                    directory = Path(temp)
                    directory.chmod(0o700)
                    receipts = directory / "receipts"
                    receipts.mkdir(mode=0o700)
                    CHECK.MAINTENANCE_DIR = str(directory)
                    manifest = self.manifest("PREPARE_READY")
                    encoded = (json.dumps(manifest, sort_keys=True,
                                          separators=(",", ":")) + "\n").encode()
                    active = directory / CHECK.ACTIVE_NAME
                    if fifo_kind == "manifest":
                        os.mkfifo(active, 0o600)
                        started = time.monotonic()
                        with CHECK.MaintenanceContext() as context:
                            with self.assertRaisesRegex(
                                    CHECK.Refusal, "manifest metadata is unsafe"):
                                context.read_manifest()
                        self.assertLess(time.monotonic() - started, 1.0)
                        active.unlink()
                        active.write_bytes(encoded)
                        active.chmod(0o600)
                    else:
                        active.write_bytes(encoded)
                        active.chmod(0o600)
                        os.mkfifo(receipts / f"{manifest['tx']}.json", 0o600)
                        started = time.monotonic()
                        with CHECK.MaintenanceContext() as context:
                            context.read_manifest()
                            context.freeze_authorization(manifest["expires_at"])
                            with self.assertRaisesRegex(
                                    CHECK.Refusal, "receipt metadata is unsafe"):
                                CHECK.secure_receipt_record(
                                    context=context, manifest_bytes=encoded,
                                    manifest=manifest,
                                    result={"tx": manifest["tx"],
                                            "generation": 1},
                                    record_phase="PREINST_ACCEPTED",
                                    package="pve-sharedlvmthin",
                                    version="0.9.0~rc5.13~tg34",
                                    flavor="dual", artifact_sha256="a" * 64,
                                    storage_sha256="b" * 64, hostname="pve02",
                                    boot_id=("22222222-2222-4222-8222-"
                                             "222222222222"),
                                    now=2_000_000_000)
                        self.assertLess(time.monotonic() - started, 1.0)
                    with CHECK.MaintenanceContext(timeout_sec=0.03):
                        pass
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_late_successful_flock_is_not_admitted(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-late-", dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                CHECK.MAINTENANCE_DIR = str(directory)
                with mock.patch.object(CHECK.time, "monotonic",
                                       side_effect=(100.0, 100.0, 100.2)):
                    with self.assertRaisesRegex(CHECK.Refusal, "timed out"):
                        with CHECK.MaintenanceContext(timeout_sec=0.1):
                            self.fail("late successful flock was admitted")
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_wall_clock_rollback_cannot_extend_receipt_deadline(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-deadline-",
                    dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                receipts = directory / "receipts"
                receipts.mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(directory)
                manifest = self.manifest("PREPARE_READY")
                encoded = (json.dumps(manifest, sort_keys=True,
                                      separators=(",", ":")) + "\n").encode()
                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(encoded)
                active.chmod(0o600)
                with CHECK.MaintenanceContext() as context:
                    context.read_manifest()
                    context.freeze_authorization(manifest["expires_at"])
                    expired = False
                    real_fsync = CHECK.os.fsync
                    real_monotonic = CHECK.time.monotonic

                    def fsync_then_expire(descriptor):
                        nonlocal expired
                        result = real_fsync(descriptor)
                        if stat.S_ISREG(os.fstat(descriptor).st_mode):
                            expired = True
                        return result

                    def monotonic_now():
                        if expired:
                            return context.authorization_monotonic_deadline + 1
                        return real_monotonic()

                    with mock.patch.object(
                            CHECK.os, "fsync", side_effect=fsync_then_expire), \
                            mock.patch.object(
                                CHECK.time, "monotonic",
                                side_effect=monotonic_now), \
                            mock.patch.object(
                                CHECK.time, "time",
                                return_value=manifest["expires_at"] - 100):
                        with self.assertRaisesRegex(
                                CHECK.Refusal,
                                "authorization expired before receipt rename"):
                            CHECK.secure_receipt_record(
                                context=context, manifest_bytes=encoded,
                                manifest=manifest,
                                result={"tx": manifest["tx"], "generation": 1},
                                record_phase="PREINST_ACCEPTED",
                                package="pve-sharedlvmthin",
                                version="0.9.0~rc5.13~tg34", flavor="dual",
                                artifact_sha256="a" * 64,
                                storage_sha256="b" * 64, hostname="pve02",
                                boot_id=("22222222-2222-4222-8222-"
                                         "222222222222"),
                                now=2_000_000_000)
                self.assertFalse((receipts / f"{manifest['tx']}.json").exists())
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_hold_probe_is_exact_tristate_and_fail_closed(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-probe-", dir="/var/lib") as temp:
                base = Path(temp)
                absent = base / "absent"
                CHECK.MAINTENANCE_DIR = str(absent)
                self.assertEqual(CHECK.probe_hold(), "ABSENT")

                directory = base / "maintenance"
                directory.mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(directory)
                self.assertEqual(CHECK.probe_hold(), "ABSENT")
                lock = directory / CHECK.LOCK_NAME
                self.assertTrue(lock.is_file())
                self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o600)

                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(b"{}\n")
                active.chmod(0o600)
                self.assertEqual(CHECK.probe_hold(), "VERIFIED_HOLD")

                active.unlink()
                os.mkfifo(active, 0o600)
                started = time.monotonic()
                with self.assertRaisesRegex(
                        CHECK.Refusal, "manifest metadata is unsafe"):
                    CHECK.probe_hold()
                self.assertLess(time.monotonic() - started, 1.0)

                active.unlink()
                active.write_bytes(b"{}\n")
                active.chmod(0o600)
                directory.chmod(0o777)
                with self.assertRaisesRegex(CHECK.Refusal, "path component"):
                    CHECK.probe_hold()
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_probe_namespace_drift_is_refused_not_absent(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-probe-drift-",
                    dir="/var/lib") as temp:
                directory = Path(temp)
                directory.chmod(0o700)
                active = directory / CHECK.ACTIVE_NAME
                active.write_bytes(b"{}\n")
                active.chmod(0o600)
                CHECK.MAINTENANCE_DIR = str(directory)
                real_read = CHECK.MaintenanceContext._read_regular

                def remove_named_lock(context, *args, **kwargs):
                    result = real_read(context, *args, **kwargs)
                    (directory / CHECK.LOCK_NAME).unlink()
                    return result

                output = io.StringIO()
                with mock.patch.object(
                        CHECK.MaintenanceContext, "_read_regular",
                        new=remove_named_lock), \
                        mock.patch.object(
                            CHECK.sys, "argv", [str(CHECK.PATH)
                                                if hasattr(CHECK, "PATH")
                                                else "checker",
                                                "--probe-hold"]), \
                        contextlib.redirect_stdout(output):
                    rc = CHECK.main()
                self.assertEqual(rc, 2)
                self.assertIn("MAINTENANCE_HOLD=REFUSED", output.getvalue())
                self.assertNotIn("MAINTENANCE_HOLD=ABSENT", output.getvalue())
                self.assertTrue(active.exists())
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    @unittest.skipUnless(os.name == "posix" and os.geteuid() == 0,
                         "secure fixed-path execution fixture requires POSIX root")
    def test_probe_parent_replacement_cannot_manufacture_absent(self):
        original_directory = CHECK.MAINTENANCE_DIR
        try:
            with tempfile.TemporaryDirectory(
                    prefix="slt-package-maintenance-parent-drift-",
                    dir="/var/lib") as temp:
                base = Path(temp)
                parent = base / "state"
                parent.mkdir(mode=0o700)
                CHECK.MAINTENANCE_DIR = str(parent / "maintenance")
                real_open = CHECK.os.open
                replaced = False

                def replace_parent(name, flags, mode=0o777, *, dir_fd=None):
                    nonlocal replaced
                    if name == "maintenance" and not replaced:
                        replaced = True
                        os.rename(parent, base / "state-old")
                        parent.mkdir(mode=0o700)
                        replacement = parent / "maintenance"
                        replacement.mkdir(mode=0o700)
                        active = replacement / CHECK.ACTIVE_NAME
                        active.write_bytes(b"{}\n")
                        active.chmod(0o600)
                    return real_open(name, flags, mode, dir_fd=dir_fd)

                output = io.StringIO()
                with mock.patch.object(CHECK.os, "open", side_effect=replace_parent), \
                        mock.patch.object(
                            CHECK.sys, "argv", ["checker", "--probe-hold"]), \
                        contextlib.redirect_stdout(output):
                    rc = CHECK.main()
                self.assertEqual(rc, 2)
                self.assertIn("MAINTENANCE_HOLD=REFUSED", output.getvalue())
                self.assertNotIn("MAINTENANCE_HOLD=ABSENT", output.getvalue())
                self.assertTrue((parent / "maintenance" / CHECK.ACTIVE_NAME).exists())
        finally:
            CHECK.MAINTENANCE_DIR = original_directory

    def test_source_has_fixed_path_and_no_mutating_or_bypass_interface(self):
        source = PATH.read_text(encoding="utf-8")
        self.assertIn('MAINTENANCE_DIR = "/var/lib/pve-sharedlvmthin/maintenance"', source)
        self.assertNotIn("--manifest", source)
        self.assertNotIn("os.environ", source)
        for forbidden in ("systemctl", "dpkg --", "pvesm", "lvcreate", "lvremove",
                          "storage.cfg\", \"w", "active.json\", \"w"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
