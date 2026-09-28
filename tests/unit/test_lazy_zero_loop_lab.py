"""Mocked only: never opens /dev, invokes dmsetup/losetup or writes sysfs."""
import contextlib
import copy
import errno
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import types
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "experiments/thick-generations/lazy-zero-loop-lab.py"
SPEC = importlib.util.spec_from_file_location("lazy_zero_loop_lab", SCRIPT)
LAB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LAB)


def identities():
    common = {"diskseq": 1, "size_bytes": 134217728, "readonly": False, "holders": []}
    loops = {role: {**common, "kind": "loop", "role": role, "devno": devno,
                    "backing_dev": 20, "backing_inode": inode, "offset": 0, "sizelimit": 0}
             for role, devno, inode in (("data", "7:1", 31), ("metadata", "7:2", 32))}
    zero = {**common, "kind": "dm", "role": "zero", "devno": "253:1",
            "name": "slt-lazy-zero-" + "a" * 32, "uuid": "SLT-LAZY-ZERO-" + "a" * 32,
            "table": "0 262144 zero", "readonly": True, "suspended": False,
            "open_count": 0, "dependencies": []}
    clone = {**common, "kind": "dm", "role": "clone", "devno": "253:2",
             "name": "slt-lazy-clone-" + "a" * 32, "uuid": "SLT-LAZY-CLONE-" + "a" * 32,
             "table": "0 262144 clone 7:2 7:1 253:1 2048 2 no_hydration no_discard_passdown",
             "suspended": False, "open_count": 0, "dependencies": ["7:1", "7:2", "253:1"]}
    return {**loops, "zero": zero, "clone": clone}


def guard_fixture(purpose="GUARDED"):
    roles = identities()
    roles["metadata"]["size_bytes"] = 33554432
    for role in ("data", "metadata", "zero"):
        roles[role]["holders"] = [roles["clone"]["devno"]]
    return {"schema": 1, "nonce": "a" * 32,
            "boot_id": "12345678-1234-1234-1234-123456789abc",
            "purpose": purpose, "region_sectors": 2048, "roles": roles}


def guarded():
    model = LAB.GuardEpochModel(guard_fixture())
    clone = model.fixture["roles"]["clone"]
    receipt = {"schema": 1, "fixture_nonce": model.fixture["nonce"],
               "boot_id": model.fixture["boot_id"], "epoch": model.epoch,
               "graph_digest": model.digest, "clone_devno": clone["devno"],
               "clone_diskseq": clone["diskseq"], "clone_table": clone["table"],
               "queue_devno": clone["devno"], "queue_diskseq": clone["diskseq"],
               "discard_max_bytes": 0, "attempt_id": "b" * 32}
    return model, clone, receipt


class MemoryJournal:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(copy.deepcopy(event))


class LazyLoopModelTests(unittest.TestCase):
    def test_default_plan_never_dispatches_and_all_cases_are_not_run(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(LAB.main([]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["classification"], "PLAN_ONLY_NO_MUTATION")
        self.assertEqual(set(result["cases"].values()), {"NOT_RUN"})
        for key in LAB.LIMITS:
            self.assertIs(result[key], False)

    def test_even_correct_execute_token_refuses_unimplemented_live_backend(self):
        for token in ("wrong", LAB.ACK):
            with contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(LAB.main(["--execute-token", token]), 2)
            result = json.loads(output.getvalue())
            self.assertEqual(result["classification"], "REFUSED_EXECUTE_BACKEND_NOT_QUALIFIED")
            self.assertEqual(result["token_valid"], token == LAB.ACK)

    def test_clean_environment_wrapper_has_no_kernel_commands(self):
        text = SCRIPT.with_suffix(".sh").read_text()
        self.assertIn("/usr/bin/env -i", text)
        self.assertIn("/usr/bin/python3 -I -B", text)
        for forbidden in ("modprobe", "dmsetup", "losetup", "trap cleanup", "rm -"):
            self.assertNotIn(forbidden, text)

    def test_intent_precedes_action_and_exception_poison_preserves(self):
        journal = MemoryJournal()
        controller = LAB.Controller(journal)
        def failed_create():
            self.assertEqual(journal.events[-1]["kind"], "INTENT")
            raise TimeoutError("create may exist")
        with self.assertRaises(TimeoutError):
            controller.action("CREATE_CLONE", failed_create)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertFalse(journal.events[-1]["cleanup_dispatched"])
        callback = mock.Mock()
        with self.assertRaises(LAB.Refusal):
            controller.action("REMOVE", callback)
        callback.assert_not_called()

    def test_intent_persistence_failure_does_not_dispatch(self):
        journal = mock.Mock()
        journal.append.side_effect = OSError("fsync unavailable")
        callback = mock.Mock()
        controller = LAB.Controller(journal)
        with self.assertRaises(OSError):
            controller.action("ATTACH_LOOP", callback)
        callback.assert_not_called()
        self.assertEqual(controller.state, "UNKNOWN")

    def test_guard_and_identity_mismatch_never_publish(self):
        for limit in (True, False, 1, "0", 0.0):
            model, expected, unused = guarded()
            controller = LAB.Controller(MemoryJournal(), model)
            with self.assertRaises(LAB.Refusal):
                controller.publish_model(expected, expected, limit)
            self.assertEqual(controller.state, "UNKNOWN")
        for key, value in (("devno", "253:9"), ("uuid", "foreign"),
                           ("diskseq", 2), ("suspended", True), ("open_count", 1)):
            model, expected, receipt = guarded()
            changed = {**expected, key: value}
            with self.assertRaises(LAB.Refusal):
                LAB.Controller(MemoryJournal(), model).publish_model(
                    expected, changed, receipt)

    def test_model_publish_withdraw_requires_terminal_and_exact_zero_opens(self):
        model, expected, receipt = guarded()
        controller = LAB.Controller(MemoryJournal(), model)
        controller.publish_model(expected, expected, receipt)
        with self.assertRaises(LAB.Refusal):
            controller.withdraw_model(True, False)
        with self.assertRaises(LAB.Refusal):
            controller.withdraw_model(False, 0)
        controller.withdraw_model(True, 0)
        self.assertEqual(controller.state, "UNPUBLISHED")
        with self.assertRaises(LAB.Refusal):
            controller.record_case("L1", "PASS")
        controller.record_case("L1", "MODEL_PASS")

    def test_ambiguous_publication_persistence_poison_cannot_republish(self):
        journal = mock.Mock()
        journal.append.side_effect = OSError("directory fsync failed")
        model, clone, receipt = guarded()
        controller = LAB.Controller(journal, model)
        with self.assertRaises(OSError):
            controller.publish_model(clone, clone, receipt)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertEqual(model.state, "UNKNOWN")
        with self.assertRaises(LAB._GUARD_MODULE.Refusal):
            model.publish()
        with self.assertRaises(LAB.Refusal):
            controller.publish_model(clone, clone, receipt)

    def test_bare_queue_limit_and_missing_guard_model_are_not_authorities(self):
        model, clone, receipt = guarded()
        with self.assertRaisesRegex(LAB.Refusal, "guard epoch authority"):
            LAB.Controller(MemoryJournal()).publish_model(clone, clone, receipt)
        with self.assertRaises(LAB.Refusal):
            LAB.Controller(MemoryJournal(), model).publish_model(clone, clone, 0)

    def test_guard_evidence_is_persisted_before_model_publication(self):
        model, clone, receipt = guarded(); journal = MemoryJournal()
        controller = LAB.Controller(journal, model)
        original = model.publish
        def checked_publish():
            self.assertEqual(journal.events[-1]["kind"], "MODEL_GUARD_EVIDENCE")
            return original()
        model.publish = checked_publish
        controller.publish_model(clone, clone, receipt)
        self.assertEqual(controller.state, "MODEL_PUBLISHED")
        self.assertEqual(journal.events[-1]["guard"], receipt)
        receipt["attempt_id"] = "c" * 32
        self.assertEqual(model.guard["attempt_id"], "b" * 32)

    def test_swallowed_reentrant_publish_cannot_be_overwritten_by_outer_success(self):
        model, clone, receipt = guarded()
        class ReentrantJournal:
            def append(inner, event):
                if event["kind"] == "MODEL_GUARD_EVIDENCE":
                    with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                        controller.publish_model(clone, clone, receipt)
                return "stored"
        controller = LAB.Controller(ReentrantJournal(), model)
        with self.assertRaises(LAB.Refusal):
            controller.publish_model(clone, clone, receipt)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertNotEqual(model.state, "MODEL_PUBLISHED")

    def test_journal_cannot_publish_guard_model_behind_controller(self):
        model, clone, receipt = guarded()
        class MutatingJournal:
            def append(inner, event):
                model.publish()
                return "stored"
        controller = LAB.Controller(MutatingJournal(), model)
        with self.assertRaises(LAB.Refusal):
            controller.publish_model(clone, clone, receipt)
        self.assertEqual(controller.state, "UNKNOWN")

    def test_swallowed_reentrant_withdraw_cannot_restore_unpublished(self):
        model, clone, receipt = guarded(); journal = MemoryJournal()
        controller = LAB.Controller(journal, model)
        controller.publish_model(clone, clone, receipt)
        class ReentrantJournal:
            def append(inner, event):
                with self.assertRaisesRegex(LAB.Refusal, "reentrant"):
                    controller.withdraw_model(True, 0)
                return "stored"
        controller.journal = ReentrantJournal()
        with self.assertRaises(LAB.Refusal):
            controller.withdraw_model(True, 0)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertEqual(model.state, "UNKNOWN")

    def test_withdraw_persistence_failure_invalidates_both_authorities(self):
        model, clone, receipt = guarded(); journal = MemoryJournal()
        controller = LAB.Controller(journal, model)
        controller.publish_model(clone, clone, receipt)
        controller.journal = mock.Mock()
        controller.journal.append.side_effect = OSError("fsync ambiguous")
        with self.assertRaises(OSError):
            controller.withdraw_model(True, 0)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertEqual(model.state, "UNKNOWN")
        with self.assertRaises(LAB._GUARD_MODULE.Refusal):
            model.withdraw(True, 0)

    def test_public_model_aba_during_persistence_cannot_rebind_outer_evidence(self):
        model, clone, receipt = guarded()
        class AbaJournal:
            def append(inner, event):
                model.publish()
                model.withdraw(True, 0)
                model.reconfigure("RELOAD", copy.deepcopy(model.fixture), True, 0)
                replacement = copy.deepcopy(receipt)
                replacement["epoch"] = model.epoch
                replacement["graph_digest"] = model.digest
                model.accept_guard(replacement)
                return "stored-old-epoch"
        controller = LAB.Controller(AbaJournal(), model)
        with self.assertRaises(LAB.Refusal):
            controller.publish_model(clone, clone, receipt)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertEqual(model.state, "UNKNOWN")

    def test_withdraw_aba_cannot_apply_old_terminal_evidence_to_new_epoch(self):
        model, clone, receipt = guarded(); journal = MemoryJournal()
        controller = LAB.Controller(journal, model)
        controller.publish_model(clone, clone, receipt)
        class AbaJournal:
            def append(inner, event):
                model.withdraw(True, 0)
                model.reconfigure("RELOAD", copy.deepcopy(model.fixture), True, 0)
                replacement = copy.deepcopy(receipt)
                replacement["epoch"] = model.epoch
                replacement["graph_digest"] = model.digest
                model.accept_guard(replacement)
                model.publish()
                return "stored-old-terminal-proof"
        controller.journal = AbaJournal()
        with self.assertRaises(LAB.Refusal):
            controller.withdraw_model(True, 0)
        self.assertEqual(controller.state, "UNKNOWN")
        self.assertEqual(model.state, "UNKNOWN")

    def test_later_record_or_action_failure_invalidates_guard_authority(self):
        for operation in ("record", "action"):
            model, clone, receipt = guarded(); journal = MemoryJournal()
            controller = LAB.Controller(journal, model)
            controller.publish_model(clone, clone, receipt)
            if operation == "record":
                controller.journal = mock.Mock()
                controller.journal.append.side_effect = OSError("case fsync")
                with self.assertRaises(OSError):
                    controller.record_case("L1", "MODEL_PASS")
            else:
                with self.assertRaises(RuntimeError):
                    controller.action("MOCK_AFTER_PUBLICATION",
                                      lambda: (_ for _ in ()).throw(RuntimeError("failed")))
            self.assertEqual(controller.state, "UNKNOWN")
            self.assertEqual(model.state, "UNKNOWN")
            with self.assertRaises(LAB._GUARD_MODULE.Refusal):
                model.withdraw(True, 0)

    def test_cleanup_is_a_nonexecutable_single_action_and_requires_terminal(self):
        owned = identities()
        with self.assertRaises(LAB.Refusal):
            LAB.cleanup_next(owned, owned, "clone")
        proposal = LAB.cleanup_next(owned, owned, "clone", terminal_proven=True)
        self.assertEqual(proposal["operation"], "REMOVE_EXACT_DM")
        self.assertIs(proposal["executable"], False)

    def test_cleanup_rejects_recycled_identity_and_unproven_absence(self):
        owned = identities()
        for role, key, value in (("clone", "uuid", "foreign"),
                                 ("data", "backing_inode", 100),
                                 ("metadata", "diskseq", 2)):
            observed = copy.deepcopy(owned)
            observed[role][key] = value
            with self.assertRaises(LAB.Refusal):
                LAB.cleanup_next(owned, observed, "clone", terminal_proven=True)
        observed = {key: value for key, value in owned.items() if key != "clone"}
        with self.assertRaisesRegex(LAB.Refusal, "absence unproven"):
            LAB.cleanup_next(owned, observed, "zero", terminal_proven=True)
        proposal = LAB.cleanup_next(owned, observed, "zero", {"clone": owned["clone"]}, True)
        self.assertFalse(proposal["executable"])

    def test_cleanup_rejects_holders_graph_alias_and_nonterminal_open(self):
        for role, key, value in (("clone", "holders", ["foreign"]),
                                 ("clone", "open_count", 1)):
            owned = identities()
            observed = copy.deepcopy(owned)
            observed[role][key] = value
            with self.assertRaises(LAB.Refusal):
                LAB.cleanup_next(owned, observed, "clone", terminal_proven=True)
        owned = identities()
        owned["data"]["devno"] = owned["metadata"]["devno"]
        with self.assertRaises(LAB.Refusal):
            LAB.cleanup_next(owned, owned, "clone", terminal_proven=True)

    def test_journal_is_exclusive_fsynced_and_not_reopenable(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            journal = LAB.Journal(directory, "1" * 32)
            try:
                journal.append({"kind": "INTENT", "operation": "MOCK_ONLY"})
                stored = json.loads((Path(directory) / "event-000001.json").read_bytes())
                self.assertEqual(stored["nonce"], "1" * 32)
                with self.assertRaises(LAB.Refusal):
                    LAB.Journal(directory, "2" * 32)
            finally:
                journal.close()

    def test_racing_empty_observations_cannot_share_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            os.chmod(directory, 0o700)
            journal = LAB.Journal(directory, "1" * 32)
            try:
                owner_before = (Path(directory) / "journal-owner.json").read_bytes()
                with mock.patch.object(LAB.os, "listdir", return_value=[]):
                    with self.assertRaises(FileExistsError):
                        LAB.Journal(directory, "2" * 32)
                self.assertEqual((Path(directory) / "journal-owner.json").read_bytes(), owner_before)
                self.assertEqual(list(Path(directory).glob("event-*")), [])
                with mock.patch.object(LAB.threading, "get_ident", return_value=-1):
                    with self.assertRaisesRegex(LAB.Refusal, "cross-thread"):
                        journal.append({"kind": "FOREIGN_THREAD"})
            finally:
                journal.close()


class LazyLoopMockedBlockTests(unittest.TestCase):
    def setUp(self):
        self.identity = {"major": 253, "minor": 2, "diskseq": 7,
                         "size_bytes": 4096, "logical_sector": 512}

    def test_fd_identity_rejects_regular_file_and_bool_schema(self):
        with mock.patch.object(LAB.os, "fstat", return_value=types.SimpleNamespace(st_mode=stat.S_IFREG)):
            with self.assertRaisesRegex(LAB.Refusal, "not a block"):
                LAB.fd_identity(999, self.identity)
        with self.assertRaisesRegex(LAB.Refusal, "FD values"):
            LAB.fd_identity(999, {**self.identity, "diskseq": True})

    def test_fd_checks_exact_devno_diskseq_size_direct_and_writable(self):
        info = types.SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(253, 2))
        numbers = {LAB.BLKGETDISKSEQ: 7, LAB.BLKGETSIZE64: 4096,
                   LAB.BLKSSZGET: 512, LAB.BLKROGET: 0}
        with mock.patch.object(LAB.os, "fstat", return_value=info), \
                mock.patch.object(LAB, "ioctl_number", side_effect=lambda fd, request, fmt: numbers[request]), \
                mock.patch.object(LAB.fcntl, "fcntl", return_value=os.O_RDWR | os.O_DIRECT):
            LAB.fd_identity(999, self.identity, writable=True, direct=True)
            numbers[LAB.BLKGETDISKSEQ] = 8
            with self.assertRaisesRegex(LAB.Refusal, "diskseq"):
                LAB.fd_identity(999, self.identity)
            numbers[LAB.BLKGETDISKSEQ] = 7
            numbers[LAB.BLKROGET] = 1
            with self.assertRaisesRegex(LAB.Refusal, "read-only"):
                LAB.fd_identity(999, self.identity, writable=True)

    def test_discard_exact_errno_not_generic_failure(self):
        for code in (errno.EOPNOTSUPP, errno.EIO, errno.EPERM, errno.EINVAL):
            with mock.patch.object(LAB, "fd_identity") as check, \
                    mock.patch.object(LAB.fcntl, "ioctl", side_effect=OSError(code, "mock")):
                result = LAB.discard_exact(999, self.identity, 0, 512)
            self.assertEqual(check.call_count, 2)
            self.assertEqual(result["guard_rejection"], code == errno.EOPNOTSUPP)
            self.assertEqual(result["errno"], code)

    def test_discard_bad_range_never_issues_ioctl(self):
        for offset, length in ((True, 512), (0, False), (1, 512), (0, 8192), (-1, 512)):
            with mock.patch.object(LAB, "fd_identity"), mock.patch.object(LAB.fcntl, "ioctl") as call:
                with self.assertRaises(LAB.Refusal):
                    LAB.discard_exact(999, self.identity, offset, length)
                call.assert_not_called()

    def test_direct_oracle_compares_independent_expected_bytes(self):
        expected = b"\x00" * 512 + b"A" * 512 + b"\x00" * 3072
        def read(fd, buffers, position):
            self.assertEqual(fd, 999)
            buffers[0][:] = expected[position:position + len(buffers[0])]
            return len(buffers[0])
        with mock.patch.object(LAB, "fd_identity") as check, \
                mock.patch.object(LAB.os, "preadv", side_effect=read):
            result = LAB.direct_oracle(999, self.identity, 0, [(512, b"A" * 512)])
        self.assertEqual(result["sha256"], hashlib.sha256(expected).hexdigest())
        self.assertEqual(check.call_count, 2)

    def test_direct_short_read_cache_like_wrong_data_and_generator_refuse(self):
        with mock.patch.object(LAB, "fd_identity"), \
                mock.patch.object(LAB.os, "preadv", return_value=512):
            with self.assertRaisesRegex(LAB.Refusal, "short direct"):
                LAB.direct_oracle(999, self.identity, 0)
        def all_zero(fd, buffers, position):
            buffers[0][:] = bytes(len(buffers[0]))
            return len(buffers[0])
        with mock.patch.object(LAB, "fd_identity"), \
                mock.patch.object(LAB.os, "preadv", side_effect=all_zero):
            with self.assertRaisesRegex(LAB.Refusal, "oracle mismatch"):
                LAB.direct_oracle(999, self.identity, 0xA5)
            with self.assertRaisesRegex(LAB.Refusal, "replayable"):
                LAB.direct_oracle(999, self.identity, 0, iter([(512, b"A")]))


if __name__ == "__main__":
    unittest.main()
