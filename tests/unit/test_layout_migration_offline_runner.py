import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "experiments/thick-generations/layout-migration-offline-runner.py"
SPEC = importlib.util.spec_from_file_location("offline_runner", PATH)
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)


class Args:
    command = "inspect"
    phase = None
    evidence_sha256 = None


class OfflineRunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.identity = root / "identity.json"
        self.journal = root / "journal.jsonl"
        self.identity.write_text(json.dumps({
            "schema": "slt-offline-migration/v1", "tx": "a" * 32,
            "participants": ["node1", "node2", "node3"], "candidate": {"sha": "c" * 64},
            "baseline_storage_cfg_sha256": "b" * 64,
            "target_storage_cfg_sha256": "d" * 64,
        }))

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, command="inspect", phase=None, evidence=None):
        args = Args()
        args.command, args.phase, args.evidence_sha256 = command, phase, evidence
        args.identity, args.journal = str(self.identity), str(self.journal)
        return RUNNER.operate(args)

    def init(self):
        return self.call("init")

    def test_empty_is_prepare_only(self):
        self.assertEqual(self.init()["next_phase"], "PREPARE_READY")

    def test_intent_requires_recovery_and_never_retries(self):
        evidence = "e" * 64
        self.init()
        result = self.call("intent", "PREPARE_READY", evidence)
        self.assertEqual(result["classification"], "RECOVERY_REQUIRED")
        with self.assertRaisesRegex(RUNNER.Refusal, "not ready"):
            self.call("intent", "PREPARE_READY", evidence)

    def test_reconcile_requires_exact_positive_evidence(self):
        evidence = "e" * 64
        self.init()
        self.call("intent", "PREPARE_READY", evidence)
        with self.assertRaisesRegex(RUNNER.Refusal, "must differ"):
            self.call("reconcile", "PREPARE_READY", evidence)
        result = self.call("reconcile", "PREPARE_READY", "f" * 64)
        self.assertEqual(result["next_phase"], "ALL_UNPACKED")

    def test_skip_phase_refused(self):
        self.init()
        with self.assertRaisesRegex(RUNNER.Refusal, "not ready"):
            self.call("intent", "CONFIG_COMMITTED", "e" * 64)

    def test_missing_journal_is_not_reinitialized_by_inspect(self):
        with self.assertRaises(FileNotFoundError):
            self.call()

    def test_replacement_journal_inode_refused(self):
        self.init()
        old = self.journal.with_suffix(".old")
        self.journal.rename(old)
        self.journal.write_bytes(old.read_bytes())
        self.journal.chmod(0o600)
        with self.assertRaisesRegex(RUNNER.Refusal, "unsafe|anchor"):
            self.call()

    def test_identity_drift_refused(self):
        self.init()
        self.call("intent", "PREPARE_READY", "e" * 64)
        value = json.loads(self.identity.read_text())
        value["target_storage_cfg_sha256"] = "f" * 64
        self.identity.write_text(json.dumps(value))
        with self.assertRaisesRegex(RUNNER.Refusal, "identity invalid"):
            self.call()

    def test_torn_and_tampered_journal_refused(self):
        self.init()
        self.call("intent", "PREPARE_READY", "e" * 64)
        self.journal.write_bytes(self.journal.read_bytes()[:-1])
        with self.assertRaisesRegex(RUNNER.Refusal, "torn"):
            self.call()

    def test_hash_chain_tamper_refused(self):
        self.init()
        self.call("intent", "PREPARE_READY", "e" * 64)
        raw = self.journal.read_text()
        self.journal.write_text(raw.replace('"phase":"PREPARE_READY"',
                                            '"phase":"ALL_UNPACKED"'))
        with self.assertRaisesRegex(RUNNER.Refusal, "chain invalid"):
            self.call()

    def test_hardlinked_journal_refused(self):
        self.init()
        alias = self.journal.with_suffix(".alias")
        alias.hardlink_to(self.journal)
        with self.assertRaisesRegex(RUNNER.Refusal, "unsafe|anchor"):
            self.call()

    def test_complete_sequence_settles_without_release_authority(self):
        self.init()
        for phase in RUNNER.PHASES:
            evidence = RUNNER.digest(("intent:" + phase).encode())
            result_evidence = RUNNER.digest(("result:" + phase).encode())
            self.call("intent", phase, evidence)
            result = self.call("reconcile", phase, result_evidence)
        self.assertEqual(result, {"classification": "SETTLED", "authorization": "NONE"})


if __name__ == "__main__":
    unittest.main()
