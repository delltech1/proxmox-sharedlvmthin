import argparse
import fcntl
import importlib.util
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
READY_PATH = ROOT / "tests/unit/test_layout_migration_all_ready_plan.py"
READY_SPEC = importlib.util.spec_from_file_location("ready_fixtures", READY_PATH)
READY_TEST = importlib.util.module_from_spec(READY_SPEC)
READY_SPEC.loader.exec_module(READY_TEST)
PATH = ROOT / "experiments/thick-generations/layout-migration-release-certificate.py"
SPEC = importlib.util.spec_from_file_location("release_certificate", PATH)
CERT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CERT)


class ReleaseCertificateTests(READY_TEST.LayoutMigrationAllReadyPlanTests):
    def persist(self, directory, name, payload, **kwargs):
        return CERT.persist_create_only(
            directory, name, payload, expected_uid=os.geteuid(),
            valid_until=kwargs.pop("valid_until", time.time() + 30), **kwargs)

    def release_fixture(self):
        temp, ready_args, manifest, _, evidence = super().fixture()
        root = Path(temp.name)
        plan = CERT.BASE.evaluate(ready_args)
        plan_path = root / "all-ready.json"
        plan_path.write_text(json.dumps(plan, sort_keys=True) + "\n")
        plan_sha = CERT.digest(plan_path.read_bytes())
        auth = {
            "schema": "slt-layout-release-authorization/v1",
            "tx": manifest["tx"], "generation": manifest["generation"],
            "manifest_sha256": self.manifest_sha,
            "all_ready_plan_sha256": plan_sha,
            "evidence_set_sha256": plan["evidence_set_sha256"],
            "candidate": manifest["candidate"],
            "participant_boots": {row["name"]: row["boot_id"]
                                  for row in manifest["nodes"]},
            "authorization_id": "a" * 32, "commit_id": "b" * 32,
            "coordinator": {"node": self.names[0],
                            "boot_id": manifest["nodes"][0]["boot_id"]},
            "issued_at": self.now - 1, "expires_at": self.now + 300,
            "committed_at": self.now,
            "release_not_after": self.now + 120,
            "allowed_effects": ["persist-release-certificate"],
        }
        auth_path = root / "release-authorization.json"
        auth_path.write_text(json.dumps(auth, sort_keys=True) + "\n")
        args = argparse.Namespace(
            manifest=ready_args.manifest,
            all_configured=ready_args.all_configured,
            refresh_authorization=ready_args.authorization,
            all_ready=str(plan_path), release_authorization=str(auth_path),
            node_evidence=evidence, max_age_sec=300, max_skew_sec=60,
            now=self.now)
        cert_dir = root / "certificates"
        cert_dir.mkdir(mode=0o700)
        return temp, args, manifest, plan, auth, auth_path, cert_dir

    def test_exact_inputs_make_non_releasing_certificate(self):
        temp, args, manifest, plan, auth, _, _ = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        self.assertEqual(cert["phase"], "RELEASE_COMMITTED")
        self.assertEqual(cert["allowed_effects"],
                         ["archive-exact-active-manifest"])
        self.assertEqual(cert["manifest_sha256"], self.manifest_sha)
        self.assertEqual(cert["evidence_set_sha256"],
                         plan["evidence_set_sha256"])
        self.assertEqual(cert["commit_id"], auth["commit_id"])
        self.assertEqual(payload, CERT.canonical(cert) + b"\n")

    def test_create_only_replay_and_conflict(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        uid = os.geteuid()
        self.assertEqual(self.persist(cert_dir, name, payload),
                         "CERTIFICATE_DURABLE_LOCAL")
        self.assertEqual(self.persist(cert_dir, name, payload),
                         "VERIFIED_REPLAY")
        with self.assertRaisesRegex(CERT.Refusal, "different bytes"):
            self.persist(cert_dir, name, payload + b"x")

    def test_exact_staged_crash_replays_but_foreign_stage_refuses(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        staged = cert_dir / f".{name}.tmp"
        staged.write_bytes(payload)
        staged.chmod(0o600)
        self.assertEqual(self.persist(cert_dir, name, payload),
            "CERTIFICATE_DURABLE_LOCAL")
        (cert_dir / name).unlink()
        staged.write_bytes(b"foreign\n")
        staged.chmod(0o600)
        with self.assertRaisesRegex(CERT.Refusal, "staged certificate"):
            self.persist(cert_dir, name, payload)

    def test_crash_after_link_before_temp_unlink_is_reconciled(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        staged = cert_dir / f".{name}.tmp"
        final = cert_dir / name
        staged.write_bytes(payload)
        staged.chmod(0o600)
        os.link(staged, final)
        self.assertEqual(final.stat().st_nlink, 2)
        self.assertEqual(self.persist(cert_dir, name, payload),
            "VERIFIED_REPLAY")
        self.assertFalse(staged.exists())
        self.assertEqual(final.stat().st_nlink, 1)

    def test_fsync_failures_never_authorize_hold_release(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        uid = os.geteuid()
        with mock.patch.object(CERT.os, "fsync",
                               side_effect=OSError("injected file fsync")):
            with self.assertRaisesRegex(OSError, "injected file fsync"):
                self.persist(cert_dir, name, payload)
        self.assertFalse((cert_dir / name).exists())
        self.assertEqual(self.persist(cert_dir, name, payload),
                         "CERTIFICATE_DURABLE_LOCAL")
        (cert_dir / name).unlink()
        calls = 0
        real_fsync = CERT.os.fsync
        def fail_directory_fsync(fd):
            nonlocal calls
            calls += 1
            if calls == 3:
                raise OSError("injected directory fsync")
            return real_fsync(fd)
        with mock.patch.object(CERT.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(OSError, "injected directory fsync"):
                self.persist(cert_dir, name, payload)
        self.assertTrue((cert_dir / name).exists())
        self.assertEqual(self.persist(cert_dir, name, payload),
                         "VERIFIED_REPLAY")

    def test_link_stored_then_error_is_reconciled(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        real_link = CERT.os.link
        def stored_then_error(*positional, **keywords):
            real_link(*positional, **keywords)
            raise OSError("injected ambiguous link result")
        with mock.patch.object(CERT.os, "link", side_effect=stored_then_error):
            self.assertEqual(self.persist(cert_dir, name, payload),
                "CERTIFICATE_DURABLE_LOCAL")
        self.assertEqual((cert_dir / name).read_bytes(), payload)
        self.assertFalse((cert_dir / f".{name}.tmp").exists())

    def test_stored_plan_is_freshly_recomputed(self):
        temp, args, _, _, _, _, _ = self.release_fixture()
        self.addCleanup(temp.cleanup)
        value = json.loads(Path(args.all_ready).read_text())
        value["node_evidence_sha256"][self.names[0]] = "f" * 64
        value["plan_sha256"] = CERT.digest(CERT.canonical(
            {key: item for key, item in value.items() if key != "plan_sha256"}))
        Path(args.all_ready).write_text(json.dumps(value, sort_keys=True) + "\n")
        with self.assertRaisesRegex(CERT.Refusal, "fresh evaluation"):
            CERT.evaluate(args)

    def test_authorization_drift_expiry_and_effect_expansion_refuse(self):
        mutations = [
            (lambda value: value.update(evidence_set_sha256="0" * 64),
             "identity differs"),
            (lambda value: value.update(expires_at=self.now - 1),
             "interval is stale"),
            (lambda value: value.update(release_not_after=self.now - 1),
             "interval is stale"),
            (lambda value: value.update(issued_at=self.now - 2,
                                        committed_at=self.now - 2),
             "interval is stale"),
            (lambda value: value["allowed_effects"].append("remove-hold"),
             "effects are not exact"),
            (lambda value: value["coordinator"].update(boot_id="0" * 36),
             "coordinator is not"),
        ]
        for mutate, message in mutations:
            with self.subTest(message=message):
                temp, args, _, _, auth, auth_path, _ = self.release_fixture()
                try:
                    mutate(auth)
                    auth_path.write_text(json.dumps(auth, sort_keys=True) + "\n")
                    with self.assertRaisesRegex(CERT.Refusal, message):
                        CERT.evaluate(args)
                finally:
                    temp.cleanup()

    def test_numeric_aliases_and_unsafe_directory_refuse(self):
        temp, args, _, _, auth, auth_path, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        auth["generation"] = float(auth["generation"])
        auth_path.write_text(json.dumps(auth, sort_keys=True) + "\n")
        with self.assertRaisesRegex(CERT.Refusal, "identity differs"):
            CERT.evaluate(args)
        cert_dir.chmod(0o777)
        with self.assertRaisesRegex(CERT.Refusal, "directory.*unsafe"):
            self.persist(cert_dir, "safe.json", b"{}\n")

    def test_manifest_swap_between_reads_is_refused(self):
        temp, args, manifest, _, _, _, _ = self.release_fixture()
        self.addCleanup(temp.cleanup)
        real_evaluate = CERT.BASE.evaluate
        def swap_then_evaluate(namespace):
            manifest["candidate"]["artifact_sha256"] = "0" * 64
            Path(args.manifest).write_text(json.dumps(manifest, sort_keys=True)
                                           + "\n")
            return real_evaluate(namespace)
        with mock.patch.object(CERT.BASE, "evaluate",
                               side_effect=swap_then_evaluate):
            with self.assertRaisesRegex(CERT.Refusal,
                                        "different manifest|identity differs"):
                CERT.evaluate(args)

    def test_one_decision_slot_per_transaction_generation(self):
        temp, args, _, _, auth, auth_path, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        first, first_payload = CERT.evaluate(args)
        name = f"{first['tx']}-{first['generation']}.json"
        self.assertEqual(self.persist(cert_dir, name, first_payload),
            "CERTIFICATE_DURABLE_LOCAL")
        auth["commit_id"] = "c" * 32
        auth_path.write_text(json.dumps(auth, sort_keys=True) + "\n")
        _, second_payload = CERT.evaluate(args)
        with self.assertRaisesRegex(CERT.Refusal, "different bytes"):
            self.persist(cert_dir, name, second_payload)

    def test_unsafe_mode_and_ancestor_symlink_refuse(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        final = cert_dir / name
        final.write_bytes(payload)
        final.chmod(0o666)
        with self.assertRaisesRegex(CERT.Refusal, "mode is unsafe"):
            self.persist(cert_dir, name, payload)
        final.unlink()
        alias = cert_dir.parent / "certificate-alias"
        os.symlink(cert_dir, alias)
        with self.assertRaisesRegex(CERT.Refusal, "symlink"):
            self.persist(alias, name, payload)

    def test_fifo_final_and_staged_refuse_without_blocking(self):
        if not hasattr(os, "mkfifo"):
            self.skipTest("FIFO unavailable")
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        final = cert_dir / name
        started = time.monotonic()
        os.mkfifo(final, 0o600)
        with self.assertRaisesRegex(CERT.Refusal, "inode is unsafe"):
            self.persist(cert_dir, name, payload)
        self.assertLess(time.monotonic() - started, 1.0)
        final.unlink()
        staged = cert_dir / f".{name}.tmp"
        os.mkfifo(staged, 0o600)
        started = time.monotonic()
        with self.assertRaisesRegex(CERT.Refusal, "inode is unsafe"):
            self.persist(cert_dir, name, payload)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_held_lock_is_bounded_and_expiry_cannot_publish(self):
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        lock = cert_dir / ".release.lock"
        lock.write_bytes(b"")
        lock.chmod(0o600)
        fd = os.open(lock, os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with self.assertRaisesRegex(CERT.Refusal, "timed out"):
            self.persist(cert_dir, name, payload, lock_timeout_sec=0.05)
        with self.assertRaisesRegex(CERT.Refusal, "expired"):
            self.persist(cert_dir, name, payload, lock_timeout_sec=0.5,
                         valid_until=time.time() + 0.03)
        self.assertFalse((cert_dir / name).exists())

    def test_user_owned_parent_refuses_root_checkpoint(self):
        if os.geteuid() != 0:
            self.skipTest("root ownership test")
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        unsafe_parent = cert_dir.parent / "unsafe-parent"
        unsafe_child = unsafe_parent / "certificates"
        unsafe_child.mkdir(parents=True, mode=0o700)
        os.chown(unsafe_parent, 65534, 65534)
        unsafe_parent.chmod(0o700)
        name = f"{cert['tx']}-{cert['generation']}.json"
        with self.assertRaisesRegex(CERT.Refusal, "chain is unsafe"):
            self.persist(unsafe_child, name, payload)

    def test_symlink_and_hardlink_final_are_refused(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        temp, args, _, _, _, _, cert_dir = self.release_fixture()
        self.addCleanup(temp.cleanup)
        cert, payload = CERT.evaluate(args)
        name = f"{cert['tx']}-{cert['generation']}.json"
        target = cert_dir / "target"
        target.write_bytes(payload)
        target.chmod(0o600)
        os.symlink(target.name, cert_dir / name)
        with self.assertRaisesRegex(CERT.Refusal,
                                    "unsafe certificate namespace"):
            self.persist(cert_dir, name, payload)
        (cert_dir / name).unlink()
        os.link(target, cert_dir / name)
        with self.assertRaisesRegex(CERT.Refusal,
                                    "two-link certificate|inode is unsafe"):
            self.persist(cert_dir, name, payload)


if __name__ == "__main__":
    unittest.main()
