"""Pure model tests: no LVM command is executed."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/lvm-sync-io-ab.py"
SPEC = importlib.util.spec_from_file_location("lvm_sync_io_ab", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


VALID_UUID = "eIz27I-qlJ3-ZlZI-B8GE-l862-ntHO-1Ip15L"


def receipt(argv, output=VALID_UUID, **changes):
    value = {
        "pid": 100, "starttime": 200, "boot_id": "b" * 32,
        "argv": argv, "terminal": "EXITED", "reaped": True,
        "returncode": 0, "descendants": [], "unread_stdout": False,
        "unread_stderr": False, "output_truncated": False,
        "identity_stable": True, "dstate_observed": False,
        "deadline_exceeded": False, "evidence_persisted": True,
        "pidfd_pinned": True, "owned_child": True,
        "stdout": output, "stderr": "",
    }
    value.update(changes)
    return value


class LvmSyncIoABTests(unittest.TestCase):
    def setUp(self):
        self.device = "/dev/mapper/mpatha"
        self.vg = "shared-vg"
        self.uuid = VALID_UUID
        self.manifest = {
            "boot_id": "b" * 32, "kernel": "7.0.14-16-pve",
            "lvm_package": "2.03.31", "executable_sha256": "a" * 64,
            "config_sha256": "c" * 64, "device_identity": "253:4/diskseq=42",
        }
        self.a = tuple(LAB.exact_argv(self.device, self.vg, False))
        self.b = tuple(LAB.exact_argv(self.device, self.vg, True))

    def test_plan_is_inert_and_default_first(self):
        value = LAB.plan(self.device, self.vg, self.uuid)
        self.assertEqual(value["classification"], "PLAN_ONLY_NO_SUBPROCESS")
        self.assertFalse(value["candidate_first"])
        self.assertFalse(value["persistent_config_change"])
        self.assertNotIn("--config", value["a_argv"])
        self.assertIn(LAB.SYNC_CONFIG, value["b_argv"])

    def test_cli_execute_always_refuses(self):
        for token in ("wrong", LAB.ACK):
            with contextlib.redirect_stdout(io.StringIO()) as stream:
                self.assertEqual(LAB.main(["--device", self.device, "--vg", self.vg,
                                           "--expect-vg-uuid", self.uuid,
                                           "--execute-token", token]), 2)
            self.assertEqual(json.loads(stream.getvalue())["classification"],
                             "REFUSED_EXECUTE_BACKEND_NOT_QUALIFIED")

    def test_scope_refuses_empty_duplicate_or_non_dev(self):
        for device in ("relative", "/dev/a b", "/dev/a,/dev/b"):
            with self.assertRaises(LAB.Refusal):
                LAB.exact_argv(device, self.vg)
        for vg in ("--foreign", "bad vg", ""):
            with self.assertRaises(LAB.Refusal):
                LAB.exact_argv(self.device, vg)

    def test_any_ambiguous_a_forbids_b(self):
        changes = (
            {"terminal": "RUNNING"}, {"reaped": False},
            {"identity_stable": False}, {"dstate_observed": True},
            {"descendants": [101]}, {"unread_stdout": True},
            {"unread_stderr": True}, {"output_truncated": True},
            {"returncode": 1}, {"argv": self.a + ("extra",)},
            {"deadline_exceeded": True}, {"evidence_persisted": False},
            {"stderr": "warning"},
        )
        for change in changes:
            value = receipt(self.a)
            value.update(change)
            admitted, _ = LAB.admit_b(value, self.a, self.manifest, self.uuid)
            self.assertFalse(admitted, change)

    def test_exact_pair_matches_expected_uuid(self):
        value = LAB.compare(receipt(self.a, f" {self.uuid}\n"), receipt(self.b),
                            self.device, self.vg, self.uuid,
                            self.manifest, self.manifest)
        self.assertEqual(value["classification"], "PAIR_MATCH_REPETITION_REQUIRED")
        self.assertFalse(value["qualified"])

    def test_semantic_mismatch_and_malformed_output_refuse(self):
        mismatch = LAB.compare(receipt(self.a),
                               receipt(self.b, "ABCDEF-1234-5678-9abc-DEF0-1234-ABCDEF"),
                               self.device, self.vg, self.uuid,
                               self.manifest, self.manifest)
        self.assertEqual(mismatch["classification"], "SEMANTIC_MISMATCH")
        malformed = LAB.compare(receipt(self.a), receipt(self.b, "one\ntwo"),
                                self.device, self.vg, self.uuid,
                                self.manifest, self.manifest)
        self.assertEqual(malformed["classification"], "OUTPUT_AMBIGUOUS")

    def test_manifest_change_refuses_pair(self):
        value = LAB.compare(
            receipt(self.a), receipt(self.b), self.device, self.vg, self.uuid,
            self.manifest, {**self.manifest, "kernel": "changed"},
        )
        self.assertEqual(value["classification"], "IDENTITY_CHANGED")

    def test_direct_compare_binds_boot_manifest_and_canonical_pair(self):
        foreign = receipt(self.a, boot_id="f" * 32)
        value = LAB.compare(foreign, receipt(self.b), self.device, self.vg,
                            self.uuid, self.manifest, self.manifest)
        self.assertEqual(value["classification"], "A_AMBIGUOUS_B_FORBIDDEN")
        value = LAB.compare(receipt(self.a), receipt(self.a), self.device, self.vg,
                            self.uuid, self.manifest, self.manifest)
        self.assertEqual(value["classification"], "B_AMBIGUOUS")

    def test_lvm_uuid_is_canonical_everywhere(self):
        for invalid in ("ERROR", "uuid-1", "", self.uuid + "x", "bad uuid"):
            with self.assertRaises(LAB.Refusal):
                LAB.validate_lvm_uuid(invalid)
            with self.assertRaises(LAB.Refusal):
                LAB.plan(self.device, self.vg, invalid)
            value = LAB.compare(receipt(self.a, invalid), receipt(self.b, invalid),
                                self.device, self.vg, invalid,
                                self.manifest, self.manifest)
            self.assertEqual(value["classification"], "INPUT_AMBIGUOUS")

    def test_wrapper_is_clean_and_model_contains_no_process_backend(self):
        wrapper = SCRIPT.with_suffix(".sh").read_text(encoding="utf-8")
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("/usr/bin/env -i", wrapper)
        for forbidden in ("import subprocess", "Popen(", "os.kill", "dmsetup ", "lvchange "):
            self.assertNotIn(forbidden, source)

    def test_controller_persists_intent_before_a_and_b(self):
        journal = mock.Mock()
        calls = []
        def executor(label, argv):
            calls.append(label)
            expected_kind = "PAIR_INTENT" if label == "A" else "B_INTENT"
            self.assertEqual(journal.append.call_args.args[0]["kind"], expected_kind)
            return receipt(argv)
        controller = LAB.Controller(journal)
        value = controller.run_pair(self.device, self.vg, self.uuid, executor,
                                    lambda: self.manifest)
        self.assertEqual(calls, ["A", "B"])
        self.assertEqual(value["classification"], "PAIR_MATCH_REPETITION_REQUIRED")
        self.assertEqual(controller.state, "EVIDENCE_COMPLETE")

    def test_intent_persistence_failure_spawns_nothing(self):
        journal = mock.Mock()
        journal.append.side_effect = OSError("fsync failed")
        executor = mock.Mock()
        controller = LAB.Controller(journal)
        with self.assertRaises(OSError):
            controller.run_pair(self.device, self.vg, self.uuid, executor,
                                lambda: self.manifest)
        executor.assert_not_called()
        self.assertEqual(controller.state, "UNKNOWN")

    def test_ambiguous_a_and_identity_change_never_dispatch_b(self):
        for reaped, changed in ((False, False), (True, True)):
            labels = []
            identities = iter((self.manifest,
                               {**self.manifest, "kernel": "changed"}
                               if changed else self.manifest))
            controller = LAB.Controller(mock.Mock())
            value = controller.run_pair(
                self.device, self.vg, self.uuid,
                lambda label, argv: labels.append(label) or receipt(argv, reaped=reaped),
                lambda: next(identities),
            )
            self.assertEqual(labels, ["A"])
            self.assertIn(value["classification"],
                          {"A_AMBIGUOUS_B_FORBIDDEN", "IDENTITY_CHANGED_B_FORBIDDEN"})
            self.assertEqual(controller.state, "UNKNOWN")

    def test_b_ambiguity_is_terminal_and_controller_cannot_retry(self):
        controller = LAB.Controller(mock.Mock())
        value = controller.run_pair(
            self.device, self.vg, self.uuid,
            lambda label, argv: receipt(argv, reaped=(label == "A")),
            lambda: self.manifest,
        )
        self.assertEqual(value["classification"], "B_AMBIGUOUS")
        self.assertEqual(controller.state, "UNKNOWN")
        with self.assertRaises(LAB.Refusal):
            controller.run_pair(self.device, self.vg, self.uuid, mock.Mock(),
                                lambda: self.manifest)

    def test_bad_a_uuid_or_shape_never_dispatches_b(self):
        for output in ("", "other", "uuid-1\nsecond"):
            labels = []
            controller = LAB.Controller(mock.Mock())
            value = controller.run_pair(
                self.device, self.vg, self.uuid,
                lambda label, argv: labels.append(label) or receipt(argv, output),
                lambda: self.manifest,
            )
            self.assertEqual(labels, ["A"])
            self.assertEqual(value["classification"], "A_AMBIGUOUS_B_FORBIDDEN")

    def test_receipt_and_manifest_schemas_are_closed_and_typed(self):
        bad_receipts = (
            {"pid": None}, {"pid": True}, {"starttime": None},
            {"reaped": "yes"}, {"returncode": False},
            {"terminal": "SIGNALED"}, {"boot_id": "f" * 32},
            {"pidfd_pinned": False}, {"owned_child": False},
            {"foreign": True},
        )
        for change in bad_receipts:
            value = receipt(self.a)
            value.update(change)
            admitted, _ = LAB.admit_b(value, self.a, self.manifest, self.uuid)
            self.assertFalse(admitted, change)
        for manifest in (None, {}, {**self.manifest, "foreign": "x"},
                         {**self.manifest, "boot_id": None},
                         {**self.manifest, "executable_sha256": "short"}):
            with self.assertRaises(LAB.Refusal):
                LAB.validate_manifest(manifest)

    def test_callback_mutation_cannot_change_frozen_plan_or_manifest(self):
        mutable_manifest = dict(self.manifest)
        labels = []
        def executor(label, argv):
            labels.append(label)
            if label == "A":
                mutable_manifest["kernel"] = "changed"
            return receipt(argv)
        value = LAB.Controller(mock.Mock()).run_pair(
            self.device, self.vg, self.uuid, executor, lambda: mutable_manifest)
        self.assertEqual(labels, ["A"])
        self.assertEqual(value["classification"], "IDENTITY_CHANGED_B_FORBIDDEN")

    def test_identity_exception_and_reentrancy_poison_single_use(self):
        controller = LAB.Controller(mock.Mock())
        with self.assertRaises(RuntimeError):
            controller.run_pair(self.device, self.vg, self.uuid, mock.Mock(),
                                lambda: (_ for _ in ()).throw(RuntimeError("identity")))
        self.assertEqual(controller.state, "UNKNOWN")

        controller = LAB.Controller(mock.Mock())
        def reentrant_identity():
            with self.assertRaises(LAB.Refusal):
                controller.run_pair(self.device, self.vg, self.uuid, mock.Mock(),
                                    lambda: self.manifest)
            return self.manifest
        value = controller.run_pair(
            self.device, self.vg, self.uuid,
            lambda _label, argv: receipt(argv), reentrant_identity)
        self.assertEqual(value["classification"], "PAIR_MATCH_REPETITION_REQUIRED")


if __name__ == "__main__":
    unittest.main()
