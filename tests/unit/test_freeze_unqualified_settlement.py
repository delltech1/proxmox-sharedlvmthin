"""Package-only settlement state-machine tests, not native fsync qualification."""
import copy
import io
import types
import unittest
from contextlib import redirect_stdout
from unittest import mock

import test_freeze_package_settlement as existing


class ClosedSettlementTests(unittest.TestCase):
    row = staticmethod(existing.FreezePackageSettlementTests.row)
    write = existing.FreezePackageSettlementTests.write
    create = existing.FreezePackageSettlementTests.create
    mark = existing.FreezePackageSettlementTests.mark
    prepare = existing.FreezePackageSettlementTests.prepare
    install = existing.FreezePackageSettlementTests.install
    replacement = existing.FreezePackageSettlementTests.replacement

    def run_command(self, argv, **kwargs):
        if argv[:2] == ["dpkg", "--verify"]:
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return existing.FreezePackageSettlementTests.run_command(self, argv, **kwargs)

    def setUp(self):
        existing.FreezePackageSettlementTests.setUp(self)
        # Profile journal filesystem effects are independently exercised with
        # the real finalizer in test_closed_profile_evidence, not on host paths.
        self.real_profile_closure = self.m.finalize_closed_profile_evidence
        self.profile_closure = self.stack.enter_context(mock.patch.object(self.m, "finalize_closed_profile_evidence"))
        self.stack.enter_context(mock.patch.object(self.m, "require_root"))
        self.stack.enter_context(mock.patch.object(self.m, "manifest_and_lib", side_effect=lambda: (self.manifest, self.core)))
        self.stack.enter_context(mock.patch.object(self.m.os, "uname", create=True,
            return_value=types.SimpleNamespace(nodename="test-node", release="test-kernel")))
        self.stack.enter_context(mock.patch.object(self.m.pathlib.Path, "exists", return_value=False))
        self.stack.enter_context(mock.patch.object(self.m, "package_state_records",
            side_effect=lambda names: copy.deepcopy([r for r in self.records if r["package"] in names])))
        self.stack.enter_context(mock.patch.object(self.m, "exact_hex_identity", return_value="b" * 64))
        self.stack.enter_context(mock.patch.object(self.m, "exact_runtime_build_id", return_value="d" * 64))
        self.tuple_probe = self.stack.enter_context(mock.patch.object(self.m, "assert_listed_runtime_tuple",
            side_effect=lambda *_: self.unlisted()))
        self.store[self.m.POLICY] = {"schema": 2, "mode": "FREEZE"}
        self.store[self.m.RUNTIME_RELEASE] = {"schema": 1, "qualified": "QUALIFIED",
            "runtime_build_id": "d" * 64, "boot_id": "test-boot"}
        receipt = self.prepare(target=self.target, version="1")
        self.helper, replacement_txid = self.replacement(receipt)
        self.install(target=self.target, version="1")
        self.args = types.SimpleNamespace(package=self.target, version="1", sha256="b" * 64,
            txid=receipt["txid"], replacement_txid=replacement_txid)

    def unlisted(self):
        raise self.m.UnlistedRuntimeTuple({"profile": "thick-only", "api": 15,
            "running_kernel": "test-kernel"}, self.core.digest(self.manifest))

    def command(self):
        with redirect_stdout(io.StringIO()) as output:
            self.m.command_settle_unqualified_freeze_package(self.args)
        return output.getvalue()

    def assert_closed(self):
        self.assertEqual(self.store[self.m.RUNTIME_QUALIFICATION]["state"], "PACKAGE_SETTLED_RUNTIME_CLOSED")
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE]["qualified"], "UNQUALIFIED")

    def test_exact_cross_profile_settles_but_never_authorizes_runtime(self):
        output = self.command()
        self.assertIn("RUNTIME_ADMISSION=CLOSED", output)
        self.assertNotIn("EXECUTE_PASS", output)
        self.assertNotIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertEqual(self.store[self.m.LAST_PACKAGE_TRANSITION]["state"], "COMPLETE")
        self.assertEqual(self.store[self.m.FREEZE]["phase"], "ACTIVE")
        self.assert_closed()
        successor = copy.deepcopy(self.store[self.m.FREEZE])
        self.effects.clear()
        self.command()
        self.assertEqual(self.store[self.m.FREEZE], successor)
        self.assertEqual(self.effects, [])
        with self.assertRaises(RuntimeError):
            self.m.command_finalize_runtime(types.SimpleNamespace(transaction_id=self.args.txid))
        self.assert_closed()

    def test_every_publication_and_unlink_lost_ack_prefix_stays_closed_and_replays(self):
        # Each cut persists the selected write/unlink, then loses its ACK.
        initial_store, initial_holds = copy.deepcopy(self.store), set(self.holds)
        events = []
        def record_write(path, value):
            events.append(("write", path)); self.write(path, value)
        def record_unlink(path):
            events.append(("unlink", path)); self.store.pop(path, None)
        with mock.patch.object(self.m, "atomic_json", side_effect=record_write), \
                mock.patch.object(self.m, "create_json", side_effect=record_write), \
                mock.patch.object(self.m, "unlink_durable", side_effect=record_unlink):
            self.command()
        self.assertGreaterEqual(len(events), 7)
        for cut in range(1, len(events) + 1):
            with self.subTest(cut=cut, event=events[cut - 1]):
                self.store, self.holds = copy.deepcopy(initial_store), set(initial_holds)
                count = [0]
                def after():
                    count[0] += 1
                    # Any prefix that releases PACKAGE_TRANSITION must already
                    # have the independent closed latch and UNQUALIFIED receipt.
                    if self.m.PACKAGE_TRANSITION not in self.store:
                        self.assert_closed()
                    if count[0] == cut:
                        raise RuntimeError("simulated durable write lost ACK")
                def write(path, value):
                    self.write(path, value); after()
                def unlink(path):
                    self.store.pop(path, None); after()
                with mock.patch.object(self.m, "atomic_json", side_effect=write), \
                        mock.patch.object(self.m, "create_json", side_effect=write), \
                        mock.patch.object(self.m, "unlink_durable", side_effect=unlink):
                    with self.assertRaisesRegex(RuntimeError, "lost ACK"):
                        self.command()
                self.assertIn(self.m.RUNTIME_QUALIFICATION, self.store)
                self.command()
                self.assert_closed()
                generation = self.store[self.m.FREEZE]["generation"]
                self.command()
                self.assertEqual(self.store[self.m.FREEZE]["generation"], generation)

    def test_generic_timeout_is_not_unlisted_and_has_zero_publication(self):
        before = copy.deepcopy(self.store)
        self.tuple_probe.side_effect = RuntimeError("probe timed out")
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.command()
        self.assertEqual(self.store, before)

    def test_listed_tuple_cannot_use_package_only_escape_path(self):
        before = copy.deepcopy(self.store)
        self.tuple_probe.side_effect = None
        self.tuple_probe.return_value = {"tuple": {"id": "known"}}
        with self.assertRaisesRegex(RuntimeError, "restricted to an UNLISTED"):
            self.command()
        self.assertEqual(self.store, before)

    def test_payload_drift_after_active_publication_keeps_both_latches(self):
        changed = [False]
        def write(path, value):
            self.write(path, value)
            if path == self.m.FREEZE and value.get("settlement_txid") == self.args.txid \
                    and value.get("phase") == "ACTIVE":
                changed[0] = True
        with mock.patch.object(self.m, "atomic_json", side_effect=write), \
                mock.patch.object(self.m, "exact_hex_identity",
                    side_effect=lambda *_: ("0" if changed[0] else "b") * 64):
            with self.assertRaisesRegex(RuntimeError, "artifact differs"):
                self.command()
        self.assertTrue(changed[0], "reached post-ACTIVE, pre-release boundary")
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assert_closed()

    def test_reboot_does_not_implicitly_requalify_closed_runtime(self):
        self.command()
        before = copy.deepcopy(self.store)
        with mock.patch.object(self.m.pathlib.Path, "read_text", return_value="another-boot"):
            with self.assertRaisesRegex(RuntimeError, "node/boot changed"):
                self.command()
        self.assertEqual(self.store, before)
        self.assert_closed()

    def test_artifact_dpkg_profile_and_txid_drift_refuse_before_closure(self):
        initial = copy.deepcopy(self.store)
        cases = [
            ("artifact", mock.patch.object(self.m, "exact_hex_identity", return_value="0" * 64)),
            ("dirty dpkg", mock.patch.object(self.m, "run", return_value=types.SimpleNamespace(returncode=0, stdout="dirty", stderr=""))),
            ("both profiles", mock.patch.object(self.m, "package_version", return_value="1")),
            ("unconfigured", mock.patch.object(self.m, "package_state_records", return_value=[dict(self.records[0], status="unpacked")])),
        ]
        for label, patch in cases:
            with self.subTest(case=label), patch, self.assertRaises(RuntimeError):
                self.command()
            self.assertEqual(self.store, initial)
        self.args.txid = "0" * 32
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            self.command()
        self.assertEqual(self.store, initial)

    def test_unknown_new_release_after_latch_is_never_overwritten(self):
        def create(path, value):
            self.create(path, value)
            if path == self.m.RUNTIME_QUALIFICATION:
                self.store[self.m.RUNTIME_RELEASE] = {"foreign": "new receipt"}
        with mock.patch.object(self.m, "create_json", side_effect=create):
            with self.assertRaisesRegex(RuntimeError, "release changed outside"):
                self.command()
        self.assertEqual(self.store[self.m.RUNTIME_RELEASE], {"foreign": "new receipt"})
        self.assertIn(self.m.PACKAGE_TRANSITION, self.store)
        self.assertIn(self.m.RUNTIME_QUALIFICATION, self.store)

    def test_normal_settlement_without_runtime_latch_is_not_a_bypass(self):
        before = copy.deepcopy(self.store)
        with mock.patch.object(self.m, "settle_freeze_package") as lower:
            with self.assertRaises(RuntimeError):
                self.m.command_settle_freeze_package(self.args)
        lower.assert_not_called()
        self.assertEqual(self.store, before)


if __name__ == "__main__":
    unittest.main()
