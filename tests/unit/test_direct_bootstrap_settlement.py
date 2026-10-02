"""Executable direct bootstrap settlement and replay invariants."""
import importlib.machinery
import importlib.util
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class DirectBootstrapSettlementTests(unittest.TestCase):
    def setUp(self):
        self.m = load("direct_bootstrap_runtime", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin-update-policy")
        self.core = load("direct_bootstrap_core", ROOT / "usr/libexec/pve-sharedlvmthin/sharedlvmthin_update_policy.py")
        self.temp = tempfile.TemporaryDirectory()
        state = Path(self.temp.name)
        for name in ("POLICY", "FREEZE", "AUTH", "PACKAGE_TRANSITION",
                     "LAST_PACKAGE_TRANSITION", "RUNTIME_RELEASE",
                     "RUNTIME_QUALIFICATION", "LAST_RUNTIME_FINALIZATION"):
            setattr(self.m, name, state / (name.lower() + ".json"))
        self.m.PACKAGE_ARTIFACT_FILE = state / "artifact"
        self.m.PACKAGE_ARTIFACT_FILE.write_text("b" * 64 + "\n", encoding="ascii")
        self.manifest = {"schema": 1, "watched_packages": []}
        self.txid = "d" * 32
        self.receipt = {
            "schema": 2, "state": "PREPARED", "kind": "DIRECT_PROFILE_GATE",
            "txid": self.txid, "hostname": "node", "boot_id": "boot",
            "source_package": "pve-sharedlvmthin",
            "source_version": "0.9.0~rc5.11~tg32",
            "target_package": "pve-sharedlvmthin",
            "target_version": "0.9.0~rc5.87~tg53", "target_architecture": "all",
            "candidate_sha256": "a" * 64, "candidate_artifact_sha256": "b" * 64,
            "candidate_manifest_sha256": self.core.digest(self.manifest),
            "policy_mode": "UNSELECTED", "requested_policy": "FREEZE",
        }
        self.pending = {"schema": 1, "state": "QUALIFYING", "txid": self.txid,
                        "transition_sha256": self.core.digest(self.receipt)}
        self.release = {"schema": 1, "qualified": "QUALIFIED",
                        "qualification_txid": self.txid,
                        "artifact_sha256": "b" * 64,
                        "transition_sha256": self.core.digest(self.receipt)}
        self.m.atomic_json(self.m.PACKAGE_TRANSITION, self.receipt)
        self.m.atomic_json(self.m.RUNTIME_QUALIFICATION, self.pending)
        self.m.atomic_json(self.m.RUNTIME_RELEASE, self.release)

    def tearDown(self):
        self.temp.cleanup()

    def versions(self, package):
        return "0.9.0~rc5.87~tg53" if package == "pve-sharedlvmthin" else None

    def settle(self):
        def freeze(_manifest, _library):
            record = {"schema": 3, "phase": "ACTIVE", "generation": "e" * 32}
            self.m.atomic_json(self.m.FREEZE, record)
            return record

        success = types.SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(self.m, "package_version", side_effect=self.versions), \
                mock.patch.object(self.m, "manifest_and_lib", return_value=(self.manifest, self.core)), \
                mock.patch.object(self.core, "strict_json", return_value=self.manifest), \
                mock.patch.object(self.m, "enter_freeze", side_effect=freeze), \
                mock.patch.object(self.m, "freeze_integrity", return_value={"result": "PASS"}), \
                mock.patch.object(self.m, "run", return_value=success):
            return self.m.settle_direct_package(
                "pve-sharedlvmthin", "0.9.0~rc5.87~tg53",
                "a" * 64, self.txid, "freeze")

    def test_settlement_and_qualifying_lost_ack_replay_are_exact(self):
        completed = self.settle()
        self.assertEqual(completed["state"], "COMPLETE")
        self.assertFalse(self.m.PACKAGE_TRANSITION.exists())
        replay = self.settle()
        self.assertEqual(replay, completed)

    def test_terminal_lost_ack_replay_uses_exact_finalization_receipt(self):
        completed = self.settle()
        self.m.unlink_durable(self.m.RUNTIME_QUALIFICATION)
        self.m.atomic_json(self.m.LAST_RUNTIME_FINALIZATION, {
            "schema": 1, "state": "COMPLETE", "txid": self.txid,
            "release_sha256": self.core.digest(self.release),
            "boot_id": "boot", "kernel_release": "kernel",
        })
        with mock.patch.object(self.m.pathlib.Path, "read_text", return_value="boot"), \
                mock.patch.object(self.m.os, "uname", return_value=types.SimpleNamespace(release="kernel")):
            replay = self.settle()
        self.assertEqual(replay, completed)

    def test_foreign_qualifying_digest_refuses_before_policy_effect(self):
        self.pending["transition_sha256"] = "f" * 64
        self.m.atomic_json(self.m.RUNTIME_QUALIFICATION, self.pending)
        before = self.m.read_json(self.m.PACKAGE_TRANSITION)
        with mock.patch.object(self.m, "package_version", side_effect=self.versions), \
                mock.patch.object(self.m, "enter_freeze") as effect:
            with self.assertRaisesRegex(RuntimeError, "matching QUALIFYING"):
                self.m.settle_direct_package(
                    "pve-sharedlvmthin", "0.9.0~rc5.87~tg53",
                    "a" * 64, self.txid, "freeze")
        effect.assert_not_called()
        self.assertEqual(self.m.read_json(self.m.PACKAGE_TRANSITION), before)
        self.assertIsNone(self.m.read_json(self.m.LAST_PACKAGE_TRANSITION))


if __name__ == "__main__":
    unittest.main()
